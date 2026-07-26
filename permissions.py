"""
Single source of truth for who sees what. Change the matrix, not code
scattered across routes or templates.

Three roles:
  admin      - you and Justin. Everything, plus user management and settings.
  technician - Kelvin, Robert, Alex, Francis. Everything operational,
               not settings/user management.
  client     - GTL themselves. Restricted: fleet health and feedback
               tracking, not tampering, not internal investigation
               notes, not raw exports.

PANEL_ACCESS controls which dashboard panels a role can see at all.
DATA_KEYS_BLOCKED controls which keys are stripped out of the JSON
payload before it ever reaches the browser, for panels a client CAN
see but with a reduced version (e.g. Critical Assets minus internal
investigation reasoning).
"""

import json
import os

ROLES = ("admin", "technician", "client")

# Human-readable names for the Roles & Visibility editor, so the UI
# never has to show raw panel ids like "p-tquality" to a person.
PANEL_LABELS = {
    "p-exec": "Executive Dashboard",
    "p-full": "Full Data",
    "p-critical": "Critical Assets",
    "p-pending": "Pending Feedback",
    "p-border": "Border Risk",
    "p-recovered": "Recovered / New",
    "p-healthy": "Healthy Fleet",
    "p-known": "Known Issues",
    "p-activity": "Recent Activity",
    "p-settings": "Settings",
    "p-users": "Manage Users",
    "p-priority": "Priority Overlap",
    "p-tsummary": "Tampering Summary",
    "p-tconfirmed": "Confirmed Cases",
    "p-tunconfirmed": "Unconfirmed Cases",
    "p-tquality": "Data Quality Log",
}

# Which data sections each panel needs to render. This is what makes
# the Roles & Visibility matrix the single source of truth: a section is
# stripped from a role's response only when that role can't open any
# panel that needs it. Before this, panel access and data filtering were
# two separate hardcoded lists, so granting a role a panel in the editor
# produced a visible-but-empty panel - the nav said yes, the payload
# still said no.
PANEL_SECTIONS = {
    "p-full":         {"full"},
    "p-border":       {"border"},
    "p-settings":     {"settingsRows"},
    "p-priority":     {"doubleFlagged"},
    "p-tsummary":     {"severityBands", "topVehicles", "tamperCards"},
    "p-tconfirmed":   {"tamperConfirmed"},
    "p-tunconfirmed": {"tamperUnconfirmed"},
    "p-tquality":     {"qualityLog"},
}

# Same idea for the KPI counters that belong to a specific panel: shown
# only if the role can open the panel those numbers come from.
PANEL_KPI_KEYS = {
    "p-border":       {"border"},
    "p-priority":     {"doubleFlagged"},
    "p-tsummary":     {"tamperGapsChecked", "nullGpsExcluded"},
    "p-tconfirmed":   {"tamperConfirmed"},
    "p-tunconfirmed": {"tamperUnconfirmed"},
}

# The raw xlsx blobs are never sent to anyone in the JSON payload,
# regardless of role or panel - /api/export/<name> serves them from disk
# on demand, gated separately by EXPORT_ACCESS.
NEVER_SENT_SECTIONS = {"xlsxB64", "tamperB64"}

_DEFAULT_PANEL_ACCESS = {
    "p-exec":          ("admin", "technician", "client"),
    "p-full":          ("admin", "technician"),
    "p-critical":      ("admin", "technician", "client"),   # client sees a reduced version, see below
    "p-pending":       ("admin", "technician", "client"),
    "p-border":        ("admin", "technician"),
    "p-recovered":     ("admin", "technician", "client"),
    "p-healthy":       ("admin", "technician", "client"),
    "p-known":         ("admin", "technician", "client"),   # explained-offline, no follow-up needed - client-facing by design
    "p-activity":      ("admin", "technician", "client"),   # global feedback feed - everyone should be able to see what's been reported and by whom
    "p-settings":      ("admin",),
    "p-users":         ("admin", "technician"),
    "p-priority":      ("admin", "technician"),
    "p-tsummary":      ("admin", "technician"),
    "p-tconfirmed":    ("admin", "technician"),
    "p-tunconfirmed":  ("admin", "technician"),
    "p-tquality":      ("admin", "technician"),
}

# Overrides saved from the Roles & Visibility editor live in their own
# file, never by rewriting this module - so the shipped defaults above
# stay readable and a bad save can always be undone by deleting the
# file. PANEL_ACCESS is rebuilt from defaults + overrides on import and
# again whenever the editor saves.
_OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), "database", "role_panels.json")


def _load_overrides():
    if not os.path.exists(_OVERRIDES_PATH):
        return {}
    try:
        with open(_OVERRIDES_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}  # unreadable/corrupt override file falls back to shipped defaults, never crashes login


def _build_panel_access():
    overrides = _load_overrides()
    access = {}
    for panel, default_roles in _DEFAULT_PANEL_ACCESS.items():
        roles = overrides.get(panel, list(default_roles))
        clean = [r for r in ROLES if r in roles]
        access[panel] = tuple(clean)
    return access


PANEL_ACCESS = _build_panel_access()


class LockoutError(ValueError):
    """Raised when a save would leave nobody able to reach this editor."""


def save_panel_access(new_access: dict):
    """
    Persists a full panel->roles map from the Roles & Visibility editor
    and reloads PANEL_ACCESS in place. Every panel is freely assignable
    to every role - the one refusal is stripping p-users from all three
    roles at once, which would make the editor permanently unreachable
    with no way back short of editing JSON on the server (and on Render's
    free tier there's no shell to do it from). Leaving it with at least
    one role keeps every other combination available.
    """
    cleaned = {
        panel: [r for r in ROLES if r in new_access.get(panel, [])]
        for panel in _DEFAULT_PANEL_ACCESS
    }
    if not cleaned.get("p-users"):
        raise LockoutError(
            "At least one role must keep Manage Users, otherwise nobody "
            "can reach this editor again to undo the change."
        )
    os.makedirs(os.path.dirname(_OVERRIDES_PATH), exist_ok=True)
    with open(_OVERRIDES_PATH, "w") as f:
        json.dump(cleaned, f, indent=2)
    PANEL_ACCESS.clear()
    PANEL_ACCESS.update(_build_panel_access())
    return PANEL_ACCESS


# Fields stripped from each row of "critical" and "full" data before
# a client-role response is built, even on panels they're allowed to
# open. Internal reasoning and cross-referenced tampering evidence
# never reaches a client response, full stop.
CLIENT_ROW_FIELD_BLOCKLIST = {"reasons", "borderDetail"}

EXPORT_ACCESS = {
    "integrity_xlsx": ("admin", "technician"),
    "tampering_xlsx": ("admin", "technician"),
}

# Who can create new accounts. Client role is external (GTL themselves)
# and should never be able to provision logins for this platform.
MANAGE_USERS_ROLES = ("admin", "technician")


def allowed_panels(role):
    return [p for p, roles in PANEL_ACCESS.items() if role in roles]


def blocked_sections_for(role):
    """Sections this role can't reach, derived from the panel matrix:
    a section is blocked only when no panel needing it is open to them."""
    open_panels = set(allowed_panels(role))
    blocked = set()
    for panel, sections in PANEL_SECTIONS.items():
        if panel not in open_panels:
            blocked |= sections
    # A section stays visible if ANY open panel needs it, so re-allow
    # anything another granted panel legitimately requires.
    for panel in open_panels:
        blocked -= PANEL_SECTIONS.get(panel, set())
    return blocked | NEVER_SENT_SECTIONS


def blocked_kpi_keys_for(role):
    open_panels = set(allowed_panels(role))
    blocked = set()
    for panel, keys in PANEL_KPI_KEYS.items():
        if panel not in open_panels:
            blocked |= keys
    for panel in open_panels:
        blocked -= PANEL_KPI_KEYS.get(panel, set())
    return blocked


def filter_payload_for_role(data: dict, role: str) -> dict:
    """Returns a NEW dict safe to serialize and send to this role.
    Never mutates the original. This is the one function every route
    must call before jsonify()-ing dashboard data.

    What gets stripped now follows the Roles & Visibility matrix rather
    than a separate hardcoded client list, so ticking a panel in the
    editor actually delivers that panel's data instead of rendering it
    empty. The raw xlsx blobs are the one unconditional exclusion."""
    filtered = {k: v for k, v in data.items() if k not in blocked_sections_for(role)}

    blocked_kpis = blocked_kpi_keys_for(role)
    if blocked_kpis and "kpi" in filtered:
        filtered["kpi"] = {k: v for k, v in filtered["kpi"].items() if k not in blocked_kpis}

    # Internal investigation reasoning stays out of client responses
    # regardless of panel grants - these are free-text notes your team
    # writes for itself, not a panel that can be handed over.
    if role == "client":
        for section_key in ("critical", "healthy", "pending", "criticalCards", "knownIssues", "full"):
            if section_key in filtered and isinstance(filtered[section_key], list):
                filtered[section_key] = [
                    {k: v for k, v in row.items() if k not in CLIENT_ROW_FIELD_BLOCKLIST}
                    for row in filtered[section_key]
                ]
    return filtered
