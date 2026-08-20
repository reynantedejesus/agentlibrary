"""Asset endpoints — the core of the registry.

    GET    /api/assets                       search / filter / sort / paginate
    GET    /api/assets/<id>                  full detail (all five tabs)
    POST   /api/assets                       submit (Add to Library wizard)
    PUT    /api/assets/<id>                  edit
    POST   /api/assets/<id>/versions         add a version
    POST   /api/assets/<id>/approve          reviewer/admin
    POST   /api/assets/<id>/reject           reviewer/admin
    POST   /api/assets/<id>/archive          reviewer/admin ("Remove from Library")
    GET    /api/assets/next-code             WGT preview for the wizard
    GET    /api/assets/duplicate-check        near-duplicate warning
    GET    /api/my-submissions               the signed-in user's submissions

Invariants enforced here, not in the browser
--------------------------------------------
* ``wgt_code`` is allocated by ``app/wgt.py`` inside the insert transaction and
  is never writable afterwards.
* ``department`` is immutable once a code exists; changing it requires the
  explicit, audited admin migration endpoint in ``app/api/admin.py``.
* Every submission starts at ``Pending Review``; only a reviewer/admin can move
  it out of that state.
* Version history is append-only — approve/reject/edit each *add* a row.
  (The prototype overwrote ``versions[0]``, destroying history.)
* Every state change writes an ``ActivityLog`` row in the same transaction.

Connects to: ``app/models.py``, ``app/wgt.py``, ``app/validators.py``,
``app/security.py``, ``app/audit.py``. Consumed by ``static/js/app.js``.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from flask import Blueprint, current_app, request
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app import audit, reference, wgt
from app.errors import ApiError, Conflict, NotFound, ValidationError, ok
from app.extensions import db, limiter
from app.models import Asset, AssetTag, AssetVersion, Tag, User, utcnow
from app.security import (assert_can_edit_asset, assert_can_view_asset,
                          current_user_or_none, login_required_json,
                          require_browse, reviewer_required)
from app.validators import (Validator, validate_asset_submission,
                            validate_version_payload)

log = logging.getLogger(__name__)

bp = Blueprint("assets", __name__)

SORT_COLUMNS = {
    "created_at": Asset.created_at,
    "last_updated_at": Asset.last_updated_at,
    "name": Asset.name,
    "wgt_code": Asset.wgt_code,
    "department": Asset.department,
    "status": Asset.status,
    "next_review_date": Asset.next_review_date,
}
DEFAULT_SORT = "-last_updated_at"


def _json_body() -> Dict[str, Any]:
    body = request.get_json(silent=True)
    if body is None and request.form:
        body = request.form.to_dict()
    return body if isinstance(body, dict) else {}


def _rate_limit_write(view):
    if limiter is None:
        return view
    return limiter.limit(
        lambda: current_app.config.get("RATELIMIT_WRITE", "60 per minute"),
        exempt_when=lambda: not current_app.config.get("RATELIMIT_ENABLED", True),
        methods=["POST", "PUT", "PATCH", "DELETE"],
    )(view)


# ---------------------------------------------------------------------------
# configuration mapping (server-side port of applyWizardTextToConfig)
# ---------------------------------------------------------------------------
def build_configuration(asset_type: str, data: Dict[str, Any],
                        existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Map the wizard's flat fields into the type-specific JSON shape.

    Only known keys are copied, so a crafted request cannot smuggle arbitrary
    structures into the JSON column.
    """
    config: Dict[str, Any] = dict(existing or {})
    instructions = data.get("instructions", "")
    description = data.get("description", "")
    link = data.get("link", "")

    if asset_type == "gpt":
        config.update({
            "gptName": data.get("name", "")[:200],
            "gptDescription": description,
            "instructions": instructions,
            "knowledgeBase": data.get("knowledgeBase", ""),
            "recommendedModel": data.get("preferredModel")
            or reference.RECOMMENDED_MODELS[0],
            "directLink": link,
        })
        config.setdefault("conversationStarters", [])
        config.setdefault("capabilities", [])
        config.setdefault("externalIntegration", "No")
        config.setdefault("integrations", [])
        config.setdefault("knowledgeFiles", [])
    elif asset_type == "skill":
        config.update({
            "skillName": data.get("name", "")[:200],
            "skillDescription": description,
            "skillMd": instructions,
            "instructions": instructions,
            "installLink": link,
        })
        config.setdefault("dependencies", [])
        config.setdefault("resources", [])
        config.setdefault("scripts", [])
        config.setdefault("whenToUse", "")
        config.setdefault("exampleTriggers", [])
        config.setdefault("supportedSurfaces", [])
    elif asset_type == "agent":
        config.update({
            "agentName": data.get("name", "")[:200],
            "description": description,
            "systemPrompt": instructions,
            "directAccessUrl": link,
        })
        config.setdefault("platformRuntime", "")
        config.setdefault("triggerType", "User initiated")
        config.setdefault("inputs", [])
        config.setdefault("outputs", [])
        config.setdefault("tools", [])
        config.setdefault("knowledgeSources", "")
        config.setdefault("dependencies", "")
        config.setdefault("humanApprovalRequired", False)
        config.setdefault("approvalLocation", "")
    else:
        config.update({
            "name": data.get("name", "")[:200],
            "description": description,
            "instructions": instructions,
            "platform": data.get("otherPlatform", ""),
            "url": link,
        })
        config.setdefault("input", "")
        config.setdefault("output", "")
        config.setdefault("dependencies", "")
        config.setdefault("notes", "")
    return config


def configuration_instructions(config: Dict[str, Any]) -> str:
    for key in ("instructions", "systemPrompt", "skillMd"):
        value = (config or {}).get(key)
        if value:
            return value
    return ""


def bump_version(current: str) -> str:
    """The prototype's +0.1 rule, made robust against odd inputs."""
    try:
        return "{0:.1f}".format(float(current) + 0.1)
    except (TypeError, ValueError):
        return "1.1"


def add_version(asset: Asset, version_number: str, summary: str,
                updated_by_name: str, actor: Optional[User] = None,
                make_current: bool = True) -> AssetVersion:
    """Append a version row. Never overwrites an existing one."""
    if make_current:
        for existing in asset.versions:
            existing.is_current = False
    number = (version_number or asset.current_version or "1.0")[:16]
    # Version numbers are unique per asset; suffix rather than clash.
    taken = {v.version_number for v in asset.versions}
    if number in taken:
        suffix = 1
        while "{0}-{1}".format(number, suffix) in taken:
            suffix += 1
        number = "{0}-{1}".format(number, suffix)[:16]
    row = AssetVersion(
        asset=asset,
        version_number=number,
        summary=summary or "",
        updated_by_id=getattr(actor, "id", None),
        updated_by_name=(updated_by_name or getattr(actor, "full_name", "") or "")[:160],
        is_current=make_current,
        instructions_snapshot=configuration_instructions(asset.configuration),
        configuration_snapshot=asset.configuration or {},
        created_at=utcnow(),
    )
    db.session.add(row)
    if make_current:
        asset.current_version = number
    return row


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------
def _base_query():
    return Asset.query.options(
        joinedload(Asset.asset_tags).joinedload(AssetTag.tag)
    )


def _visible_status_filter(query, requested_status: Optional[str]):
    """Apply the governance rule: the catalogue shows Approved assets only.

    Anything else is restricted to reviewers/admins, or to the asset's own
    creator/owner via /api/my-submissions. This is the IDOR guard for the list
    endpoint — a plain user cannot page through pending submissions by adding
    ``?status=Pending Review``.
    """
    viewer = current_user_or_none()
    is_reviewer = bool(viewer and viewer.has_role(reference.ROLE_REVIEWER))

    if not is_reviewer:
        # A non-reviewer never sees anything but the published catalogue, no
        # matter what ?status= they send.
        return query.filter(Asset.status == reference.STATUS_APPROVED)

    if not requested_status:
        # Default to the catalogue even for reviewers, so the Library view and
        # the Admin view cannot be confused by whoever happens to be signed in.
        return query.filter(Asset.status == reference.STATUS_APPROVED)
    if requested_status.lower() == "all":
        return query
    if requested_status not in reference.STATUSES:
        raise ValidationError("Unknown status filter.",
                              {"status": "Unknown status."})
    return query.filter(Asset.status == requested_status)


def _apply_search(query, term: str):
    """Full-text-ish search across the indexed columns plus tags.

    Uses SQLAlchemy's parameter binding throughout — the term never reaches a
    SQL string, so this cannot be injected. ``%`` and ``_`` are escaped so a
    user typing them searches literally instead of widening the match.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = "%{0}%".format(escaped)
    tag_subquery = (db.session.query(AssetTag.asset_id)
                    .join(Tag, Tag.id == AssetTag.tag_id)
                    .filter(Tag.name.like(pattern, escape="\\")))
    return query.filter(or_(
        Asset.wgt_code.like(pattern, escape="\\"),
        Asset.name.like(pattern, escape="\\"),
        Asset.description.like(pattern, escape="\\"),
        Asset.use_case.like(pattern, escape="\\"),
        Asset.department.like(pattern, escape="\\"),
        Asset.owner_name.like(pattern, escape="\\"),
        Asset.creator_name.like(pattern, escape="\\"),
        Asset.id.in_(tag_subquery),
    ))


def _apply_sort(query, raw_sort: str):
    descending = raw_sort.startswith("-")
    key = raw_sort[1:] if descending else raw_sort
    column = SORT_COLUMNS.get(key)
    if column is None:
        column = SORT_COLUMNS["last_updated_at"]
        descending = True
    return query.order_by(column.desc() if descending else column.asc(), Asset.id.desc())


def _pagination_args():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    default_size = current_app.config.get("DEFAULT_PAGE_SIZE", 48)
    maximum = current_app.config.get("MAX_PAGE_SIZE", 200)
    try:
        per_page = int(request.args.get("per_page", default_size))
    except (TypeError, ValueError):
        per_page = default_size
    per_page = max(1, min(per_page, maximum))
    return page, per_page


@bp.route("/api/assets", methods=["GET"])
def list_assets():
    require_browse()
    query = _base_query()
    query = _visible_status_filter(query, (request.args.get("status") or "").strip())

    asset_type = (request.args.get("asset_type") or request.args.get("type") or "").strip()
    if asset_type:
        if asset_type not in reference.ASSET_TYPE_IDS:
            raise ValidationError("Unknown asset type.", {"asset_type": "Unknown asset type."})
        query = query.filter(Asset.asset_type == asset_type)

    department = (request.args.get("department") or "").strip()
    if department:
        query = query.filter(Asset.department == department)

    platform = (request.args.get("platform") or "").strip()
    if platform:
        query = query.filter(Asset.platform == platform)

    tag = (request.args.get("tag") or "").strip()
    if tag:
        escaped = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        tag_ids = (db.session.query(AssetTag.asset_id)
                   .join(Tag, Tag.id == AssetTag.tag_id)
                   .filter(Tag.name.like("%{0}%".format(escaped), escape="\\")))
        query = query.filter(Asset.id.in_(tag_ids))

    if (request.args.get("featured") or "").strip().lower() in ("1", "true", "yes"):
        query = query.filter(Asset.featured.is_(True))

    owner_email = (request.args.get("owner_email") or "").strip().lower()
    if owner_email:
        query = query.filter(Asset.owner_email == owner_email)

    term = (request.args.get("q") or "").strip()[:200]
    if term:
        query = _apply_search(query, term)

    review_due = (request.args.get("review_due") or "").strip().lower()
    if review_due in ("1", "true", "yes"):
        query = query.filter(Asset.next_review_date.isnot(None),
                             Asset.next_review_date <= dt.date.today())

    query = _apply_sort(query, (request.args.get("sort") or DEFAULT_SORT).strip())

    page, per_page = _pagination_args()
    total = query.order_by(None).count()
    rows = query.limit(per_page).offset((page - 1) * per_page).all()

    return ok({
        "items": [a.to_dict() for a in rows],
        "total": total,
        "page": page,
        "perPage": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 0,
        "query": term,
    })


@bp.route("/api/assets/<int:asset_id>", methods=["GET"])
def get_asset(asset_id: int):
    asset = db.session.get(Asset, asset_id)
    if asset is None:
        raise NotFound("That asset could not be found.")
    assert_can_view_asset(asset)
    return ok({"asset": asset.to_dict(include_detail=True,
                                      viewer=current_user_or_none())})


@bp.route("/api/assets/next-code", methods=["GET"])
@login_required_json
def next_code():
    """Preview only — the authoritative code is allocated at insert time."""
    department = (request.args.get("department") or "").strip()
    departments = reference.get_reference("departments")
    if department not in departments:
        raise ValidationError("Choose a department.", {"department": "Unknown department."})
    return ok({"department": department, "preview": wgt.preview_next_code(department),
               "authoritative": False})


@bp.route("/api/assets/duplicate-check", methods=["GET"])
@login_required_json
def duplicate_check():
    """Server-side port of the prototype's findPossibleDuplicates().

    Same heuristic, but run against the real corpus rather than one browser's
    cache, and it never reveals assets the caller could not otherwise see.
    """
    department = (request.args.get("department") or "").strip()
    name = (request.args.get("name") or "").strip()[:200]
    exclude_id = request.args.get("exclude_id", type=int)
    if not department or not name:
        return ok({"duplicates": []})

    normalised = _normalise_name(name)
    if not normalised:
        return ok({"duplicates": []})
    words = set(normalised.split())

    candidates = (Asset.query
                  .filter(Asset.department == department,
                          Asset.status != reference.STATUS_REJECTED)
                  .limit(500).all())
    matches: List[Asset] = []
    for candidate in candidates:
        if exclude_id and candidate.id == exclude_id:
            continue
        other = _normalise_name(candidate.name)
        if not other:
            continue
        if other == normalised or other in normalised or normalised in other:
            matches.append(candidate)
            continue
        other_words = set(other.split())
        overlap = len(words & other_words)
        if overlap / max(len(words), len(other_words), 1) >= 0.6:
            matches.append(candidate)

    viewer = current_user_or_none()
    visible = []
    for match in matches[:8]:
        if match.status == reference.STATUS_APPROVED or (
                viewer and (viewer.has_role(reference.ROLE_REVIEWER)
                            or match.creator_id == viewer.id)):
            visible.append({"id": match.id, "wgtCode": match.wgt_code,
                            "name": match.name, "status": match.status})
        else:
            visible.append({"id": None, "wgtCode": match.wgt_code,
                            "name": match.name, "status": match.status})
    return ok({"duplicates": visible})


def _normalise_name(name: str) -> str:
    import re
    value = re.sub(r"^wgt\d{3}\s*", "", (name or "").lower())
    value = re.sub(r"[^a-z0-9 ]", "", value)
    return re.sub(r"\s+", " ", value).strip()


# ---------------------------------------------------------------------------
# create / update
# ---------------------------------------------------------------------------
@bp.route("/api/assets", methods=["POST"])
@login_required_json
@_rate_limit_write
def create_asset():
    data = validate_asset_submission(_json_body(), editing=False)
    actor = current_user._get_current_object()
    department = data["department"]
    now = utcnow()

    def _insert(code: str) -> Asset:
        asset = Asset(
            wgt_code=code,
            name=data["name"],
            asset_type=data["type"],
            platform=reference.platform_for_type(data["type"]),
            department=department,
            status=reference.STATUS_PENDING,
            description=data["description"],
            use_case=data.get("useCase") or data["description"],
            problem_solved=data.get("problemSolved"),
            input_requirements=data.get("inputRequirements"),
            expected_output=data.get("expectedOutput"),
            example_use=data.get("exampleUse"),
            owner_id=actor.id,
            creator_id=actor.id,
            owner_name=data["ownerName"],
            owner_email=data["ownerEmail"],
            creator_name=data["ownerName"],
            submitted_by_name=actor.full_name or data["ownerName"],
            intended_audience=data.get("intendedAudience") or "Company-wide",
            access_level=data.get("intendedAudience") or "Company-wide",
            sensitivity=data.get("sensitivity") or "Standard Internal",
            review_frequency=data.get("reviewFrequency") or "Every 6 months",
            current_version="1.0",
            direct_url=data["link"],
            featured=False,          # only a reviewer may feature an asset
            configuration=build_configuration(data["type"], data),
            additional_departments=[],
            created_at=now,
            last_updated_at=now,
            submitted_at=now,
            last_verified_at=now,
        )
        asset.set_tags(data.get("tags") or [])
        asset.next_review_date = asset.compute_next_review()
        db.session.add(asset)
        db.session.flush()
        add_version(asset, "1.0", "Initial submission, pending governance review.",
                    data["ownerName"], actor, make_current=True)
        audit.record(audit.ASSET_SUBMIT, asset,
                     {"department": department, "type": data["type"]}, actor=actor)
        return asset

    asset = wgt.allocate_with_retry(department, _insert)
    log.info("Asset %s submitted by %s", asset.wgt_code, actor.email)
    return ok({"asset": asset.to_dict(include_detail=True, viewer=actor)}, 201)


@bp.route("/api/assets/<int:asset_id>", methods=["PUT", "PATCH"])
@login_required_json
@_rate_limit_write
def update_asset(asset_id: int):
    asset = db.session.get(Asset, asset_id)
    if asset is None:
        raise NotFound("That asset could not be found.")
    assert_can_view_asset(asset)
    assert_can_edit_asset(asset)

    body = _json_body()
    data = validate_asset_submission(body, editing=True)
    actor = current_user._get_current_object()

    # Immutable fields. Silently ignoring an attempt would hide a real client
    # bug, so say so plainly instead.
    if body.get("wgtCode") and body["wgtCode"] != asset.wgt_code:
        raise Conflict("A WGT code cannot be changed once assigned.",
                       code="IMMUTABLE_FIELD", fields={"wgtCode": "This code is fixed."})
    requested_department = (body.get("department") or "").strip()
    if requested_department and requested_department != asset.department:
        raise Conflict(
            "Department is fixed once a WGT code is assigned. An administrator "
            "can perform a controlled department migration.",
            code="DEPARTMENT_IMMUTABLE",
            fields={"department": "This asset's department cannot be changed here."},
        )
    if body.get("type") and body["type"] != asset.asset_type:
        raise Conflict("An asset's type cannot be changed after submission.",
                       code="IMMUTABLE_FIELD", fields={"type": "Type is fixed."})

    is_reviewer = actor.has_role(reference.ROLE_REVIEWER)
    changed: Dict[str, Any] = {}

    def _apply(field: str, value: Any, attr: str) -> None:
        if getattr(asset, attr) != value:
            changed[field] = True
            setattr(asset, attr, value)

    _apply("name", data["name"], "name")
    _apply("description", data["description"], "description")
    _apply("useCase", data.get("useCase") or data["description"], "use_case")
    _apply("problemSolved", data.get("problemSolved"), "problem_solved")
    _apply("inputRequirements", data.get("inputRequirements"), "input_requirements")
    _apply("expectedOutput", data.get("expectedOutput"), "expected_output")
    _apply("exampleUse", data.get("exampleUse"), "example_use")
    _apply("ownerName", data["ownerName"], "owner_name")
    _apply("ownerEmail", data["ownerEmail"], "owner_email")
    _apply("directUrl", data["link"], "direct_url")
    _apply("sensitivity", data.get("sensitivity") or asset.sensitivity, "sensitivity")
    _apply("intendedAudience",
           data.get("intendedAudience") or asset.intended_audience, "intended_audience")
    _apply("reviewFrequency",
           data.get("reviewFrequency") or asset.review_frequency, "review_frequency")

    if "tags" in body:
        before = sorted(asset.tag_names)
        asset.set_tags(data.get("tags") or [])
        if sorted(asset.tag_names) != before:
            changed["tags"] = True

    # Only a reviewer/admin may promote an asset onto the Featured rail.
    if "featured" in body:
        wanted = bool(data.get("featured"))
        if wanted != bool(asset.featured):
            if not is_reviewer:
                raise ApiError("FORBIDDEN",
                               "Only a reviewer or administrator can feature an asset.",
                               403, {"featured": "Not permitted."})
            asset.featured = wanted
            changed["featured"] = True

    asset.configuration = build_configuration(asset.asset_type, data, asset.configuration)
    changed["configuration"] = True
    asset.last_updated_at = utcnow()
    asset.next_review_date = asset.compute_next_review()

    summary = (body.get("changeSummary") or "").strip()[:2000]
    if not summary:
        summary = ("Edited by {0}.".format("an admin" if is_reviewer else "the owner"))
    add_version(asset, bump_version(asset.current_version), summary,
                actor.full_name or data["ownerName"], actor, make_current=True)

    audit.record(audit.ASSET_UPDATE, asset,
                 {"fields": sorted(changed.keys()), "status": asset.status}, actor=actor)
    db.session.commit()
    log.info("Asset %s updated by %s", asset.wgt_code, actor.email)
    return ok({"asset": asset.to_dict(include_detail=True, viewer=actor)})


@bp.route("/api/assets/<int:asset_id>/versions", methods=["POST"])
@login_required_json
@_rate_limit_write
def create_version(asset_id: int):
    asset = db.session.get(Asset, asset_id)
    if asset is None:
        raise NotFound("That asset could not be found.")
    assert_can_view_asset(asset)
    actor = current_user._get_current_object()
    # Reviewers may version anything; owners only their own asset.
    if not (actor.has_role(reference.ROLE_REVIEWER)
            or asset.owner_id == actor.id or asset.creator_id == actor.id):
        from app.errors import Forbidden
        raise Forbidden("Only the asset's owner or a reviewer can add a version.")

    data = validate_version_payload(_json_body())
    if any(v.version_number == data["versionNumber"] for v in asset.versions):
        raise Conflict("That version number already exists for this asset.",
                       code="VERSION_EXISTS",
                       fields={"versionNumber": "Already used — pick another."})

    version = add_version(asset, data["versionNumber"], data["summary"],
                          data.get("updatedBy") or actor.full_name, actor,
                          make_current=True)
    asset.last_updated_at = utcnow()
    audit.record(audit.VERSION_CREATE, asset,
                 {"version": version.version_number}, actor=actor)
    db.session.commit()
    return ok({"asset": asset.to_dict(include_detail=True, viewer=actor),
               "version": version.to_dict()}, 201)


# ---------------------------------------------------------------------------
# governance actions
# ---------------------------------------------------------------------------
def _load_for_review(asset_id: int) -> Asset:
    asset = db.session.get(Asset, asset_id)
    if asset is None:
        raise NotFound("That asset could not be found.")
    return asset


@bp.route("/api/assets/<int:asset_id>/approve", methods=["POST"])
@reviewer_required
@_rate_limit_write
def approve_asset(asset_id: int):
    asset = _load_for_review(asset_id)
    if asset.status == reference.STATUS_APPROVED:
        raise Conflict("That asset is already approved.", code="ALREADY_APPROVED")
    if asset.status == reference.STATUS_ARCHIVED:
        raise Conflict("Restore the asset from the archive before approving it.",
                       code="INVALID_TRANSITION")

    actor = current_user._get_current_object()
    now = utcnow()
    asset.status = reference.STATUS_APPROVED
    asset.rejection_reason = None
    asset.last_verified_at = now
    asset.last_updated_at = now
    asset.published_at = asset.published_at or now
    asset.next_review_date = asset.compute_next_review()
    add_version(asset, bump_version(asset.current_version),
                "Approved by governance review — published to the Library.",
                actor.full_name or "Reviewer", actor, make_current=True)
    audit.record(audit.ASSET_APPROVE, asset, {"version": asset.current_version}, actor=actor)
    db.session.commit()
    log.info("Asset %s approved by %s", asset.wgt_code, actor.email)
    return ok({"asset": asset.to_dict(include_detail=True, viewer=actor)})


@bp.route("/api/assets/<int:asset_id>/reject", methods=["POST"])
@reviewer_required
@_rate_limit_write
def reject_asset(asset_id: int):
    asset = _load_for_review(asset_id)
    if asset.status == reference.STATUS_REJECTED:
        raise Conflict("That asset has already been rejected.", code="ALREADY_REJECTED")

    validator = Validator(_json_body())
    reason = validator.string("reason", "Reason", required=False, max_length=2000)
    validator.raise_if_invalid()

    actor = current_user._get_current_object()
    asset.status = reference.STATUS_REJECTED
    asset.rejection_reason = reason or None
    asset.last_updated_at = utcnow()
    add_version(asset, bump_version(asset.current_version),
                "Rejected by governance review." + (" " + reason if reason else ""),
                actor.full_name or "Reviewer", actor, make_current=True)
    audit.record(audit.ASSET_REJECT, asset, {"reason": reason or ""}, actor=actor)
    db.session.commit()
    log.info("Asset %s rejected by %s", asset.wgt_code, actor.email)
    return ok({"asset": asset.to_dict(include_detail=True, viewer=actor)})


@bp.route("/api/assets/<int:asset_id>/archive", methods=["POST"])
@reviewer_required
@_rate_limit_write
def archive_asset(asset_id: int):
    """"Remove from Library" — hides the asset but keeps the record and history."""
    asset = _load_for_review(asset_id)
    if asset.status == reference.STATUS_ARCHIVED:
        raise Conflict("That asset is already archived.", code="ALREADY_ARCHIVED")

    validator = Validator(_json_body())
    reason = validator.string("reason", "Reason", required=False, max_length=2000)
    validator.raise_if_invalid()

    actor = current_user._get_current_object()
    previous = asset.status
    asset.status = reference.STATUS_ARCHIVED
    asset.featured = False
    asset.last_updated_at = utcnow()
    add_version(asset, bump_version(asset.current_version),
                "Removed from the Library by an admin." + (" " + reason if reason else ""),
                actor.full_name or "Admin", actor, make_current=True)
    audit.record(audit.ASSET_ARCHIVE, asset,
                 {"from": previous, "reason": reason or ""}, actor=actor)
    db.session.commit()
    log.info("Asset %s archived by %s", asset.wgt_code, actor.email)
    return ok({"asset": asset.to_dict(include_detail=True, viewer=actor)})


# ---------------------------------------------------------------------------
# my submissions
# ---------------------------------------------------------------------------
@bp.route("/api/my-submissions", methods=["GET"])
@login_required_json
def my_submissions():
    """Everything the signed-in user submitted or owns, bucketed by status."""
    actor = current_user._get_current_object()
    query = (_base_query()
             .filter(or_(Asset.creator_id == actor.id, Asset.owner_id == actor.id))
             .order_by(Asset.last_updated_at.desc()))

    term = (request.args.get("q") or "").strip()[:200]
    if term:
        query = _apply_search(query, term)

    page, per_page = _pagination_args()
    total = query.order_by(None).count()
    rows = query.limit(per_page).offset((page - 1) * per_page).all()

    buckets: Dict[str, List[Dict[str, Any]]] = {status: [] for status in reference.STATUSES}
    items = []
    for asset in rows:
        payload = asset.to_dict()
        payload["canEdit"] = asset.viewer_can_edit(actor)
        items.append(payload)
        buckets.setdefault(asset.status, []).append(payload)

    return ok({
        "items": items,
        "buckets": buckets,
        "counts": {status: len(entries) for status, entries in buckets.items()},
        "total": total,
        "page": page,
        "perPage": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 0,
    })
