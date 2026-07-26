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

ROLES = ("admin", "technician", "client")

PANEL_ACCESS = {
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
    "p-priority":      ("admin", "technician"),
    "p-tsummary":      ("admin", "technician"),
    "p-tconfirmed":    ("admin", "technician"),
    "p-tunconfirmed":  ("admin", "technician"),
    "p-tquality":      ("admin", "technician"),
}

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
