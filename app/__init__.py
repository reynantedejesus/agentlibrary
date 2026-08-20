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


def _load_dotenv_for_development() -> None:
    """Load ``.env`` — development only.

    In production systemd supplies the environment via ``EnvironmentFile=``;
    a ``.env`` file is never read there, and never committed.
    """
    env_name = (os.environ.get("FLASK_ENV") or "production").strip().lower()
    if env_name not in ("development", "dev", "testing", "test"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    dotenv_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path, override=False)


def create_app(config_name: Optional[str] = None) -> Flask:
    _load_dotenv_for_development()

    from app.config import get_config

    app = Flask(
        __name__,
        template_folder=os.path.join(PROJECT_ROOT, "templates"),
        static_folder=os.path.join(PROJECT_ROOT, "static"),
        static_url_path="/static",
    )
    app.config.from_object(get_config(config_name))

    _assert_secret_key(app)

    from app.logging_config import configure_logging
    configure_logging(app)

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
        raise RuntimeError(
            "SECRET_KEY is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(64))\"` "
            "and put it in the environment file."
        )
    if len(key) < 32 and not app.config.get("TESTING"):
        app.logger.warning("SECRET_KEY is shorter than 32 characters.")


def _init_extensions(app: Flask) -> None:
    from werkzeug.middleware.proxy_fix import ProxyFix

    from app.extensions import csrf, db, limiter, login_manager, migrate

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
    login_manager.init_app(app)

    if limiter is not None and app.config.get("RATELIMIT_ENABLED"):
        limiter.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def _load_user(token: str):
        # get_id() is "<id>|<password-hash-tail>"; a password change therefore
        # invalidates every previously issued session cookie.
        raw_id, _, fingerprint = (token or "").partition("|")
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        user = db.session.get(User, user_id)
        if user is None or not user.is_active:
            return None
        if fingerprint and (user.password_hash or "")[-16:] != fingerprint:
            return None
        return user

    @login_manager.unauthorized_handler
    def _unauthorized():
        from app.errors import error
        return error("AUTH_REQUIRED", "Sign in to continue.", 401)


def _register_error_handlers(app: Flask) -> None:
    from app.errors import register_error_handlers
    register_error_handlers(app)


def _register_blueprints(app: Flask) -> None:
    from app.api.admin import bp as admin_bp
    from app.api.assets import bp as assets_bp
    from app.api.auth import bp as auth_bp
    from app.api.files import bp as files_bp
    from app.api.meta import bp as meta_bp

    app.register_blueprint(meta_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(assets_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(admin_bp)


def _register_page_routes(app: Flask) -> None:
    from flask_wtf.csrf import generate_csrf

    @app.route("/")
    def index():
        """The single-page shell. All data arrives over /api/*."""
        from flask_login import current_user
        viewer = None
        if getattr(current_user, "is_authenticated", False):
            viewer = current_user.to_dict()
        return render_template(
            "index.html",
            csrf_token=generate_csrf(),
            bootstrap_user=viewer,
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
