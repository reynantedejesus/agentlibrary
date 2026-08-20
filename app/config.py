"""Configuration objects for the Agent Library.

Every setting is read from the process environment. In development a ``.env``
file at the project root is loaded by :func:`app.create_app`; in production the
values come from systemd's ``EnvironmentFile=`` so that no secret is ever
written into source control.

Connects to: ``app/__init__.py`` selects one of these classes via ``FLASK_ENV``.
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
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    ENV_NAME = os.environ.get("FLASK_ENV", "production")
    DEBUG = False
    TESTING = False
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
    LOG_DIR = os.environ.get("LOG_DIR", "/var/log/agentlibrary")
    LOG_TO_STDOUT = _bool("LOG_TO_STDOUT", True)

    # --- database ---------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = _build_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": _int("DB_POOL_RECYCLE", 280),
        "pool_size": _int("DB_POOL_SIZE", 5),
        "max_overflow": _int("DB_MAX_OVERFLOW", 10),
    }

    # --- sessions & cookies ----------------------------------------------
    SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "agentlibrary_session")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", True)
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    PERMANENT_SESSION_LIFETIME = _int("SESSION_LIFETIME_SECONDS", 60 * 60 * 12)
    SESSION_REFRESH_EACH_REQUEST = False

    # --- CSRF -------------------------------------------------------------
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None          # tied to session lifetime instead
    WTF_CSRF_HEADERS = ["X-CSRFToken", "X-CSRF-Token"]
    WTF_CSRF_SSL_STRICT = _bool("WTF_CSRF_SSL_STRICT", True)

    # --- uploads ----------------------------------------------------------
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/var/lib/agentlibrary/uploads")
    MAX_CONTENT_LENGTH = _int("MAX_CONTENT_LENGTH", 26214400)   # 25 MiB
    MAX_FILES_PER_ASSET = _int("MAX_FILES_PER_ASSET", 25)
    ALLOWED_UPLOAD_EXTENSIONS = _list(
        "ALLOWED_UPLOAD_EXTENSIONS",
        ["pdf", "md", "txt", "docx", "zip", "json", "yaml", "yml",
         "csv", "xlsx", "png", "jpg", "jpeg"],
    )
    CLAMAV_ENABLED = _bool("CLAMAV_ENABLED", False)
    CLAMAV_HOST = os.environ.get("CLAMAV_HOST", "127.0.0.1")
    CLAMAV_PORT = _int("CLAMAV_PORT", 3310)
    CLAMAV_TIMEOUT = _int("CLAMAV_TIMEOUT", 30)

    # --- request hardening ------------------------------------------------
    TRUSTED_HOSTS = _list("TRUSTED_HOSTS")
    PROXY_FIX_ENABLED = _bool("PROXY_FIX_ENABLED", True)
    PROXY_FIX_X_FOR = _int("PROXY_FIX_X_FOR", 1)
    PROXY_FIX_X_PROTO = _int("PROXY_FIX_X_PROTO", 1)
    PROXY_FIX_X_HOST = _int("PROXY_FIX_X_HOST", 1)
    FORCE_HTTPS_REDIRECT = _bool("FORCE_HTTPS_REDIRECT", False)

    # --- rate limiting ----------------------------------------------------
    RATELIMIT_ENABLED = _bool("RATELIMIT_ENABLED", True)
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = os.environ.get("RATELIMIT_DEFAULT", "600 per hour")
    RATELIMIT_LOGIN = os.environ.get("RATELIMIT_LOGIN", "10 per minute;60 per hour")
    RATELIMIT_WRITE = os.environ.get("RATELIMIT_WRITE", "60 per minute")
    RATELIMIT_HEADERS_ENABLED = True

    # --- application policy ----------------------------------------------
    REQUIRE_LOGIN_TO_BROWSE = _bool("REQUIRE_LOGIN_TO_BROWSE", False)
    ALLOWED_EMAIL_DOMAINS = _list("ALLOWED_EMAIL_DOMAINS")
    MIN_PASSWORD_LENGTH = _int("MIN_PASSWORD_LENGTH", 12)
    MAX_LOGIN_FAILURES = _int("MAX_LOGIN_FAILURES", 8)
    LOGIN_LOCKOUT_SECONDS = _int("LOGIN_LOCKOUT_SECONDS", 900)
    DEFAULT_PAGE_SIZE = _int("DEFAULT_PAGE_SIZE", 48)
    MAX_PAGE_SIZE = _int("MAX_PAGE_SIZE", 200)
    MAX_TAGS_PER_ASSET = _int("MAX_TAGS_PER_ASSET", 4)
    ACTIVITY_LOG_PAGE_SIZE = _int("ACTIVITY_LOG_PAGE_SIZE", 50)

    # --- misc -------------------------------------------------------------
    JSON_SORT_KEYS = False
    SEND_FILE_MAX_AGE_DEFAULT = _int("STATIC_CACHE_SECONDS", 3600)
    PREFERRED_URL_SCHEME = os.environ.get("PREFERRED_URL_SCHEME", "https")


class DevelopmentConfig(BaseConfig):
    DEBUG = _bool("FLASK_DEBUG", True)
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    WTF_CSRF_SSL_STRICT = _bool("WTF_CSRF_SSL_STRICT", False)
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-a-real-secret")
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(os.getcwd(), "var", "uploads"))
    PREFERRED_URL_SCHEME = "http"


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = False
    SECRET_KEY = "testing-secret-key"
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite://")
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
