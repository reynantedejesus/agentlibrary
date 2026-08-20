"""Configuration objects for the Agent Library.

Every setting is read from the process environment. In development a ``.env``
file at the project root is loaded by :func:`app.create_app`; in production the
values come from systemd's ``EnvironmentFile=`` so that no secret is ever
written into source control.

Connects to: ``app/__init__.py`` selects one of these classes via ``FLASK_ENV``.


DEFAULTS ARE STATIC ON PURPOSE
------------------------------
Class bodies below contain plain literals, not ``os.environ`` reads. A class
body executes once, at first import, so reading the environment here would
freeze whatever happened to be set at that moment — and ``create_app()`` loads
the environment file *after* that point. Every environment-configurable setting
is therefore applied by ``app/__init__.py::_apply_environment_overrides()``,
which runs on each ``create_app()`` call and is the single place the
environment is consulted. Add a new tunable in both places: a default here, and
its name in ``_ENV_SETTINGS`` there.
"""
from __future__ import annotations

import os
from typing import List, Optional


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = os.environ.get(name, "")
    items = [p.strip() for p in raw.split(",") if p.strip()]
    return items if items else list(default or [])


def _build_database_url() -> str:
    """Prefer an explicit DATABASE_URL; otherwise assemble one from parts."""
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    from urllib.parse import quote_plus

    host = os.environ.get("MYSQL_HOST", "127.0.0.1")
    port = os.environ.get("MYSQL_PORT", "3306")
    name = os.environ.get("MYSQL_DATABASE", "agentlibrary")
    user = os.environ.get("MYSQL_USER", "agentlibrary")
    password = os.environ.get("MYSQL_PASSWORD", "")
    return "mysql+pymysql://{u}:{p}@{h}:{port}/{db}?charset=utf8mb4".format(
        u=quote_plus(user), p=quote_plus(password), h=host, port=port, db=name
    )


class BaseConfig(object):
    # --- core -------------------------------------------------------------
    SECRET_KEY = ""
    # Set per subclass below; get_config() picks the class from FLASK_ENV, so
    # this always reflects the environment actually in force.
    ENV_NAME = "production"
    DEBUG = False
    TESTING = False
    LOG_LEVEL = "INFO"
    LOG_DIR = "/var/log/agentlibrary"
    LOG_TO_STDOUT = True

    # --- database ---------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = _build_database_url()   # re-derived in create_app
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": _int("DB_POOL_RECYCLE", 280),
        "pool_size": _int("DB_POOL_SIZE", 5),
        "max_overflow": _int("DB_MAX_OVERFLOW", 10),
        # NOTE: pool sizing is MySQL-specific; SQLite's StaticPool rejects it,
        # so TestingConfig clears this dict entirely.
    }

    # --- sessions & cookies ----------------------------------------------
    SESSION_COOKIE_NAME = "agentlibrary_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12
    SESSION_REFRESH_EACH_REQUEST = False

    # --- CSRF -------------------------------------------------------------
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None          # tied to session lifetime instead
    WTF_CSRF_HEADERS = ["X-CSRFToken", "X-CSRF-Token"]
    WTF_CSRF_SSL_STRICT = True

    # --- uploads ----------------------------------------------------------
    UPLOAD_DIR = "/var/lib/agentlibrary/uploads"
    MAX_CONTENT_LENGTH = 26214400               # 25 MiB
    MAX_FILES_PER_ASSET = 25
    ALLOWED_UPLOAD_EXTENSIONS = ["pdf", "md", "txt", "docx", "zip", "json",
                                 "yaml", "yml", "csv", "xlsx", "png", "jpg",
                                 "jpeg"]
    CLAMAV_ENABLED = False
    CLAMAV_HOST = "127.0.0.1"
    CLAMAV_PORT = 3310
    CLAMAV_TIMEOUT = 30

    # --- request hardening ------------------------------------------------
    TRUSTED_HOSTS = []
    PROXY_FIX_ENABLED = True
    PROXY_FIX_X_FOR = 1
    PROXY_FIX_X_PROTO = 1
    PROXY_FIX_X_HOST = 1
    FORCE_HTTPS_REDIRECT = False

    # --- rate limiting ----------------------------------------------------
    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = "memory://"
    RATELIMIT_DEFAULT = "600 per hour"
    RATELIMIT_LOGIN = "10 per minute;60 per hour"
    RATELIMIT_WRITE = "60 per minute"
    RATELIMIT_HEADERS_ENABLED = True

    # --- application policy ----------------------------------------------
    REQUIRE_LOGIN_TO_BROWSE = False
    ALLOWED_EMAIL_DOMAINS = []
    MIN_PASSWORD_LENGTH = 12
    MAX_LOGIN_FAILURES = 8
    LOGIN_LOCKOUT_SECONDS = 900
    DEFAULT_PAGE_SIZE = 48
    MAX_PAGE_SIZE = 200
    MAX_TAGS_PER_ASSET = 4
    ACTIVITY_LOG_PAGE_SIZE = 50

    # --- misc -------------------------------------------------------------
    JSON_SORT_KEYS = False
    SEND_FILE_MAX_AGE_DEFAULT = 3600
    PREFERRED_URL_SCHEME = "https"


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_SSL_STRICT = False
    SECRET_KEY = "dev-only-not-a-real-secret"
    LOG_LEVEL = "DEBUG"
    UPLOAD_DIR = os.path.join(os.getcwd(), "var", "uploads")
    PREFERRED_URL_SCHEME = "http"


class TestingConfig(BaseConfig):
    ENV_NAME = "testing"
    TESTING = True
    DEBUG = False
    SECRET_KEY = "testing-secret-key"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False            # individual tests re-enable it
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_SSL_STRICT = False
    RATELIMIT_ENABLED = False
    PROXY_FIX_ENABLED = False
    CLAMAV_ENABLED = False
    LOG_TO_STDOUT = False
    TRUSTED_HOSTS = []
    MIN_PASSWORD_LENGTH = 8


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"
    DEBUG = False


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "dev": DevelopmentConfig,
    "testing": TestingConfig,
    "test": TestingConfig,
    "production": ProductionConfig,
    "prod": ProductionConfig,
}


def get_config(name: Optional[str] = None):
    key = (name or os.environ.get("FLASK_ENV") or "production").strip().lower()
    return CONFIG_MAP.get(key, ProductionConfig)
