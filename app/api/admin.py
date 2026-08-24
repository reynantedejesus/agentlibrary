"""Administrative endpoints.

    GET   /api/admin/activity            audit trail
    GET   /api/admin/stats               dashboard counters
    POST  /api/admin/assets/<id>/migrate-department   controlled, audited

All of these require the shared admin password (``admin_required``).

The department migration endpoint is the single sanctioned way to change an
asset's department after a WGT code exists. It mints a new code for the target
department, keeps the old code in the audit detail and in the version history,
and refuses to run for anyone below admin.

Connects to: ``app/models.py``, ``app/wgt.py``, ``app/audit.py``.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, request
from sqlalchemy import func

from app import audit, reference, wgt
from app.api.assets import add_version, bump_version
from app.errors import ApiError, Conflict, NotFound, ok
from app.extensions import db
from app.models import ActivityLog, Asset, AssetFile, UpdateRequest, utcnow
from app.security import admin_required, current_user_or_none
from app.validators import Validator

log = logging.getLogger(__name__)

bp = Blueprint("admin", __name__, url_prefix="/api/admin")


@bp.route("/activity", methods=["GET"])
@admin_required
def activity():
    """Paginated audit trail, newest first."""
    query = ActivityLog.query.order_by(ActivityLog.ts.desc(), ActivityLog.id.desc())

    action = (request.args.get("action") or "").strip()
    if action:
        query = query.filter(ActivityLog.action == action)

    object_id = request.args.get("object_id", type=int)
    if object_id:
        query = query.filter(ActivityLog.object_id == object_id)

    actor_email = (request.args.get("actor") or "").strip().lower()
    if actor_email:
        query = query.filter(ActivityLog.actor_email == actor_email)

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = min(
        max(1, request.args.get(
            "per_page", current_app.config.get("ACTIVITY_LOG_PAGE_SIZE", 50), type=int)),
        200,
    )

    total = query.order_by(None).count()
    rows = query.limit(per_page).offset((page - 1) * per_page).all()
    return ok({
        "items": [row.to_dict() for row in rows],
        "total": total,
        "page": page,
        "perPage": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 0,
        "actions": sorted({a for (a,) in db.session.query(ActivityLog.action).distinct()}),
    })


@bp.route("/stats", methods=["GET"])
@admin_required
def stats():
    counts = {status: 0 for status in reference.STATUSES}
    rows = (db.session.query(Asset.status, func.count(Asset.id))
            .group_by(Asset.status).all())
    for status, count in rows:
        counts[status] = int(count)

    import datetime as dt
    due = (db.session.query(func.count(Asset.id))
           .filter(Asset.next_review_date.isnot(None),
                   Asset.next_review_date <= dt.date.today(),
                   Asset.status == reference.STATUS_APPROVED)
           .scalar())
    unowned = (db.session.query(func.count(Asset.id))
               .filter(Asset.status == reference.STATUS_APPROVED,
                       func.coalesce(Asset.owner_name, "") == "")
               .scalar())
    open_updates = (db.session.query(func.count(UpdateRequest.id))
                    .filter(UpdateRequest.status == reference.UPDATE_STATUS_OPEN)
                    .scalar())
    return ok({
        "statusCounts": counts,
        "totalAssets": sum(counts.values()),
        "reviewDue": int(due or 0),
        "missingOwner": int(unowned or 0),
        "openUpdateRequests": int(open_updates or 0),
        "totalFiles": int(db.session.query(func.count(AssetFile.id))
                          .filter(AssetFile.asset_id.isnot(None)).scalar() or 0),
    })


# ---------------------------------------------------------------------------
# controlled department migration
# ---------------------------------------------------------------------------
@bp.route("/assets/<int:asset_id>/migrate-department", methods=["POST"])
@admin_required
def migrate_department(asset_id: int):
    """Move an asset to a different department, minting a new WGT code.

    The old code is preserved in the audit record and in the version summary,
    because references to it may exist outside this system. This is deliberately
    an admin-only, explicitly-named endpoint rather than a side effect of PUT.
    """
    asset = db.session.get(Asset, asset_id)
    if asset is None:
        raise NotFound("That asset could not be found.")

    body = request.get_json(silent=True) or {}
    v = Validator(body)
    target = v.choice("department", "Department",
                      reference.get_reference("departments"), required=True)
    reason = v.string("reason", "Reason", required=True, max_length=2000)
    v.raise_if_invalid()

    if target == asset.department:
        raise Conflict("That asset is already in {0}.".format(target),
                       code="NO_CHANGE")

    actor = current_user_or_none()
    previous_department = asset.department
    previous_code = asset.wgt_code

    def _apply(new_code: str) -> Asset:
        asset.department = target
        asset.wgt_code = new_code
        asset.last_updated_at = utcnow()
        add_version(
            asset, bump_version(asset.current_version),
            "Department migrated from {0} to {1} by an administrator. "
            "Previous reference {2}. Reason: {3}".format(
                previous_department, target, previous_code, reason),
            getattr(actor, "full_name", None) or "Admin", actor, make_current=True,
        )
        audit.record(audit.ASSET_DEPARTMENT_MIGRATE, asset, {
            "fromDepartment": previous_department, "toDepartment": target,
            "fromCode": previous_code, "toCode": new_code, "reason": reason,
        }, actor=actor)
        return asset

    try:
        wgt.allocate_with_retry(target, _apply)
    except ApiError:
        raise
    log.warning("Asset %s migrated %s -> %s (now %s) from %s",
                previous_code, previous_department, target, asset.wgt_code,
                request.remote_addr)
    return ok({"asset": asset.to_dict(include_detail=True, viewer=actor),
               "previousCode": previous_code})
