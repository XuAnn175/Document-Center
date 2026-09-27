"""Rate limiting, the public-files cache and the user cache."""
import re
from contextlib import contextmanager
from io import BytesIO

import fakeredis
from sqlalchemy import event

from ..app import db
from ..models import File, User
from ..redis_client import PUBLIC_FILES_KEY, set_redis, user_key


# ─────────────────────────── helpers ───────────────────────────
def _register(client, username, password="pwd"):
    rv = client.post("/register", json={
        "username": username, "email": f"{username}@mail",
        "password": password, "grade": 1,
    })
    assert rv.status_code == 200, rv.get_json()


def _login(client, username, password="pwd", ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return client.post("/login", json={"username": username, "password": password},
                       headers=headers)


@contextmanager
def count_selects(app):
    """Collect every SELECT issued against the database inside the block."""
    statements = []

    def record(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def reads_table(statements, table):
    """True if any statement selects FROM *table* (quoted or not).

    A plain substring test would be wrong twice over: "FROM file" also
    matches "FROM file_version", and some dialects quote reserved names
    such as user, so "FROM user" would silently never match.
    """
    pattern = re.compile(rf'\bFROM\s+["`]?{table}["`]?(?![\w])', re.IGNORECASE)
    return any(pattern.search(s) for s in statements)


# ──────────────────────── rate limiting ────────────────────────
def test_rate_limit_keys_always_expire(client, fake_redis):
    # A counter without a TTL never resets, locking that client out for good.
    # This is what the common INCR-then-EXPIRE pattern risks if the process
    # dies between the two calls.
    _login(client, "anyone", "x")
    keys = fake_redis.keys("ratelimit:*")
    assert keys
    assert all(fake_redis.ttl(k) > 0 for k in keys)


def test_login_locks_a_username_after_repeated_failures(client):
    _register(client, "rl_user")
    for _ in range(5):
        assert _login(client, "rl_user", "wrong").status_code == 401

    # Locked out even with the right password: the attempt is counted before
    # the password is checked.
    rv = _login(client, "rl_user")
    assert rv.status_code == 429
    assert int(rv.headers["Retry-After"]) > 0


def test_username_limit_ignores_case(client):
    _register(client, "rl_case")
    for name in ("rl_case", "RL_CASE", "Rl_Case", "rl_CASE", "RL_case"):
        assert _login(client, name, "wrong").status_code == 401
    assert _login(client, "rl_case").status_code == 429


def test_successful_login_resets_the_username_counter(client):
    _register(client, "rl_reset")
    for _ in range(4):
        _login(client, "rl_reset", "wrong")
    assert _login(client, "rl_reset").status_code == 200

    # The four earlier failures no longer count toward the lockout.
    for _ in range(4):
        assert _login(client, "rl_reset", "wrong").status_code == 401


def test_login_limits_one_ip_across_many_usernames(client):
    for i in range(20):
        assert _login(client, f"nobody{i}", "x").status_code == 401
    assert _login(client, "nobody_final", "x").status_code == 429


def test_ip_limit_keys_on_the_forwarded_client_address(client):
    # Behind nginx every request arrives from the proxy's address. If the
    # limit keyed on that, one abusive client would lock out everybody.
    for i in range(20):
        _login(client, f"x{i}", "x", ip="10.0.0.1")
    assert _login(client, "x_last", "x", ip="10.0.0.1").status_code == 429
    assert _login(client, "x_other", "x", ip="10.0.0.2").status_code == 401


def test_register_is_rate_limited(client):
    for i in range(10):
        _register(client, f"reg{i}")
    rv = client.post("/register", json={
        "username": "reg_last", "email": "reg_last@mail", "password": "pwd", "grade": 1,
    })
    assert rv.status_code == 429


def test_password_reset_is_limited_per_email(client):
    for _ in range(3):
        assert client.post("/request-reset", json={"email": "who@mail"}).status_code == 200
    assert client.post("/request-reset", json={"email": "who@mail"}).status_code == 429
    # A different address from the same client is still allowed.
    assert client.post("/request-reset", json={"email": "else@mail"}).status_code == 200


def test_everything_keeps_working_when_redis_is_down(client):
    server = fakeredis.FakeServer()
    server.connected = False           # every command raises ConnectionError
    set_redis(fakeredis.FakeRedis(server=server, decode_responses=True))

    _register(client, "rl_outage")
    assert _login(client, "rl_outage").status_code == 200
    assert client.get("/session-status").get_json()["authenticated"] is True
    assert client.get("/public-files").status_code == 200


# ───────────────────── public-files cache ──────────────────────
def _upload(client, name="doc.txt"):
    rv = client.post("/upload", data={"file": (BytesIO(b"hello"), name)},
                     content_type="multipart/form-data")
    return rv.get_json()["file_id"]


def _public_ids(client):
    return {f["id"] for f in client.get("/public-files").get_json()["pfiles"]}


def test_public_files_second_request_skips_the_database(client, app, fake_redis):
    _register(client, "pf_reader")
    _login(client, "pf_reader")
    client.get("/public-files")
    assert fake_redis.get(PUBLIC_FILES_KEY) is not None

    with count_selects(app) as selects:
        client.get("/public-files")
    assert not reads_table(selects, "file")


def test_publishing_a_file_evicts_the_public_files_cache(client, app, fake_redis):
    _register(client, "pf_owner")
    _login(client, "pf_owner")
    fid = _upload(client)
    assert fid not in _public_ids(client)            # cached without the file

    with app.app_context():
        db.session.get(File, fid).is_published = True
        db.session.commit()

    assert fake_redis.get(PUBLIC_FILES_KEY) is None
    assert fid in _public_ids(client)


def test_rolled_back_change_keeps_the_cache(client, app, fake_redis):
    _register(client, "pf_rollback")
    _login(client, "pf_rollback")
    fid = _upload(client)
    _public_ids(client)

    with app.app_context():
        db.session.get(File, fid).is_published = True
        db.session.flush()
        db.session.rollback()

    assert fake_redis.get(PUBLIC_FILES_KEY) is not None


# ───────────────────────── user cache ──────────────────────────
def test_cached_user_is_loaded_without_a_query(client, app, fake_redis):
    _register(client, "uc_user")
    _login(client, "uc_user")
    client.get("/session-status")                     # populates the cache

    with count_selects(app) as selects:
        rv = client.get("/session-status")
    assert rv.get_json()["user"]["username"] == "uc_user"
    assert not reads_table(selects, "user")


def test_password_hash_is_not_cached(client, fake_redis):
    _register(client, "uc_hash")
    _login(client, "uc_hash")
    client.get("/session-status")
    cached = [fake_redis.get(k) for k in fake_redis.keys("cache:v1:user:*")]
    assert cached and not any("password" in c for c in cached)


def test_password_change_through_a_cached_user_is_saved(client):
    _register(client, "uc_pwd", password="old-pass")
    _login(client, "uc_pwd", "old-pass")
    client.get("/session-status")                     # current_user now comes from cache

    rv = client.post("/change-password",
                     json={"current_password": "old-pass", "new_password": "new-pass"})
    assert rv.status_code == 200

    client.post("/logout")
    assert _login(client, "uc_pwd", "old-pass").status_code == 401
    assert _login(client, "uc_pwd", "new-pass").status_code == 200


def test_revoking_admin_takes_effect_immediately(client, app, fake_redis):
    _register(client, "uc_admin")
    with app.app_context():
        user = User.query.filter_by(username="uc_admin").one()
        user.is_admin = True
        db.session.commit()
        uid = user.id

    _login(client, "uc_admin")
    assert client.get("/admin/list-users").status_code == 200
    assert fake_redis.get(user_key(uid)) is not None  # is_admin=True is cached

    with app.app_context():
        db.session.get(User, uid).is_admin = False
        db.session.commit()

    # Not after the TTL: the commit itself evicted the cached user.
    assert client.get("/admin/list-users").status_code == 403
