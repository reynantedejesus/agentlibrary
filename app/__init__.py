"""Application factory for the Agent Library.

``create_app()`` is the single entry point used by:

* ``wsgi.py``          — gunicorn in production
* ``flask run``        — development (``FLASK_APP=wsgi.py``)
* ``tests/conftest.py`` — the pytest suite (``create_app("testing")``)

Order matters here: configuration, then logging, then extensions, then error
handlers, then blueprints. Security headers and the host guard are installed
last so they wrap everything.
"""
from __future__ import annotations

import os
from typing import Optional

from flask import Flask, render_template, request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
__version__ = "1.0.0"


# Where a production deployment keeps its environment file. systemd loads this
# through EnvironmentFile= for the service itself; the loader below reads the
# same file so that commands run BY HAND — gunicorn, `flask db upgrade`,
# `flask create-admin` — see identical configuration without the operator
# having to re-export anything.
DEFAULT_PRODUCTION_ENV_FILE = "/etc/agentlibrary/agentlibrary.env"

DEV_ENV_NAMES = ("development", "dev", "testing", "test")


def _parse_env_file(path):
    """Minimal KEY=VALUE parser used when python-dotenv is unavailable.

    Handles the subset systemd's EnvironmentFile= and .env agree on: comments,
    blank lines, an optional ``export`` prefix, and single- or double-quoted
    values. Anything more exotic should be quoted.
    """
    values = {}
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key] = value
    return values


def _apply_env_file(path):
    """Load ``path`` into os.environ without overriding what is already set.

    Not overriding matters: when systemd has already supplied the environment
    via EnvironmentFile=, or an operator has exported something deliberately,
    that value wins. This loader only fills in the gaps.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        for key, value in _parse_env_file(path).items():
            os.environ.setdefault(key, value)
        return
    load_dotenv(path, override=False)


def _env_file_candidates():
    """Ordered (path, required) pairs to try for this environment.

    An explicitly named ENV_FILE is *required* — if the operator points at a
    file, a typo in that path should fail loudly rather than fall through to a
    confusing "SECRET_KEY is not set" further down.
    """
    explicit = (os.environ.get("ENV_FILE")
                or os.environ.get("AGENTLIBRARY_ENV_FILE") or "").strip()
    if explicit:
        return [(explicit, True)]

    env_name = (os.environ.get("FLASK_ENV") or "production").strip().lower()
    if env_name in DEV_ENV_NAMES:
        return [(os.path.join(PROJECT_ROOT, ".env"), False)]
    return [(DEFAULT_PRODUCTION_ENV_FILE, False)]


def load_environment_files():
    """Populate os.environ from the appropriate env file.

    Returns diagnostic notes to be logged once logging is configured (this runs
    before that, so it cannot log for itself). Never raises for a merely absent
    default file — a deployment that supplies everything through systemd or the
    shell is perfectly valid.
    """
    notes = []
    for path, required in _env_file_candidates():
        if not os.path.isfile(path):
            if required:
                raise RuntimeError(
                    "ENV_FILE points at {0}, which does not exist.".format(path))
            notes.append(("debug", "No environment file at {0}".format(path)))
            continue
        if not os.access(path, os.R_OK):
            message = (
                "Environment file {0} exists but is not readable by uid {1}. "
                "It is normally mode 0640 owned root:agentlibrary — run as the "
                "agentlibrary service account (sudo -u agentlibrary ...), or "
                "add your account to the agentlibrary group."
            ).format(path, os.getuid())
            if required:
                raise RuntimeError(message)
            notes.append(("warning", message))
            continue
        _apply_env_file(path)
        notes.append(("info", "Loaded environment from {0}".format(path)))
    return notes


def create_app(config_name: Optional[str] = None) -> Flask:
    env_notes = load_environment_files()

    from app.config import get_config

    app = Flask(
        __name__,
        template_folder=os.path.join(PROJECT_ROOT, "templates"),
        static_folder=os.path.join(PROJECT_ROOT, "static"),
        static_url_path="/static",
    )
    app.config.from_object(get_config(config_name))
    _apply_environment_overrides(app)

    _assert_secret_key(app)

    from app.logging_config import configure_logging
    configure_logging(app)
    for level, message in env_notes:
        getattr(app.logger, level)(message)

    _init_extensions(app)
    _register_error_handlers(app)
    _register_blueprints(app)
    _register_page_routes(app)
    _register_cli(app)

    from app.security import check_trusted_host, install_security_headers
    check_trusted_host(app)
    install_security_headers(app)
    _register_https_redirect(app)

    app.logger.info("Agent Library %s started (env=%s, debug=%s)",
                    __version__, app.config.get("ENV_NAME"), app.debug)
    return app


# Config classes in app/config.py read os.environ in their CLASS BODIES, which
# runs once, at first import of that module. create_app() loads the environment
# file *before* importing it, so the ordinary path is correct — but a single
# stray ``import app.config`` anywhere earlier in a process would freeze those
# reads against an environment that had not been populated yet, and the app
# would start with silently wrong settings.
#
# Rather than depend on import order, every environment-configurable setting is
# re-applied here, after the class has been loaded. The list is explicit on
# purpose: it is the supported environment surface, and it means an unexpected
# variable (DEBUG=1, say) can never reach app.config by accident.
_ENV_SETTINGS = (
    "SECRET_KEY", "LOG_LEVEL", "LOG_DIR", "LOG_TO_STDOUT",
    "SESSION_COOKIE_NAME", "SESSION_COOKIE_SECURE", "SESSION_COOKIE_SAMESITE",
    "WTF_CSRF_SSL_STRICT",
    "UPLOAD_DIR", "MAX_CONTENT_LENGTH", "MAX_FILES_PER_ASSET",
    "ALLOWED_UPLOAD_EXTENSIONS",
    "CLAMAV_ENABLED", "CLAMAV_HOST", "CLAMAV_PORT", "CLAMAV_TIMEOUT",
    "TRUSTED_HOSTS", "PROXY_FIX_ENABLED", "PROXY_FIX_X_FOR",
    "PROXY_FIX_X_PROTO", "PROXY_FIX_X_HOST", "FORCE_HTTPS_REDIRECT",
    "RATELIMIT_ENABLED", "RATELIMIT_STORAGE_URI", "RATELIMIT_DEFAULT",
    "RATELIMIT_LOGIN", "RATELIMIT_WRITE",
    "REQUIRE_LOGIN_TO_BROWSE", "ALLOWED_EMAIL_DOMAINS", "MIN_PASSWORD_LENGTH",
    "MAX_LOGIN_FAILURES", "LOGIN_LOCKOUT_SECONDS", "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE", "MAX_TAGS_PER_ASSET", "ACTIVITY_LOG_PAGE_SIZE",
    "PREFERRED_URL_SCHEME",
)

# Environment variables whose name differs from the config key they set.
_ENV_ALIASES = {
    "SESSION_LIFETIME_SECONDS": "PERMANENT_SESSION_LIFETIME",
    "STATIC_CACHE_SECONDS": "SEND_FILE_MAX_AGE_DEFAULT",
}


def _coerce_like(raw, current):
    """Convert an environment string to the type the config default uses."""
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            return current
    if isinstance(current, (list, tuple)):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


def _apply_environment_overrides(app: Flask) -> None:
    """Re-apply environment settings on top of the imported config class."""
    for key in _ENV_SETTINGS:
        raw = os.environ.get(key)
        if raw is None:
            continue
        app.config[key] = _coerce_like(raw, app.config.get(key))

    for env_name, config_key in _ENV_ALIASES.items():
        raw = os.environ.get(env_name)
        if raw is not None:
            app.config[config_key] = _coerce_like(raw, app.config.get(config_key))

    # The database URL is assembled from several variables, so rebuild it
    # whenever any of them is present.
    if any(os.environ.get(name) for name in
           ("DATABASE_URL", "MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE",
            "MYSQL_USER", "MYSQL_PASSWORD")):
        from app.config import _build_database_url
        app.config["SQLALCHEMY_DATABASE_URI"] = _build_database_url()


def _secret_key_help() -> str:
    """Explain precisely why SECRET_KEY is missing on THIS invocation.

    The usual cause is running gunicorn or flask by hand: systemd loads
    /etc/agentlibrary/agentlibrary.env through EnvironmentFile=, but a plain
    shell does not, so the message has to distinguish "file missing" from
    "file present but unreadable by you".
    """
    explicit = (os.environ.get("ENV_FILE")
                or os.environ.get("AGENTLIBRARY_ENV_FILE") or "").strip()
    path = explicit or DEFAULT_PRODUCTION_ENV_FILE

    if not os.path.isfile(path):
        cause = "No environment file was found at {0}.".format(path)
        remedy = (
            "Create it (see .env.example), or point at it explicitly:\n"
            "    ENV_FILE=/path/to/agentlibrary.env gunicorn -c gunicorn.conf.py wsgi:application"
        )
    elif not os.access(path, os.R_OK):
        cause = ("{0} exists but uid {1} cannot read it (it is normally mode "
                 "0640, owned root:agentlibrary).".format(path, os.getuid()))
        remedy = (
            "Run as the service account:\n"
            "    sudo -u agentlibrary .venv/bin/gunicorn -c gunicorn.conf.py wsgi:application\n"
            "or add your account to the group (log out and back in afterwards):\n"
            "    sudo usermod -a -G agentlibrary $USER"
        )
    else:
        cause = "{0} was read, but it does not define SECRET_KEY.".format(path)
        remedy = (
            "Add a generated key to it:\n"
            "    python -c \"import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(64))\""
            " | sudo tee -a {0}".format(path)
        )

    return (
        "SECRET_KEY is not set, so the application refuses to start in "
        "production (a missing key resets every session on restart and makes "
        "CSRF tokens forgeable).\n"
        "  Cause:  {0}\n"
        "  Fix:    {1}\n"
        "Note: `systemctl start agentlibrary` loads that file automatically via "
        "EnvironmentFile=; a plain shell does not.".format(cause, remedy)
    )


def _assert_secret_key(app: Flask) -> None:
    """Refuse to boot in production without a real SECRET_KEY.

    A missing key would silently reset sessions on every restart and make CSRF
    tokens forgeable, so this is a hard failure rather than a warning.
    """
    key = app.config.get("SECRET_KEY") or ""
    if app.config.get("TESTING"):
        return
    if not key or key == "dev-only-not-a-real-secret":
        if app.config.get("ENV_NAME", "production").lower() in ("development", "dev"):
            app.logger.warning("Using the development SECRET_KEY — never do this in production.")
            return
        raise RuntimeError(_secret_key_help())
    if len(key) < 32 and not app.config.get("TESTING"):
        app.logger.warning("SECRET_KEY is shorter than 32 characters.")


def _init_extensions(app: Flask) -> None:
    from werkzeug.middleware.proxy_fix import ProxyFix

    from app.extensions import csrf, db, limiter, migrate

    if app.config.get("PROXY_FIX_ENABLED"):
        # nginx sets X-Forwarded-For/-Proto/-Host; without this, remote_addr is
        # always 127.0.0.1 and url_for() builds http:// links behind TLS.
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=app.config.get("PROXY_FIX_X_FOR", 1),
            x_proto=app.config.get("PROXY_FIX_X_PROTO", 1),
            x_host=app.config.get("PROXY_FIX_X_HOST", 1),
        )

    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(PROJECT_ROOT, "migrations"))
    csrf.init_app(app)
    if limiter is not None and app.config.get("RATELIMIT_ENABLED"):
        limiter.init_app(app)


def _register_error_handlers(app: Flask) -> None:
    from app.errors import register_error_handlers
    register_error_handlers(app)


def _register_blueprints(app: Flask) -> None:
    from app.api.admin import bp as admin_bp
    from app.api.assets import bp as assets_bp
    from app.api.auth import bp as auth_bp
    from app.api.files import bp as files_bp
    from app.api.meta import bp as meta_bp
    from app.api.updates import bp as updates_bp

    app.register_blueprint(meta_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(assets_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(updates_bp)
    app.register_blueprint(admin_bp)


def _register_page_routes(app: Flask) -> None:
    from flask_wtf.csrf import generate_csrf

    @app.route("/")
    def index():
        """The single-page shell. All data arrives over /api/*."""
        from app.security import is_admin
        return render_template(
            "index.html",
            csrf_token=generate_csrf(),
            bootstrap_unlocked=is_admin(),
            app_version=__version__,
        )

    @app.route("/favicon.ico")
    def favicon():
        from flask import redirect, url_for
        return redirect(url_for("static", filename="images/wings-logo.png"), code=302)

    @app.after_request
    def _set_csrf_cookie(response):
        """Expose the CSRF token to JavaScript.

        Readable by design (that is the double-submit half of the defence);
        the *session* cookie stays HttpOnly. Same-site + the session-bound
        token mean a cross-origin page can neither read this nor forge one.
        """
        if request.path.startswith("/api/") or request.path == "/":
            try:
                response.set_cookie(
                    "csrf_token",
                    generate_csrf(),
                    secure=bool(app.config.get("SESSION_COOKIE_SECURE")),
                    httponly=False,
                    samesite=app.config.get("SESSION_COOKIE_SAMESITE", "Lax"),
                    path="/",
                )
            except Exception:       # pragma: no cover - no session available
                pass
        return response


def _register_https_redirect(app: Flask) -> None:
    if not app.config.get("FORCE_HTTPS_REDIRECT"):
        return

    @app.before_request
    def _https_only():
        from flask import redirect
        if request.is_secure or request.path == "/health":
            return None
        target = request.url.replace("http://", "https://", 1)
        return redirect(target, code=308)


def _register_cli(app: Flask) -> None:
    from app.cli import register_cli
    register_cli(app)
