"""Flask extension singletons.

Kept in their own module so models, blueprints and the app factory can import
them without creating a circular import back through ``app/__init__.py``.
"""
from __future__ import annotations

from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()
login_manager = LoginManager()

# The API never redirects to an HTML login page; unauthenticated requests get a
# 401 JSON envelope instead (wired up in app/__init__.py).
login_manager.session_protection = "strong"

try:  # pragma: no cover - exercised only when the optional dep is installed
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address)
    LIMITER_AVAILABLE = True
except ImportError:  # pragma: no cover
    limiter = None
    LIMITER_AVAILABLE = False
