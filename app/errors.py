"""Consistent JSON envelopes and the error handlers that produce them.

Success::

    {"ok": true, "data": {}}

Error::

    {"ok": false, "error": {"code": "VALIDATION_ERROR",
                            "message": "Human-readable error",
                            "fields": {}}}

Every API route returns one of these two shapes, including for framework-level
failures (404, 405, 413, CSRF rejection, unhandled exceptions), so the browser
only ever has to parse one thing.

Connects to: ``app/__init__.py`` calls :func:`register_error_handlers`; every
blueprint raises :class:`ApiError` or returns :func:`ok`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from flask import current_app, jsonify, request
from werkzeug.exceptions import HTTPException

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Raise anywhere inside a request to produce a structured error."""

    def __init__(self, code: str, message: str, status: int = 400,
                 fields: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or {}

    def to_response(self):
        return error(self.code, self.message, self.status, self.fields)


class ValidationError(ApiError):
    def __init__(self, message: str = "Please correct the highlighted fields.",
                 fields: Optional[Dict[str, str]] = None):
        super().__init__("VALIDATION_ERROR", message, 400, fields)


class AuthRequired(ApiError):
    def __init__(self, message: str = "Sign in to continue."):
        super().__init__("AUTH_REQUIRED", message, 401)


class Forbidden(ApiError):
    def __init__(self, message: str = "You do not have permission to do that."):
        super().__init__("FORBIDDEN", message, 403)


class NotFound(ApiError):
    def __init__(self, message: str = "Not found."):
        super().__init__("NOT_FOUND", message, 404)


class Conflict(ApiError):
    def __init__(self, message: str = "That conflicts with the current state.",
                 code: str = "CONFLICT", fields: Optional[Dict[str, str]] = None):
        super().__init__(code, message, 409, fields)


class PayloadTooLarge(ApiError):
    def __init__(self, message: str = "That file is larger than the upload limit."):
        super().__init__("PAYLOAD_TOO_LARGE", message, 413)


def ok(data: Any = None, status: int = 200):
    """Success envelope. ``data`` is always present, defaulting to ``{}``."""
    return jsonify({"ok": True, "data": data if data is not None else {}}), status


def error(code: str, message: str, status: int = 400,
          fields: Optional[Dict[str, str]] = None):
    payload = {"ok": False, "error": {"code": code, "message": message,
                                      "fields": fields or {}}}
    return jsonify(payload), status


_HTTP_CODE_NAMES = {
    400: "BAD_REQUEST", 401: "AUTH_REQUIRED", 403: "FORBIDDEN", 404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE", 429: "RATE_LIMITED", 500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY", 503: "SERVICE_UNAVAILABLE",
}


def _wants_json() -> bool:
    if request.path.startswith("/api/") or request.path == "/health":
        return True
    accept = request.accept_mimetypes
    return bool(accept["application/json"] >= accept["text/html"])


def register_error_handlers(app) -> None:
    from flask_wtf.csrf import CSRFError

    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):
        if exc.status >= 500:
            log.error("API error %s: %s", exc.code, exc.message)
        return exc.to_response()

    @app.errorhandler(CSRFError)
    def _csrf_error(exc):
        # Deliberately generic: the reason string can leak session details.
        log.warning("CSRF rejection on %s %s", request.method, request.path)
        return error("CSRF_INVALID",
                     "Your session security token expired. Reload the page and try again.",
                     400)

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        status = exc.code or 500
        code = _HTTP_CODE_NAMES.get(status, "HTTP_ERROR")
        message = exc.description or exc.name
        if status == 413:
            limit = current_app.config.get("MAX_CONTENT_LENGTH", 0)
            message = ("That upload is larger than the {0} MB limit."
                       .format(round(limit / (1024 * 1024), 1)))
        if status == 429:
            message = "Too many requests. Wait a moment and try again."
        if not _wants_json():
            return exc
        return error(code, message, status)

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        # Never leak stack traces, SQL, or connection strings to the client.
        log.exception("Unhandled exception on %s %s", request.method, request.path)
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:       # pragma: no cover - defensive
            pass
        if not _wants_json():
            raise exc
        return error("INTERNAL_ERROR",
                     "Something went wrong on the server. The error has been logged.",
                     500)
