"""Admin unlock — the Agent Library's only authentication.

There are no user accounts. Browsing the catalogue, submitting a new asset and
raising an update request are all open, exactly as in the prototype. The Admin
/ Review console is gated by a single shared password.

    POST /api/auth/unlock          {"password": "..."} -> admin session
    POST /api/auth/lock            end the admin session
    GET  /api/auth/me              {"unlocked": bool, "csrfToken": "..."}
    POST /api/auth/change-password {"currentPassword", "newPassword"}
    GET  /api/auth/csrf            a fresh CSRF token

How this differs from the prototype it replaces
-----------------------------------------------
The prototype compared ``attempt === CONFIG.defaultAdminPassword`` in
JavaScript, with the password visible in page source. Here the password never
leaves the server: it is stored only as a PBKDF2-SHA256 hash on a single
``users`` row, compared with a constant-time digest check, and success sets a
signed HttpOnly session cookie. Editing ``State`` in the browser console grants
nothing, because every protected endpoint re-checks the session server-side.

A single shared secret is one credential for everyone, so it gets defended
accordingly: failed attempts are throttled per IP by Flask-Limiter, repeated
failures lock the console for a cooling-off period, and every attempt —
successful or not — is written to the activity log with its source address.

Connects to: ``app/security.py`` (``is_admin``/``admin_required``),
``app/models.py`` (the admin ``User`` row), ``app/audit.py``.
"""
from __future__ import annotations

import datetime as dt
import logging

from flask import Blueprint, current_app, request, session
from flask_wtf.csrf import generate_csrf

from app import audit, reference
from app.errors import ApiError, ok
from app.extensions import db, limiter
from app.models import User, utcnow
from app.security import SESSION_ADMIN_KEY, admin_required, is_admin
from app.validators import Validator, validate_new_password

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

WRONG_PASSWORD = "Incorrect password."


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


def admin_account():
    """The single account whose hash backs the shared password.

    Created by ``flask create-admin``. If several exist (an older multi-user
    deployment), the oldest admin wins so the answer is stable.
    """
    return (User.query
            .filter_by(role=reference.ROLE_ADMIN)
            .order_by(User.id.asc())
            .first())


@bp.route("/csrf", methods=["GET"])
def csrf_token():
    """Hand the page a fresh token after the session rotates."""
    return ok({"csrfToken": generate_csrf()})


@bp.route("/me", methods=["GET"])
def me():
    """Whether this browser holds an admin session.

    Always 200 — "locked" is the normal state, not an error, so the page can
    boot without treating it as one.
    """
    account = admin_account()
    return ok({
        "unlocked": is_admin(),
        "adminConfigured": account is not None,
        "csrfToken": generate_csrf(),
    })


@bp.route("/unlock", methods=["POST"])
@_rate_limit("RATELIMIT_LOGIN")
def unlock():
    """Verify the shared admin password and open an admin session."""
    body = _json_body()
    password = body.get("password")
    if not isinstance(password, str) or not password:
        v = Validator(body)
        v.fail("password", "Enter the admin password.")
        v.raise_if_invalid("Enter the admin password.")

    account = admin_account()
    if account is None:
        # Nothing to compare against. Say so plainly: this is a deployment
        # step that has not been run, not a wrong password.
        log.error("Admin unlock attempted but no administrator account exists")
        raise ApiError(
            "ADMIN_NOT_CONFIGURED",
            "No administrator password has been set up yet. Run "
            "`flask create-admin` on the server first.",
            503,
        )

    now = utcnow()
    if account.locked_until and account.locked_until > now:
        audit.record_now(audit.LOGIN_LOCKED, account, {"reason": "cooling off"})
        raise ApiError(
            "ACCOUNT_LOCKED",
            "Too many incorrect attempts. Try again in a few minutes.",
            403,
        )

    if not account.check_password(password):
        account.failed_login_count = int(account.failed_login_count or 0) + 1
        maximum = current_app.config.get("MAX_LOGIN_FAILURES", 8)
        if account.failed_login_count >= maximum:
            account.locked_until = now + dt.timedelta(
                seconds=current_app.config.get("LOGIN_LOCKOUT_SECONDS", 900))
            account.failed_login_count = 0
            log.warning("Admin console locked after %s failed attempts from %s",
                        maximum, request.remote_addr)
        audit.record_now(audit.LOGIN_FAILURE, account,
                         {"attempts": account.failed_login_count})
        raise ApiError("INVALID_CREDENTIALS", WRONG_PASSWORD, 401)

    # Rotate the session id before granting the new privilege level.
    session.clear()
    session[SESSION_ADMIN_KEY] = True
    session["admin_user_id"] = account.id
    session.permanent = True

    account.failed_login_count = 0
    account.locked_until = None
    account.last_login_at = now
    audit.record(audit.LOGIN_SUCCESS, account, {"console": "admin"})
    db.session.commit()

    log.info("Admin console unlocked from %s", request.remote_addr)
    return ok({"unlocked": True, "csrfToken": generate_csrf()})


@bp.route("/lock", methods=["POST"])
def lock():
    """End the admin session."""
    if is_admin():
        account = admin_account()
        audit.record(audit.LOGOUT, account, {"console": "admin"})
        db.session.commit()
    session.clear()
    return ok({"unlocked": False, "csrfToken": generate_csrf()})


@bp.route("/change-password", methods=["POST"])
@admin_required
@_rate_limit("RATELIMIT_WRITE")
def change_password():
    """Rotate the shared admin password. Requires the current one."""
    body = _json_body()
    account = admin_account()
    if account is None:
        raise ApiError("ADMIN_NOT_CONFIGURED",
                       "No administrator account exists.", 503)

    current_password = body.get("currentPassword")
    if not isinstance(current_password, str) or not account.check_password(current_password):
        audit.record_now(audit.LOGIN_FAILURE, account,
                         {"reason": "wrong current password on change"})
        raise ApiError("INVALID_CREDENTIALS", "The current password is incorrect.",
                       403, {"currentPassword": WRONG_PASSWORD})

    new_password = validate_new_password(body)
    if new_password == current_password:
        raise ApiError("VALIDATION_ERROR",
                       "The new password must differ from the old one.", 400,
                       {"newPassword": "Choose a different password."})

    account.set_password(new_password)
    audit.record(audit.PASSWORD_CHANGE, account, {"console": "admin"})
    db.session.commit()

    # Everyone else holding the old password is now out; keep this browser in.
    session.clear()
    session[SESSION_ADMIN_KEY] = True
    session["admin_user_id"] = account.id
    session.permanent = True

    log.info("Admin password rotated from %s", request.remote_addr)
    return ok({"unlocked": True, "csrfToken": generate_csrf()})
