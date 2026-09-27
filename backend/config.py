import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key')
    
    UPLOAD_FOLDER = 'uploads'                          # Root uploads dir
    USER_ROOT_FOLDER = os.path.join(UPLOAD_FOLDER, 'users')  # /uploads/users/{user_id}/...

    MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB per upload

    USER_FOLDER_QUOTA_MB = 500             # Optional: 500 MB per user (soft limit)
    FOLDER_DEPTH_LIMIT = 10                # Optional: nested folder levels

    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://flaskuser:flaskpass@host.docker.internal/cloud_docs"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Number of reverse proxies in front of the app that append to
    # X-Forwarded-For. Rate limits key on the client IP, so this must match
    # the deployment: 1 behind nginx alone (compose), 2 behind an ingress
    # controller plus nginx (k8s). Set too low and every request is keyed on
    # a proxy's IP, so one client can lock everyone out; set too high and
    # clients can choose their own IP by sending the header themselves.
    TRUSTED_PROXY_COUNT = int(os.getenv("TRUSTED_PROXY_COUNT", "1"))

    # Session Configuration for better isolation
    SESSION_COOKIE_NAME = "session"
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_HTTPONLY = True
    PERMANENT_SESSION_LIFETIME = 3600  # 1 hour
    SESSION_TYPE = 'filesystem'  # Store sessions on filesystem instead of cookies