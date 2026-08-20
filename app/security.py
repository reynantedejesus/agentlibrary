"""Authorization decorators, security headers and host validation.

Authentication itself is Flask-Login session cookies (see ``app/api/auth.py``);
this module decides *what an authenticated user may do* and enforces it on the
server for every protected endpoint. Nothing here trusts a client-side flag.

Connects to: every blueprint in ``app/api`` (decorators), and
``app/__init__.py`` (``install_security_headers``, ``check_trusted_host``).
"""
from __future__ import annotations

import functools
from typing import Callable, Optional

from flask import current_app, request
from flask_login import current_user

from app import reference
from app.errors import AuthRequired, Forbidden

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
# decorators
# ---------------------------------------------------------------------------
def login_required_json(view: Callable) -> Callable:
    """Like flask_login.login_required, but 401 JSON instead of a redirect."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not getattr(current_user, "is_authenticated", False):
            raise AuthRequired()
        if not current_user.is_active:
            raise Forbidden("This account has been disabled.")
        return view(*args, **kwargs)

    return wrapper


def role_required(minimum: str) -> Callable:
    """Require at least ``minimum`` role (user < reviewer < admin)."""

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if not getattr(current_user, "is_authenticated", False):
                raise AuthRequired()
            if not current_user.is_active:
                raise Forbidden("This account has been disabled.")
            if not current_user.has_role(minimum):
                raise Forbidden(
                    "This action requires the {0} role.".format(minimum))
            return view(*args, **kwargs)

        return wrapper

    return decorator


reviewer_required = role_required(reference.ROLE_REVIEWER)
admin_required = role_required(reference.ROLE_ADMIN)


def browse_permitted() -> bool:
    """Whether the caller may read the approved catalogue.

    Anonymous browsing matches the prototype and is the default; set
    ``REQUIRE_LOGIN_TO_BROWSE=1`` to require a session for reads too.
    """
    if not current_app.config.get("REQUIRE_LOGIN_TO_BROWSE", False):
        return True
    return bool(getattr(current_user, "is_authenticated", False))


def require_browse() -> None:
    if not browse_permitted():
        raise AuthRequired("Sign in to browse the Agent Library.")


def current_user_or_none() -> Optional[object]:
    return current_user if getattr(current_user, "is_authenticated", False) else None


def can_view_asset(asset) -> bool:
    """IDOR guard for a single asset.

    Approved assets are visible to anyone allowed to browse. Everything else
    (pending, rejected, deprecated, archived) is visible only to its
    creator/owner or to a reviewer/admin.
    """
    if asset.status == reference.STATUS_APPROVED:
        return browse_permitted()
    viewer = current_user_or_none()
    if viewer is None:
        return False
    if viewer.has_role(reference.ROLE_REVIEWER):
        return True
    return asset.creator_id == viewer.id or asset.owner_id == viewer.id


def assert_can_view_asset(asset) -> None:
    if not can_view_asset(asset):
        # 404 rather than 403: do not confirm that a hidden asset exists.
        from app.errors import NotFound
        raise NotFound("That asset could not be found.")


def assert_can_edit_asset(asset) -> None:
    if not asset.viewer_can_edit(current_user_or_none()):
        if not getattr(current_user, "is_authenticated", False):
            raise AuthRequired()
        raise Forbidden("You can only edit your own submissions "
                        "while they are pending or rejected.")
