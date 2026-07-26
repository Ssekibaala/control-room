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

# Panels that can never be granted to a client, no matter what someone
# ticks in the Roles editor. These carry tampering evidence and internal
# investigation notes - the whole reason filter_payload_for_role()
# exists. Making them toggleable would let a UI click undo the security
# boundary the rest of this file enforces, so the editor refuses them
# server-side rather than trusting the checkbox.
CLIENT_FORBIDDEN_PANELS = {
    "p-border", "p-priority", "p-tsummary", "p-tconfirmed",
    "p-tunconfirmed", "p-tquality", "p-settings", "p-users", "p-full",
}

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
        if panel in CLIENT_FORBIDDEN_PANELS:
            clean = [r for r in clean if r != "client"]
        # p-users is what grants access to this editor itself. If every
        # role loses it, nobody can ever get back in to fix it, so admin
        # is pinned on unconditionally.
        if panel == "p-users" and "admin" not in clean:
            clean.append("admin")
        access[panel] = tuple(clean)
    return access


PANEL_ACCESS = _build_panel_access()


def save_panel_access(new_access: dict):
    """
    Persists a full panel->roles map from the Roles & Visibility editor
    and reloads PANEL_ACCESS in place. Sanitising happens in
    _build_panel_access(), so whatever gets written here is re-checked
    against CLIENT_FORBIDDEN_PANELS on the way back out - a client can
    never end up with a tampering panel even if the request said so.
    """
    cleaned = {
        panel: [r for r in ROLES if r in new_access.get(panel, [])]
        for panel in _DEFAULT_PANEL_ACCESS
    }
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

# Top-level data sections never sent to a client role, regardless of
# panel visibility, this is the belt-and-suspenders check applied at
# serialization time, not just at nav-render time.
CLIENT_BLOCKED_SECTIONS = {
    "tamperConfirmed", "tamperUnconfirmed", "qualityLog", "severityBands",
    "topVehicles", "doubleFlagged", "tamperCards", "settingsRows",
    "border", "full", "xlsxB64", "tamperB64",
}

# Even inside sections a client IS allowed (like "kpi"), these specific
# keys are counts that would tell them tampering activity exists at
# all, even without case detail. Blocked too, same principle as the
# section-level list above: not client-facing until your team has
# verified it.
CLIENT_BLOCKED_KPI_KEYS = {
    "tamperConfirmed", "tamperUnconfirmed", "tamperGapsChecked",
    "nullGpsExcluded", "doubleFlagged", "border",
}

EXPORT_ACCESS = {
    "integrity_xlsx": ("admin", "technician"),
    "tampering_xlsx": ("admin", "technician"),
}

# Who can create new accounts. Client role is external (GTL themselves)
# and should never be able to provision logins for this platform.
MANAGE_USERS_ROLES = ("admin", "technician")


def allowed_panels(role):
    return [p for p, roles in PANEL_ACCESS.items() if role in roles]


def filter_payload_for_role(data: dict, role: str) -> dict:
    """Returns a NEW dict safe to serialize and send to this role.
    Never mutates the original. This is the one function every route
    must call before jsonify()-ing dashboard data."""
    if role == "admin" or role == "technician":
        return data

    filtered = {k: v for k, v in data.items() if k not in CLIENT_BLOCKED_SECTIONS}

    if "kpi" in filtered:
        filtered["kpi"] = {k: v for k, v in filtered["kpi"].items() if k not in CLIENT_BLOCKED_KPI_KEYS}

    for section_key in ("critical", "healthy", "pending", "criticalCards", "knownIssues"):
        if section_key in filtered and isinstance(filtered[section_key], list):
            filtered[section_key] = [
                {k: v for k, v in row.items() if k not in CLIENT_ROW_FIELD_BLOCKLIST}
                for row in filtered[section_key]
            ]
    return filtered
