"""Reference data and health endpoints.

``GET /api/config``  — replaces the prototype's client-side ``CONFIG`` constant.
``GET /health``      — liveness + database readiness for systemd, nginx and
                        monitoring. Never requires authentication and never
                        leaks configuration.

Connects to: ``app/reference.py`` for the vocabularies; the browser calls
``/api/config`` once at boot in ``static/js/app.js``.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

from app import __version__, reference
from app.errors import ok
from app.extensions import db

log = logging.getLogger(__name__)

bp = Blueprint("meta", __name__)


@bp.route("/api/config", methods=["GET"])
def get_config():
    """Everything the UI needs to render dropdowns, filters and labels."""
    data = reference.get_reference()
    data["limits"] = {
        "maxTagsPerAsset": current_app.config.get("MAX_TAGS_PER_ASSET", 4),
        "maxContentLength": current_app.config.get("MAX_CONTENT_LENGTH"),
        "maxFilesPerAsset": current_app.config.get("MAX_FILES_PER_ASSET"),
        "allowedUploadExtensions": current_app.config.get("ALLOWED_UPLOAD_EXTENSIONS"),
        "defaultPageSize": current_app.config.get("DEFAULT_PAGE_SIZE"),
        "maxPageSize": current_app.config.get("MAX_PAGE_SIZE"),
        "minPasswordLength": current_app.config.get("MIN_PASSWORD_LENGTH"),
    }
    data["policy"] = {
        "requireLoginToBrowse": bool(current_app.config.get("REQUIRE_LOGIN_TO_BROWSE")),
        "selfRegistration": False,
    }
    data["appVersion"] = __version__
    return ok(data)


@bp.route("/health", methods=["GET"])
def health():
    """Liveness and database readiness.

    Returns 200 with ``status: "ok"`` when the database answers, 503 with
    ``status: "degraded"`` when it does not. The body carries no credentials,
    hostnames or configuration values.
    """
    database_ok = True
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:      # pragma: no cover - exercised in ops, not tests
        database_ok = False
        log.error("Health check database probe failed: %s", exc)
        db.session.rollback()

    payload = {
        "status": "ok" if database_ok else "degraded",
        "version": __version__,
        "checks": {"database": "ok" if database_ok else "error"},
    }
    return jsonify(payload), (200 if database_ok else 503)
