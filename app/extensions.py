"""Flask extension singletons.

Kept in their own module so models, blueprints and the app factory can import
them without creating a circular import back through ``app/__init__.py``.
"""
from __future__ import annotations

from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()

# NOTE: there is no Flask-Login here. The Agent Library has no user accounts —
# the Admin / Review console is gated by a single shared password held in the
# signed session cookie. See app/security.py and app/api/auth.py.

try:  # pragma: no cover - exercised only when the optional dep is installed
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address)
    LIMITER_AVAILABLE = True
except ImportError:  # pragma: no cover
    limiter = None
    LIMITER_AVAILABLE = False
