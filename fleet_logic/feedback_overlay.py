"""
Applies human feedback to an already-built dashboard payload, at request
time.

WHY THIS EXISTS: the two inputs to this dashboard move at completely
different speeds. Telemetry arrives once a day by email import and is
slow by nature. Human judgement - a client saying "it's parked in the
workshop", a technician recording what they did - is immediate, and
waiting a full import cycle to see it makes the feature useless: an
asset explicitly marked "no follow-up needed" would keep sitting in
Priority Watch and Critical Assets until the next morning's run.

So classification still happens at import time against the telemetry,
and this module re-applies the human layer on top of the served payload
on every request. The rules here deliberately mirror classifier.py's
known-issue handling and control_room.py's bucketing, so the overlay
produces exactly what the next import will produce - "now" and "after
tomorrow's import" never disagree.
"""

SEVERITY_KEYS = ("Critical - Long-term Fault", "High - Escalate This Week", "Elevated - Monitor")


def _fmt_date(dt):
    return dt.strftime("%d %b %Y, %H:%M") if dt and getattr(dt, "year", 0) > 1 else ""


def _apply_to_row(row, entry):
    """Rewrites one asset row in place from its feedback entries. Mirrors
    classifier.py: an explicit requiresFollowup=False is what converts an
    asset into a Known Issue, and it is an explicit choice, never guessed
    from the wording of the comment."""
    latest = entry["latest"]
    fb = entry.get("latestFeedback")
    action_entry = entry.get("latestAction")

    if fb:
        # Status and comment are separate fields - see control_room.py's
        # _integrity_row(). The dashboard badges the status so the
        # client's words get the full width of the cell.
        row["feedback"] = fb["comment"]
        row["feedbackStatus"] = fb["status"]
        row["requiresFollowup"] = fb.get("requiresFollowup")
        row["reportedBy"] = fb.get("addedBy", "")
        row["feedbackDate"] = _fmt_date(fb.get("date"))

    # A technician's written instruction outranks the system's computed
    # suggestion - they've seen the asset, the rule engine hasn't.
    if action_entry:
        row["action"] = action_entry["comment"]
        row["actionBy"] = action_entry.get("addedBy", "")
        row["actionDate"] = _fmt_date(action_entry.get("date"))

    known_issue = latest.get("requiresFollowup") is False
    if known_issue:
        row["status"] = "Known Issue"
        row["severity"] = "No Follow-up Needed"
        row["border"] = "No"
        row["borderDetail"] = ""
        if not action_entry:
            who = latest.get("addedBy") or "the client"
            row["action"] = f"No action needed - {who} confirmed this is a known issue, no follow-up requested"
    elif latest.get("requiresFollowup") is True and row.get("status") == "Known Issue":
        # Reopened: a later entry asks for follow-up again, so the asset
        # goes back into the active queue rather than staying explained.
        row["status"] = "Technical Escalation"
        if row.get("severity") == "No Follow-up Needed":
            row["severity"] = "Elevated - Monitor"
    return row


def apply_feedback(data: dict, feedback: dict) -> dict:
    """
    Returns a NEW payload with feedback applied. Never mutates the input,
    because the caller holds the cached import result and a mutation
    would corrupt it for every later request.

    `feedback` is sheets_store.load_feedback()'s shape:
    {plate: {"latest", "latestFeedback", "latestAction", "history"}}.
    """
    if not feedback:
        return data

    out = dict(data)
    # "full" is the authoritative per-asset list; every other bucket is
    # derived from it, so rebuilding them from a single updated source
    # keeps them impossible to disagree with each other.
    full = [dict(r) for r in data.get("full", [])]
    by_plate = {}
    for row in full:
        entry = feedback.get(row.get("plate"))
        if entry:
            _apply_to_row(row, entry)
        by_plate[row["plate"]] = row

    def rows_with_status(status):
        return [r for r in full if r.get("status") == status]

    escalations = rows_with_status("Technical Escalation")
    pending = rows_with_status("Pending Customer Confirmation")
    known = rows_with_status("Known Issue")
    online = rows_with_status("Online")

    by_days = lambda rows: sorted(rows, key=lambda r: -(r.get("days") or 0))
    ranked_critical = by_days(escalations)

    out["full"] = full
    out["critical"] = ranked_critical
    out["criticalCards"] = ranked_critical[:6]
    out["pending"] = by_days(pending)
    out["knownIssues"] = by_days(known)
    out["healthy"] = sorted(online, key=lambda r: r.get("plate", ""))
    out["border"] = [r for r in full if r.get("border") == "Yes"]

    # Priority Overlap and the tampering tables carry their own row
    # shapes, but the integrity half of a doubleFlagged row comes from
    # the same asset - refresh it so a Known Issue stops being presented
    # as an active escalation there too.
    refreshed_double = []
    for row in data.get("doubleFlagged", []):
        updated = by_plate.get(row.get("plate"))
        merged = {**row, **{k: v for k, v in (updated or {}).items() if k in row}}
        if merged.get("status") != "Known Issue":
            refreshed_double.append(merged)
    out["doubleFlagged"] = refreshed_double

    sev_counts = {k: 0 for k in SEVERITY_KEYS}
    for r in escalations:
        sev = r.get("severity")
        if sev:
            sev_counts[sev] = sev_counts.get(sev, 0) + 1
    out["sevCounts"] = sev_counts

    kpi = dict(data.get("kpi", {}))
    total = len(full)
    kpi["total"] = total
    kpi["online"] = len(online)
    kpi["healthPct"] = round(len(online) / total * 100) if total else 0
    kpi["escalations"] = len(escalations)
    kpi["pending"] = len(pending)
    kpi["knownIssues"] = len(known)
    kpi["border"] = len(out["border"])
    kpi["doubleFlagged"] = len(refreshed_double)
    out["kpi"] = kpi

    return out
