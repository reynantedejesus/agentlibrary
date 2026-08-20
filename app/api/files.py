"""File upload, listing, download and deletion.

    GET    /api/assets/<id>/files       list (respects asset visibility)
    POST   /api/assets/<id>/files       multipart upload
    GET    /api/files/<id>/download     authorised, audited stream
    DELETE /api/files/<id>              owner or reviewer

The prototype simulated all of this with a toast. Here:

* Uploads are ``multipart/form-data`` and go through ``app/storage.py``, which
  validates extension, sniffed MIME type and byte size, writes a randomly-named
  file with mode 0600 under ``UPLOAD_DIR``, and optionally scans it with ClamAV.
* Downloads never take a path from the client — only an integer row id — so
  path traversal has no input to work with, and ``storage.absolute_path()``
  re-checks containment anyway.
* ``send_file`` is called with ``as_attachment=True`` by default and an
  explicit ``download_name``, so a stored ``.html`` masquerading as something
  else can never execute in the site's origin.
* Every download writes an ``ActivityLog`` row.

Connects to: ``app/storage.py``, ``app/models.py`` (AssetFile),
``app/security.py``. Consumed by the Files tab in ``static/js/app.js``.
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, current_app, request, send_file
from flask_login import current_user

from app import audit, reference, storage
from app.errors import Conflict, Forbidden, NotFound, ValidationError, ok
from app.extensions import db, limiter
from app.models import Asset, AssetFile, AssetVersion, utcnow
from app.security import (assert_can_view_asset, current_user_or_none,
                          login_required_json)

log = logging.getLogger(__name__)

bp = Blueprint("files", __name__)

# Content types we are willing to render inline. Everything else is forced to
# download, which neutralises stored-XSS via an uploaded document.
INLINE_SAFE_MIME = {"application/pdf", "image/png", "image/jpeg", "text/plain"}


def _rate_limit_write(view):
    if limiter is None:
        return view
    return limiter.limit(
        lambda: current_app.config.get("RATELIMIT_WRITE", "60 per minute"),
        exempt_when=lambda: not current_app.config.get("RATELIMIT_ENABLED", True),
        methods=["POST", "DELETE"],
    )(view)


def _load_asset(asset_id: int) -> Asset:
    asset = db.session.get(Asset, asset_id)
    if asset is None:
        raise NotFound("That asset could not be found.")
    assert_can_view_asset(asset)
    return asset


def _may_manage_files(asset: Asset) -> bool:
    viewer = current_user_or_none()
    if viewer is None:
        return False
    if viewer.has_role(reference.ROLE_REVIEWER):
        return True
    return asset.owner_id == viewer.id or asset.creator_id == viewer.id


@bp.route("/api/assets/<int:asset_id>/files", methods=["GET"])
def list_files(asset_id: int):
    asset = _load_asset(asset_id)
    return ok({
        "files": [f.to_dict() for f in asset.files],
        "canUpload": _may_manage_files(asset),
        "fileKinds": reference.FILE_KINDS,
    })


@bp.route("/api/assets/<int:asset_id>/files", methods=["POST"])
@login_required_json
@_rate_limit_write
def upload_file(asset_id: int):
    asset = _load_asset(asset_id)
    if not _may_manage_files(asset):
        raise Forbidden("Only the asset's owner or a reviewer can attach files.")

    maximum = current_app.config.get("MAX_FILES_PER_ASSET", 25)
    if len(asset.files) >= maximum:
        raise Conflict("This asset already has the maximum of {0} files.".format(maximum),
                       code="FILE_LIMIT_REACHED")

    upload = request.files.get("file")
    if upload is None:
        raise ValidationError("No file was supplied.",
                              {"file": "Choose a file to upload."})

    kind = (request.form.get("kind") or "documentation").strip()
    if kind not in reference.FILE_KIND_IDS:
        raise ValidationError("Unknown file kind.",
                              {"kind": "Choose one of: {0}.".format(
                                  ", ".join(reference.FILE_KIND_IDS))})

    category = (request.form.get("category") or "").strip()[:48] or None
    if category and category not in reference.RESOURCE_CATEGORIES:
        raise ValidationError("Unknown resource category.",
                              {"category": "Not a recognised category."})

    version_id = request.form.get("versionId", type=int)
    version = None
    if version_id:
        version = db.session.get(AssetVersion, version_id)
        if version is None or version.asset_id != asset.id:
            # IDOR guard: a version id from another asset must not attach here.
            raise ValidationError("That version does not belong to this asset.",
                                  {"versionId": "Unknown version."})

    metadata, absolute = storage.save_upload(upload)
    actor = current_user._get_current_object()

    try:
        row = AssetFile(
            asset_id=asset.id,
            asset_version_id=version.id if version else None,
            original_name=metadata["display_name"],
            stored_name=metadata["stored_name"],
            relative_path=metadata["relative_path"],
            kind=kind,
            category=category,
            extension=metadata["extension"],
            mime_type=metadata["mime_type"],
            size_bytes=metadata["size_bytes"],
            sha256=metadata["sha256"],
            scan_status=metadata["scan_status"],
            uploaded_by_id=actor.id,
        )
        db.session.add(row)
        asset.last_updated_at = utcnow()
        audit.record(audit.FILE_UPLOAD, row, {
            "assetId": asset.id, "wgtCode": asset.wgt_code, "kind": kind,
            "sizeBytes": metadata["size_bytes"], "sha256": metadata["sha256"],
            "scanStatus": metadata["scan_status"],
        }, actor=actor)
        db.session.commit()
    except Exception:
        # Do not leave an orphan on disk if the row could not be written.
        db.session.rollback()
        storage.discard(metadata["relative_path"])
        raise

    log.info("File %s attached to %s by %s",
             row.stored_name, asset.wgt_code, actor.email)
    return ok({"file": row.to_dict(), "assetId": asset.id}, 201)


@bp.route("/api/files/<int:file_id>/download", methods=["GET"])
def download_file(file_id: int):
    """Authorised download. The physical path is never exposed."""
    row = db.session.get(AssetFile, file_id)
    if row is None:
        raise NotFound("That file could not be found.")

    asset = db.session.get(Asset, row.asset_id)
    if asset is None:
        raise NotFound("That file could not be found.")
    # Same visibility rule as the asset itself: a file on a pending or archived
    # asset is not downloadable by anyone who cannot see the asset.
    assert_can_view_asset(asset)

    if row.scan_status == "infected":
        raise Forbidden("That file was quarantined by the malware scanner.")

    path = storage.absolute_path(row.relative_path)
    if not os.path.isfile(path):
        log.error("Missing file on disk for AssetFile %s (%s)", row.id, row.relative_path)
        raise NotFound("That file is no longer available.")

    inline = (request.args.get("disposition") or "").lower() == "inline"
    as_attachment = not (inline and row.mime_type in INLINE_SAFE_MIME)

    viewer = current_user_or_none()
    audit.record(audit.FILE_DOWNLOAD, row, {
        "assetId": asset.id, "wgtCode": asset.wgt_code,
        "inline": not as_attachment,
    }, actor=viewer, actor_email=getattr(viewer, "email", None) or "anonymous")
    db.session.commit()

    response = send_file(
        path,
        mimetype=row.mime_type or "application/octet-stream",
        as_attachment=as_attachment,
        download_name=row.original_name,
        conditional=True,
        max_age=0,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.route("/api/files/<int:file_id>", methods=["DELETE"])
@login_required_json
@_rate_limit_write
def delete_file(file_id: int):
    row = db.session.get(AssetFile, file_id)
    if row is None:
        raise NotFound("That file could not be found.")
    asset = db.session.get(Asset, row.asset_id)
    if asset is None or not _may_manage_files(asset):
        raise Forbidden("Only the asset's owner or a reviewer can remove files.")

    actor = current_user._get_current_object()
    relative_path = row.relative_path
    audit.record(audit.FILE_DELETE, row, {
        "assetId": asset.id, "wgtCode": asset.wgt_code,
        "name": row.original_name,
    }, actor=actor)
    db.session.delete(row)
    asset.last_updated_at = utcnow()
    db.session.commit()
    # Delete from disk only after the row is safely gone.
    storage.discard(relative_path)
    log.info("File %s removed from %s by %s", file_id, asset.wgt_code, actor.email)
    return ok({"deleted": file_id})
