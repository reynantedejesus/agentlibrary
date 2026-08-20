"""Server-side validation.

The prototype validated only in JavaScript (``validateWizardForSubmit``); that
check is preserved for fast feedback but is no longer trusted. Everything that
arrives from a browser is re-validated here against the same rules plus the
reference vocabularies, and violations become a ``VALIDATION_ERROR`` envelope
with a per-field message the wizard can display inline.

Connects to: ``app/api/assets.py``, ``app/api/auth.py``, ``app/api/files.py``.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from flask import current_app

from app import reference
from app.errors import ValidationError

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
VERSION_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){0,2}$")
SAFE_URL_SCHEMES = ("http", "https")

MAX_LENGTHS = {
    "name": 200,
    "description": 4000,
    "instructions": 200000,
    "useCase": 4000,
    "problemSolved": 4000,
    "inputRequirements": 4000,
    "expectedOutput": 4000,
    "exampleUse": 4000,
    "knowledgeBase": 8000,
    "ownerName": 160,
    "ownerEmail": 254,
    "otherPlatform": 120,
    "link": 2048,
    "summary": 2000,
    "rejectionReason": 2000,
    "tag": 80,
}


class Validator(object):
    """Accumulates field errors so the client gets them all at once."""

    def __init__(self, payload: Optional[Dict[str, Any]] = None):
        self.payload = payload if isinstance(payload, dict) else {}
        self.errors: Dict[str, str] = {}
        self.clean: Dict[str, Any] = {}

    # -- primitives --------------------------------------------------------
    def _raw(self, field: str) -> Any:
        return self.payload.get(field)

    def fail(self, field: str, message: str) -> None:
        self.errors.setdefault(field, message)

    def string(self, field: str, label: str, required: bool = False,
               max_length: Optional[int] = None, default: str = "") -> str:
        raw = self._raw(field)
        if raw is None:
            value = default
        elif isinstance(raw, str):
            value = raw
        else:
            self.fail(field, "{0} must be text.".format(label))
            return default
        value = value.replace("\x00", "").strip()
        limit = max_length or MAX_LENGTHS.get(field, 1000)
        if required and not value:
            self.fail(field, "{0} is required.".format(label))
        elif len(value) > limit:
            self.fail(field, "{0} must be {1} characters or fewer.".format(label, limit))
            value = value[:limit]
        self.clean[field] = value
        return value

    def choice(self, field: str, label: str, options: List[str],
               required: bool = False, default: str = "") -> str:
        value = self.string(field, label, required=required, max_length=120,
                            default=default)
        if value and value not in options:
            self.fail(field, "{0} must be one of: {1}.".format(label, ", ".join(options)))
        elif not value and default:
            value = default
            self.clean[field] = value
        return value

    def email(self, field: str, label: str, required: bool = True) -> str:
        value = self.string(field, label, required=required, max_length=254)
        if value and not EMAIL_RE.match(value):
            self.fail(field, "{0} doesn't look like a valid email address.".format(label))
            return value
        if value:
            allowed = current_app.config.get("ALLOWED_EMAIL_DOMAINS") or []
            if allowed:
                domain = value.rsplit("@", 1)[-1].lower()
                if domain not in [d.lower() for d in allowed]:
                    self.fail(field, "{0} must be on an approved domain ({1}).".format(
                        label, ", ".join(allowed)))
            self.clean[field] = value.lower()
        return self.clean.get(field, value)

    def url(self, field: str, label: str, required: bool = False) -> str:
        value = self.string(field, label, required=required, max_length=2048)
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme.lower() not in SAFE_URL_SCHEMES or not parsed.netloc:
            # Blocks javascript: and data: URLs reaching an href.
            self.fail(field, "{0} must be a full http:// or https:// link.".format(label))
        return value

    def boolean(self, field: str, default: bool = False) -> bool:
        raw = self._raw(field)
        if raw is None:
            value = default
        elif isinstance(raw, bool):
            value = raw
        elif isinstance(raw, str):
            value = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            value = bool(raw)
        self.clean[field] = value
        return value

    def tags(self, field: str = "tags") -> List[str]:
        raw = self._raw(field)
        if raw is None:
            raw = []
        if isinstance(raw, str):
            raw = [part for part in raw.split(",")]
        if not isinstance(raw, list):
            self.fail(field, "Tags must be a list.")
            return []
        limit = current_app.config.get("MAX_TAGS_PER_ASSET", 4)
        cleaned: List[str] = []
        seen = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            value = item.replace("\x00", "").strip()[:MAX_LENGTHS["tag"]]
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(value)
        if len(cleaned) > limit:
            self.fail(field, "A maximum of {0} tags is allowed.".format(limit))
            cleaned = cleaned[:limit]
        self.clean[field] = cleaned
        return cleaned

    def version_number(self, field: str, label: str, required: bool = True) -> str:
        value = self.string(field, label, required=required, max_length=16)
        if value and not VERSION_RE.match(value):
            self.fail(field, "{0} must look like 1.0 or 2.1.3.".format(label))
        return value

    # -- finish ------------------------------------------------------------
    def raise_if_invalid(self, message: str = "Please correct the highlighted fields.") -> None:
        if self.errors:
            raise ValidationError(message, self.errors)

    def result(self) -> Dict[str, Any]:
        self.raise_if_invalid()
        return self.clean


# ---------------------------------------------------------------------------
# composite validators
# ---------------------------------------------------------------------------
def validate_asset_submission(payload: Dict[str, Any],
                              editing: bool = False) -> Dict[str, Any]:
    """Validate the Add-to-Library / Edit payload.

    Mirrors the prototype's ``validateWizardForSubmit`` field-for-field, then
    adds the checks JavaScript could not be trusted to make: vocabulary
    membership, length caps, URL scheme safety and tag limits.
    """
    v = Validator(payload)
    ref = reference.get_reference()

    asset_type = v.choice("type", "Asset type", reference.ASSET_TYPE_IDS, required=True)
    if not editing:
        v.choice("department", "Department", ref["departments"], required=True)
    v.string("name", "Name", required=True)
    v.string("description", "Description", required=True)
    v.string("instructions", "Instructions", required=True,
             max_length=MAX_LENGTHS["instructions"])
    v.url("link", "Link to the artifact", required=True)
    v.string("ownerName", "Creator / Owner Name", required=True)
    v.email("ownerEmail", "Creator / Owner Email", required=True)

    if asset_type == "other":
        v.string("otherPlatform", "What tool is this", required=True)
    if asset_type == "gpt":
        v.string("knowledgeBase", "Knowledge Base", required=False)
        v.choice("preferredModel", "Preferred Model", ref["recommendedModels"],
                 required=False, default=ref["recommendedModels"][0])

    v.tags()
    v.boolean("featured", False)
    v.choice("sensitivity", "Sensitivity", ref["sensitivity"],
             required=False, default="Standard Internal")
    v.choice("intendedAudience", "Intended audience", ref["intendedAudience"],
             required=False, default="Company-wide")
    v.choice("reviewFrequency", "Review frequency", ref["reviewFrequency"],
             required=False, default="Every 6 months")
    v.string("useCase", "Use case", required=False)
    v.string("problemSolved", "Problem solved", required=False)
    v.string("inputRequirements", "Input requirements", required=False)
    v.string("expectedOutput", "Expected output", required=False)
    v.string("exampleUse", "Example use", required=False)

    return v.result()


def validate_version_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    v = Validator(payload)
    v.version_number("versionNumber", "Version number", required=True)
    v.string("summary", "Change summary", required=True)
    v.string("updatedBy", "Updated by", required=False, max_length=160)
    return v.result()


def validate_login(payload: Dict[str, Any]) -> Tuple[str, str]:
    v = Validator(payload)
    email = v.string("email", "Email", required=True, max_length=254).lower()
    password = v.payload.get("password")
    if not isinstance(password, str) or not password:
        v.fail("password", "Password is required.")
        password = ""
    v.raise_if_invalid("Enter your email address and password.")
    return email, password


def validate_new_password(payload: Dict[str, Any], field: str = "newPassword") -> str:
    v = Validator(payload)
    raw = payload.get(field)
    if not isinstance(raw, str):
        raw = ""
    minimum = current_app.config.get("MIN_PASSWORD_LENGTH", 12)
    if len(raw) < minimum:
        v.fail(field, "Password must be at least {0} characters.".format(minimum))
    elif len(raw) > 200:
        v.fail(field, "Password must be 200 characters or fewer.")
    elif raw.strip() != raw:
        v.fail(field, "Password must not start or end with a space.")
    else:
        lowered = raw.lower()
        if lowered in ("password", "changeme") or lowered.startswith("wings123"):
            v.fail(field, "Choose a less predictable password.")
    confirm = payload.get("confirmPassword")
    if confirm is not None and confirm != raw:
        v.fail("confirmPassword", "The two passwords don't match.")
    v.raise_if_invalid("That password can't be used.")
    return raw


def password_strength_problem(raw: str, minimum: int) -> Optional[str]:
    """Shared with the CLI, which has no request context."""
    if not isinstance(raw, str) or len(raw) < minimum:
        return "Password must be at least {0} characters.".format(minimum)
    if raw.strip() != raw:
        return "Password must not start or end with a space."
    if raw.lower() in ("password", "changeme") or raw.lower().startswith("wings123"):
        return "Choose a less predictable password."
    return None
