"""
Control room v3. Sidebar navigation mirrors the two source workbooks
sheet-for-sheet, so switching tabs here feels exactly like switching
sheets in Excel, plus a cross-analysis layer neither workbook has on
its own. Real SVG charts (donut, grouped bars, horizontal bars), no
external chart library, no CDN dependency beyond the type family.

Export is one click: the actual .xlsx files (Fleet Integrity report
and Tampering report) are embedded as base64 inside this HTML and
downloaded byte-for-byte identical to the originals when the button
is clicked. No re-generation, no drift between what you see and what
you download.
"""

import json
import base64
from datetime import datetime

PLATFORM_ORDER = ["Teletrac", "MiX Unity", "FT Cloud Camera"]
PLATFORM_SHORT = {"Teletrac": "TLT", "MiX Unity": "MIX", "FT Cloud Camera": "CAM"}


def _platform_row(info):
    return {PLATFORM_SHORT[p]: info["platform_status"].get(p, ("N/A", None))[0] for p in PLATFORM_ORDER}


def _plural(n, unit):
    return f"{n} {unit} ago" if n == 1 else f"{n} {unit}s ago"


def _relative_age(ts, now):
    """
    Compact "how long ago" for a per-platform badge tooltip/subtext -
    minutes/hours/days/months, always relative to `now` (the same
    latest-timestamp-across-all-reports reference classify_fleet() used
    for days_silent, NOT wall-clock datetime.now() - so this stays
    consistent with every other "days offline" figure already on the
    dashboard rather than introducing a second, subtly different clock).
    Spelled-out units ("1 min ago", "1 day ago") rather than abbreviations
    ("1m ago", "1d ago") - unambiguous at a glance, no risk of reading
    "1mo" as "1m" in a narrow table cell.
    """
    if ts is None or now is None:
        return None
    seconds = (now - ts).total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return _plural(minutes, "min")
    hours = int(seconds // 3600)
    if hours < 24:
        return _plural(hours, "hour")
    days = int(seconds // 86400)
    if days < 30:
        return _plural(days, "day")
    months = int(days // 30)
    return _plural(months, "month")


def _format_last_position(platform_status, now=None):
    """
    Every asset already has a per-platform (status, datetime) pair computed
    by classifier.py. This just picks the most recent timestamp across
    platforms and formats it for display, absolute + relative. No new
    calculation, purely surfacing data that already existed but wasn't shown.
    """
    timestamps = [ts for (_, ts) in platform_status.values() if ts is not None]
    if not timestamps:
        return "No data", None, {}, {}
    latest = max(timestamps)
    per_platform = {
        PLATFORM_SHORT[p]: ts.strftime("%d %b %Y, %H:%M") if ts else "No data"
        for p, (_, ts) in platform_status.items()
    }
    per_platform_ago = {
        PLATFORM_SHORT[p]: _relative_age(ts, now) if ts else None
        for p, (_, ts) in platform_status.items()
    }
    return latest.strftime("%d %b %Y, %H:%M"), latest, per_platform, per_platform_ago


def _integrity_row(plate, info, now=None):
    border = info.get("border_detail")
    fb = info.get("feedback")
    last_position, last_position_dt, per_platform_seen, per_platform_ago = _format_last_position(
        info["platform_status"], now)
    return {
        "plate": plate, "status": info["status"], "severity": info["severity"], "days": info["days_silent"],
        **_platform_row(info),
        "lastPosition": last_position,
        "tltSeen": per_platform_seen.get("TLT", "No data"),
        "mixSeen": per_platform_seen.get("MIX", "No data"),
        "camSeen": per_platform_seen.get("CAM", "No data"),
        # Relative ("2h ago") companions to the absolute *Seen fields above -
        # None when that platform has no timestamp at all, distinct from a
        # genuinely stale one, so the frontend can render an em-dash instead
        # of a misleading "0m ago".
        "tltAgo": per_platform_ago.get("TLT"),
        "mixAgo": per_platform_ago.get("MIX"),
        "camAgo": per_platform_ago.get("CAM"),
        "border": "Yes" if info["border_flag"] else "No",
        "borderDetail": f"{border[0]} ({border[1]}) {border[2]}km" if border else "",
        # Status and comment stay SEPARATE fields. Concatenating them
        # ("Follow-up Requested: asset is...") meant a fixed prefix ate
        # the front of every cell and the truncation landed on the
        # client's actual words - the only part carrying information.
        # The dashboard renders the status as a compact badge instead.
        "feedback": fb["comment"] if fb else "",
        "feedbackStatus": fb["status"] if fb else "",
        "requiresFollowup": fb.get("requiresFollowup") if fb else None,
        "reportedBy": fb.get("addedBy", "") if fb else "",
        "feedbackDate": fb["date"].strftime("%d %b %Y, %H:%M") if fb and fb.get("date") else "",
        "action": info["action"], "reasons": "; ".join(info["reasons"]),
        "location": info.get("last_location") or "",
    }


def _tamper_row(c):
    return {
        "plate": c["Plate"], "vehicle": c.get("Vehicle", ""), "fleetNumber": c.get("FleetNumber", ""),
        "severity": c.get("Severity", ""), "arrivalDate": c.get("ArrivalDate", ""), "arrivalTime": c.get("ArrivalTime", ""),
        "atLocation": c.get("AtLocation", ""), "nextDate": c.get("NextDate", ""), "nextTime": c.get("NextTime", ""),
        "fromLocation": c.get("FromLocation", ""), "distanceKm": c.get("DistanceKm", 0),
        "gapDuration": c.get("GapDuration", ""), "impliedSpeed": c.get("ImpliedSpeedKmh", 0),
        "powerEvent": c.get("PowerEventInGap", ""),
    }


def _quality_row(q):
    return {
        "plate": q.get("Plate", ""), "vehicle": q.get("Vehicle", ""), "fleetNumber": q.get("FleetNumber", ""),
        "arrivalDate": q.get("ArrivalDate", ""), "arrivalTime": q.get("ArrivalTime", ""),
        "rawEnd": q.get("RawEndCoordinate", ""), "nextDate": q.get("NextDepartureDate", ""),
        "nextTime": q.get("NextDepartureTime", ""), "rawStart": q.get("RawStartCoordinate", ""),
    }


def _b64_file(path):
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except (FileNotFoundError, OSError):
        return None


def _build_data(results, settings, tampering, recovered, newly_offline, report_date, history_available,
                 xlsx_path, tamper_xlsx_path, checked_assets=None):
    online = [p for p, v in results.items() if v["status"] == "Online"]
    escalations = [p for p, v in results.items() if v["status"] == "Technical Escalation"]
    pending = [p for p, v in results.items() if v["status"] == "Pending Customer Confirmation"]
    # An explicit "no follow-up needed" from the client (classifier.py's
    # known_issue check) already keeps these out of escalations/pending
    # by construction (their status is "Known Issue", not "Technical
    # Escalation"), so they never inflate the active-work counts below.
    known_issues = [p for p, v in results.items() if v["status"] == "Known Issue"]
    border_plates = [p for p, v in results.items() if v["border_flag"]]

    sev_counts = {"Critical - Long-term Fault": 0, "High - Escalate This Week": 0, "Elevated - Monitor": 0}
    for p in escalations:
        sev_counts[results[p]["severity"]] = sev_counts.get(results[p]["severity"], 0) + 1

    confirmed = tampering.get("confirmed", [])
    unconfirmed = tampering.get("unconfirmed", [])
    tamper_summary = tampering.get("summary", {})
    severity_bands = tampering.get("severity_bands", [])
    top_vehicles = tampering.get("top_vehicles", [])
    quality_log = tampering.get("quality_log", [])

    tamper_plates_all = {c["Plate"] for c in confirmed} | {c["Plate"] for c in unconfirmed}
    double_flagged = []
    for p in tamper_plates_all:
        info = results.get(p)
        if info and info["status"] != "Online":
            n_confirmed = sum(1 for c in confirmed if c["Plate"] == p)
            n_unconfirmed = sum(1 for c in unconfirmed if c["Plate"] == p)
            double_flagged.append({**_integrity_row(p, info, report_date), "tamperConfirmed": n_confirmed, "tamperUnconfirmed": n_unconfirmed})
    double_flagged.sort(key=lambda r: (-r["tamperConfirmed"], -r["days"]))

    ranked_critical = sorted(escalations, key=lambda p: -results[p]["days_silent"])

    return {
        "meta": {
            "generated": report_date.strftime("%d %B %Y, %H:%M"),
            "offlineThreshold": settings["OFFLINE_THRESHOLD_DAYS"], "longTermFault": settings["LONG_TERM_FAULT_DAYS"],
            "borderRadius": settings["BORDER_RADIUS_KM"], "historyAvailable": history_available,
            "tamperPeriod": tamper_summary.get("period_label", ""),
            "settingsFile": settings.get("_path", ""), "ignoreDemoVehicles": settings["IGNORE_DEMO_VEHICLES"],
            "highPriorityDays": settings["HIGH_PRIORITY_DAYS"],
        },
        "kpi": {
            "total": len(results), "online": len(online),
            "healthPct": round(len(online) / len(results) * 100) if results else 0,
            "escalations": len(escalations), "pending": len(pending), "border": len(border_plates),
            "tamperConfirmed": len(confirmed), "tamperUnconfirmed": len(unconfirmed),
            "tamperGapsChecked": tamper_summary.get("gaps_checked", 0), "nullGpsExcluded": tamper_summary.get("null_gps_excluded", 0),
            "doubleFlagged": len(double_flagged), "recovered": len(recovered), "newlyOffline": len(newly_offline),
            "knownIssues": len(known_issues),
        },
        "sevCounts": sev_counts,
        "severityBands": severity_bands,
        "topVehicles": top_vehicles,
        "doubleFlagged": double_flagged,
        "criticalCards": [_integrity_row(p, results[p], report_date) for p in ranked_critical[:6]],
        "tamperCards": sorted(confirmed, key=lambda c: -c.get("DistanceKm", 0))[:6],
        "critical": [_integrity_row(p, results[p], report_date) for p in ranked_critical],
        "pending": [_integrity_row(p, results[p], report_date) for p in sorted(pending, key=lambda p: -results[p]["days_silent"])],
        "border": [_integrity_row(p, results[p], report_date) for p in border_plates],
        "healthy": [_integrity_row(p, results[p], report_date) for p in sorted(online)],
        "knownIssues": [_integrity_row(p, results[p], report_date) for p in sorted(known_issues, key=lambda p: -results[p]["days_silent"])],
        "full": [_integrity_row(p, results[p], report_date) for p in sorted(results.keys(), key=lambda p: -results[p]["days_silent"])],
        "recoveredList": [{"plate": p, "status": "Online"} for p in recovered],
        "newlyOfflineList": [{"plate": p, "status": results.get(p, {}).get("status", "")} for p in newly_offline],
        "tamperConfirmed": [_tamper_row(c) for c in sorted(confirmed, key=lambda c: -c.get("DistanceKm", 0))],
        "tamperUnconfirmed": [_tamper_row(c) for c in sorted(unconfirmed, key=lambda c: -c.get("DistanceKm", 0))],
        "qualityLog": [_quality_row(q) for q in quality_log],
        # Every physical-check record (sheets_store.load_tamper_checks(),
        # flattened by run_import.py's _checked_assets_summary()) - shown
        # in its own dashboard section precisely BECAUSE these gaps are
        # already excluded from tamperConfirmed/tamperUnconfirmed above,
        # not folded back into them.
        "tamperChecked": checked_assets or [],
        "recovered": recovered, "newlyOffline": newly_offline,
        "settingsRows": [
            {"setting": "Offline Threshold (days)", "value": settings["OFFLINE_THRESHOLD_DAYS"]},
            {"setting": "High Priority Threshold (days)", "value": settings["HIGH_PRIORITY_DAYS"]},
            {"setting": "Long-term Fault Threshold (days)", "value": settings["LONG_TERM_FAULT_DAYS"]},
            {"setting": "Border Radius (km)", "value": settings["BORDER_RADIUS_KM"]},
            {"setting": "Ignore Demo Vehicles", "value": settings["IGNORE_DEMO_VEHICLES"]},
        ],
        "xlsxB64": _b64_file(xlsx_path),
        "tamperB64": _b64_file(tamper_xlsx_path),
    }




def build_dashboard(results, settings, tampering, recovered, newly_offline, report_date=None,
                     history_available=True, xlsx_path=None, tamper_xlsx_path=None):
    report_date = report_date or datetime.now()
    data = _build_data(results, settings, tampering, recovered, newly_offline, report_date,
                        history_available, xlsx_path, tamper_xlsx_path)
    data_json = json.dumps(data)

    html = _HTML_TEMPLATE
    html = html.replace("__CSS__", _css())
    html = html.replace("__JS__", _js())
    html = html.replace("__DATA_JSON__", data_json)
    html = html.replace("__GENERATED__", data["meta"]["generated"])
    html = html.replace("__HEALTHPCT__", str(data["kpi"]["healthPct"]))
    html = html.replace("__TOTAL__", str(data["kpi"]["total"]))
    html = html.replace("__ONLINE__", str(data["kpi"]["online"]))
    html = html.replace("__ESCALATIONS__", str(data["kpi"]["escalations"]))
    html = html.replace("__PENDING__", str(data["kpi"]["pending"]))
    html = html.replace("__BORDER__", str(data["kpi"]["border"]))
    html = html.replace("__TAMPERCONF__", str(data["kpi"]["tamperConfirmed"]))
    html = html.replace("__TAMPERUNCONF__", str(data["kpi"]["tamperUnconfirmed"]))
    html = html.replace("__DOUBLEFLAGGED__", str(data["kpi"]["doubleFlagged"]))
    html = html.replace("__RECOVERED__", str(data["kpi"]["recovered"]))
    html = html.replace("__NEWLYOFFLINE__", str(data["kpi"]["newlyOffline"]))
    html = html.replace("__GAPSCHECKED__", f'{data["kpi"]["tamperGapsChecked"]:,}')
    html = html.replace("__NULLGPS__", str(data["kpi"]["nullGpsExcluded"]))
    html = html.replace("__TAMPERPERIOD__", data["meta"]["tamperPeriod"])
    html = html.replace("__OFFLINETHRESH__", str(settings["OFFLINE_THRESHOLD_DAYS"]))
    html = html.replace("__LONGTERM__", str(settings["LONG_TERM_FAULT_DAYS"]))
    html = html.replace("__BORDERRADIUS__", f'{settings["BORDER_RADIUS_KM"]:.0f}')
    html = html.replace("__QUALITYCOUNT__", str(len(data["qualityLog"])))
    notif_count = data["kpi"]["escalations"] + data["kpi"]["border"] + data["kpi"]["doubleFlagged"]
    html = html.replace("__NOTIFCOUNT__", str(notif_count))
    return html


def write_dashboard(results, settings, tampering, recovered, newly_offline, output_path, report_date=None,
                     history_available=True, xlsx_path=None, tamper_xlsx_path=None):
    html = build_dashboard(results, settings, tampering, recovered, newly_offline, report_date,
                            history_available, xlsx_path, tamper_xlsx_path)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GTL Fleet Intelligence Command Centre</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Manrope:wght@600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__CSS__</style>
</head>
<body>

<div class="app" id="app">

  <aside class="sidebar" id="sidebar">
    <div class="sidebar-top">
      <button class="collapse-btn" id="collapse-btn" title="Collapse sidebar" aria-label="Collapse sidebar">
        <svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>
      </button>
      <div class="sidebar-brand">
        <div class="brand-mark">GTL</div>
        <div class="brand-text">
          <div class="brand-name">Fleet Intelligence</div>
          <div class="brand-sub">Command Centre</div>
        </div>
      </div>
    </div>

    <div class="quick-stats">
      <div class="qs-item"><span class="qs-num" id="qs-health">__HEALTHPCT__%</span><span class="qs-label">Health</span></div>
      <div class="qs-item"><span class="qs-num" id="qs-total">__TOTAL__</span><span class="qs-label">Assets</span></div>
      <div class="qs-item warn"><span class="qs-num" id="qs-crit">__ESCALATIONS__</span><span class="qs-label">Critical</span></div>
    </div>

    <div class="nav-scroll">
      <div class="nav-group-label">Fleet Integrity</div>
      <nav class="nav-group">
        <button class="nav-item active" data-panel="p-exec" data-label="Executive Dashboard">
          <svg class="nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>
          <span>Executive Dashboard</span>
        </button>
        <button class="nav-item" data-panel="p-full" data-label="Full Data">
          <svg class="nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/></svg>
          <span>Full Data</span><span class="nav-count">__TOTAL__</span>
        </button>
        <button class="nav-item" data-panel="p-critical" data-label="Critical Assets">
          <svg class="nav-icon" viewBox="0 0 24 24"><path d="M12 2 2 20h20z"/><path d="M12 9v5M12 17h.01"/></svg>
          <span>Critical Assets</span><span class="nav-count warn">__ESCALATIONS__</span>
        </button>
        <button class="nav-item" data-panel="p-pending" data-label="Pending Customer Feedback">
          <svg class="nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
          <span>Pending Feedback</span><span class="nav-count">__PENDING__</span>
        </button>
        <button class="nav-item" data-panel="p-border" data-label="Border Risk">
          <svg class="nav-icon" viewBox="0 0 24 24"><path d="M12 2 2 7l10 5 10-5z"/><path d="M2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
          <span>Border Risk</span><span class="nav-count danger">__BORDER__</span>
        </button>
        <button class="nav-item" data-panel="p-recovered" data-label="Recovered Since Yesterday">
          <svg class="nav-icon" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 3v5h5"/></svg>
          <span>Recovered / New</span><span class="nav-count good">__RECOVERED__</span>
        </button>
        <button class="nav-item" data-panel="p-healthy" data-label="Healthy Fleet">
          <svg class="nav-icon" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>
          <span>Healthy Fleet</span><span class="nav-count good">__ONLINE__</span>
        </button>
        <button class="nav-item" data-panel="p-settings" data-label="Settings">
          <svg class="nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>
          <span>Settings</span>
        </button>
      </nav>

      <div class="nav-group-label">Cross-Analysis</div>
      <nav class="nav-group">
        <button class="nav-item priority-item" data-panel="p-priority" data-label="Priority Overlap">
          <svg class="nav-icon" viewBox="0 0 24 24"><path d="m12 2 2.4 7.4H22l-6.2 4.5 2.4 7.4L12 16.8l-6.2 4.5 2.4-7.4L2 9.4h7.6z"/></svg>
          <span>Priority Overlap</span><span class="nav-count danger">__DOUBLEFLAGGED__</span>
        </button>
      </nav>

      <div class="nav-group-label">Device Tampering Risk Report</div>
      <nav class="nav-group">
        <button class="nav-item" data-panel="p-tsummary" data-label="Tampering Summary">
          <svg class="nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>
          <span>Summary</span>
        </button>
        <button class="nav-item" data-panel="p-tconfirmed" data-label="Confirmed Tampering Cases">
          <svg class="nav-icon" viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 2.5 18a2 2 0 0 0 1.7 3h15.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>
          <span>Confirmed Cases</span><span class="nav-count danger">__TAMPERCONF__</span>
        </button>
        <button class="nav-item" data-panel="p-tunconfirmed" data-label="Unconfirmed Tampering Cases">
          <svg class="nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2-3 4M12 17h.01"/></svg>
          <span>Unconfirmed Cases</span><span class="nav-count warn">__TAMPERUNCONF__</span>
        </button>
        <button class="nav-item" data-panel="p-tquality" data-label="Data Quality Log">
          <svg class="nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
          <span>Data Quality Log</span><span class="nav-count">__QUALITYCOUNT__</span>
        </button>
      </nav>

      <div class="nav-group-label" id="recent-label" style="display:none">Recently Viewed</div>
      <nav class="nav-group" id="recent-group"></nav>
    </div>

    <div class="sidebar-foot">
      <div class="status-line"><span class="status-dot"></span> System Nominal</div>
    </div>
  </aside>

  <div class="sidebar-backdrop" id="sidebar-backdrop"></div>

  <div class="main">
    <header class="topbar">
      <div class="topbar-left">
        <button class="hamburger-btn" id="hamburger-btn" aria-label="Open menu">
          <svg viewBox="0 0 24 24"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
        </button>
        <div>
          <h1 id="panel-title">Executive Dashboard</h1>
          <span id="panel-sub" class="panel-sub">Cross-platform fleet health overview</span>
        </div>
      </div>
      <div class="topbar-right">
        <div class="header-pill live-pill"><span class="pulse-dot"></span>LIVE</div>
        <div class="header-pill" id="refresh-pill">Updated __GENERATED__</div>
        <div class="search-box">
          <svg viewBox="0 0 24 24" class="search-icon"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
          <input type="text" id="global-search" placeholder="Search plate, location, action...">
        </div>
        <button class="icon-btn" id="notif-btn" title="Active alerts">
          <svg viewBox="0 0 24 24"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>
          <span class="notif-badge" id="notif-badge">__NOTIFCOUNT__</span>
        </button>
        <button class="theme-toggle" id="theme-toggle" title="Toggle light / dark mode" aria-label="Toggle theme">
          <div class="knob"><svg id="theme-icon" viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36A5.4 5.4 0 0 1 12 3z"/></svg></div>
        </button>
        <button class="icon-btn" id="autotour-btn" title="Toggle auto-tour (cycles panels, scrolls tables)">
          <svg id="autotour-icon" viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg>
        </button>
        <button class="speed-btn" id="tour-speed-btn" title="Auto-tour scroll speed, click to change">
          <span id="tour-speed-label">1x</span>
        </button>
        <div class="export-wrap">
          <button class="btn-primary" id="export-toggle">Export <svg viewBox="0 0 24 24" class="chev"><path d="m6 9 6 6 6-6"/></svg></button>
          <div class="export-menu" id="export-menu">
            <button id="export-integrity">Fleet Integrity Report (.xlsx)</button>
            <button id="export-tamper">Tampering Risk Report (.xlsx)</button>
          </div>
        </div>
        <button class="icon-btn" id="fullscreen-btn" title="Toggle full screen">
          <svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>
        </button>
        <div class="avatar" title="GTL Operations">GTL</div>
      </div>
    </header>
    <div class="tour-progress" id="tour-progress"><div class="tour-progress-fill" id="tour-progress-fill"></div></div>

    <main class="content">

      <section id="p-exec" class="panel active">
        <div class="kpi-row">
          <div class="kpi-card primary">
            <div class="kpi-head"><span class="kpi-label">Fleet Health</span><span class="kpi-status-pill good">Nominal</span></div>
            <div class="kpi-main"><span class="kpi-num" data-count="__HEALTHPCT__">0</span><span class="kpi-unit">%</span></div>
            <div class="kpi-foot" id="kpi-health-foot">First day of monitoring</div>
          </div>
          <div class="kpi-card clickable" data-goto="p-full" data-goto-label="Full Data">
            <div class="kpi-head"><span class="kpi-label">Total Assets</span></div>
            <div class="kpi-main"><span class="kpi-num" data-count="__TOTAL__">0</span></div>
            <div class="kpi-foot muted">Across Teletrac, MiX Unity, FT Cloud</div>
            <div class="kpi-goto">View all assets <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></div>
          </div>
          <div class="kpi-card success clickable" data-goto="p-healthy" data-goto-label="Healthy Fleet">
            <div class="kpi-head"><span class="kpi-label">Online</span><span class="kpi-status-pill good">Healthy</span></div>
            <div class="kpi-main"><span class="kpi-num" data-count="__ONLINE__">0</span></div>
            <div class="kpi-foot" id="kpi-online-foot">&mdash;</div>
            <div class="kpi-goto">View healthy fleet <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></div>
          </div>
          <div class="kpi-card warning clickable" data-goto="p-critical" data-goto-label="Critical Assets">
            <div class="kpi-head"><span class="kpi-label">Technical Escalation</span><span class="kpi-status-pill warn">Action Needed</span></div>
            <div class="kpi-main"><span class="kpi-num" data-count="__ESCALATIONS__">0</span></div>
            <div class="kpi-foot muted">Ranked in Critical Assets</div>
            <div class="kpi-goto">View the __ESCALATIONS__ assets <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></div>
          </div>
          <div class="kpi-card danger clickable" data-goto="p-border" data-goto-label="Border Risk">
            <div class="kpi-head"><span class="kpi-label">Border Risk</span><span class="kpi-status-pill danger">Investigate</span></div>
            <div class="kpi-main"><span class="kpi-num" data-count="__BORDER__">0</span></div>
            <div class="kpi-foot muted">Offline within radius of a crossing</div>
            <div class="kpi-goto">View flagged assets <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></div>
          </div>
          <div class="kpi-card investigation clickable" data-goto="p-priority" data-goto-label="Priority Overlap">
            <div class="kpi-head"><span class="kpi-label">Priority Overlap</span><span class="kpi-status-pill investigation">Confirmed</span></div>
            <div class="kpi-main"><span class="kpi-num" data-count="__DOUBLEFLAGGED__">0</span></div>
            <div class="kpi-foot muted">Integrity + tampering evidence</div>
            <div class="kpi-goto">View the evidence <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></div>
          </div>
        </div>

        <div class="grid-row">
          <div class="panel-card">
            <div class="panel-card-head"><h3>Fleet Health</h3></div>
            <div class="gauge-wrap">
              <svg viewBox="0 0 200 200" class="radial">
                <defs>
                  <linearGradient id="healthGradient" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" stop-color="#0A84FF"/>
                    <stop offset="100%" stop-color="#BF5AF2"/>
                  </linearGradient>
                </defs>
                <circle cx="100" cy="100" r="82" class="radial-track"/>
                <circle cx="100" cy="100" r="82" class="radial-fill" id="radial-health"/>
              </svg>
              <div class="radial-center">
                <div class="radial-num" id="health-num">0%</div>
                <div class="radial-label">HEALTHY</div>
              </div>
            </div>
          </div>
          <div class="panel-card">
            <div class="panel-card-head"><h3>Fleet Status Breakdown</h3></div>
            <div class="chart-body" id="chart-donut"></div>
          </div>
          <div class="panel-card">
            <div class="panel-card-head"><h3>Escalation Severity</h3></div>
            <div class="chart-body" id="chart-severity"></div>
          </div>
          <div class="panel-card">
            <div class="panel-card-head"><h3>Day-over-Day</h3></div>
            <div class="chart-body" id="chart-dayover"></div>
          </div>
        </div>

        <div class="section-head"><h2>Priority Watch</h2><span>Worst integrity cases, ranked</span></div>
        <div class="card-grid" id="critical-cards"></div>

        <div class="section-head"><h2>Tampering Alerts</h2><span>Confirmed location gaps with a power event logged</span></div>
        <div class="card-grid route-grid" id="tamper-cards"></div>
      </section>

      <section id="p-full" class="panel">
        <div class="grid-toolbar"><h3>Full Fleet Data</h3><span class="row-count" id="full-count"></span></div>
        <div class="table-wrap"><table id="full-table"></table></div>
      </section>

      <section id="p-critical" class="panel">
        <div class="grid-toolbar"><h3>Critical Assets</h3><span class="row-count" id="critical-count"></span></div>
        <div class="table-wrap"><table id="critical-table"></table></div>
      </section>

      <section id="p-pending" class="panel">
        <div class="grid-toolbar"><h3>Pending Customer Feedback</h3><span class="row-count" id="pending-count"></span></div>
        <div class="table-wrap"><table id="pending-table"></table></div>
      </section>

      <section id="p-border" class="panel">
        <div class="grid-toolbar"><h3>Border Risk</h3><span class="row-count" id="border-count"></span></div>
        <div class="table-wrap"><table id="border-table"></table></div>
      </section>

      <section id="p-recovered" class="panel">
        <div class="two-col">
          <div>
            <div class="grid-toolbar"><h3 class="good-text">Recovered Since Yesterday</h3></div>
            <div class="table-wrap"><table id="recovered-table"></table></div>
          </div>
          <div>
            <div class="grid-toolbar"><h3 class="warn-text">Newly Offline Since Yesterday</h3></div>
            <div class="table-wrap"><table id="newlyoffline-table"></table></div>
          </div>
        </div>
      </section>

      <section id="p-healthy" class="panel">
        <div class="grid-toolbar"><h3>Healthy Fleet</h3><span class="row-count" id="healthy-count"></span></div>
        <div class="table-wrap"><table id="healthy-table"></table></div>
      </section>

      <section id="p-settings" class="panel">
        <div class="panel-card">
          <div class="panel-card-head"><h3>Report Configuration</h3></div>
          <table id="settings-table"></table>
          <p class="settings-note">Edit settings.ini next to the EXE and re-run to change these values. No code changes needed.</p>
        </div>
      </section>

      <section id="p-priority" class="panel">
        <div class="callout investigation">
          <strong id="priority-callout-num">0 asset(s)</strong> appear in <em>both</em> datasets independently: currently
          offline or pending in the integrity check, <em>and</em> caught relocating without being tracked in the
          tampering analysis. This overlap is stronger evidence than either report gives alone.
        </div>
        <div class="grid-toolbar"><h3>Priority Overlap</h3></div>
        <div class="table-wrap"><table id="priority-table"></table></div>
      </section>

      <section id="p-tsummary" class="panel">
        <div class="kpi-row tamper-kpis">
          <div class="kpi-card"><div class="kpi-head"><span class="kpi-label">Trip Gaps Checked</span></div><div class="kpi-main"><span class="kpi-num" data-count="__GAPSCHECKED__">0</span></div></div>
          <div class="kpi-card warning clickable" data-goto="p-tconfirmed" data-goto-label="Confirmed Tampering Cases"><div class="kpi-head"><span class="kpi-label">Location Mismatches</span></div><div class="kpi-main"><span class="kpi-num" data-count-raw="mismatches">0</span></div><div class="kpi-goto">View mismatches <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></div></div>
          <div class="kpi-card danger clickable" data-goto="p-tconfirmed" data-goto-label="Confirmed Tampering Cases"><div class="kpi-head"><span class="kpi-label">Confirmed</span></div><div class="kpi-main"><span class="kpi-num" data-count="__TAMPERCONF__">0</span></div><div class="kpi-goto">View cases <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></div></div>
          <div class="kpi-card clickable" data-goto="p-tunconfirmed" data-goto-label="Unconfirmed Tampering Cases"><div class="kpi-head"><span class="kpi-label">Unconfirmed</span></div><div class="kpi-main"><span class="kpi-num" data-count="__TAMPERUNCONF__">0</span></div><div class="kpi-goto">View cases <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></div></div>
          <div class="kpi-card clickable" data-goto="p-tquality" data-goto-label="Data Quality Log"><div class="kpi-head"><span class="kpi-label">Null GPS Excluded</span></div><div class="kpi-main"><span class="kpi-num" data-count="__NULLGPS__">0</span></div><div class="kpi-goto">View log <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></div></div>
        </div>
        <p class="period-label">__TAMPERPERIOD__</p>
        <div class="grid-row">
          <div class="panel-card wide">
            <div class="panel-card-head"><h3>Severity Bands &mdash; Confirmed vs Unconfirmed</h3></div>
            <div class="chart-body" id="chart-sevbands"></div>
          </div>
          <div class="panel-card wide">
            <div class="panel-card-head"><h3>Top Vehicles &mdash; Confirmed Cases</h3></div>
            <div class="chart-body" id="chart-topvehicles"></div>
          </div>
        </div>
        <div class="panel-card">
          <div class="panel-card-head"><h3>Method</h3></div>
          <ol class="method-list">
            <li>Every trip's arrival location is compared to the departure location of that vehicle's next trip.</li>
            <li>If the two points are more than 2 km apart, the vehicle relocated without a trip being logged.</li>
            <li>Each flagged gap is checked against Power Disconnect / Power Reconnect events in the same window.</li>
            <li><b>Confirmed:</b> a power event was logged in the gap window, strongest evidence.</li>
            <li><b>Unconfirmed:</b> no power event logged, vehicle still moved untracked, needs a field check.</li>
          </ol>
        </div>
      </section>

      <section id="p-tconfirmed" class="panel">
        <div class="grid-toolbar"><h3>Confirmed Tampering Cases</h3><span class="row-count" id="tconfirmed-count"></span></div>
        <div class="table-wrap"><table id="tconfirmed-table"></table></div>
      </section>

      <section id="p-tunconfirmed" class="panel">
        <div class="grid-toolbar"><h3>Unconfirmed Tampering Cases</h3><span class="row-count" id="tunconfirmed-count"></span></div>
        <div class="table-wrap"><table id="tunconfirmed-table"></table></div>
      </section>

      <section id="p-tquality" class="panel">
        <div class="callout muted">__QUALITYCOUNT__ trip boundaries excluded because one side reported a null (0,0) GPS coordinate, a device placeholder for "no signal", not a real position.</div>
        <div class="grid-toolbar"><h3>Data Quality Log</h3></div>
        <div class="table-wrap"><table id="tquality-table"></table></div>
      </section>

    </main>
  </div>
</div>

<div class="ticker-wrap"><div class="ticker" id="ticker"></div></div>

<script>
const DATA = __DATA_JSON__;
__JS__
</script>

</body>
</html>"""


def _css():
    return """
  /* ============ DESIGN TOKENS: DARK (default) ============ */
  :root {
    --bg:#05050A; --surface:#0D0D14; --header-bg:rgba(13,13,20,0.72); --sidebar-bg:rgba(10,10,16,0.82);
    --panel-bg:#12121C; --card-bg:#161622; --card-bg-2:#1C1C2C; --border:rgba(255,255,255,0.10); --divider:rgba(255,255,255,0.08);
    --text-primary:#FAFAFC; --text-secondary:rgba(250,250,252,0.66); --text-tertiary:rgba(250,250,252,0.42);
    --hover-surface:rgba(255,255,255,0.07);

    --primary:#2E8CFF; --primary-2:#7B61FF; --primary-hover:#59A6FF; --primary-surface:rgba(46,140,255,0.18);
    --success:#2EE6A6; --success-surface:rgba(46,230,166,0.16);
    --danger:#FF5C7A; --danger-2:#FF3D71; --danger-surface:rgba(255,92,122,0.18);
    --warning:#FFB020; --warning-surface:rgba(255,176,32,0.18);
    --info:#4FC3FF; --info-surface:rgba(79,195,255,0.16);
    --investigation:#C563FF; --investigation-2:#7B61FF; --investigation-surface:rgba(197,99,255,0.18);

    --grad-primary: linear-gradient(135deg, var(--primary), var(--primary-2));
    --grad-danger: linear-gradient(135deg, var(--danger), var(--danger-2));
    --grad-invest: linear-gradient(135deg, var(--investigation), var(--investigation-2));
    --grad-success: linear-gradient(135deg, #2EE6A6, #2E8CFF);
    --grad-warm: linear-gradient(135deg, #FFB020, #FF5C7A);

    --shadow-1: 0 1px 2px rgba(0,0,0,0.55), 0 1px 1px rgba(0,0,0,0.35);
    --shadow-2: 0 10px 28px rgba(0,0,0,0.5), 0 3px 8px rgba(0,0,0,0.35);
    --shadow-3: 0 28px 70px rgba(0,0,0,0.6);
    --blur: blur(26px) saturate(1.9);

    --sp-1:8px; --sp-2:16px; --sp-3:24px; --sp-4:32px; --sp-6:48px;
    --sidebar-w:284px; --sidebar-w-collapsed:78px;
    --radius-sm:10px; --radius-md:14px; --radius-lg:20px;

    color-scheme: dark;
  }

  /* ============ DESIGN TOKENS: LIGHT ============ */
  :root[data-theme="light"] {
    --bg:#F2F2F5; --surface:#FFFFFF; --header-bg:rgba(255,255,255,0.78); --sidebar-bg:rgba(255,255,255,0.86);
    --panel-bg:#FFFFFF; --card-bg:#FFFFFF; --border:rgba(0,0,0,0.08); --divider:rgba(0,0,0,0.06);
    --text-primary:#1D1D1F; --text-secondary:rgba(29,29,31,0.62); --text-tertiary:rgba(29,29,31,0.42);
    --hover-surface:rgba(0,0,0,0.035);

    --primary:#007AFF; --primary-2:#5856D6; --primary-hover:#0064D6; --primary-surface:rgba(0,122,255,0.10);
    --success:#248A3D; --success-surface:rgba(36,138,61,0.10);
    --danger:#D70015; --danger-2:#FF375F; --danger-surface:rgba(215,0,21,0.09);
    --warning:#C93400; --warning-surface:rgba(201,52,0,0.09);
    --info:#0071A4; --info-surface:rgba(0,113,164,0.09);
    --investigation:#8944AB; --investigation-2:#5856D6; --investigation-surface:rgba(137,68,171,0.10);

    --shadow-1: 0 1px 2px rgba(0,0,0,0.06), 0 1px 1px rgba(0,0,0,0.04);
    --shadow-2: 0 8px 24px rgba(0,0,0,0.08), 0 2px 6px rgba(0,0,0,0.05);
    --shadow-3: 0 24px 60px rgba(0,0,0,0.14);
    color-scheme: light;
  }

  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { height:100%; }
  body {
    background: var(--bg); color:var(--text-primary);
    font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Inter',sans-serif;
    font-size:15px; line-height:1.5; overflow-x:hidden; -webkit-font-smoothing:antialiased;
    transition: background 0.35s ease, color 0.35s ease;
    background-image:
      radial-gradient(circle at 8% -8%, rgba(46,140,255,0.22), transparent 42%),
      radial-gradient(circle at 92% 4%, rgba(197,99,255,0.18), transparent 40%),
      radial-gradient(circle at 50% 100%, rgba(46,230,166,0.08), transparent 45%);
    background-attachment: fixed;
    background-size: 140% 140%;
    animation: meshShift 30s ease-in-out infinite alternate;
  }
  @keyframes meshShift {
    0% { background-position: 0% 0%, 100% 0%, 50% 100%; }
    100% { background-position: 6% 4%, 94% 6%, 50% 92%; }
  }
  :root[data-theme="light"] body { animation:none; background-image:none; }
  h1,h2,h3 { font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Manrope',sans-serif; font-weight:700; letter-spacing:-0.015em; }
  ::-webkit-scrollbar { width:8px; height:8px; }
  ::-webkit-scrollbar-thumb { background:var(--border); border-radius:6px; }
  ::-webkit-scrollbar-track { background:transparent; }
  * { transition: background-color 0.3s ease, border-color 0.3s ease, color 0.3s ease; }

  .app { display:flex; min-height:100vh; }

  /* ============ SIDEBAR ============ */
  .sidebar {
    width:var(--sidebar-w); flex-shrink:0; background:var(--sidebar-bg); backdrop-filter:var(--blur); -webkit-backdrop-filter:var(--blur);
    border-right:1px solid var(--border); display:flex; flex-direction:column; position:sticky; top:0; height:100vh;
    transition:width 0.3s cubic-bezier(.16,1,.3,1);
  }
  .sidebar.collapsed { width:var(--sidebar-w-collapsed); }
  .sidebar.collapsed .brand-text, .sidebar.collapsed .nav-item span:not(.nav-count),
  .sidebar.collapsed .nav-group-label, .sidebar.collapsed .quick-stats, .sidebar.collapsed .status-line span:last-child {
    display:none;
  }
  .sidebar.collapsed .collapse-btn svg { transform:rotate(180deg); }

  .sidebar-top { display:flex; align-items:center; gap:var(--sp-1); padding:18px var(--sp-2); border-bottom:1px solid var(--divider); }
  .collapse-btn { background:var(--hover-surface); border:1px solid var(--border); border-radius:9px; width:28px; height:28px;
    display:flex; align-items:center; justify-content:center; cursor:pointer; color:var(--text-secondary); flex-shrink:0; }
  .collapse-btn svg { width:14px; height:14px; fill:none; stroke:currentColor; stroke-width:2; transition:transform 0.3s; }
  .collapse-btn:hover { color:var(--text-primary); }
  .sidebar-brand { display:flex; align-items:center; gap:10px; overflow:hidden; }
  .brand-mark { width:34px; height:34px; border-radius:10px; background:var(--grad-primary); color:#fff;
    display:flex; align-items:center; justify-content:center; font-family:'JetBrains Mono'; font-weight:700; font-size:11px; flex-shrink:0;
    box-shadow: 0 4px 14px rgba(10,132,255,0.35); }
  .brand-name { font-family:-apple-system,'Manrope'; font-weight:700; font-size:14px; white-space:nowrap; }
  .brand-sub { font-size:11px; color:var(--text-tertiary); white-space:nowrap; }

  .quick-stats { display:flex; justify-content:space-around; padding:var(--sp-2); border-bottom:1px solid var(--divider); }
  .qs-item { display:flex; flex-direction:column; align-items:center; gap:2px; }
  .qs-num { font-family:'JetBrains Mono'; font-weight:700; font-size:17px; color:var(--text-primary); }
  .qs-item.warn .qs-num { color:var(--warning); }
  .qs-label { font-size:10px; color:var(--text-tertiary); text-transform:uppercase; letter-spacing:0.4px; }

  .nav-scroll { flex:1; overflow-y:auto; padding:var(--sp-1) var(--sp-1) var(--sp-2); }
  .nav-group-label { font-size:10.5px; text-transform:uppercase; letter-spacing:1px; color:var(--text-tertiary);
    font-weight:600; padding:var(--sp-2) var(--sp-2) 6px; }
  .nav-group { display:flex; flex-direction:column; gap:2px; padding:0 var(--sp-1); }
  .nav-item {
    display:flex; align-items:center; gap:10px; width:100%; text-align:left; background:none; border:none;
    color:var(--text-secondary); padding:10px 11px; border-radius:11px; font-size:13.5px; font-weight:500;
    cursor:pointer; white-space:nowrap; overflow:hidden;
  }
  .nav-item span:first-of-type { overflow:hidden; text-overflow:ellipsis; }
  .nav-item:hover { background:var(--hover-surface); color:var(--text-primary); }
  .nav-item.active { background:var(--grad-primary); color:#fff; font-weight:600; box-shadow:0 4px 14px rgba(10,132,255,0.3); }
  .nav-item.priority-item.active { background:var(--grad-danger); box-shadow:0 4px 14px rgba(255,69,58,0.3); }
  .nav-item.active .nav-count { background:rgba(255,255,255,0.25); color:#fff; }
  .nav-icon { width:16px; height:16px; flex-shrink:0; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
  .nav-count { margin-left:auto; font-family:'JetBrains Mono'; font-size:10.5px; background:var(--hover-surface);
    color:var(--text-secondary); padding:1px 7px; border-radius:8px; flex-shrink:0; }
  .nav-count.good { background:var(--success-surface); color:var(--success); }
  .nav-count.warn { background:var(--warning-surface); color:var(--warning); }
  .nav-count.danger { background:var(--danger-surface); color:var(--danger); }

  .sidebar-foot { padding:var(--sp-2); border-top:1px solid var(--divider); }
  .status-line { display:flex; align-items:center; gap:7px; font-size:11.5px; color:var(--success); font-weight:600; }
  .status-dot { width:7px; height:7px; border-radius:50%; background:var(--success); box-shadow:0 0 0 3px var(--success-surface); animation:blink 2s ease-in-out infinite; }
  @keyframes blink { 0%,100%{opacity:1;} 50%{opacity:0.4;} }

  /* ============ MAIN / HEADER ============ */
  .main { flex:1; min-width:0; display:flex; flex-direction:column; }
  .topbar {
    display:flex; justify-content:space-between; align-items:center; gap:var(--sp-3);
    padding:14px var(--sp-3); background:var(--header-bg); backdrop-filter:var(--blur); -webkit-backdrop-filter:var(--blur);
    border-bottom:1px solid var(--border); position:sticky; top:0; z-index:30; flex-wrap:wrap;
  }
  .topbar-left h1 { font-size:20px; }
  .panel-sub { font-size:12px; color:var(--text-tertiary); }
  .topbar-right { display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
  .header-pill { display:flex; align-items:center; gap:6px; background:var(--hover-surface); border:1px solid var(--border);
    border-radius:20px; padding:6px 12px; font-size:11.5px; color:var(--text-secondary); font-family:'JetBrains Mono'; white-space:nowrap; }
  .live-pill { color:var(--success); border-color:var(--success-surface); }
  .live-pill .pulse-dot { width:6px; height:6px; border-radius:50%; background:var(--success); animation:blink 1.6s ease-in-out infinite; }
  .search-box { position:relative; display:flex; align-items:center; }
  .search-icon { width:14px; height:14px; position:absolute; left:11px; fill:none; stroke:var(--text-tertiary); stroke-width:2; }
  .search-box input { background:var(--hover-surface); border:1px solid var(--border); color:var(--text-primary);
    padding:8px 12px 8px 32px; border-radius:12px; font-size:12.5px; width:230px; }
  .search-box input:focus { outline:none; border-color:var(--primary); background:var(--card-bg); }
  .icon-btn { position:relative; background:var(--hover-surface); border:1px solid var(--border); border-radius:12px;
    width:34px; height:34px; display:flex; align-items:center; justify-content:center; cursor:pointer; color:var(--text-secondary); }
  .icon-btn:hover { color:var(--text-primary); transform:translateY(-1px); }
  .icon-btn svg { width:16px; height:16px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
  .notif-badge { position:absolute; top:-5px; right:-5px; background:var(--grad-danger); color:#fff; font-family:'JetBrains Mono';
    font-size:9.5px; font-weight:700; padding:1px 5px; border-radius:8px; min-width:16px; text-align:center; }
  .notif-badge[data-zero="true"] { display:none; }

  /* Theme toggle */
  .theme-toggle { position:relative; width:52px; height:30px; border-radius:16px; border:1px solid var(--border);
    background:var(--hover-surface); cursor:pointer; padding:2px; }
  .theme-toggle .knob { width:24px; height:24px; border-radius:50%; background:var(--grad-primary); display:flex; align-items:center;
    justify-content:center; transition:transform 0.3s cubic-bezier(.16,1,.3,1); box-shadow:var(--shadow-1); }
  .theme-toggle .knob svg { width:13px; height:13px; stroke:#fff; fill:none; stroke-width:2; }
  :root[data-theme="light"] .theme-toggle .knob { transform:translateX(22px); background:linear-gradient(135deg,#FF9F0A,#FFD60A); }

  .export-wrap { position:relative; }
  .btn-primary { display:flex; align-items:center; gap:6px; background:var(--grad-primary); border:none; color:#fff;
    padding:9px 15px; border-radius:12px; font-family:inherit; font-size:12.5px; font-weight:600; cursor:pointer;
    box-shadow:0 4px 14px rgba(10,132,255,0.3); }
  .btn-primary:hover { transform:translateY(-1px); filter:brightness(1.06); }
  .btn-primary:active { transform:translateY(0) scale(0.98); }
  .btn-primary .chev { width:12px; height:12px; fill:none; stroke:currentColor; stroke-width:2.5; }
  .export-menu { display:none; position:absolute; right:0; top:calc(100% + 8px); background:var(--card-bg); border:1px solid var(--border);
    border-radius:var(--radius-md); box-shadow:var(--shadow-2); min-width:230px; overflow:hidden; z-index:40;
    backdrop-filter:var(--blur); -webkit-backdrop-filter:var(--blur); }
  .export-menu.open { display:block; animation:popIn 0.15s cubic-bezier(.16,1,.3,1); }
  @keyframes popIn { from{opacity:0; transform:translateY(-4px);} to{opacity:1; transform:translateY(0);} }
  .export-menu button { display:block; width:100%; text-align:left; background:none; border:none; color:var(--text-primary);
    padding:11px 14px; font-size:12.5px; cursor:pointer; border-bottom:1px solid var(--divider); }
  .export-menu button:last-child { border-bottom:none; }
  .export-menu button:hover { background:var(--hover-surface); }
  .avatar { width:32px; height:32px; border-radius:50%; background:var(--grad-invest); color:#fff;
    display:flex; align-items:center; justify-content:center; font-family:'JetBrains Mono'; font-size:10px; font-weight:700; }

  .content { padding:var(--sp-3) var(--sp-4) var(--sp-6); }
  .panel { display:none; }
  .panel.active { display:block; animation:fadein 0.25s cubic-bezier(.16,1,.3,1); }
  @keyframes fadein { from{opacity:0; transform:translateY(6px);} to{opacity:1; transform:translateY(0);} }

  /* ============ KPI CARDS ============ */
  .kpi-row { display:grid; grid-template-columns:repeat(6, 1fr); gap:var(--sp-2); margin-bottom:var(--sp-3); }
  .tamper-kpis { grid-template-columns:repeat(5, 1fr); }
  .kpi-card { background:linear-gradient(160deg, var(--card-bg-2, var(--card-bg)), var(--card-bg)); border:1px solid var(--border); border-radius:var(--radius-lg);
    padding:var(--sp-2); box-shadow:var(--shadow-1); position:relative; overflow:hidden; transition:transform 0.25s cubic-bezier(.16,1,.3,1), box-shadow 0.25s; }
  .kpi-card:hover { transform:translateY(-4px); box-shadow:var(--shadow-2); }
  .kpi-card::before { content:""; position:absolute; top:0; left:0; right:0; height:3px; background:rgba(255,255,255,0.15); }
  .kpi-card.primary::before { background:var(--grad-primary); }
  .kpi-card.primary { box-shadow:var(--shadow-1), 0 10px 30px -14px rgba(46,140,255,0.35); }
  .kpi-card.success::before { background:var(--grad-success); }
  .kpi-card.success { box-shadow:var(--shadow-1), 0 10px 30px -14px rgba(46,230,166,0.3); }
  .kpi-card.warning::before { background:var(--grad-warm, linear-gradient(135deg,var(--warning),#FFD60A)); }
  .kpi-card.warning { box-shadow:var(--shadow-1), 0 10px 30px -14px rgba(255,176,32,0.3); }
  .kpi-card.danger::before { background:var(--grad-danger); }
  .kpi-card.danger { box-shadow:var(--shadow-1), 0 10px 30px -14px rgba(255,92,122,0.3); }
  .kpi-card.investigation::before { background:var(--grad-invest); }
  .kpi-card.investigation { box-shadow:var(--shadow-1), 0 10px 30px -14px rgba(197,99,255,0.3); }
  .kpi-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:var(--sp-1); }
  .kpi-label { font-size:11.5px; color:var(--text-tertiary); text-transform:uppercase; letter-spacing:0.5px; font-weight:600; }
  .kpi-status-pill { font-size:10px; font-weight:700; padding:2px 8px; border-radius:10px; text-transform:uppercase; letter-spacing:0.3px; }
  .kpi-status-pill.good { background:var(--success-surface); color:var(--success); }
  .kpi-status-pill.warn { background:var(--warning-surface); color:var(--warning); }
  .kpi-status-pill.danger { background:var(--danger-surface); color:var(--danger); }
  .kpi-status-pill.investigation { background:var(--investigation-surface); color:var(--investigation); }
  .kpi-main { display:flex; align-items:baseline; gap:3px; }
  .kpi-num { font-family:'JetBrains Mono'; font-size:30px; font-weight:700; }
  .kpi-unit { font-family:'JetBrains Mono'; font-size:16px; color:var(--text-tertiary); }
  .kpi-foot { font-size:11px; color:var(--text-secondary); margin-top:6px; }
  .kpi-foot.muted { color:var(--text-tertiary); }

  /* ============ CHARTS / PANEL CARDS ============ */
  .grid-row { display:grid; grid-template-columns:repeat(4, 1fr); gap:var(--sp-2); margin-bottom:var(--sp-4); }
  .panel-card { background:var(--panel-bg); border:1px solid var(--border); border-radius:var(--radius-lg);
    padding:var(--sp-2); box-shadow:var(--shadow-1); }
  .panel-card.wide { min-height:260px; }
  .panel-card-head { margin-bottom:var(--sp-2); }
  .panel-card-head h3 { font-size:13.5px; color:var(--text-primary); font-weight:700; }
  .chart-body { display:flex; align-items:center; justify-content:center; min-height:170px; }
  .period-label { font-size:11.5px; color:var(--text-tertiary); font-family:'JetBrains Mono'; margin:-14px 0 var(--sp-2); }

  .gauge-wrap { position:relative; display:flex; align-items:center; justify-content:center; }
  .radial { width:150px; height:150px; transform:rotate(-90deg); }
  .radial-track { fill:none; stroke:var(--divider); stroke-width:13; }
  .radial-fill { fill:none; stroke:url(#healthGradient); stroke-width:13; stroke-linecap:round;
    stroke-dasharray:515; stroke-dashoffset:515; transition:stroke-dashoffset 1.3s cubic-bezier(.16,1,.3,1); }
  .radial-center { position:absolute; text-align:center; }
  .radial-num { font-family:'JetBrains Mono'; font-size:30px; font-weight:800;
    background:var(--grad-primary); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
  .radial-label { font-size:10px; letter-spacing:1px; color:var(--text-tertiary); margin-top:2px; }

  .legend { display:flex; flex-direction:column; gap:7px; margin-left:var(--sp-2); }
  .legend-item { display:flex; align-items:center; gap:7px; font-size:11.5px; color:var(--text-secondary); }
  .legend-dot { width:9px; height:9px; border-radius:3px; }
  .legend-item b { color:var(--text-primary); font-family:'JetBrains Mono'; margin-left:auto; padding-left:12px; }
  .dayover-row { display:flex; gap:28px; }
  .dayover-stat { text-align:center; }
  .dayover-num { font-family:'JetBrains Mono'; font-size:32px; font-weight:700; }

  /* ============ ASSET / ROUTE CARDS ============ */
  .section-head { display:flex; align-items:baseline; gap:10px; margin:var(--sp-4) 0 var(--sp-2); }
  .section-head h2 { font-size:16px; }
  .section-head span { font-size:12px; color:var(--text-tertiary); }
  .card-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:var(--sp-2); }
  .asset-card { background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius-md);
    padding:var(--sp-2); box-shadow:var(--shadow-1); position:relative; overflow:hidden;
    transition:transform 0.25s cubic-bezier(.16,1,.3,1), box-shadow 0.25s; }
  .asset-card:hover { transform:translateY(-3px); box-shadow:var(--shadow-2); }
  .asset-card::before { content:""; position:absolute; top:0; left:0; bottom:0; width:3px; background:var(--accent); }
  .card-top { display:flex; justify-content:space-between; align-items:center; }
  .plate { font-family:'JetBrains Mono'; font-size:16.5px; font-weight:700; }
  .mini-gauge { width:56px; height:56px; }
  .mini-gauge-track { fill:none; stroke:var(--divider); stroke-width:5; }
  .mini-gauge-fill { fill:none; stroke-width:5; stroke-linecap:round; }
  .mini-gauge-num { font-family:'JetBrains Mono'; font-size:13px; font-weight:700; }
  .severity-pill { display:inline-block; font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:0.3px;
    margin-top:8px; padding:2px 8px; border-radius:9px; }
  .leds { display:flex; gap:12px; margin-top:11px; padding-top:11px; border-top:1px solid var(--divider); }
  .led-item { display:flex; align-items:center; gap:5px; font-family:'JetBrains Mono'; font-size:10.5px; color:var(--text-secondary); }
  .led-dot { width:7px; height:7px; border-radius:50%; }
  .badge-row { display:flex; gap:6px; flex-wrap:wrap; margin-top:9px; }
  .badge { font-size:9.5px; font-weight:700; padding:3px 8px; border-radius:6px; letter-spacing:0.2px; }
  .badge-border { background:var(--danger-surface); color:var(--danger); }
  .badge-tamper { background:var(--investigation-surface); color:var(--investigation); }
  .last-seen { margin-top:9px; font-size:11px; color:var(--text-tertiary); }
  .last-seen b { color:var(--text-secondary); font-family:'JetBrains Mono'; font-weight:600; }
  .card-action { margin-top:8px; font-size:12px; color:var(--text-secondary); line-height:1.45; }

  .route-grid { grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); }
  .route-card { background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius-md);
    padding:var(--sp-2); box-shadow:var(--shadow-1); position:relative; overflow:hidden;
    transition:transform 0.25s cubic-bezier(.16,1,.3,1); }
  .route-card:hover { transform:translateY(-3px); box-shadow:var(--shadow-2); }
  .route-card::before { content:""; position:absolute; top:0; left:0; bottom:0; width:3px; background:var(--grad-invest); }
  .route-top { display:flex; justify-content:space-between; align-items:baseline; }
  .route-plate { font-family:'JetBrains Mono'; font-size:15.5px; font-weight:700; }
  .route-sev { font-size:10px; font-weight:700; color:var(--investigation); text-transform:uppercase; }
  .route-diagram { display:flex; align-items:center; gap:8px; margin:16px 0 8px; position:relative; }
  .pin { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
  .pin.start { background:var(--success); } .pin.end { background:var(--investigation); }
  .route-line { flex:1; height:0; border-top:2px dashed var(--border); position:relative; }
  .route-dist { position:absolute; top:-18px; left:50%; transform:translateX(-50%); font-family:'JetBrains Mono';
    font-size:10.5px; color:var(--investigation); font-weight:700; white-space:nowrap; background:var(--card-bg); padding:0 6px; }
  .route-locs { font-size:10.5px; color:var(--text-secondary); display:flex; justify-content:space-between; gap:10px; }
  .route-locs span { max-width:48%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .route-stats { display:flex; gap:16px; margin-top:11px; padding-top:10px; border-top:1px solid var(--divider);
    font-family:'JetBrains Mono'; font-size:10.5px; color:var(--text-secondary); }
  .route-stats b { color:var(--text-primary); }
  .power-tag { margin-top:8px; font-size:10.5px; color:var(--warning); }

  /* ============ TABLES (enterprise grid) ============ */
  .grid-toolbar { display:flex; justify-content:space-between; align-items:baseline; margin:var(--sp-3) 0 var(--sp-1); }
  .grid-toolbar h3 { font-size:14px; }
  .grid-toolbar .row-count { font-size:11.5px; color:var(--text-tertiary); font-family:'JetBrains Mono'; }
  .good-text { color:var(--success); } .warn-text { color:var(--warning); }
  .two-col { display:grid; grid-template-columns:1fr 1fr; gap:var(--sp-3); }
  .table-wrap { background:var(--panel-bg); border:1px solid var(--border); border-radius:var(--radius-lg);
    overflow:auto; box-shadow:var(--shadow-1); max-height:70vh; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  thead th { background:var(--card-bg); color:var(--text-tertiary); text-align:left; padding:12px 14px;
    font-size:10.5px; text-transform:uppercase; letter-spacing:0.4px; cursor:pointer; white-space:nowrap;
    border-bottom:1px solid var(--border); position:sticky; top:0; font-weight:600; }
  thead th:hover { color:var(--primary); }
  thead th.sorted::after { content:" \\2193"; color:var(--primary); }
  tbody tr:nth-child(even) { background:var(--hover-surface); }
  tbody td { padding:10px 14px; border-bottom:1px solid var(--divider); color:var(--text-primary); white-space:nowrap;
    max-width:280px; overflow:hidden; text-overflow:ellipsis; }
  tbody tr:hover { background:var(--primary-surface); }
  .pill { display:inline-block; padding:2px 9px; border-radius:9px; font-size:10.5px; font-weight:700; }
  .pill-critical { background:var(--danger-surface); color:var(--danger); }
  .pill-high { background:var(--warning-surface); color:var(--warning); }
  .pill-elevated { background:rgba(255,159,10,0.09); color:#D9A22B; }
  .pill-pending { background:var(--info-surface); color:var(--info); }
  .pill-online { background:var(--success-surface); color:var(--success); }
  .pill-offline { background:var(--danger-surface); color:var(--danger); }
  .pill-neutral { background:var(--hover-surface); color:var(--text-secondary); }

  .callout { padding:var(--sp-2) var(--sp-3); border-radius:var(--radius-md); font-size:13px; line-height:1.6;
    margin-bottom:var(--sp-2); border:1px solid var(--border); background:var(--panel-bg); }
  .callout.investigation { border-color:rgba(191,90,242,0.35); background:var(--investigation-surface); }
  .callout.investigation em { color:var(--investigation); font-style:normal; font-weight:700; }
  .callout.muted { color:var(--text-secondary); font-size:12px; }

  .settings-note { margin-top:var(--sp-2); font-size:11.5px; color:var(--text-tertiary); }
  .method-list { margin:0 0 0 18px; font-size:12.5px; line-height:1.9; color:var(--text-secondary); }
  .method-list b { color:var(--text-primary); }

  .kpi-card.clickable { cursor:pointer; }
  .kpi-card.clickable:hover { transform:translateY(-4px); box-shadow:var(--shadow-2); border-color:var(--primary); }
  .kpi-goto { display:flex; align-items:center; gap:4px; font-size:10.5px; color:var(--primary); font-weight:600;
    margin-top:10px; opacity:0; transform:translateY(2px); transition:opacity 0.2s, transform 0.2s; }
  .kpi-goto svg { width:11px; height:11px; fill:none; stroke:currentColor; stroke-width:2.5; }
  .kpi-card.clickable:hover .kpi-goto { opacity:1; transform:translateY(0); }

  .tour-progress { height:2px; background:var(--divider); position:sticky; top:0; z-index:29; }
  .tour-progress-fill { height:100%; width:0%; background:var(--grad-primary); transition:width linear; }
  .tour-progress.active { display:block; }
  .tour-progress:not(.active) { opacity:0; }

  .icon-btn.tour-active { background:var(--grad-primary); color:#fff; border-color:transparent; }
  .icon-btn.tour-active svg { animation:none; }
  .speed-btn { background:var(--hover-surface); border:1px solid var(--border); border-radius:12px;
    width:40px; height:34px; display:flex; align-items:center; justify-content:center; cursor:pointer;
    color:var(--text-secondary); font-family:'JetBrains Mono'; font-size:11.5px; font-weight:700; }
  .speed-btn:hover { color:var(--primary); border-color:var(--primary); }

  .kpi-card, .asset-card, .route-card, .panel-card { animation:cardIn 0.4s cubic-bezier(.16,1,.3,1) backwards; }
  .kpi-row .kpi-card:nth-child(1){animation-delay:.02s} .kpi-row .kpi-card:nth-child(2){animation-delay:.06s}
  .kpi-row .kpi-card:nth-child(3){animation-delay:.10s} .kpi-row .kpi-card:nth-child(4){animation-delay:.14s}
  .kpi-row .kpi-card:nth-child(5){animation-delay:.18s} .kpi-row .kpi-card:nth-child(6){animation-delay:.22s}
  @keyframes cardIn { from{opacity:0; transform:translateY(10px);} to{opacity:1; transform:translateY(0);} }

  .table-wrap.auto-scrolling { scroll-behavior:smooth; }

  /* ============ TICKER ============ */
  .ticker-wrap { position:fixed; bottom:0; left:var(--sidebar-w); right:0; background:var(--header-bg);
    backdrop-filter:var(--blur); -webkit-backdrop-filter:var(--blur);
    border-top:1px solid var(--border); padding:8px 0; overflow:hidden; white-space:nowrap; z-index:20; transition:left 0.3s; }
  .ticker { display:inline-block; font-family:'JetBrains Mono'; font-size:12px; color:var(--text-secondary); animation:scroll 42s linear infinite; padding-left:100%; }
  @keyframes scroll { from{transform:translateX(0);} to{transform:translateX(-100%);} }

  @media (max-width: 1400px) { .kpi-row { grid-template-columns:repeat(3,1fr); } .grid-row { grid-template-columns:repeat(2,1fr); } }

  /* ============ MOBILE ============ */
  .hamburger-btn { display:none; background:var(--hover-surface); border:1px solid var(--border); border-radius:10px;
    width:36px; height:36px; align-items:center; justify-content:center; cursor:pointer; color:var(--text-primary); flex-shrink:0; }
  .hamburger-btn svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round; }
  .sidebar-backdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.55); z-index:99;
    backdrop-filter:blur(2px); opacity:0; transition:opacity 0.25s ease; }
  .sidebar-backdrop.open { display:block; opacity:1; }

  @media (max-width: 900px) {
    .app { display:block; }
    .sidebar {
      position:fixed; top:0; left:0; height:100vh; width:280px; max-width:82vw; z-index:100;
      transform:translateX(-100%); transition:transform 0.3s cubic-bezier(.16,1,.3,1); box-shadow:var(--shadow-3);
    }
    .sidebar.mobile-open { transform:translateX(0); }
    .sidebar.collapsed { width:280px; }
    .sidebar.collapsed .brand-text, .sidebar.collapsed .nav-item span:not(.nav-count),
    .sidebar.collapsed .nav-group-label, .sidebar.collapsed .quick-stats { display:flex; }
    .sidebar.collapsed .nav-group-label { display:block; }
    .collapse-btn { display:none; }
    .hamburger-btn { display:flex; }
    .main { width:100%; }

    .topbar { padding:10px 14px; flex-wrap:wrap; gap:10px; }
    .topbar-left { display:flex; align-items:center; gap:10px; min-width:0; }
    .topbar-left h1 { font-size:16px; }
    .panel-sub { display:none; }
    .topbar-right { width:100%; gap:8px; justify-content:flex-start; }
    .search-box { order:10; width:100%; }
    .search-box input { width:100%; }
    .header-pill { font-size:10px; padding:5px 9px; }
    .live-pill span:last-child, .header-pill#refresh-pill { display:none; }
    .tour-speed-label, #tour-speed-btn { width:36px; }

    .content { padding:16px 14px 90px; }
    .kpi-row, .tamper-kpis { grid-template-columns:repeat(2, 1fr) !important; }
    .kpi-num { font-size:24px; }
    .grid-row { grid-template-columns:1fr !important; }
    .card-grid, .route-grid { grid-template-columns:1fr !important; }
    .two-col { grid-template-columns:1fr; }
    .gauge-wrap .radial { width:120px; height:120px; }

    .grid-toolbar { flex-direction:column; align-items:flex-start; gap:4px; }
    .table-wrap { max-height:60vh; }
    table { font-size:11.5px; }
    thead th, tbody td { padding:8px 10px; }

    .ticker-wrap { left:0 !important; }
    .export-menu { right:auto; left:0; width:calc(100vw - 28px); max-width:320px; }
  }

  @media (max-width: 480px) {
    .kpi-row, .tamper-kpis { grid-template-columns:1fr !important; }
    .brand-name { font-size:13px; }
    .kpi-num { font-size:26px; }
  }

  @media print {
    .sidebar, .ticker-wrap, .topbar-right { display:none; }
    .panel { display:block !important; page-break-before:always; }
    body { background:white; color:black; background-image:none; }
  }
"""


def _js():
    return """
// =====================================================================
// PRESENTATION LAYER ONLY. Every calculation below (sort comparator,
// search predicate, KPI/animateCount math, chart data math, export
// byte-decoding) is identical in behavior to the prior build. Only
// markup/classes used to DISPLAY values changed (badges, pills, etc).
// =====================================================================

// ---- Light/Dark theme toggle (new, presentation-only, persisted locally) ----
(function() {
  let stored = null;
  try { stored = localStorage.getItem('gtl-theme'); } catch (e) {}
  const theme = stored || 'dark';
  if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
  const iconPath = {
    dark: '<path d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36A5.4 5.4 0 0 1 12 3z"/>',
    light: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  };
  document.getElementById('theme-icon').innerHTML = iconPath[theme];
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    const next = current === 'light' ? 'dark' : 'light';
    if (next === 'light') document.documentElement.setAttribute('data-theme', 'light');
    else document.documentElement.removeAttribute('data-theme');
    try { localStorage.setItem('gtl-theme', next); } catch (e) {}
    document.getElementById('theme-icon').innerHTML = iconPath[next];
  });
})();

// ---- Mobile sidebar drawer (new, presentation-only) ----
function openMobileSidebar() {
  document.getElementById('sidebar').classList.add('mobile-open');
  document.getElementById('sidebar-backdrop').classList.add('open');
}
function closeMobileSidebar() {
  document.getElementById('sidebar').classList.remove('mobile-open');
  document.getElementById('sidebar-backdrop').classList.remove('open');
}
document.getElementById('hamburger-btn').addEventListener('click', openMobileSidebar);
document.getElementById('sidebar-backdrop').addEventListener('click', closeMobileSidebar);

// ---- Sidebar collapse (new, presentation-only) ----
document.getElementById('collapse-btn').addEventListener('click', () => {
  document.getElementById('sidebar').classList.toggle('collapsed');
});

// ---- Recently viewed tracker (new, presentation-only, no data changes) ----
const recentlyViewed = [];
function pushRecent(panelId, label) {
  const idx = recentlyViewed.findIndex(r => r.id === panelId);
  if (idx > -1) recentlyViewed.splice(idx, 1);
  recentlyViewed.unshift({id: panelId, label: label});
  if (recentlyViewed.length > 4) recentlyViewed.pop();
  renderRecent();
}
function renderRecent() {
  const group = document.getElementById('recent-group');
  const label = document.getElementById('recent-label');
  const items = recentlyViewed.slice(1); // don't show the currently active one
  if (items.length === 0) { label.style.display = 'none'; group.innerHTML = ''; return; }
  label.style.display = '';
  group.innerHTML = items.map(r =>
    '<button class="nav-item" data-panel="' + r.id + '" data-label="' + r.label + '">' +
    '<svg class="nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg><span>' + r.label + '</span></button>'
  ).join('');
  group.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => activatePanel(btn.dataset.panel, btn.dataset.label)));
}

// ---- Sidebar navigation (same show/hide logic as before) ----
const PANEL_SUBS = {
  'p-exec': 'Cross-platform fleet health overview', 'p-full': 'Every asset, every platform, one table',
  'p-critical': 'Ranked worst-first, technical escalation', 'p-pending': 'Awaiting confirmation from GTL',
  'p-border': 'Offline assets near a known crossing', 'p-recovered': 'Day-over-day change',
  'p-healthy': 'Reporting normally on all platforms', 'p-settings': 'Thresholds used to generate this report',
  'p-priority': 'Integrity issues confirmed by tampering evidence', 'p-tsummary': 'Location-continuity analysis overview',
  'p-tconfirmed': 'Location gap plus a power event logged', 'p-tunconfirmed': 'Location gap, no power event, needs a field check',
  'p-tquality': 'Excluded for null GPS fix',
};
function activatePanel(panelId, label, isAutomatic) {
  if (!isAutomatic && typeof stopTour === 'function') stopTour();
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('[data-panel="' + panelId + '"]').forEach(b => b.classList.add('active'));
  document.getElementById(panelId).classList.add('active');
  document.getElementById('panel-title').textContent = label;
  document.getElementById('panel-sub').textContent = PANEL_SUBS[panelId] || '';
  pushRecent(panelId, label);
  if (typeof closeMobileSidebar === 'function') closeMobileSidebar();
}
document.querySelectorAll('.nav-item[data-panel]').forEach(btn => {
  btn.addEventListener('click', () => activatePanel(btn.dataset.panel, btn.dataset.label));
});

// ---- Drill-down: click a KPI number, land on the exact table backing it (proves the number is real) ----
document.querySelectorAll('.kpi-card[data-goto]').forEach(card => {
  card.addEventListener('click', () => activatePanel(card.dataset.goto, card.dataset.gotoLabel));
});

// ---- Auto-Tour / TV Mode (new, presentation-only): cycles through panels
// automatically, scrolling whatever needs scrolling, an internal table OR
// the page itself for panels like Executive Dashboard, at a speed you
// control, so nothing whips past too fast to read. ----
const TOUR_PANELS = [
  ['p-exec', 'Executive Dashboard'], ['p-priority', 'Priority Overlap'],
  ['p-critical', 'Critical Assets'], ['p-border', 'Border Risk'],
  ['p-pending', 'Pending Customer Feedback'], ['p-tsummary', 'Tampering Summary'],
  ['p-tconfirmed', 'Confirmed Tampering Cases'], ['p-healthy', 'Healthy Fleet'],
];
const SPEED_PRESETS = [0.5, 1, 1.5, 2];
let speedIndex = 1; // start at 1x
const BASE_PX_PER_SEC = 55;      // how fast content scrolls at 1x
const MIN_DWELL_MS = 5000;       // shortest time on any panel, even with nothing to scroll
const MAX_DWELL_MS = 22000;      // longest time on any one panel, even if content is very long
const HOLD_AFTER_SCROLL_MS = 1800; // pause at the bottom before moving on

let tourIndex = -1, tourTimer = null, tourRAF = null, isTouring = false;

function currentSpeed() { return SPEED_PRESETS[speedIndex]; }

function updateSpeedLabel() {
  const el = document.getElementById('tour-speed-label');
  if (el) el.textContent = currentSpeed() + 'x';
}
document.getElementById('tour-speed-btn').addEventListener('click', () => {
  speedIndex = (speedIndex + 1) % SPEED_PRESETS.length;
  updateSpeedLabel();
});
updateSpeedLabel();

function stopTour() {
  if (!isTouring) return;
  isTouring = false;
  clearTimeout(tourTimer);
  if (tourRAF) cancelAnimationFrame(tourRAF);
  document.getElementById('autotour-btn').classList.remove('tour-active');
  document.getElementById('autotour-icon').innerHTML = '<path d="M5 3l14 9-14 9V3z"/>';
  document.getElementById('tour-progress').classList.remove('active');
  document.getElementById('tour-progress-fill').style.transition = 'none';
  document.getElementById('tour-progress-fill').style.width = '0%';
}

// Finds whatever actually needs scrolling for this panel: an internal
// table if it overflows, otherwise the page itself if the panel's
// content is taller than the viewport.
function findScrollTarget(panelId) {
  const panel = document.getElementById(panelId);
  const tableWrap = panel.querySelector('.table-wrap');
  if (tableWrap && tableWrap.scrollHeight > tableWrap.clientHeight + 4) {
    return { el: tableWrap, isWindow: false, maxScroll: tableWrap.scrollHeight - tableWrap.clientHeight };
  }
  const doc = document.scrollingElement || document.documentElement;
  const maxScroll = doc.scrollHeight - window.innerHeight;
  if (maxScroll > 40) return { el: doc, isWindow: true, maxScroll };
  return null;
}

function tourStep() {
  tourIndex = (tourIndex + 1) % TOUR_PANELS.length;
  const [panelId, label] = TOUR_PANELS[tourIndex];
  activatePanel(panelId, label, true);
  window.scrollTo(0, 0);

  requestAnimationFrame(() => {
    const target = findScrollTarget(panelId);
    const speed = BASE_PX_PER_SEC * currentSpeed();
    const scrollMs = target ? Math.min(Math.max((target.maxScroll / speed) * 1000, 0), MAX_DWELL_MS - HOLD_AFTER_SCROLL_MS) : 0;
    const dwellMs = Math.max(MIN_DWELL_MS, scrollMs + HOLD_AFTER_SCROLL_MS);

    const fill = document.getElementById('tour-progress-fill');
    fill.style.transition = 'none';
    fill.style.width = '0%';
    requestAnimationFrame(() => {
      fill.style.transition = 'width ' + dwellMs + 'ms linear';
      fill.style.width = '100%';
    });

    if (target && scrollMs > 300) {
      const start = performance.now();
      function step(now) {
        if (!isTouring) return;
        const p = Math.min((now - start) / scrollMs, 1);
        const y = target.maxScroll * p;
        if (target.isWindow) window.scrollTo(0, y); else target.el.scrollTop = y;
        if (p < 1) tourRAF = requestAnimationFrame(step);
      }
      tourRAF = requestAnimationFrame(step);
    }

    tourTimer = setTimeout(tourStep, dwellMs);
  });
}

function startTour() {
  isTouring = true;
  tourIndex = -1;
  document.getElementById('autotour-btn').classList.add('tour-active');
  document.getElementById('autotour-icon').innerHTML = '<rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/>';
  document.getElementById('tour-progress').classList.add('active');
  tourStep();
}

document.getElementById('autotour-btn').addEventListener('click', () => {
  if (isTouring) stopTour(); else startTour();
});

// ---- Keyboard shortcuts: 1-8 jump panels, Escape stops tour (new, presentation-only) ----
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'Escape') { stopTour(); return; }
  const n = parseInt(e.key, 10);
  if (n >= 1 && n <= TOUR_PANELS.length) {
    const [panelId, label] = TOUR_PANELS[n - 1];
    activatePanel(panelId, label);
  }
});

// ---- Fullscreen toggle (new, presentation-only) ----
document.getElementById('fullscreen-btn').addEventListener('click', () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen();
  else document.exitFullscreen();
});

// ---- Export dropdown (same downloadBase64 logic as before, new menu UI) ----
document.getElementById('export-toggle').addEventListener('click', (e) => {
  e.stopPropagation();
  document.getElementById('export-menu').classList.toggle('open');
});
document.addEventListener('click', () => document.getElementById('export-menu').classList.remove('open'));

function downloadBase64(b64, filename) {
  if (!b64) { alert('This report was not embedded in this dashboard build.'); return; }
  const byteChars = atob(b64);
  const byteNumbers = new Array(byteChars.length);
  for (let i = 0; i < byteChars.length; i++) byteNumbers[i] = byteChars.charCodeAt(i);
  const byteArray = new Uint8Array(byteNumbers);
  const blob = new Blob([byteArray], {type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
document.getElementById('export-integrity').addEventListener('click', () => downloadBase64(DATA.xlsxB64, 'GTL_Integrity_Report.xlsx'));
document.getElementById('export-tamper').addEventListener('click', () => downloadBase64(DATA.tamperB64, 'Device_Tampering_Risk_Report.xlsx'));

// ---- Notification badge (display only, uses existing computed counts) ----
(function() {
  const n = DATA.kpi.escalations + DATA.kpi.border + DATA.kpi.doubleFlagged;
  const badge = document.getElementById('notif-badge');
  badge.textContent = n;
  if (n === 0) badge.setAttribute('data-zero', 'true');
})();

// ---- Count-up animation (identical math to before) ----
function animateCount(el, target, duration) {
  const start = 0; const startTime = performance.now();
  function tick(now) {
    const p = Math.min((now - startTime) / duration, 1);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(start + (target - start) * eased).toLocaleString();
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
document.querySelectorAll('[data-count]').forEach(el => {
  const target = parseFloat(el.dataset.count.replace(/,/g, '')) || 0;
  animateCount(el, target, 1000);
});
const mismatchEl = document.querySelector('[data-count-raw="mismatches"]');
if (mismatchEl) animateCount(mismatchEl, DATA.kpi.tamperConfirmed + DATA.kpi.tamperUnconfirmed, 1000);

// ---- Radial fleet health gauge (identical math to before) ----
(function() {
  const circumference = 2 * Math.PI * 82;
  const fill = document.getElementById('radial-health');
  const pct = DATA.kpi.healthPct / 100;
  const offset = circumference * (1 - pct);
  setTimeout(() => { fill.style.strokeDashoffset = offset; }, 150);
  const color = pct >= 0.8 ? 'url(#healthGradient)' : pct >= 0.5 ? '#FFB020' : '#FF5C7A';
  fill.style.stroke = color;
  const el = document.getElementById('health-num');
  if (pct < 0.8) {
    el.style.background = 'none';
    el.style.webkitTextFillColor = color;
    el.style.color = color;
  }
  setTimeout(() => {
    const target = DATA.kpi.healthPct; const startTime = performance.now();
    function tick(now) {
      const p = Math.min((now - startTime) / 1100, 1);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * eased) + '%';
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }, 150);
})();

// ---- Honest KPI footers: only show real deltas when history exists (no fabricated trends) ----
(function() {
  const healthFoot = document.getElementById('kpi-health-foot');
  const onlineFoot = document.getElementById('kpi-online-foot');
  if (DATA.meta.historyAvailable) {
    const delta = DATA.kpi.recovered - DATA.kpi.newlyOffline;
    healthFoot.textContent = (delta >= 0 ? '+' : '') + delta + ' net change vs yesterday';
    onlineFoot.textContent = DATA.kpi.recovered + ' recovered, ' + DATA.kpi.newlyOffline + ' newly offline';
  } else {
    healthFoot.textContent = 'First day of monitoring, trend from tomorrow';
    onlineFoot.textContent = 'Baseline day, no prior snapshot';
  }
})();

// ---- Ticker (identical logic to before) ----
(function() {
  const items = [];
  DATA.doubleFlagged.forEach(r => items.push('PRIORITY OVERLAP: ' + r.plate + ' \\u2014 offline + tampering match'));
  DATA.border.forEach(r => items.push('BORDER RISK: ' + r.plate + ' \\u2014 ' + r.borderDetail));
  DATA.newlyOffline.forEach(p => items.push('NEWLY OFFLINE: ' + p));
  DATA.recovered.forEach(p => items.push('RECOVERED: ' + p));
  if (items.length === 0) items.push('No active alerts \\u2014 fleet operating within normal parameters');
  document.getElementById('ticker').textContent = items.concat(items, items).join('   \\u2022   ');
})();
document.getElementById('priority-callout-num').textContent = DATA.kpi.doubleFlagged + ' asset(s)';

// =====================================================================
// TABLE ENGINE. Sort comparator and search predicate are IDENTICAL to
// the prior build. Only the per-cell rendering wraps certain columns
// in styled pill markup for readability; the underlying value used for
// sorting/searching is untouched.
// =====================================================================
function pillFor(key, val) {
  if (key === 'severity') {
    if (String(val).includes('Critical')) return 'pill pill-critical';
    if (String(val).includes('High')) return 'pill pill-high';
    if (String(val).includes('Elevated')) return 'pill pill-elevated';
    if (String(val).includes('Awaiting')) return 'pill pill-pending';
  }
  if (key === 'TLT' || key === 'MIX' || key === 'CAM') {
    if (val === 'Online') return 'pill pill-online';
    if (val === 'Offline') return 'pill pill-offline';
    return 'pill pill-neutral';
  }
  if (key === 'border' && val === 'Yes') return 'pill pill-offline';
  if (key === 'status') {
    if (val === 'Online') return 'pill pill-online';
    if (val === 'Technical Escalation') return 'pill pill-critical';
    if (val === 'Pending Customer Confirmation') return 'pill pill-pending';
  }
  return null;
}
function cellHTML(c, r) {
  const val = r[c.key] !== undefined && r[c.key] !== null ? r[c.key] : '';
  const pillClass = pillFor(c.key, val);
  return '<td>' + (pillClass && val !== '' ? '<span class="' + pillClass + '">' + val + '</span>' : val) + '</td>';
}
function renderTable(tableId, rows, columns, countElId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  if (countElId) { const ce = document.getElementById(countElId); if (ce) ce.textContent = rows.length + ' record' + (rows.length===1?'':'s'); }
  if (!rows || rows.length === 0) {
    table.innerHTML = '<tbody><tr><td style="padding:20px;color:var(--text-tertiary)">No records.</td></tr></tbody>';
    table._rows = []; table._columns = columns; return;
  }
  let sortKey = null, sortDir = 1;
  function draw(data) {
    let thead = '<thead><tr>' + columns.map(c => '<th data-key="' + c.key + '">' + c.label + '</th>').join('') + '</tr></thead>';
    let tbody = '<tbody>' + data.map(r => '<tr>' + columns.map(c => cellHTML(c, r)).join('') + '</tr>').join('') + '</tbody>';
    table.innerHTML = thead + tbody;
    table.querySelectorAll('th').forEach(th => {
      if (th.dataset.key === sortKey) th.classList.add('sorted');
      th.addEventListener('click', () => {
        const key = th.dataset.key;
        sortDir = (sortKey === key) ? -sortDir : 1;
        sortKey = key;
        const sorted = [...data].sort((a, b) => {
          let av = a[key], bv = b[key];
          if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sortDir;
          return String(av).localeCompare(String(bv)) * sortDir;
        });
        draw(sorted);
      });
    });
  }
  draw(rows);
  table._rows = rows; table._columns = columns;
}

// ---- Global search (identical predicate to before: substring match across all raw values) ----
document.getElementById('global-search').addEventListener('input', (e) => {
  const q = e.target.value.toLowerCase();
  const activePanel = document.querySelector('.panel.active');
  if (!activePanel) return;
  const table = activePanel.querySelector('table[id]');
  if (!table || !table._rows) return;
  const filtered = q ? table._rows.filter(r => Object.values(r).some(v => String(v).toLowerCase().includes(q))) : table._rows;
  const cols = table._columns;
  let thead = '<thead><tr>' + cols.map(c => '<th data-key="' + c.key + '">' + c.label + '</th>').join('') + '</tr></thead>';
  let tbody = '<tbody>' + filtered.map(r => '<tr>' + cols.map(c => cellHTML(c, r)).join('') + '</tr>').join('') + '</tbody>';
  table.innerHTML = thead + tbody;
});

// ---- Column definitions (identical to before) ----
const INTEGRITY_COLS = [
  {key:'plate', label:'Plate'}, {key:'status', label:'Status'}, {key:'severity', label:'Severity'},
  {key:'days', label:'Days Offline'}, {key:'lastPosition', label:'Last Position Time'},
  {key:'TLT', label:'Teletrac'}, {key:'MIX', label:'MiX Unity'},
  {key:'CAM', label:'FT Cloud'}, {key:'border', label:'Border Risk'}, {key:'borderDetail', label:'Border Detail'},
  {key:'feedback', label:'Customer Feedback'}, {key:'action', label:'Recommended Action'},
  {key:'reasons', label:'Investigation Reasons'}, {key:'location', label:'Last Known Location'},
];
const HEALTHY_COLS = [
  {key:'plate', label:'Plate'}, {key:'lastPosition', label:'Last Position Time'},
  {key:'TLT', label:'Teletrac'}, {key:'MIX', label:'MiX Unity'},
  {key:'CAM', label:'FT Cloud'}, {key:'location', label:'Last Known Location'},
];
const PRIORITY_COLS = [
  {key:'plate', label:'Plate'}, {key:'severity', label:'Severity'}, {key:'days', label:'Days Offline'},
  {key:'tamperConfirmed', label:'Confirmed Tamper Cases'}, {key:'tamperUnconfirmed', label:'Unconfirmed Gaps'},
  {key:'action', label:'Recommended Action'}, {key:'location', label:'Last Known Location'},
];
const TAMPER_COLS = [
  {key:'plate', label:'Plate'}, {key:'vehicle', label:'Vehicle'}, {key:'fleetNumber', label:'Fleet #'},
  {key:'severity', label:'Severity'}, {key:'arrivalDate', label:'Arrival Date'}, {key:'arrivalTime', label:'Arrival Time'},
  {key:'atLocation', label:'Last Location'}, {key:'nextDate', label:'Next Trip Date'}, {key:'nextTime', label:'Next Trip Time'},
  {key:'fromLocation', label:'Next Trip From'}, {key:'distanceKm', label:'Gap Distance (km)'},
  {key:'gapDuration', label:'Gap Duration'}, {key:'impliedSpeed', label:'Implied Speed (km/h)'}, {key:'powerEvent', label:'Power Event'},
];
const QUALITY_COLS = [
  {key:'plate', label:'Plate'}, {key:'vehicle', label:'Vehicle'}, {key:'fleetNumber', label:'Fleet #'},
  {key:'arrivalDate', label:'Arrival Date'}, {key:'arrivalTime', label:'Arrival Time'}, {key:'rawEnd', label:'Raw End Coordinate'},
  {key:'nextDate', label:'Next Departure Date'}, {key:'nextTime', label:'Next Departure Time'}, {key:'rawStart', label:'Raw Start Coordinate'},
];
const SIMPLE_COLS = [{key:'plate', label:'Plate'}, {key:'status', label:'Current Status'}];
const SETTINGS_COLS = [{key:'setting', label:'Setting'}, {key:'value', label:'Value Used For This Report'}];

renderTable('full-table', DATA.full, INTEGRITY_COLS, 'full-count');
renderTable('critical-table', DATA.critical, INTEGRITY_COLS, 'critical-count');
renderTable('pending-table', DATA.pending, INTEGRITY_COLS, 'pending-count');
renderTable('border-table', DATA.border, INTEGRITY_COLS, 'border-count');
renderTable('healthy-table', DATA.healthy, HEALTHY_COLS, 'healthy-count');
renderTable('priority-table', DATA.doubleFlagged, PRIORITY_COLS);
renderTable('tconfirmed-table', DATA.tamperConfirmed, TAMPER_COLS, 'tconfirmed-count');
renderTable('tunconfirmed-table', DATA.tamperUnconfirmed, TAMPER_COLS, 'tunconfirmed-count');
renderTable('tquality-table', DATA.qualityLog, QUALITY_COLS);
renderTable('recovered-table', DATA.recoveredList, SIMPLE_COLS);
renderTable('newlyoffline-table', DATA.newlyOfflineList, SIMPLE_COLS);
renderTable('settings-table', DATA.settingsRows, SETTINGS_COLS);

// =====================================================================
// CHARTS. All underlying math (fractions, max-scaling, circumference)
// identical to the prior build. Only colors/typography restyled and
// tooltips added.
// =====================================================================
function sevColor(sev) {
  if (sev.includes('Critical')) return '#FF5C7A';
  if (sev.includes('High')) return '#FFB020';
  if (sev.includes('Elevated')) return '#E6A93D';
  return '#4FC3FF';
}
function ledColor(v) { return v === 'Offline' ? '#FF5C7A' : (v === 'Online' ? '#2EE6A6' : '#4A5266'); }

function miniGauge(days, maxDays, color) {
  const pct = Math.min(days / maxDays, 1.0);
  const c = 2 * Math.PI * 24;
  const offset = c * (1 - pct);
  return '<svg viewBox="0 0 60 60" class="mini-gauge"><circle cx="30" cy="30" r="24" class="mini-gauge-track"/>' +
    '<circle cx="30" cy="30" r="24" class="mini-gauge-fill" stroke="' + color + '" stroke-dasharray="' + c + '" stroke-dashoffset="' + offset + '" transform="rotate(-90 30 30)"/>' +
    '<text x="30" y="34" text-anchor="middle" class="mini-gauge-num" fill="' + color + '">' + Math.round(days) + '</text></svg>';
}

(function() {
  const segments = [
    {label:'Online', value:DATA.kpi.online, color:'#2EE6A6'},
    {label:'Pending Confirmation', value:DATA.kpi.pending, color:'#4FC3FF'},
    {label:'Elevated', value:DATA.sevCounts['Elevated - Monitor']||0, color:'#E6A93D'},
    {label:'High', value:DATA.sevCounts['High - Escalate This Week']||0, color:'#FFB020'},
    {label:'Critical', value:DATA.sevCounts['Critical - Long-term Fault']||0, color:'#FF5C7A'},
  ].filter(s => s.value > 0);
  const total = segments.reduce((a,s) => a+s.value, 0) || 1;
  const r = 54, c = 2*Math.PI*r;
  let offsetAcc = 0;
  const circles = segments.map(s => {
    const frac = s.value/total;
    const dash = frac*c;
    const el = '<circle cx="65" cy="65" r="' + r + '" fill="none" stroke="' + s.color + '" stroke-width="18" ' +
      'stroke-dasharray="' + dash + ' ' + (c-dash) + '" stroke-dashoffset="' + (-offsetAcc) + '" transform="rotate(-90 65 65)"><title>' + s.label + ': ' + s.value + '</title></circle>';
    offsetAcc += dash;
    return el;
  }).join('');
  const legend = segments.map(s => '<div class="legend-item"><span class="legend-dot" style="background:' + s.color + '"></span>' + s.label + '<b>' + s.value + '</b></div>').join('');
  document.getElementById('chart-donut').innerHTML =
    '<svg viewBox="0 0 130 130" style="width:140px;height:140px">' + circles +
    '<text x="65" y="61" text-anchor="middle" style="font-family:\\'JetBrains Mono\\';font-size:20px;font-weight:700;fill:#F7F8FA">' + total + '</text>' +
    '<text x="65" y="77" text-anchor="middle" style="font-family:\\'Inter\\';font-size:9px;fill:#8A92A2">ASSETS</text></svg>' +
    '<div class="legend">' + legend + '</div>';
})();

(function() {
  const items = [
    {label:'Elevated', value:DATA.sevCounts['Elevated - Monitor']||0, color:'#E6A93D'},
    {label:'High', value:DATA.sevCounts['High - Escalate This Week']||0, color:'#FFB020'},
    {label:'Critical', value:DATA.sevCounts['Critical - Long-term Fault']||0, color:'#FF5C7A'},
  ];
  const max = Math.max(...items.map(i => i.value), 1);
  const bars = items.map(i => {
    const h = Math.round((i.value/max)*110);
    return '<div style="display:flex;flex-direction:column;align-items:center;gap:8px;" title="' + i.label + ': ' + i.value + '">' +
      '<div style="font-family:\\'JetBrains Mono\\';font-size:13px;font-weight:700;color:' + i.color + '">' + i.value + '</div>' +
      '<div style="width:36px;height:' + h + 'px;background:' + i.color + ';border-radius:5px 5px 2px 2px;"></div>' +
      '<div style="font-size:10px;color:#8A92A2">' + i.label + '</div></div>';
  }).join('');
  document.getElementById('chart-severity').innerHTML = '<div style="display:flex;gap:22px;align-items:flex-end;height:170px;">' + bars + '</div>';
})();

(function() {
  const el = document.getElementById('chart-dayover');
  if (!DATA.meta.historyAvailable) {
    el.innerHTML = '<div style="text-align:center;color:var(--text-tertiary);font-size:12px;line-height:1.6;">No prior-day snapshot yet.<br>Tracking begins from tomorrow\\'s run.</div>';
  } else {
    el.innerHTML = '<div class="dayover-row">' +
      '<div class="dayover-stat"><div class="dayover-num" style="color:#22C55E">' + DATA.kpi.recovered + '</div><div class="kpi-label">Recovered</div></div>' +
      '<div class="dayover-stat"><div class="dayover-num" style="color:#F59E0B">' + DATA.kpi.newlyOffline + '</div><div class="kpi-label">Newly Offline</div></div>' +
      '</div>';
  }
})();

(function() {
  const bands = DATA.severityBands;
  if (!bands.length) { document.getElementById('chart-sevbands').innerHTML = '<p style="color:var(--text-tertiary)">No data.</p>'; return; }
  const max = Math.max(...bands.map(b => Math.max(b.confirmed, b.unconfirmed)), 1);
  const groups = bands.map(b => {
    const hc = Math.round((b.confirmed/max)*160), hu = Math.round((b.unconfirmed/max)*160);
    const color = sevColor(b.severity);
    return '<div style="display:flex;flex-direction:column;align-items:center;gap:6px;">' +
      '<div style="display:flex;gap:6px;align-items:flex-end;height:170px;">' +
        '<div style="width:24px;height:' + hc + 'px;background:' + color + ';border-radius:4px 4px 0 0;" title="Confirmed: ' + b.confirmed + '"></div>' +
        '<div style="width:24px;height:' + hu + 'px;background:' + color + '55;border-radius:4px 4px 0 0;" title="Unconfirmed: ' + b.unconfirmed + '"></div>' +
      '</div>' +
      '<div style="font-size:10.5px;color:#8A92A2;text-align:center">' + b.severity + '<br><span style="color:#5C6478">' + b.rule + '</span></div>' +
      '<div style="font-family:\\'JetBrains Mono\\';font-size:10.5px;color:' + color + '">' + b.confirmed + ' / ' + b.unconfirmed + '</div>' +
      '</div>';
  }).join('');
  document.getElementById('chart-sevbands').innerHTML =
    '<div style="display:flex;gap:26px;align-items:flex-end;">' + groups + '</div>' +
    '<div class="legend" style="margin-top:18px;flex-direction:row;gap:16px;margin-left:0;">' +
    '<div class="legend-item"><span class="legend-dot" style="background:#8A92A2"></span>Solid = Confirmed</div>' +
    '<div class="legend-item"><span class="legend-dot" style="background:#8A92A255"></span>Faded = Unconfirmed</div></div>';
})();

(function() {
  const top = DATA.topVehicles.slice(0, 8);
  if (!top.length) { document.getElementById('chart-topvehicles').innerHTML = '<p style="color:var(--text-tertiary)">No data.</p>'; return; }
  const max = Math.max(...top.map(v => v.confirmedCases), 1);
  const rows = top.map(v => {
    const w = Math.round((v.confirmedCases/max)*100);
    return '<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;" title="' + v.vehicle + ': ' + v.confirmedCases + ' confirmed cases">' +
      '<div style="width:90px;font-family:\\'JetBrains Mono\\';font-size:11px;color:#F7F8FA;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + v.vehicle + '</div>' +
      '<div style="flex:1;background:var(--n-700);border-radius:4px;height:16px;overflow:hidden;">' +
        '<div style="width:' + w + '%;height:100%;background:var(--investigation);border-radius:4px;"></div></div>' +
      '<div style="width:26px;font-family:\\'JetBrains Mono\\';font-size:11px;color:var(--investigation);font-weight:700;text-align:right;">' + v.confirmedCases + '</div>' +
      '</div>';
  }).join('');
  document.getElementById('chart-topvehicles').innerHTML = '<div style="width:100%">' + rows + '</div>';
})();

// ---- Critical / route cards (identical selection logic, restyled markup) ----
const cardsHTML = DATA.criticalCards.map(r => {
  const color = sevColor(r.severity);
  const badges = [];
  if (r.border === 'Yes') badges.push('<span class="badge badge-border">BORDER: ' + r.borderDetail + '</span>');
  const isTampered = DATA.doubleFlagged.some(d => d.plate === r.plate);
  if (isTampered) badges.push('<span class="badge badge-tamper">TAMPERING MATCH</span>');
  return '<div class="asset-card" style="--accent:' + color + '">' +
    '<div class="card-top"><div class="plate">' + r.plate + '</div>' + miniGauge(r.days, DATA.meta.longTermFault * 1.5, color) + '</div>' +
    '<span class="severity-pill" style="background:' + color + '22;color:' + color + '">' + r.severity + '</span>' +
    '<div class="leds">' +
      '<div class="led-item"><div class="led-dot" style="background:' + ledColor(r.TLT) + '"></div>TLT</div>' +
      '<div class="led-item"><div class="led-dot" style="background:' + ledColor(r.MIX) + '"></div>MIX</div>' +
      '<div class="led-item"><div class="led-dot" style="background:' + ledColor(r.CAM) + '"></div>CAM</div>' +
    '</div>' +
    (badges.length ? '<div class="badge-row">' + badges.join('') + '</div>' : '') +
    '<div class="last-seen">Last position: <b>' + r.lastPosition + '</b></div>' +
    '<div class="card-action">' + r.action + '</div></div>';
}).join('');
document.getElementById('critical-cards').innerHTML = cardsHTML || '<p style="color:var(--text-tertiary)">No critical cases.</p>';

const routeHTML = DATA.tamperCards.map(c => {
  return '<div class="route-card">' +
    '<div class="route-top"><div class="route-plate">' + c.Plate + '</div><div class="route-sev">' + c.Severity + '</div></div>' +
    '<div class="route-diagram"><div class="pin start"></div><div class="route-line"><span class="route-dist">' + c.DistanceKm + ' km</span></div><div class="pin end"></div></div>' +
    '<div class="route-locs"><span>' + (c.AtLocation || '') + '</span><span style="text-align:right">' + (c.FromLocation || '') + '</span></div>' +
    '<div class="route-locs" style="margin-top:2px;opacity:0.7"><span>' + (c.ArrivalDate||'') + ' ' + (c.ArrivalTime||'') + '</span><span style="text-align:right">' + (c.NextDate||'') + ' ' + (c.NextTime||'') + '</span></div>' +
    '<div class="route-stats"><span>Gap: <b>' + (c.GapDuration || '') + '</b></span><span>Speed: <b>' + (c.ImpliedSpeedKmh || 0) + ' km/h</b></span></div>' +
    '<div class="power-tag">' + (c.PowerEventInGap || '') + '</div></div>';
}).join('');
document.getElementById('tamper-cards').innerHTML = routeHTML || '<p style="color:var(--text-tertiary)">No confirmed tampering cases.</p>';
"""
