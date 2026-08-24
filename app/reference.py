"""Server-side reference data — the authoritative copy of the prototype's
``CONFIG`` object.

The browser no longer hard-codes departments, tags, statuses or model tiers;
it fetches them from ``GET /api/config``. Validation in ``app/validators.py``
checks submitted values against these same lists, so the client cannot invent
a department or a status by editing its own JavaScript.

Anything an administrator should be able to change without a code deploy is
also mirrored into the ``config_settings`` table by ``flask seed-config``;
``get_reference()`` prefers the database row and falls back to the constant
below, so a fresh checkout works before the seed has been run.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

ASSET_TYPES = [
    {
        "id": "gpt", "label": "ChatGPT GPT", "short": "ChatGPT", "platform": "ChatGPT",
        "blurb": "A configured ChatGPT assistant with its own instructions, "
                 "knowledge base, and preferred model.",
        "colorClass": "type-gpt", "railClass": "tl-gpt",
    },
    {
        "id": "skill", "label": "Claude Skill / Project", "short": "Claude", "platform": "Claude",
        "blurb": "A packaged SKILL.md, or a Claude Project, that extends what Claude can do.",
        "colorClass": "type-skill", "railClass": "tl-skill",
    },
    {
        "id": "agent", "label": "Copilot Agent", "short": "Copilot", "platform": "Copilot",
        "blurb": "An autonomous or semi-autonomous workflow built in Copilot.",
        "colorClass": "type-agent", "railClass": "tl-agent",
    },
    {
        "id": "other", "label": "Other", "short": "Other", "platform": "Other",
        "blurb": "Runs somewhere else, or doesn't fit the options above.",
        "colorClass": "type-other", "railClass": "tl-other",
    },
]

ASSET_TYPE_IDS = [t["id"] for t in ASSET_TYPES]
PLATFORMS = ["ChatGPT", "Claude", "Copilot", "Other"]

# Display order. Freely reorderable — it has no bearing on WGT numbering.
DEPARTMENTS = [
    "Company Wide", "Sales", "Finance", "Operations", "Marketing",
    "Human Resources", "Tech", "Account Management", "Database", "Other",
]

# Numbering order. NEW DEPARTMENTS MUST BE APPENDED, NEVER INSERTED — inserting
# one shifts every digit after it and existing WGT codes would stop matching
# their department.
DEPARTMENT_DIGIT_ORDER = [
    "Sales", "Finance", "Operations", "Marketing", "Human Resources",
    "Tech", "Account Management", "Database", "Other", "Company Wide",
]

TAGS = [
    "Research", "Writing", "Analysis", "Reporting", "Data", "Presentation",
    "Automation", "Compliance", "Productivity", "Customer-facing",
    "Internal Operations",
]

STATUS_PENDING = "Pending Review"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"
STATUS_DEPRECATED = "Deprecated"
STATUS_ARCHIVED = "Archived"
STATUSES = [STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED,
            STATUS_DEPRECATED, STATUS_ARCHIVED]

INTENDED_AUDIENCE = ["Company-wide", "Specific departments", "Specific roles", "Restricted"]
SENSITIVITY = ["Standard Internal", "Confidential", "Restricted"]
REVIEW_FREQUENCY = ["Monthly", "Quarterly", "Every 6 months", "Annually", "No scheduled review"]
REVIEW_FREQUENCY_MONTHS = {"Monthly": 1, "Quarterly": 3, "Every 6 months": 6, "Annually": 12}

# No "org default" entry: a blank preferred model means "no preference, let the
# user decide", and the wizard never auto-picks one.
RECOMMENDED_MODELS = [
    "Fast / low-cost tier", "Balanced tier",
    "Highest-capability tier", "Legacy / pinned version",
]
NO_PREFERRED_MODEL_LABEL = "No preferred model (allow user to decide)"

GPT_CAPABILITIES = ["Web Search", "Image Generation", "Canvas",
                    "Code Interpreter / Data Analysis", "Other"]
CLAUDE_SURFACES = ["Claude", "Claude Desktop", "Claude Code", "Cowork", "Other"]
RESOURCE_CATEGORIES = ["Reference", "Template", "Script", "Example", "Documentation", "Other"]
AGENT_TRIGGER_TYPES = ["User initiated", "Scheduled", "Event triggered", "Autonomous", "Other"]
EXTERNAL_INTEGRATION_OPTIONS = ["No", "App", "Action / API"]

FILE_KINDS = {
    "source": {
        "label": "Source configuration",
        "desc": "Instructions, prompts, SKILL.md — the thing that defines behaviour.",
    },
    "deployable": {
        "label": "Deployable artifact",
        "desc": "The packaged, installable tool itself (e.g. a Skill ZIP).",
    },
    "knowledge": {
        "label": "Knowledge Base",
        "desc": "Reference material and knowledge files the asset was given.",
    },
    "context": {
        "label": "Context",
        "desc": "Files a Claude Skill or Project was given as context.",
    },
    "documentation": {
        "label": "Documentation",
        "desc": "Guides, READMEs, implementation notes for humans.",
    },
}
FILE_KIND_IDS = list(FILE_KINDS.keys())

FILE_TYPE_ICONS = {
    "pdf": "PDF", "md": "MD", "txt": "TXT", "docx": "DOC", "zip": "ZIP",
    "json": "JSON", "yaml": "YML", "yml": "YML", "csv": "CSV", "xlsx": "XLS",
    "png": "IMG", "jpg": "IMG", "jpeg": "IMG", "other": "FILE",
}

UPDATE_STATUS_OPEN = "Open"
UPDATE_STATUS_ACCEPTED = "Accepted"
UPDATE_STATUS_DECLINED = "Declined"
UPDATE_REQUEST_STATUSES = [UPDATE_STATUS_OPEN, UPDATE_STATUS_ACCEPTED,
                           UPDATE_STATUS_DECLINED]

# Every field an Update Request may propose a new value for. Ids are unique
# across asset types so one flat label map serves the admin view regardless of
# what kind of asset a request targets.
UPDATE_REQUEST_FIELD_LABELS = {
    "name": "Name",
    "description": "Description",
    "instructions": "Instructions / Prompt",
    "link": "Link to the artifact",
    "tags": "Tags",
    "owner": "Creator / Owner details",
    "knowledgeBase": "Knowledge Base",
    "context": "Context",
    "preferredModel": "Preferred Model",
    "otherPlatform": "What tool is this",
}

# Fields offered to every asset type.
_UPDATE_FIELDS_BASE = [
    {"id": "name", "label": "Name", "promptLabel": "New Name", "kind": "text",
     "placeholder": "New name for this tool"},
    {"id": "description", "label": "Description",
     "promptLabel": "Latest description", "kind": "textarea"},
    {"id": "instructions", "label": "Instructions / Prompt",
     "promptLabel": "Latest instructions / prompt", "kind": "textarea"},
    {"id": "link", "label": "Link to the artifact", "promptLabel": "New link",
     "kind": "text", "placeholder": "https://..."},
    {"id": "tags", "label": "Tags", "promptLabel": "Latest tags", "kind": "text",
     "placeholder": "e.g. Writing, Reporting, Analysis",
     "hint": "Comma-separated \u2014 replaces the current tags."},
    {"id": "owner", "label": "Creator / Owner details", "kind": "owner"},
]

# Per-type extras. "files" fields store their uploads against the request via
# asset_files.update_request_id rather than in the JSON payload.
_UPDATE_FIELDS_BY_TYPE = {
    "gpt": [
        {"id": "knowledgeBase", "label": "Knowledge Base",
         "promptLabel": "Latest knowledge base files", "kind": "files",
         "filesKey": "knowledgeFiles"},
        {"id": "preferredModel", "label": "Preferred Model",
         "promptLabel": "Preferred model", "kind": "select"},
    ],
    "skill": [
        {"id": "context", "label": "Context",
         "promptLabel": "Latest context files", "kind": "files",
         "filesKey": "contextFiles"},
    ],
    "agent": [
        {"id": "knowledgeBase", "label": "Knowledge Base",
         "promptLabel": "Latest knowledge base files", "kind": "files",
         "filesKey": "knowledgeFiles"},
    ],
    "other": [
        {"id": "otherPlatform", "label": "What tool is this",
         "promptLabel": "What tool is this now?", "kind": "text",
         "placeholder": "e.g. n8n, Zapier, an internal PowerApp"},
    ],
}


def update_request_field_options(asset_type):
    """Fields a requester may propose changes to, for one asset type."""
    import copy
    options = copy.deepcopy(_UPDATE_FIELDS_BASE)
    for extra in copy.deepcopy(_UPDATE_FIELDS_BY_TYPE.get(asset_type, [])):
        if extra.get("kind") == "select" and extra["id"] == "preferredModel":
            extra["options"] = [""] + list(RECOMMENDED_MODELS)
            extra["optionLabels"] = {"": NO_PREFERRED_MODEL_LABEL}
        options.append(extra)
    return options


def update_request_field_ids(asset_type):
    return [option["id"] for option in update_request_field_options(asset_type)]


def update_field_label(field_id):
    return UPDATE_REQUEST_FIELD_LABELS.get(field_id, field_id)


ROLE_USER = "user"
ROLE_REVIEWER = "reviewer"
ROLE_ADMIN = "admin"
ROLES = [ROLE_USER, ROLE_REVIEWER, ROLE_ADMIN]
# Higher number == more authority. Used by app/security.py.
ROLE_RANK = {ROLE_USER: 10, ROLE_REVIEWER: 20, ROLE_ADMIN: 30}

# Keys that flask seed-config mirrors into config_settings so an admin can edit
# them later without a deploy.
SEEDABLE_KEYS = [
    "departments", "departmentDigitOrder", "tags", "recommendedModels",
    "gptCapabilities", "claudeSurfaces", "resourceCategories",
    "agentTriggerTypes", "externalIntegrationOptions", "intendedAudience",
    "sensitivity", "reviewFrequency",
]

_DEFAULTS: Dict[str, Any] = {
    "assetTypes": ASSET_TYPES,
    "platforms": PLATFORMS,
    "departments": DEPARTMENTS,
    "departmentDigitOrder": DEPARTMENT_DIGIT_ORDER,
    "tags": TAGS,
    "statuses": STATUSES,
    "intendedAudience": INTENDED_AUDIENCE,
    "sensitivity": SENSITIVITY,
    "reviewFrequency": REVIEW_FREQUENCY,
    "recommendedModels": RECOMMENDED_MODELS,
    "gptCapabilities": GPT_CAPABILITIES,
    "claudeSurfaces": CLAUDE_SURFACES,
    "resourceCategories": RESOURCE_CATEGORIES,
    "agentTriggerTypes": AGENT_TRIGGER_TYPES,
    "externalIntegrationOptions": EXTERNAL_INTEGRATION_OPTIONS,
    "fileKinds": FILE_KINDS,
    "fileTypeIcons": FILE_TYPE_ICONS,
    "roles": ROLES,
    "updateRequestStatuses": UPDATE_REQUEST_STATUSES,
    "updateRequestFieldLabels": UPDATE_REQUEST_FIELD_LABELS,
    "noPreferredModelLabel": NO_PREFERRED_MODEL_LABEL,
}


def default_reference() -> Dict[str, Any]:
    """A deep-ish copy of the built-in defaults."""
    import copy
    return copy.deepcopy(_DEFAULTS)


def get_reference(key: Optional[str] = None) -> Any:
    """Reference data, preferring an admin-edited ``config_settings`` row.

    Falls back to the constants above when the table is missing (fresh
    checkout, or during ``flask db upgrade`` before the seed has run).
    """
    data = default_reference()
    try:
        from app.models import ConfigSetting
        for row in ConfigSetting.query.all():
            if row.key in data and row.value is not None:
                data[row.key] = row.value
    except Exception:      # table not created yet, or no app context
        pass
    if key is None:
        return data
    return data.get(key)


def department_digit(department: str, digit_order: Optional[List[str]] = None) -> str:
    """WGT department digit: 1-based position in the *numbering* order.

    Unknown or blank departments fall back to "Other"'s digit, exactly as the
    prototype's ``departmentDigit()`` did.
    """
    order = digit_order if digit_order is not None else get_reference("departmentDigitOrder")
    try:
        idx = order.index(department)
    except (ValueError, AttributeError):
        try:
            idx = order.index("Other")
        except (ValueError, AttributeError):
            idx = len(DEPARTMENT_DIGIT_ORDER) - 2
    return str(idx + 1)


def type_meta(type_id: str) -> Dict[str, Any]:
    for t in ASSET_TYPES:
        if t["id"] == type_id:
            return t
    return ASSET_TYPES[-1]


def platform_for_type(type_id: str) -> str:
    return type_meta(type_id)["platform"]
