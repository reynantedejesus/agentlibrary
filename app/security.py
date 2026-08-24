"""Authorization decorators, security headers and host validation.

Authentication is a single shared admin password held in a signed session
cookie (see ``app/api/auth.py``); this module decides *what a caller may do*
and enforces it on the server for every protected endpoint. Nothing here
trusts a client-side flag.

Connects to: every blueprint in ``app/api`` (decorators), and
``app/__init__.py`` (``install_security_headers``, ``check_trusted_host``).
"""
from __future__ import annotations

import functools
from typing import Callable, Optional

from flask import current_app, request

from app import reference
from app.errors import AuthRequired

# A conservative CSP. The app inlines no scripts, but it does inline a handful
# of SVG icons and uses Google Fonts, so styles and fonts allow those origins.
CSP_DIRECTIVES = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
}


def install_security_headers(app) -> None:
    @app.after_request
    def _headers(response):
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        response.headers.setdefault("Content-Security-Policy", CSP_DIRECTIVES)
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        # API responses must never be cached by a shared proxy.
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def check_trusted_host(app) -> None:
    """Reject Host headers we did not configure — blocks host-header poisoning
    of absolute URLs and password-reset style links."""

    @app.before_request
    def _host_guard():
        trusted = app.config.get("TRUSTED_HOSTS") or []
        if not trusted:
            return None
        host = (request.host or "").split(":")[0].lower()
        allowed = {h.split(":")[0].lower() for h in trusted}
        if host in allowed:
            return None
        from app.errors import error
        return error("UNTRUSTED_HOST", "Unrecognised host.", 400)


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------
# The Agent Library has no user accounts. Browsing, submitting a new asset and
# raising an update request are all open; the Admin / Review console is gated
# by a single shared password, verified server-side against a hash and held in
# a signed, HttpOnly session cookie (see app/api/auth.py).
#
# Everything below still enforces on the server. The client's idea of whether
# it is "unlocked" is a rendering hint only — every protected view re-checks
# the session here, so editing the browser's state grants nothing.
SESSION_ADMIN_KEY = "admin_unlocked"


def is_admin() -> bool:
    """Whether this request carries a valid admin session."""
    from flask import session
    return session.get(SESSION_ADMIN_KEY) is True


def admin_required(view: Callable) -> Callable:
    """Refuse the request unless the admin password has been entered."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            raise AuthRequired("Enter the admin password to continue.")
        return view(*args, **kwargs)

    return wrapper


# Kept as an alias so the intent reads correctly at review-only call sites and
# so a future move back to per-person roles has one place to change.
reviewer_required = admin_required


def browse_permitted() -> bool:
    """Whether the caller may read the approved catalogue.

    Open by default, matching the prototype. Set ``REQUIRE_LOGIN_TO_BROWSE=1``
    to require the admin password even to browse (useful if the site is ever
    exposed beyond the internal network).
    """
    if not current_app.config.get("REQUIRE_LOGIN_TO_BROWSE", False):
        return True
    return is_admin()


def require_browse() -> None:
    if not browse_permitted():
        raise AuthRequired("Enter the admin password to browse the Agent Library.")


def can_view_asset(asset) -> bool:
    """IDOR guard for a single asset.

    Approved assets are visible to anyone allowed to browse. Everything else
    — pending, rejected, deprecated, archived — is admin-only, because with no
    accounts there is nobody else who could legitimately be its owner.
    """
    if asset.status == reference.STATUS_APPROVED:
        return browse_permitted()
    return is_admin()


def assert_can_view_asset(asset) -> None:
    if not can_view_asset(asset):
        # 404 rather than 403: do not confirm that a hidden asset exists.
        from app.errors import NotFound
        raise NotFound("That asset could not be found.")


def assert_can_edit_asset(asset) -> None:
    """Only an administrator edits a record once it has been submitted."""
    if not is_admin():
        raise AuthRequired("Enter the admin password to edit this asset.")


def current_user_or_none() -> Optional[object]:
    """The admin account backing this session, when there is one.

    Used for audit attribution. Returns None for anonymous callers, which is
    the normal case for browsing and submitting.
    """
    if not is_admin():
        return None
    from flask import session
    from app.extensions import db
    from app.models import User
    user_id = session.get("admin_user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)
