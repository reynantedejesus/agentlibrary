"""Administrative endpoints.

    GET   /api/admin/activity            audit trail (reviewer+)
    GET   /api/admin/stats               dashboard counters (reviewer+)
    GET   /api/admin/users               list accounts (admin)
    POST  /api/admin/users               create an account (admin)
    PATCH /api/admin/users/<id>          role / active / password reset (admin)
    POST  /api/assets/<id>/migrate-department   controlled, audited (admin)

The department migration endpoint is the single sanctioned way to change an
asset's department after a WGT code exists. It mints a new code for the target
department, keeps the old code in the audit detail and in the version history,
and refuses to run for anyone below admin.

Connects to: ``app/models.py``, ``app/wgt.py``, ``app/audit.py``.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, request
from flask_login import current_user
from sqlalchemy import func

from app import audit, reference, wgt
from app.api.assets import add_version, bump_version
from app.errors import ApiError, Conflict, NotFound, ValidationError, ok
from app.extensions import db
from app.models import ActivityLog, Asset, AssetFile, User, utcnow
from app.security import admin_required, reviewer_required
from app.validators import Validator, validate_new_password

log = logging.getLogger(__name__)

bp = Blueprint("admin", __name__, url_prefix="/api/admin")


@bp.route("/activity", methods=["GET"])
@reviewer_required
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
@reviewer_required
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
    return ok({
        "statusCounts": counts,
        "totalAssets": sum(counts.values()),
        "reviewDue": int(due or 0),
        "missingOwner": int(unowned or 0),
        "totalFiles": int(db.session.query(func.count(AssetFile.id)).scalar() or 0),
        "totalUsers": int(db.session.query(func.count(User.id)).scalar() or 0),
    })


# ---------------------------------------------------------------------------
# user administration
# ---------------------------------------------------------------------------
@bp.route("/users", methods=["GET"])
@admin_required
def list_users():
    rows = User.query.order_by(User.email.asc()).limit(500).all()
    return ok({"users": [u.to_dict() for u in rows], "roles": reference.ROLES})


@bp.route("/users", methods=["POST"])
@admin_required
def create_user():
    body = request.get_json(silent=True) or {}
    v = Validator(body)
    email = v.email("email", "Email", required=True)
    full_name = v.string("fullName", "Full name", required=True, max_length=160)
    role = v.choice("role", "Role", reference.ROLES, required=True)
    v.raise_if_invalid()

    if User.query.filter_by(email=email).first():
        raise Conflict("An account with that email already exists.",
                       code="USER_EXISTS", fields={"email": "Already registered."})

    password = validate_new_password(body, "password")
    user = User(email=email, full_name=full_name, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    audit.record(audit.USER_CREATE, user, {"role": role})
    db.session.commit()
    log.info("User %s created by %s", user.email, current_user.email)
    return ok({"user": user.to_dict()}, 201)


@bp.route("/users/<int:user_id>", methods=["PATCH"])
@admin_required
def update_user(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        raise NotFound("No such user.")
    body = request.get_json(silent=True) or {}
    actor = current_user._get_current_object()
    detail = {}

    if "role" in body:
        v = Validator(body)
        role = v.choice("role", "Role", reference.ROLES, required=True)
        v.raise_if_invalid()
        if user.id == actor.id and role != reference.ROLE_ADMIN:
            # Prevent an admin locking the last door behind themselves.
            raise Conflict("You cannot remove your own administrator role.",
                           code="SELF_DEMOTION")
        detail["role"] = {"from": user.role, "to": role}
        user.role = role

    if "isActive" in body:
        wanted = bool(body.get("isActive"))
        if user.id == actor.id and not wanted:
            raise Conflict("You cannot deactivate your own account.",
                           code="SELF_DEACTIVATION")
        detail["isActive"] = wanted
        user.is_active_flag = wanted

    if "password" in body:
        user.set_password(validate_new_password(body, "password"))
        user.failed_login_count = 0
        user.locked_until = None
        detail["passwordReset"] = True
        audit.record(audit.PASSWORD_CHANGE, user, {"by_admin": True})

    if "unlock" in body and body.get("unlock"):
        user.failed_login_count = 0
        user.locked_until = None
        detail["unlocked"] = True

    if not detail:
        raise ValidationError("Nothing to update.", {"_": "Supply a field to change."})

    audit.record(audit.USER_UPDATE, user, detail)
    db.session.commit()
    return ok({"user": user.to_dict()})


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

    actor = current_user._get_current_object()
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
            actor.full_name or "Admin", actor, make_current=True,
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
    log.warning("Asset %s migrated %s -> %s (now %s) by %s",
                previous_code, previous_department, target, asset.wgt_code, actor.email)
    return ok({"asset": asset.to_dict(include_detail=True, viewer=actor),
               "previousCode": previous_code})
