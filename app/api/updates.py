"""Update requests — flagging a change to something already published.

    GET   /api/update-requests             admin: the review queue
    POST  /api/update-requests             open: raise one
    GET   /api/update-requests/<id>        admin: full proposed content
    POST  /api/update-requests/<id>/accept  admin: apply it to the asset
    POST  /api/update-requests/<id>/decline admin: close it, change nothing

Why this exists
---------------
The Library is read-only to everyone but the administrator, yet the people who
actually maintain these tools are the ones who notice when a prompt or a link
has moved on. An update request lets anyone say "this is out of date, here is
the new version" without touching the live record. Nothing changes until an
administrator reads the proposal and accepts it.

Accepting is a single transaction: the proposed values are written onto the
asset, a version entry is appended describing which fields changed, any
proposed files are moved onto the asset, and the request is closed. If any
part fails the whole thing rolls back and the request stays Open.

Connects to: ``app/models.py`` (``UpdateRequest``), ``app/api/assets.py``
(``add_version``, ``build_configuration``, ``claim_staged_files``),
``app/reference.py`` (which fields each asset type offers).
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, request

from app import audit, reference
from app.api.assets import (add_version, bump_version, claim_staged_files,
                            configuration_instructions)
from app.errors import Conflict, NotFound, ValidationError, ok
from app.extensions import db, limiter
from app.models import Asset, UpdateRequest, utcnow
from app.security import admin_required, current_user_or_none
from app.validators import Validator

log = logging.getLogger(__name__)

bp = Blueprint("updates", __name__, url_prefix="/api/update-requests")

# Field ids whose proposed value is a file list rather than text.
FILE_FIELDS = {"knowledgeBase": "knowledgeFiles", "context": "contextFiles"}


def _json_body() -> dict:
    body = request.get_json(silent=True)
    if body is None and request.form:
        body = request.form.to_dict()
    return body if isinstance(body, dict) else {}


def _rate_limit(view):
    if limiter is None:
        return view
    return limiter.limit(
        lambda: current_app.config.get("RATELIMIT_WRITE", "60 per minute"),
        exempt_when=lambda: not current_app.config.get("RATELIMIT_ENABLED", True),
        methods=["POST"],
    )(view)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def validate_update_request(body, asset):
    """Server-side port of the prototype's validateUpdateRequest().

    Every ticked field must be one this asset type actually offers, and must
    carry a value — a request that changes nothing wastes a reviewer's time.
    """
    v = Validator(body)
    allowed = reference.update_request_field_ids(asset.asset_type)
    options = {o["id"]: o for o in reference.update_request_field_options(asset.asset_type)}

    raw_fields = body.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        v.fail("fields", "Select at least one thing that changed.")
        raw_fields = []

    fields = []
    for field_id in raw_fields:
        if not isinstance(field_id, str) or field_id not in allowed:
            v.fail("fields", "That field can't be changed on this kind of tool.")
            continue
        if field_id not in fields:
            fields.append(field_id)

    raw_proposed = body.get("proposed")
    if raw_proposed is None:
        raw_proposed = {}
    if not isinstance(raw_proposed, dict):
        v.fail("proposed", "Expected the proposed values as an object.")
        raw_proposed = {}

    proposed = {}
    for field_id in fields:
        meta = options.get(field_id, {})
        kind = meta.get("kind", "text")
        label = reference.update_field_label(field_id)

        if kind == "owner":
            name = str(raw_proposed.get("ownerName") or "").strip()[:160]
            email = str(raw_proposed.get("ownerEmail") or "").strip()[:254]
            if not name and not email:
                v.fail("proposed",
                       'Provide the new owner name or email, or unselect "{0}".'
                       .format(label))
            if email and not _looks_like_email(email):
                v.fail("proposed", "The new owner email doesn't look like a valid address.")
            proposed["ownerName"] = name
            proposed["ownerEmail"] = email.lower()
            continue

        if kind == "files":
            # Files arrive as staged upload ids, resolved by the caller.
            continue

        value = raw_proposed.get(field_id)
        value = "" if value is None else str(value)
        value = value.replace("\x00", "").strip()
        if not value:
            v.fail("proposed", 'Provide the new value for "{0}", or unselect it.'.format(label))
            continue

        if kind == "select":
            if value not in (meta.get("options") or []):
                v.fail("proposed", "{0} isn't one of the available options.".format(label))
        if field_id == "link":
            from urllib.parse import urlparse
            parsed = urlparse(value)
            if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                v.fail("proposed", "The new link must be a full http:// or https:// address.")
        limit = 200000 if field_id == "instructions" else 4000
        if len(value) > limit:
            v.fail("proposed", "{0} is too long.".format(label))
            value = value[:limit]
        proposed[field_id] = value

    v.string("notes", "Notes", required=False, max_length=2000)
    v.string("requesterName", "Your full name", required=True, max_length=160)
    v.email("requesterEmail", "Your email", required=True)
    v.raise_if_invalid()

    clean = v.clean
    clean["fields"] = fields
    clean["proposed"] = proposed
    return clean


def _looks_like_email(value: str) -> bool:
    import re
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", value))


# ---------------------------------------------------------------------------
# create (open to anyone)
# ---------------------------------------------------------------------------
@bp.route("", methods=["POST"])
@bp.route("/", methods=["POST"])
@_rate_limit
def create_update_request():
    body = _json_body()

    try:
        asset_id = int(body.get("assetId"))
    except (TypeError, ValueError):
        raise ValidationError("Choose which tool needs updating.",
                              {"assetId": "Pick a tool from the list."})

    asset = db.session.get(Asset, asset_id)
    # Only published assets can be the subject of a request, and only those are
    # visible to an anonymous caller — so an unknown id and a hidden one give
    # the same answer.
    if asset is None or asset.status != reference.STATUS_APPROVED:
        raise NotFound("That tool could not be found in the Library.")

    data = validate_update_request(body, asset)

    record = UpdateRequest(
        asset_id=asset.id,
        wgt_code=asset.wgt_code,
        asset_name=asset.name,
        fields=data["fields"],
        proposed=data["proposed"],
        notes=data.get("notes") or None,
        requester_name=data["requesterName"],
        requester_email=data["requesterEmail"],
        status=reference.UPDATE_STATUS_OPEN,
        submitted_ip=(request.remote_addr or "")[:45] or None,
    )
    db.session.add(record)
    db.session.flush()

    # Bind any proposed files to this request rather than to the asset.
    staged = claim_staged_files(
        _file_id_payload(body, data["fields"]), asset.asset_type,
        update_request=record)
    for stored in staged:
        stored.asset_id = None
        stored.update_request_id = record.id

    audit.record(audit.UPDATE_REQUEST_SUBMIT, record, {
        "assetId": asset.id, "wgtCode": asset.wgt_code,
        "fields": data["fields"], "files": len(staged),
        "requestedBy": data["requesterName"],
        "requestedEmail": data["requesterEmail"],
    })
    db.session.commit()

    log.info("Update request %s raised for %s by %s <%s> from %s",
             record.id, asset.wgt_code, data["requesterName"],
             data["requesterEmail"], request.remote_addr)
    return ok({"updateRequest": record.to_dict(include_detail=True)}, 201)


# Which submission key carries the ids for each file field.
FIELD_TO_ID_KEY = {"knowledgeBase": "knowledgeFileIds", "context": "contextFileIds"}


def _file_id_payload(body, fields):
    """Extract the file-id lists, refusing ids for fields that were not ticked.

    Passing ids for an unticked field is always a client bug, and quietly
    dropping them would leave the requester believing they attached something
    that never arrived. Say so instead.
    """
    payload = {}
    ticked_keys = {FIELD_TO_ID_KEY[f] for f in fields if f in FIELD_TO_ID_KEY}
    errors = {}
    for field_id, key in FIELD_TO_ID_KEY.items():
        if not body.get(key):
            continue
        if key not in ticked_keys:
            errors[key] = ('Select "{0}" as something that changed before '
                           "attaching files to it.".format(
                               reference.update_field_label(field_id)))
            continue
        payload[key] = body[key]
    if errors:
        raise ValidationError("Some attachments could not be applied.", errors)
    return payload


# ---------------------------------------------------------------------------
# review (admin)
# ---------------------------------------------------------------------------
@bp.route("", methods=["GET"])
@bp.route("/", methods=["GET"])
@admin_required
def list_update_requests():
    query = UpdateRequest.query.order_by(UpdateRequest.created_at.desc(),
                                         UpdateRequest.id.desc())
    status = (request.args.get("status") or reference.UPDATE_STATUS_OPEN).strip()
    if status.lower() != "all":
        if status not in reference.UPDATE_REQUEST_STATUSES:
            raise ValidationError("Unknown status filter.",
                                  {"status": "Unknown status."})
        query = query.filter(UpdateRequest.status == status)

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = min(max(1, request.args.get("per_page", 50, type=int)), 200)

    total = query.order_by(None).count()
    rows = query.limit(per_page).offset((page - 1) * per_page).all()
    return ok({
        "items": [r.to_dict() for r in rows],
        "total": total, "page": page, "perPage": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 0,
        "openCount": UpdateRequest.query.filter_by(
            status=reference.UPDATE_STATUS_OPEN).count(),
    })


@bp.route("/<int:request_id>", methods=["GET"])
@admin_required
def get_update_request(request_id: int):
    record = db.session.get(UpdateRequest, request_id)
    if record is None:
        raise NotFound("That update request could not be found.")
    return ok({"updateRequest": record.to_dict(include_detail=True)})


@bp.route("/<int:request_id>/decline", methods=["POST"])
@admin_required
@_rate_limit
def decline_update_request(request_id: int):
    record = db.session.get(UpdateRequest, request_id)
    if record is None:
        raise NotFound("That update request could not be found.")
    if not record.is_open:
        raise Conflict("That request has already been {0}.".format(record.status.lower()),
                       code="ALREADY_RESOLVED")

    v = Validator(_json_body())
    note = v.string("reason", "Reason", required=False, max_length=2000)
    v.raise_if_invalid()

    record.status = reference.UPDATE_STATUS_DECLINED
    record.resolved_at = utcnow()
    record.resolution_note = note or None
    audit.record(audit.UPDATE_REQUEST_DECLINE, record, {
        "assetId": record.asset_id, "wgtCode": record.wgt_code,
        "fields": list(record.fields or []), "reason": note or "",
    })
    db.session.commit()
    log.info("Update request %s declined from %s", record.id, request.remote_addr)
    return ok({"updateRequest": record.to_dict(include_detail=True)})


@bp.route("/<int:request_id>/accept", methods=["POST"])
@admin_required
@_rate_limit
def accept_update_request(request_id: int):
    """Apply the proposal to the asset, in one transaction."""
    record = db.session.get(UpdateRequest, request_id)
    if record is None:
        raise NotFound("That update request could not be found.")
    if not record.is_open:
        raise Conflict("That request has already been {0}.".format(record.status.lower()),
                       code="ALREADY_RESOLVED")

    asset = db.session.get(Asset, record.asset_id)
    if asset is None:
        raise Conflict(
            "The asset this request refers to no longer exists, so there is "
            "nothing to apply it to.", code="ASSET_GONE")

    actor = current_user_or_none()
    applied = []
    for field_id in (record.fields or []):
        if apply_update_field(asset, field_id, record):
            applied.append(field_id)

    asset.last_updated_at = utcnow()
    asset.next_review_date = asset.compute_next_review()

    labels = ", ".join(reference.update_field_label(f) for f in applied) or "no fields"
    add_version(
        asset, bump_version(asset.current_version),
        "Updated via an accepted Update Request ({0}). Requested by {1}.".format(
            labels, record.requester_name or "someone"),
        record.requester_name or "Admin", actor, make_current=True,
    )

    record.status = reference.UPDATE_STATUS_ACCEPTED
    record.resolved_at = utcnow()
    audit.record(audit.UPDATE_REQUEST_ACCEPT, record, {
        "assetId": asset.id, "wgtCode": asset.wgt_code,
        "fields": applied, "version": asset.current_version,
    })
    db.session.commit()

    log.info("Update request %s accepted for %s (%s) from %s",
             record.id, asset.wgt_code, labels, request.remote_addr)
    return ok({
        "updateRequest": record.to_dict(include_detail=True),
        "asset": asset.to_dict(include_detail=True),
    })


def apply_update_field(asset: Asset, field_id: str, record: UpdateRequest) -> bool:
    """Write one proposed field onto the asset.

    Mirrors the prototype's applyUpdateRequestField(), including keeping the
    type-specific mirrors inside ``configuration`` in step with the top-level
    columns. Returns whether anything was applied.
    """
    proposed = record.proposed or {}
    config = dict(asset.configuration or {})
    asset_type = asset.asset_type

    if field_id == "name":
        asset.name = (proposed.get("name") or "").strip()[:200]
    elif field_id == "description":
        value = proposed.get("description") or ""
        asset.description = value
        asset.use_case = value
        key = {"gpt": "gptDescription", "skill": "skillDescription"}.get(asset_type, "description")
        config[key] = value
    elif field_id == "instructions":
        value = proposed.get("instructions") or ""
        if asset_type == "gpt":
            config["instructions"] = value
        elif asset_type == "skill":
            config["skillMd"] = value
            config["instructions"] = value
        elif asset_type == "agent":
            config["systemPrompt"] = value
        else:
            config["instructions"] = value
    elif field_id == "link":
        value = proposed.get("link") or ""
        asset.direct_url = value
        key = {"gpt": "directLink", "skill": "installLink",
               "agent": "directAccessUrl"}.get(asset_type, "url")
        config[key] = value
    elif field_id == "tags":
        raw = proposed.get("tags") or ""
        names = [t.strip() for t in raw.split(",") if t.strip()]
        limit = current_app.config.get("MAX_TAGS_PER_ASSET", 4)
        asset.set_tags(names[:limit])
    elif field_id == "owner":
        if (proposed.get("ownerName") or "").strip():
            asset.owner_name = proposed["ownerName"].strip()[:160]
        if (proposed.get("ownerEmail") or "").strip():
            asset.owner_email = proposed["ownerEmail"].strip()[:254]
    elif field_id == "preferredModel":
        config["recommendedModel"] = proposed.get("preferredModel") or ""
    elif field_id == "otherPlatform":
        config["platform"] = proposed.get("otherPlatform") or ""
    elif field_id in FILE_FIELDS:
        # Proposed files replace the asset's current set for that kind, exactly
        # as the prototype's applyUpdateRequestField() replaced the array.
        kind = "knowledge" if field_id == "knowledgeBase" else "context"
        incoming = [f for f in record.files if f.kind == kind]
        if not incoming:
            return False
        for existing in list(asset.files):
            if existing.kind == kind:
                db.session.delete(existing)
        for stored in incoming:
            stored.asset_id = asset.id
            stored.update_request_id = None
        asset.configuration = config
        return True
    else:
        return False

    asset.configuration = config
    return True


def open_request_count() -> int:
    """Used by the admin dashboard and the sidebar pill."""
    return UpdateRequest.query.filter_by(status=reference.UPDATE_STATUS_OPEN).count()


# Referenced by app/api/assets.py's version snapshotting.
__all__ = ["bp", "open_request_count", "apply_update_field",
           "validate_update_request", "configuration_instructions"]
