"""
Rebuild, per direct feedback:
  - every threshold comes from settings.ini, nothing hardcoded
  - NO confidence score. Each platform is reported individually as
    Online/Offline. If MiX is offline, that's a problem on its own,
    it is not "diluted" by Teletrac being fine.
  - border risk uses real distance to known crossings, and only
    applies to OFFLINE assets, never online ones.
  - location mismatch removed, the per-platform status table plus
    the comment/detail field already carries that information.
  - every asset gets Investigation Reasons (why it matters) and an
    Action Required (what to do), not just a status label.
"""

from datetime import datetime
from collections import defaultdict

from border_risk import border_risk
from schema import is_valid_plate


def _severity_for(days_silent, settings):
    if days_silent >= settings["LONG_TERM_FAULT_DAYS"]:
        return "Critical - Long-term Fault"
    if days_silent >= settings["HIGH_PRIORITY_DAYS"]:
        return "High - Escalate This Week"
    return "Elevated - Monitor"


def _platform_status(reports, now, threshold_days):
    """One row per platform: is THIS platform's freshest signal stale."""
    latest_per_platform = {}
    for r in reports:
        if r.last_report_time is None:
            continue
        cur = latest_per_platform.get(r.source_platform)
        if cur is None or r.last_report_time > cur.last_report_time:
            latest_per_platform[r.source_platform] = r
    for r in reports:
        latest_per_platform.setdefault(r.source_platform, r)

    status = {}
    for platform, r in latest_per_platform.items():
        if r.last_report_time is None:
            status[platform] = ("No Data", None)
        else:
            days = (now - r.last_report_time).total_seconds() / 86400
            status[platform] = ("Offline" if days >= threshold_days else "Online", r.last_report_time)
    return status, latest_per_platform


def _investigation_reasons(days_silent, platform_status, feedback, has_border_risk, offline_platforms_count):
    reasons = []
    if days_silent is not None and days_silent > 0:
        reasons.append(f"Offline {days_silent} day(s)")
    if offline_platforms_count > 1:
        reasons.append(f"Offline on {offline_platforms_count} independent platforms")
    if has_border_risk:
        reasons.append("Last known position near a high-incident border crossing")
    if feedback is None:
        reasons.append("No customer feedback on file")
    else:
        reasons.append(f"Customer feedback: {feedback['status']}")
    return reasons


def _recommended_action(status, days_silent, has_border_risk, feedback, settings):
    if feedback and feedback["status"] == "Closed - Do Not Chase":
        return "Close - customer confirmed, no action needed"
    if feedback and feedback["status"] == "Pending - In Workshop":
        return "Wait - asset already in workshop per customer"
    if feedback and feedback["status"] == "Acknowledged - Monitoring":
        return "Monitor - customer aware, no field visit needed yet"
    if status == "Online":
        return "None"
    if has_border_risk:
        return "Immediate escalation - possible cross-border loss"
    if days_silent is None:
        return "Contact customer to confirm status"
    if days_silent >= settings["LONG_TERM_FAULT_DAYS"]:
        return "Recover device - long-term fault, schedule field recovery"
    if days_silent >= settings["HIGH_PRIORITY_DAYS"]:
        return "Schedule physical inspection this week"
    return "Contact customer for status confirmation"


def classify_fleet(reports_by_plate: dict, settings: dict, feedback: dict, now=None):
    now = now or datetime.utcnow()
    threshold_days = settings["OFFLINE_THRESHOLD_DAYS"]
    result = {}

    for plate, reports in reports_by_plate.items():
        platform_status, latest_per_platform = _platform_status(reports, now, threshold_days)
        platforms = sorted(platform_status.keys())
        offline_platforms = [p for p, (s, _) in platform_status.items() if s == "Offline"]
        any_offline = len(offline_platforms) > 0
        all_offline = len(offline_platforms) == len(platforms)

        useful_reports = list(latest_per_platform.values())
        geo_report = max(
            (r for r in useful_reports if r.last_lat is not None),
            key=lambda r: r.last_report_time or datetime.min,
            default=None
        )
        lat, lon = (geo_report.last_lat, geo_report.last_lon) if geo_report else (None, None)
        last_location = next((r.last_location_text for r in useful_reports if r.last_location_text), None)

        stalest = min((r.last_report_time for r in useful_reports if r.last_report_time), default=None)
        days_silent = round((now - stalest).total_seconds() / 86400, 1) if stalest else None

        fb = feedback.get(plate)
        is_risk, risk_detail = border_risk(
            is_offline=all_offline,
            days_offline=days_silent or 0,
            lat=lat, lon=lon,
            radius_km=settings["BORDER_RADIUS_KM"],
            min_days=threshold_days,
        )

        if not any_offline:
            status = "Online"
            severity = "-"
            action = _recommended_action(status, 0, False, fb, settings)
            result[plate] = {
                "status": status, "severity": severity, "days_silent": 0,
                "platform_status": platform_status, "platforms": platforms,
                "border_flag": False, "border_detail": None,
                "last_location": last_location, "feedback": fb,
                "action": action,
                "reasons": ["Reporting normally on all platforms"],
                "detail": "reporting normally on all platforms",
            }
            continue

        if all_offline and days_silent is not None and days_silent >= threshold_days:
            severity = _severity_for(days_silent, settings)
            status = "Technical Escalation"
        else:
            severity = "Awaiting Confirmation"
            status = "Pending Customer Confirmation"

        action = _recommended_action(status, days_silent, is_risk, fb, settings)
        reasons = _investigation_reasons(days_silent, platform_status, fb, is_risk, len(offline_platforms))

        result[plate] = {
            "status": status, "severity": severity, "days_silent": days_silent or 0,
            "platform_status": platform_status, "platforms": platforms,
            "border_flag": is_risk, "border_detail": risk_detail,
            "last_location": last_location, "feedback": fb,
            "action": action, "reasons": reasons,
            "detail": f"offline on {len(offline_platforms)}/{len(platforms)} platform(s) for {days_silent} days",
        }

    return result


def group_by_plate(all_reports, ignore_demo=True):
    grouped = defaultdict(list)
    skipped = []
    for r in all_reports:
        plate = r.asset_plate
        if is_valid_plate(plate):
            grouped[plate].append(r)
        else:
            skipped.append(plate)
    return grouped, skipped
