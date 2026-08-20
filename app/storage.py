"""Private file storage for asset attachments.

Rules enforced here
-------------------
* Files live in ``UPLOAD_DIR`` (default ``/var/lib/agentlibrary/uploads``),
  **outside** the Flask static directory and outside anything nginx serves, so
  there is no URL that maps to a stored file. Downloads only happen through
  ``GET /api/files/<id>/download``, which checks authorisation first.
* The stored filename is 32 random hex characters plus a validated extension —
  the user's filename is never used on disk. ``secure_filename`` is applied to
  the *display* name only, as a secondary measure.
* Path traversal is impossible by construction (no user input reaches the path)
  and is re-checked with ``os.path.realpath`` containment before every read.
* Extension, sniffed MIME type and byte size are all validated. Content is read
  in bounded chunks so a lying ``Content-Length`` cannot exhaust memory.
* A SHA-256 digest is recorded for integrity checks and de-duplication.

Malware scanning
----------------
``scan_file()`` speaks the ClamAV INSTREAM protocol over TCP. Set
``CLAMAV_ENABLED=1`` plus ``CLAMAV_HOST``/``CLAMAV_PORT`` and every upload is
scanned before its database row is committed; an infected file is deleted and
the upload is rejected with ``FILE_INFECTED``. With scanning disabled (the
default) rows are marked ``scan_status='skipped'``. See the README section
"Malware scanning" for the deployment steps.

Connects to: ``app/api/files.py`` (upload/download/delete) and
``app/models.py`` (``AssetFile``).
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import struct
import socket
from typing import Tuple

from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.errors import ApiError, PayloadTooLarge, ValidationError

log = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024

# Extension -> the MIME types we are willing to accept for it. A file whose
# sniffed type is not in this set is rejected even if the extension is allowed.
EXTENSION_MIME_ALLOWLIST = {
    "pdf": {"application/pdf"},
    "md": {"text/markdown", "text/plain", "text/x-markdown"},
    "txt": {"text/plain"},
    "csv": {"text/csv", "text/plain"},
    "json": {"application/json", "text/plain"},
    "yaml": {"application/x-yaml", "text/yaml", "text/plain"},
    "yml": {"application/x-yaml", "text/yaml", "text/plain"},
    "zip": {"application/zip", "application/x-zip-compressed"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "application/zip"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "application/zip"},
    "png": {"image/png"},
    "jpg": {"image/jpeg"},
    "jpeg": {"image/jpeg"},
}

# Magic-byte signatures used to sniff content rather than trusting the
# browser-supplied Content-Type header.
MAGIC_SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)

# Content that must never be stored regardless of extension.
FORBIDDEN_SIGNATURES = (
    (b"MZ", "Windows executable"),
    (b"\x7fELF", "Linux executable"),
    (b"#!", "executable script"),
    (b"\xca\xfe\xba\xbe", "Java class file"),
)

TEXT_EXTENSIONS = {"md", "txt", "csv", "json", "yaml", "yml"}


class InfectedFile(ApiError):
    def __init__(self, signature: str):
        super().__init__("FILE_INFECTED",
                         "That file was rejected by the malware scanner ({0}).".format(signature),
                         400)


def upload_root() -> str:
    return os.path.abspath(current_app.config["UPLOAD_DIR"])


def ensure_upload_root() -> str:
    root = upload_root()
    os.makedirs(root, mode=0o750, exist_ok=True)
    return root


def split_extension(filename: str) -> str:
    name = (filename or "").strip().lower()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1][:16]


def sniff_mime(head: bytes, extension: str) -> str:
    for signature, mime in MAGIC_SIGNATURES:
        if head.startswith(signature):
            return mime
    if extension in TEXT_EXTENSIONS:
        try:
            head.decode("utf-8")
            return {"csv": "text/csv", "json": "application/json",
                    "yaml": "application/x-yaml", "yml": "application/x-yaml",
                    "md": "text/markdown"}.get(extension, "text/plain")
        except UnicodeDecodeError:
            return "application/octet-stream"
    return "application/octet-stream"


def reject_forbidden_content(head: bytes) -> None:
    for signature, label in FORBIDDEN_SIGNATURES:
        if head.startswith(signature):
            raise ValidationError(
                "Executable content is not accepted ({0}).".format(label),
                {"file": "Executable content is not accepted."},
            )


def validate_extension(filename: str) -> str:
    allowed = [e.lower() for e in current_app.config["ALLOWED_UPLOAD_EXTENSIONS"]]
    extension = split_extension(filename)
    if not extension:
        raise ValidationError("That file has no extension.",
                              {"file": "The file needs a recognised extension."})
    if extension not in allowed:
        raise ValidationError(
            "Files of type .{0} are not accepted.".format(extension),
            {"file": "Allowed types: {0}.".format(", ".join(sorted(allowed)))},
        )
    return extension


def _relative_path_for(stored_name: str) -> str:
    """Two-level sharding keeps any single directory small."""
    return os.path.join(stored_name[:2], stored_name[2:4], stored_name)


def absolute_path(relative_path: str) -> str:
    """Resolve a stored relative path and assert it stays inside UPLOAD_DIR.

    ``relative_path`` comes from our own database, but this containment check
    runs anyway: a corrupted or tampered row must not be able to read
    ``/etc/shadow`` via ``../../``.
    """
    root = upload_root()
    candidate = os.path.realpath(os.path.join(root, relative_path))
    root_real = os.path.realpath(root)
    if not (candidate == root_real or candidate.startswith(root_real + os.sep)):
        log.error("Path traversal attempt blocked: %r", relative_path)
        raise ApiError("STORAGE_ERROR", "That file could not be read.", 500)
    return candidate


def save_upload(file_storage: FileStorage) -> Tuple[dict, str]:
    """Validate and persist one uploaded file.

    Returns ``(metadata, absolute_path)``. The caller is responsible for
    creating the ``AssetFile`` row and for calling :func:`discard` if the
    surrounding transaction fails.
    """
    if file_storage is None or not (file_storage.filename or "").strip():
        raise ValidationError("No file was supplied.", {"file": "Choose a file to upload."})

    original_name = os.path.basename((file_storage.filename or "").replace("\\", "/"))
    # secure_filename is the SECONDARY measure — the stored name is random.
    display_name = secure_filename(original_name) or "upload"
    display_name = display_name[:255]
    extension = validate_extension(display_name)

    max_bytes = int(current_app.config["MAX_CONTENT_LENGTH"])
    stored_name = "{0}.{1}".format(secrets.token_hex(16), extension)
    relative_path = _relative_path_for(stored_name)
    target = absolute_path(relative_path)
    os.makedirs(os.path.dirname(target), mode=0o750, exist_ok=True)

    digest = hashlib.sha256()
    total = 0
    head = b""
    stream = file_storage.stream
    try:
        # 0o600: only the service account may read the raw file.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PayloadTooLarge(
                        "That file is larger than the {0} MB upload limit.".format(
                            round(max_bytes / (1024 * 1024), 1)))
                if len(head) < 512:
                    head += chunk[: 512 - len(head)]
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        _unlink(target)
        raise

    if total == 0:
        _unlink(target)
        raise ValidationError("That file is empty.", {"file": "The file contains no data."})

    try:
        reject_forbidden_content(head)
        sniffed = sniff_mime(head, extension)
        permitted = EXTENSION_MIME_ALLOWLIST.get(extension, set())
        if permitted and sniffed not in permitted:
            raise ValidationError(
                "The contents of that file don't match a .{0} file.".format(extension),
                {"file": "Detected content type {0}.".format(sniffed)},
            )
        scan_status = scan_file(target)
    except Exception:
        _unlink(target)
        raise

    metadata = {
        "original_name": original_name[:255] or display_name,
        "display_name": display_name,
        "stored_name": stored_name,
        "relative_path": relative_path,
        "extension": extension,
        "mime_type": sniffed,
        "size_bytes": total,
        "sha256": digest.hexdigest(),
        "scan_status": scan_status,
    }
    log.info("Stored upload %s (%s bytes, %s, scan=%s)",
             stored_name, total, sniffed, scan_status)
    return metadata, target


def discard(relative_path: str) -> None:
    """Remove a stored file — used when the DB transaction rolls back."""
    try:
        _unlink(absolute_path(relative_path))
    except Exception:       # pragma: no cover - defensive
        log.exception("Failed to discard stored file %s", relative_path)


def _unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        log.warning("Could not remove %s", path)


# ---------------------------------------------------------------------------
# ClamAV
# ---------------------------------------------------------------------------
def scan_file(path: str) -> str:
    """Scan ``path`` with clamd. Returns 'clean', 'skipped', or raises.

    Protocol: ``zINSTREAM\\0`` then length-prefixed chunks, terminated by a
    zero-length chunk. clamd answers ``stream: OK`` or ``stream: <sig> FOUND``.
    """
    if not current_app.config.get("CLAMAV_ENABLED"):
        return "skipped"

    host = current_app.config["CLAMAV_HOST"]
    port = int(current_app.config["CLAMAV_PORT"])
    timeout = int(current_app.config["CLAMAV_TIMEOUT"])
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b"zINSTREAM\0")
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            sock.sendall(struct.pack("!L", 0))
            response = b""
            while b"\0" not in response and len(response) < 4096:
                part = sock.recv(4096)
                if not part:
                    break
                response += part
    except (socket.error, OSError) as exc:
        log.error("ClamAV scan failed for %s: %s", path, exc)
        raise ApiError("SCAN_UNAVAILABLE",
                       "The malware scanner is unavailable, so the upload was refused.",
                       503)

    text = response.decode("utf-8", "replace").strip("\0 \r\n")
    if text.endswith("OK"):
        return "clean"
    if "FOUND" in text:
        signature = text.split(":", 1)[-1].replace("FOUND", "").strip() or "unknown"
        log.warning("Malware detected in upload: %s", signature)
        raise InfectedFile(signature)
    log.error("Unexpected clamd response: %r", text)
    raise ApiError("SCAN_UNAVAILABLE",
                   "The malware scanner returned an unexpected result.", 503)
