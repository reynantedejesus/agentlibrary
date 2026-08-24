"""Audit logging.

Every security-relevant event lands in ``activity_log``: login success and
failure, logout, password change, submission, edit, approval, rejection,
archive, version creation, file upload and file download.

``record()`` deliberately does not commit — it adds the row to the current
session so it participates in the caller's transaction. If the business change
rolls back, so does its audit entry. Use ``record_now()`` for events that must
survive a rollback (failed logins in particular).

Connects to: every blueprint in ``app/api``; the rows are read back by
``GET /api/admin/activity``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from flask import has_request_context, request

from app.extensions import db
from app.models import ActivityLog

log = logging.getLogger("agentlibrary.audit")

# Canonical action names.
LOGIN_SUCCESS = "login.success"
LOGIN_FAILURE = "login.failure"
LOGIN_LOCKED = "login.locked"
LOGOUT = "logout"
PASSWORD_CHANGE = "password.change"
USER_CREATE = "user.create"
USER_UPDATE = "user.update"
ASSET_SUBMIT = "asset.submit"
UPDATE_REQUEST_SUBMIT = "update_request.submit"
UPDATE_REQUEST_ACCEPT = "update_request.accept"
UPDATE_REQUEST_DECLINE = "update_request.decline"
UPLOAD_STAGED = "file.stage"
ASSET_UPDATE = "asset.update"
ASSET_APPROVE = "asset.approve"
ASSET_REJECT = "asset.reject"
ASSET_ARCHIVE = "asset.archive"
ASSET_DEPARTMENT_MIGRATE = "asset.department_migrate"
VERSION_CREATE = "version.create"
FILE_UPLOAD = "file.upload"
FILE_DOWNLOAD = "file.download"
FILE_DELETE = "file.delete"
CONFIG_UPDATE = "config.update"


def _client_ip() -> Optional[str]:
    if not has_request_context():
        return None
    # ProxyFix has already normalised remote_addr from X-Forwarded-For.
    return (request.remote_addr or "")[:45] or None


def _user_agent() -> Optional[str]:
    if not has_request_context():
        return None
    return (request.headers.get("User-Agent") or "")[:255] or None


def _actor(actor=None):
    """Who performed this action.

    With no user accounts, most actions are anonymous: submissions and update
    requests carry only the name the person typed, which is recorded in
    ``detail`` rather than treated as an identity. The one real actor is the
    admin session, so that is what this resolves.
    """
    if actor is not None:
        return actor
    if has_request_context():
        from app.security import current_user_or_none
        return current_user_or_none()
    return None


def build(action: str, obj: Any = None, detail: Optional[Dict[str, Any]] = None,
          actor=None, actor_email: Optional[str] = None) -> ActivityLog:
    """Construct (but do not add) an ActivityLog row."""
    who = _actor(actor)
    object_type = None
    object_id = None
    object_label = None
    if obj is not None:
        object_type = obj.__class__.__name__.lower()
        object_id = getattr(obj, "id", None)
        object_label = (getattr(obj, "wgt_code", None)
                        or getattr(obj, "original_name", None)
                        or getattr(obj, "email", None))
        if object_label:
            object_label = str(object_label)[:200]

    entry = ActivityLog(
        action=action,
        actor_id=getattr(who, "id", None),
        actor_email=(actor_email or getattr(who, "email", None)),
        object_type=object_type,
        object_id=object_id,
        object_label=object_label,
        ip=_client_ip(),
        user_agent=_user_agent(),
        detail=detail or {},
    )
    log.info("audit action=%s actor=%s object=%s:%s ip=%s",
             action, entry.actor_email or "-", object_type or "-",
             object_id if object_id is not None else "-", entry.ip or "-")
    return entry


def record(action: str, obj: Any = None, detail: Optional[Dict[str, Any]] = None,
           actor=None, actor_email: Optional[str] = None) -> ActivityLog:
    """Add an audit row to the current transaction (commits with the caller)."""
    entry = build(action, obj, detail, actor, actor_email)
    db.session.add(entry)
    return entry


def record_now(action: str, obj: Any = None, detail: Optional[Dict[str, Any]] = None,
               actor=None, actor_email: Optional[str] = None) -> None:
    """Add and commit immediately — for events that must persist even when the
    surrounding request fails (e.g. a rejected login attempt)."""
    record(action, obj, detail, actor, actor_email)
    try:
        db.session.commit()
    except Exception:       # pragma: no cover - defensive
        db.session.rollback()
        log.exception("Failed to persist audit entry for %s", action)
