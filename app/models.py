"""SQLAlchemy models.

Nine tables: users, assets, asset_versions, asset_files, tags, asset_tags,
activity_log, wgt_counters, config_settings.

Design notes
------------
* Frequently searched columns carry ordinary B-tree indexes (see the
  ``index=True`` flags and the ``__table_args__`` composites); the search
  endpoint filters on those columns rather than on JSON.
* Type-specific fields live in the ``assets.configuration`` JSON column so a
  fifth asset type never needs a schema migration. ``db.JSON`` renders as
  MySQL's native ``JSON`` type and as ``TEXT``-backed JSON on SQLite, so the
  test-suite and production share one model definition.
* ``asset_versions`` is append-only — the prototype's approve/reject path
  overwrote ``versions[0]``; nothing here does.
* ``asset_files.relative_path`` is deliberately never serialised to the client.

Connects to: ``app/extensions.py`` (``db``), every blueprint in ``app/api``,
and ``migrations/versions/0001_initial.py``.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from flask_login import UserMixin
from sqlalchemy import Index, UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash

from app import reference
from app.extensions import db

PASSWORD_HASH_METHOD = "pbkdf2:sha256:600000"


def utcnow() -> _dt.datetime:
    return _dt.datetime.utcnow()


def _iso_date(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)


def _iso_dt(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.replace(microsecond=0).isoformat() + "Z"
    return str(value)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    email = db.Column(db.String(254), nullable=False, unique=True, index=True)
    full_name = db.Column(db.String(160), nullable=False, default="")
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default=reference.ROLE_USER, index=True)
    is_active_flag = db.Column("is_active", db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    password_changed_at = db.Column(db.DateTime, nullable=True)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)

    owned_assets = db.relationship(
        "Asset", foreign_keys="Asset.owner_id", back_populates="owner", lazy="dynamic"
    )
    created_assets = db.relationship(
        "Asset", foreign_keys="Asset.creator_id", back_populates="creator", lazy="dynamic"
    )

    # -- password handling (never store plaintext) -------------------------
    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw, method=PASSWORD_HASH_METHOD)
        self.password_changed_at = utcnow()

    def check_password(self, raw: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw)

    # -- flask-login -------------------------------------------------------
    @property
    def is_active(self) -> bool:          # type: ignore[override]
        return bool(self.is_active_flag)

    def get_id(self) -> str:
        # The password-hash fragment invalidates every existing session when
        # the password changes (flask-login "strong" session protection).
        return "{0}|{1}".format(self.id, (self.password_hash or "")[-16:])

    # -- roles -------------------------------------------------------------
    @property
    def rank(self) -> int:
        return reference.ROLE_RANK.get(self.role, 0)

    def has_role(self, minimum: str) -> bool:
        return self.rank >= reference.ROLE_RANK.get(minimum, 999)

    @property
    def is_admin(self) -> bool:
        return self.role == reference.ROLE_ADMIN

    @property
    def is_reviewer(self) -> bool:
        return self.has_role(reference.ROLE_REVIEWER)

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > utcnow())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "fullName": self.full_name,
            "role": self.role,
            "isActive": self.is_active,
            "createdAt": _iso_dt(self.created_at),
            "lastLoginAt": _iso_dt(self.last_login_at),
        }

    def __repr__(self) -> str:
        return "<User {0} ({1})>".format(self.email, self.role)


# ---------------------------------------------------------------------------
# tags / asset_tags
# ---------------------------------------------------------------------------
class Tag(db.Model):
    __tablename__ = "tags"

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    slug = db.Column(db.String(80), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    assets = db.relationship("AssetTag", back_populates="tag", cascade="all, delete-orphan")

    @staticmethod
    def slugify(name: str) -> str:
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
        return slug[:80] or "tag"

    @classmethod
    def get_or_create(cls, name: str) -> "Tag":
        clean = (name or "").strip()[:80]
        slug = cls.slugify(clean)
        existing = cls.query.filter_by(slug=slug).first()
        if existing:
            return existing
        tag = cls(name=clean, slug=slug)
        db.session.add(tag)
        db.session.flush()
        return tag

    def __repr__(self) -> str:
        return "<Tag {0}>".format(self.name)


class AssetTag(db.Model):
    __tablename__ = "asset_tags"

    asset_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True,
    )
    tag_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    asset = db.relationship("Asset", back_populates="asset_tags")
    tag = db.relationship("Tag", back_populates="assets")


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------
class Asset(db.Model):
    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_assets_status_department", "status", "department"),
        Index("ix_assets_status_type", "status", "asset_type"),
        Index("ix_assets_status_updated", "status", "last_updated_at"),
    )

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)

    # -- identity ----------------------------------------------------------
    wgt_code = db.Column(db.String(16), nullable=False, unique=True, index=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    asset_type = db.Column(db.String(16), nullable=False, index=True)
    platform = db.Column(db.String(40), nullable=False, index=True)
    department = db.Column(db.String(80), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False,
                       default=reference.STATUS_PENDING, index=True)

    # -- narrative ---------------------------------------------------------
    description = db.Column(db.Text, nullable=False, default="")
    use_case = db.Column(db.Text, nullable=False, default="")
    problem_solved = db.Column(db.Text, nullable=True)
    input_requirements = db.Column(db.Text, nullable=True)
    expected_output = db.Column(db.Text, nullable=True)
    example_use = db.Column(db.Text, nullable=True)

    # -- people ------------------------------------------------------------
    owner_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    creator_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    owner_name = db.Column(db.String(160), nullable=False, default="")
    owner_email = db.Column(db.String(254), nullable=False, default="")
    creator_name = db.Column(db.String(160), nullable=False, default="")
    backup_owner = db.Column(db.String(160), nullable=True)
    submitted_by_name = db.Column(db.String(160), nullable=False, default="")

    # -- governance --------------------------------------------------------
    intended_audience = db.Column(db.String(48), nullable=False, default="Company-wide")
    access_level = db.Column(db.String(48), nullable=False, default="Company-wide")
    sensitivity = db.Column(db.String(48), nullable=False, default="Standard Internal")
    review_frequency = db.Column(db.String(48), nullable=False, default="Every 6 months")
    next_review_date = db.Column(db.Date, nullable=True, index=True)
    featured = db.Column(db.Boolean, nullable=False, default=False, index=True)
    rejection_reason = db.Column(db.Text, nullable=True)

    # -- versioning / links ------------------------------------------------
    current_version = db.Column(db.String(16), nullable=False, default="1.0")
    direct_url = db.Column(db.String(2048), nullable=True)

    # -- type-specific payload --------------------------------------------
    configuration = db.Column(db.JSON, nullable=False, default=dict)
    additional_departments = db.Column(db.JSON, nullable=False, default=list)

    # -- timestamps --------------------------------------------------------
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    last_updated_at = db.Column(db.DateTime, nullable=False,
                                default=utcnow, onupdate=utcnow, index=True)
    submitted_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    published_at = db.Column(db.DateTime, nullable=True)
    last_verified_at = db.Column(db.DateTime, nullable=True)

    owner = db.relationship("User", foreign_keys=[owner_id], back_populates="owned_assets")
    creator = db.relationship("User", foreign_keys=[creator_id], back_populates="created_assets")
    versions = db.relationship(
        "AssetVersion", back_populates="asset",
        cascade="all, delete-orphan", order_by="AssetVersion.created_at.desc()",
    )
    files = db.relationship(
        "AssetFile", back_populates="asset",
        cascade="all, delete-orphan", order_by="AssetFile.created_at.asc()",
    )
    asset_tags = db.relationship(
        "AssetTag", back_populates="asset", cascade="all, delete-orphan",
    )

    # -- helpers -----------------------------------------------------------
    @property
    def tag_names(self) -> List[str]:
        return [at.tag.name for at in self.asset_tags if at.tag is not None]

    def set_tags(self, names: List[str]) -> None:
        """Replace this asset's tags, de-duplicating by slug."""
        wanted = {}
        for raw in names or []:
            clean = (raw or "").strip()[:80]
            if not clean:
                continue
            wanted.setdefault(Tag.slugify(clean), clean)

        current = {at.tag.slug: at for at in self.asset_tags if at.tag is not None}
        for slug, link in list(current.items()):
            if slug not in wanted:
                self.asset_tags.remove(link)
                db.session.delete(link)

        # Resolve every Tag row first. get_or_create() issues a SELECT, which
        # triggers an autoflush; doing that while a half-built AssetTag is
        # attached to a Tag's backref makes SQLAlchemy warn and skip the row.
        missing = [clean for slug, clean in wanted.items() if slug not in current]
        resolved = [Tag.get_or_create(clean) for clean in missing]
        for tag in resolved:
            link = AssetTag(tag_id=tag.id)
            link.tag = tag
            self.asset_tags.append(link)

    @property
    def current_version_row(self) -> Optional["AssetVersion"]:
        for v in self.versions:
            if v.is_current:
                return v
        return self.versions[0] if self.versions else None

    def compute_next_review(self) -> Optional[_dt.date]:
        months = reference.REVIEW_FREQUENCY_MONTHS.get(self.review_frequency)
        if not months:
            return None
        base = (self.last_verified_at or self.created_at or utcnow()).date()
        month = base.month - 1 + months
        year = base.year + month // 12
        month = month % 12 + 1
        import calendar
        day = min(base.day, calendar.monthrange(year, month)[1])
        return _dt.date(year, month, day)

    def freshness_badge(self, today: Optional[_dt.date] = None) -> Optional[str]:
        """Mirror of the prototype's Util.freshnessBadge()."""
        today = today or _dt.date.today()
        created = (self.created_at or utcnow()).date()
        updated = (self.last_updated_at or utcnow()).date()
        submitted = (self.submitted_at or self.created_at or utcnow()).date()
        if updated != created and 0 <= (today - updated).days <= 30:
            return "Updated"
        if 0 <= (today - submitted).days <= 30:
            return "New"
        return None

    # -- serialisation -----------------------------------------------------
    def to_dict(self, include_detail: bool = False,
                viewer: Optional[User] = None) -> Dict[str, Any]:
        """Camel-cased payload shaped like the prototype's asset record, so the
        existing render functions in ``app.js`` keep working unchanged."""
        data = {
            "id": self.id,
            "wgtCode": self.wgt_code,
            "name": self.name,
            "type": self.asset_type,
            "platform": self.platform,
            "department": self.department,
            "status": self.status,
            "description": self.description or "",
            "useCase": self.use_case or "",
            "tags": self.tag_names,
            "creator": self.creator_name or "",
            "owner": self.owner_name or "",
            "ownerEmail": self.owner_email or "",
            "currentVersion": self.current_version,
            "directUrl": self.direct_url or "",
            "featured": bool(self.featured),
            "accessLevel": self.access_level,
            "sensitivity": self.sensitivity,
            "createdDate": _iso_date(self.created_at),
            "publishedDate": _iso_date(self.published_at) or "",
            "lastUpdated": _iso_date(self.last_updated_at),
            "lastVerified": _iso_date(self.last_verified_at),
            "submissionDate": _iso_date(self.submitted_at),
            "nextReviewDate": _iso_date(self.next_review_date),
            "reviewFrequency": self.review_frequency,
            "submittedBy": self.submitted_by_name or "",
            "freshness": self.freshness_badge(),
            "fileCount": len(self.files),
        }
        if include_detail:
            data.update({
                "problemSolved": self.problem_solved or "",
                "inputRequirements": self.input_requirements or "",
                "expectedOutput": self.expected_output or "",
                "exampleUse": self.example_use or "",
                "intendedAudience": self.intended_audience,
                "backupOwner": self.backup_owner or "",
                "rejectionReason": self.rejection_reason or "",
                "additionalDepartments": self.additional_departments or [],
                "configuration": self.configuration or {},
                "versions": [v.to_dict() for v in sorted(
                    self.versions, key=lambda v: (v.created_at or utcnow()), reverse=True)],
                "files": [f.to_dict() for f in self.files],
                "ownerUserId": self.owner_id,
                "creatorUserId": self.creator_id,
            })
            data["canEdit"] = self.viewer_can_edit(viewer)
            data["canReview"] = bool(viewer and viewer.is_authenticated
                                     and viewer.has_role(reference.ROLE_REVIEWER))
        return data

    def viewer_can_edit(self, viewer: Optional[User]) -> bool:
        """IDOR guard used by both the API and the serialiser."""
        if viewer is None or not getattr(viewer, "is_authenticated", False):
            return False
        if viewer.has_role(reference.ROLE_REVIEWER):
            return True
        if self.creator_id and self.creator_id == viewer.id:
            return self.status in (reference.STATUS_PENDING, reference.STATUS_REJECTED)
        if self.owner_id and self.owner_id == viewer.id:
            return self.status in (reference.STATUS_PENDING, reference.STATUS_REJECTED)
        return False

    def __repr__(self) -> str:
        return "<Asset {0} {1}>".format(self.wgt_code, self.name)


# ---------------------------------------------------------------------------
# asset_versions
# ---------------------------------------------------------------------------
class AssetVersion(db.Model):
    __tablename__ = "asset_versions"
    __table_args__ = (
        UniqueConstraint("asset_id", "version_number", name="uq_asset_version_number"),
        Index("ix_asset_versions_asset_current", "asset_id", "is_current"),
    )

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    asset_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    version_number = db.Column(db.String(16), nullable=False)
    summary = db.Column(db.Text, nullable=False, default="")
    updated_by_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    updated_by_name = db.Column(db.String(160), nullable=False, default="")
    is_current = db.Column(db.Boolean, nullable=False, default=False)
    instructions_snapshot = db.Column(db.Text, nullable=True)
    configuration_snapshot = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    asset = db.relationship("Asset", back_populates="versions")
    updated_by = db.relationship("User")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "versionNumber": self.version_number,
            "date": _iso_date(self.created_at),
            "summary": self.summary or "",
            "updatedBy": self.updated_by_name or "",
            "isCurrent": bool(self.is_current),
        }

    def __repr__(self) -> str:
        return "<AssetVersion {0} v{1}>".format(self.asset_id, self.version_number)


# ---------------------------------------------------------------------------
# asset_files
# ---------------------------------------------------------------------------
class AssetFile(db.Model):
    __tablename__ = "asset_files"

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    asset_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    asset_version_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("asset_versions.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(64), nullable=False, unique=True, index=True)
    # Storage-relative path (e.g. "a3/f1/a3f1...bin"). NEVER serialised.
    relative_path = db.Column(db.String(255), nullable=False)
    kind = db.Column(db.String(24), nullable=False, default="documentation", index=True)
    category = db.Column(db.String(48), nullable=True)
    extension = db.Column(db.String(16), nullable=False, default="other")
    mime_type = db.Column(db.String(120), nullable=False, default="application/octet-stream")
    size_bytes = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"),
                           nullable=False, default=0)
    sha256 = db.Column(db.String(64), nullable=True, index=True)
    scan_status = db.Column(db.String(16), nullable=False, default="skipped")
    uploaded_by_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    asset = db.relationship("Asset", back_populates="files")
    version = db.relationship("AssetVersion")
    uploaded_by = db.relationship("User")

    @property
    def human_size(self) -> str:
        size = float(self.size_bytes or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return ("{0:.0f} {1}" if unit == "B" else "{0:.1f} {1}").format(size, unit)
            size /= 1024.0
        return "{0:.1f} GB".format(size)

    def to_dict(self) -> Dict[str, Any]:
        """Note the absence of ``relative_path`` — the physical location of the
        file is never exposed to the browser."""
        return {
            "id": self.id,
            "name": self.original_name,
            "kind": self.kind,
            "category": self.category,
            "ext": self.extension if self.extension in reference.FILE_TYPE_ICONS else "other",
            "size": self.human_size,
            "sizeBytes": self.size_bytes,
            "mimeType": self.mime_type,
            "scanStatus": self.scan_status,
            "versionId": self.asset_version_id,
            "uploadedDate": _iso_date(self.created_at),
            "downloadUrl": "/api/files/{0}/download".format(self.id),
        }

    def __repr__(self) -> str:
        return "<AssetFile {0} {1}>".format(self.id, self.original_name)


# ---------------------------------------------------------------------------
# activity_log
# ---------------------------------------------------------------------------
class ActivityLog(db.Model):
    __tablename__ = "activity_log"
    __table_args__ = (
        Index("ix_activity_object", "object_type", "object_id"),
        Index("ix_activity_ts_action", "ts", "action"),
    )

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    ts = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    actor_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    actor_email = db.Column(db.String(254), nullable=True)
    action = db.Column(db.String(48), nullable=False, index=True)
    object_type = db.Column(db.String(32), nullable=True)
    object_id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), nullable=True)
    object_label = db.Column(db.String(200), nullable=True)
    ip = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    detail = db.Column(db.JSON, nullable=True)

    actor = db.relationship("User")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ts": _iso_dt(self.ts),
            "action": self.action,
            "actor": self.actor_email or "system",
            "objectType": self.object_type,
            "objectId": self.object_id,
            "objectLabel": self.object_label,
            "ip": self.ip,
            "detail": self.detail or {},
        }

    def __repr__(self) -> str:
        return "<ActivityLog {0} {1}>".format(self.action, self.ts)


# ---------------------------------------------------------------------------
# wgt_counters — the race-condition fix
# ---------------------------------------------------------------------------
class WgtCounter(db.Model):
    """One row per department digit, locked FOR UPDATE during allocation.

    See ``app/wgt.py``. The row lock serialises concurrent submissions so two
    transactions can never read the same ``last_sequence``.
    """
    __tablename__ = "wgt_counters"

    department_digit = db.Column(db.String(8), primary_key=True)
    last_sequence = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return "<WgtCounter digit={0} seq={1}>".format(self.department_digit, self.last_sequence)


# ---------------------------------------------------------------------------
# config_settings — admin-editable reference data
# ---------------------------------------------------------------------------
class ConfigSetting(db.Model):
    __tablename__ = "config_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.JSON, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    updated_by_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    def __repr__(self) -> str:
        return "<ConfigSetting {0}>".format(self.key)
