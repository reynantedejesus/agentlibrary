"""Authentication and account endpoints.

Replaces the prototype's ``AdminAuth`` module, which compared a plaintext
password held in ``localStorage`` against a constant in the page source.

    POST /api/auth/login            email + password -> session cookie
    POST /api/auth/logout
    GET  /api/auth/me               current user (200 with null when anonymous)
    POST /api/auth/change-password  current + new password
    GET  /api/auth/csrf             a fresh CSRF token

Defences
--------
* Passwords are PBKDF2-SHA256 hashes (``User.set_password``); plaintext is
  never stored, logged or returned.
* Failed attempts increment a counter and lock the account for
  ``LOGIN_LOCKOUT_SECONDS`` after ``MAX_LOGIN_FAILURES``.
* Flask-Limiter throttles the login route (``RATELIMIT_LOGIN``).
* The response is identical for "no such user" and "wrong password", so the
  endpoint is not a user-enumeration oracle.
* ``session.clear()`` before login rotates the session identifier, defeating
  session fixation. Changing the password invalidates every existing session,
  because ``User.get_id()`` embeds a fragment of the hash.

Connects to: ``app/models.py`` (User), ``app/security.py`` (decorators),
``app/audit.py`` (login/logout/password events).
"""
from __future__ import annotations

import datetime as dt
import logging

from flask import Blueprint, current_app, request, session
from flask_login import current_user, login_user, logout_user
from flask_wtf.csrf import generate_csrf

from app import audit
from app.errors import ApiError, AuthRequired, ok
from app.extensions import db, limiter
from app.models import User, utcnow
from app.security import login_required_json
from app.validators import validate_login, validate_new_password

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

GENERIC_LOGIN_FAILURE = "That email address and password combination wasn't recognised."


def _json_body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _rate_limit(rule_key: str):
    """Apply a configured limit if Flask-Limiter is installed and enabled."""
    def decorator(view):
        if limiter is None:
            return view
        return limiter.limit(
            lambda: current_app.config.get(rule_key, "30 per minute"),
            exempt_when=lambda: not current_app.config.get("RATELIMIT_ENABLED", True),
        )(view)
    return decorator


@bp.route("/csrf", methods=["GET"])
def csrf_token():
    """Hand the SPA a fresh token after login or a session rotation."""
    return ok({"csrfToken": generate_csrf()})


@bp.route("/me", methods=["GET"])
def me():
    """Who is signed in. 200 with ``user: null`` when anonymous, so the SPA can
    boot without treating "not logged in" as an error."""
    payload = {"user": None, "csrfToken": generate_csrf()}
    if getattr(current_user, "is_authenticated", False):
        payload["user"] = current_user.to_dict()
    return ok(payload)


@bp.route("/login", methods=["POST"])
@_rate_limit("RATELIMIT_LOGIN")
def login():
    email, password = validate_login(_json_body())
    user = User.query.filter_by(email=email).first()

    if user is not None and user.is_locked:
        audit.record_now(audit.LOGIN_LOCKED, user, {"email": email})
        raise ApiError(
            "ACCOUNT_LOCKED",
            "This account is temporarily locked after too many failed attempts. "
            "Try again later or ask an administrator to reset it.",
            403,
        )

    if user is None or not user.check_password(password):
        if user is not None:
            user.failed_login_count = int(user.failed_login_count or 0) + 1
            maximum = current_app.config.get("MAX_LOGIN_FAILURES", 8)
            if user.failed_login_count >= maximum:
                user.locked_until = utcnow() + dt.timedelta(
                    seconds=current_app.config.get("LOGIN_LOCKOUT_SECONDS", 900))
                user.failed_login_count = 0
                log.warning("Account locked after repeated failures: %s", email)
        # Log the attempt even for unknown addresses — that is the signal an
        # analyst needs during credential stuffing.
        audit.record_now(audit.LOGIN_FAILURE, user, {"email": email},
                         actor_email=email)
        raise ApiError("INVALID_CREDENTIALS", GENERIC_LOGIN_FAILURE, 401)

    if not user.is_active:
        audit.record_now(audit.LOGIN_FAILURE, user, {"reason": "inactive"},
                         actor_email=email)
        raise ApiError("ACCOUNT_DISABLED", "This account has been disabled.", 403)

    # Rotate the session id before establishing the new identity.
    session.clear()
    login_user(user, remember=False)
    session.permanent = True

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    audit.record(audit.LOGIN_SUCCESS, user, {"role": user.role})
    db.session.commit()

    log.info("Login success for %s (role=%s)", user.email, user.role)
    return ok({"user": user.to_dict(), "csrfToken": generate_csrf()})


@bp.route("/logout", methods=["POST"])
def logout():
    if getattr(current_user, "is_authenticated", False):
        audit.record(audit.LOGOUT, current_user)
        db.session.commit()
    logout_user()
    session.clear()
    return ok({"user": None, "csrfToken": generate_csrf()})


@bp.route("/change-password", methods=["POST"])
@login_required_json
@_rate_limit("RATELIMIT_WRITE")
def change_password():
    body = _json_body()
    current_password = body.get("currentPassword")
    if not isinstance(current_password, str) or not current_user.check_password(current_password):
        audit.record_now(audit.LOGIN_FAILURE, current_user,
                         {"reason": "wrong current password on change"})
        raise ApiError("INVALID_CREDENTIALS", "Your current password is incorrect.", 403,
                       {"currentPassword": "Incorrect password."})

    new_password = validate_new_password(body)
    if new_password == current_password:
        raise ApiError("VALIDATION_ERROR", "The new password must differ from the old one.",
                       400, {"newPassword": "Choose a different password."})

    user = current_user._get_current_object()
    user.set_password(new_password)
    audit.record(audit.PASSWORD_CHANGE, user, {"self_service": True})
    db.session.commit()

    # get_id() embeds the hash tail, so every other session for this account is
    # now invalid. Re-login the current one so the user is not signed out here.
    session.clear()
    login_user(user, remember=False)
    session.permanent = True
    log.info("Password changed for %s", user.email)
    return ok({"user": user.to_dict(), "csrfToken": generate_csrf()})


@bp.route("/session", methods=["GET"])
def session_info():
    """Small helper the SPA polls after a 401 to decide whether to prompt."""
    if not getattr(current_user, "is_authenticated", False):
        raise AuthRequired()
    return ok({"user": current_user.to_dict()})
