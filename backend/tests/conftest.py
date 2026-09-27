import os
import shutil
import tempfile
import pytest

# ✅ 設定測試用資料庫
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

# ✅ 正確引入 Flask 應用
from ..app import app as real_app, db
from ..redis_client import set_redis

import fakeredis

@pytest.fixture(scope="session")
def flask_app():
    tmp_dir = tempfile.mkdtemp()
    real_app.config.update(
        TESTING=True,
        UPLOAD_FOLDER=tmp_dir,
        WTF_CSRF_ENABLED=False,
    )
    # Create the schema, then release the application context. Keeping one
    # pushed for the whole session made every request reuse it, so Flask's g
    # and the SQLAlchemy session were shared across requests instead of being
    # fresh per request as in production. That hid per-request behaviour:
    # Flask-Login caches the logged-in user on g, so after the first login
    # user_loader was never called again.
    with real_app.app_context():
        db.create_all()
    yield real_app
    with real_app.app_context():
        db.session.remove()
        db.drop_all()
    shutil.rmtree(tmp_dir)

# ✅ pytest-flask 預期這個名字
@pytest.fixture(scope="session")
def app(flask_app):
    return flask_app

@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _push_request_context():
    """Disable pytest-flask's fixture of the same name.

    pytest-flask pushes one request context for the whole of each test, and
    every test-client request then reuses its application context instead of
    getting a fresh one as in production. Flask-Login caches the logged-in
    user on g, so user_loader ran once per test rather than once per request,
    and anything per-request (the user cache included) went untested while
    the tests still passed.
    """
    yield


@pytest.fixture(autouse=True)
def fake_redis():
    """Give every test its own in-memory Redis.

    The whole suite runs with rate limiting and caching enabled, as it does
    in production. A fresh instance per test keeps rate-limit counters and
    cached entries from leaking between tests that share the same database.
    """
    client = fakeredis.FakeRedis(decode_responses=True)
    set_redis(client)
    yield client
    set_redis(None)
