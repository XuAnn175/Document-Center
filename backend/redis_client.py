"""Redis-backed rate limiting and caching.

Redis is optional at runtime. When REDIS_URL is unset, or the server cannot
be reached, every helper here degrades to a no-op and the application keeps
working against the database alone. For rate limiting that means failing
open: a Redis outage stops limits from being enforced (and is logged) rather
than locking every user out of the login page.
"""
from __future__ import annotations

import json
import logging
import os
from itertools import chain

import redis
from sqlalchemy import event
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Keys are versioned so a deploy that changes a payload's shape never reads
# entries written by the previous release.
PUBLIC_FILES_KEY = "cache:v1:public_files"
PUBLIC_FILES_TTL = 300

# Kept short because the cached user carries is_admin. Invalidation on commit
# is the primary guarantee; the TTL bounds the window left by the cache-aside
# race (a read that began before a commit writing its result back after it).
USER_TTL = 60


def user_key(user_id: int) -> str:
    return f"cache:v1:user:{user_id}"


# ─────────────────────────── client ───────────────────────────
_client: redis.Redis | None = None
_client_initialised = False


def get_redis() -> redis.Redis | None:
    global _client, _client_initialised
    if not _client_initialised:
        url = os.getenv("REDIS_URL")
        if url:
            # Short timeouts: this sits in front of every request, so an
            # unresponsive Redis must fail fast rather than stall the worker.
            _client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
            )
        _client_initialised = True
    return _client


def set_redis(client: redis.Redis | None) -> None:
    """Replace the client. Used by the test suite to inject fakeredis."""
    global _client, _client_initialised
    _client = client
    _client_initialised = True


# ──────────────────────── rate limiting ───────────────────────
def hit_rate_limit(key: str, limit: int, window_sec: int) -> tuple[bool, int]:
    """Record one attempt against *key*.

    Returns (allowed, retry_after_seconds). The window is fixed: it starts
    at the first attempt and every attempt inside it counts.
    """
    r = get_redis()
    if r is None:
        return True, 0
    try:
        # SET NX EX starts the window and its expiry together, and INCR keeps
        # the existing TTL. Running both in one MULTI/EXEC matters: the common
        # INCR-then-EXPIRE pattern leaves a key with no expiry if the process
        # dies between the two calls, locking that client out permanently.
        pipe = r.pipeline()
        pipe.set(key, 0, ex=window_sec, nx=True)
        pipe.incr(key)
        pipe.ttl(key)
        _, count, ttl = pipe.execute()
    except redis.RedisError:
        log.warning("Rate limiting unavailable; allowing request", exc_info=True)
        return True, 0
    if count > limit:
        return False, max(int(ttl), 1)
    return True, 0


def reset_rate_limit(key: str) -> None:
    r = get_redis()
    if r is None:
        return
    try:
        r.delete(key)
    except redis.RedisError:
        log.warning("Could not reset rate limit %s", key, exc_info=True)


# ─────────────────────────── caching ──────────────────────────
def cache_get_json(key: str):
    r = get_redis()
    if r is None:
        return None
    try:
        raw = r.get(key)
    except redis.RedisError:
        log.warning("Cache read failed for %s", key, exc_info=True)
        return None
    return json.loads(raw) if raw is not None else None


def cache_set_json(key: str, value, ttl_sec: int) -> None:
    r = get_redis()
    if r is None:
        return
    try:
        r.set(key, json.dumps(value), ex=ttl_sec)
    except redis.RedisError:
        log.warning("Cache write failed for %s", key, exc_info=True)


def cache_delete(*keys: str) -> None:
    r = get_redis()
    if r is None or not keys:
        return
    try:
        r.delete(*keys)
    except redis.RedisError:
        # The entry stays until its TTL expires; nothing more can be done here.
        log.warning("Cache invalidation failed for %s", keys, exc_info=True)


# ───────────────────── cache invalidation ─────────────────────
_PENDING = "pending_cache_invalidations"


def register_cache_invalidation(file_model, user_model) -> None:
    """Evict cached entries whenever the underlying rows change.

    Rather than calling cache_delete() at each of the dozen endpoints that
    modify a File or User, hook the ORM: any File or User inserted, updated
    or deleted in a flush marks the matching key, and the key is removed
    only once the transaction commits. A rollback discards the marks, so a
    failed request never evicts a valid entry. Deleting after commit rather
    than before also stops a concurrent reader from refilling the cache with
    the pre-commit state.
    """

    def collect(session, _flush_context):
        pending = session.info.setdefault(_PENDING, set())
        for obj in chain(session.new, session.dirty, session.deleted):
            if isinstance(obj, file_model):
                pending.add(PUBLIC_FILES_KEY)
            elif isinstance(obj, user_model) and obj.id is not None:
                pending.add(user_key(obj.id))

    def apply(session):
        keys = session.info.pop(_PENDING, None)
        if keys:
            cache_delete(*keys)

    def discard(session):
        session.info.pop(_PENDING, None)

    event.listen(Session, "after_flush", collect)
    event.listen(Session, "after_commit", apply)
    event.listen(Session, "after_rollback", discard)
