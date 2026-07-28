"""
The daily import job. Split deliberately into two halves:

  fetch_reports()   - needs real IMAP + internet access. Cannot be
                       tested from a sandboxed dev environment, only
                       from wherever this actually runs (Render).
  process_reports()  - pure data processing, zero network calls, and
                       therefore fully testable right now against the
                       same real sample files already proven correct
                       earlier in this project. This is 95% of the
                       actual logic and all of the risk.

run_import() wires them together and is what /api/import calls.
"""

import os
import sys
import glob
import json
import tempfile
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))  # so this also works standalone, outside app.py

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fleet_logic"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mail_reader
from tamper_engine import analyse as run_tamper_analysis, build_workbook as build_tamper_workbook

from adapters import teletrac_csv, mix_mobile_status, mix_power_events, mix_movement, ft_cloud_camera
from classifier import group_by_plate, classify_fleet
from settings import load_settings
from control_room import _build_data
import report_writer

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
WORK_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "_incoming")


class ImportError_(Exception):
    """Raised for anything that should stop the import and be reported,
    rather than silently producing a wrong number."""
    pass


def check_periods_overlap(movement_csv_path, event_csv_path):
    """
    Real finding from testing this pipeline earlier: if the movement
    report and event report cover different months, tampering
    correlation silently produces zero confirmed cases, not an error,
    just a quietly wrong number. Hard-fail instead.
    """
    import csv as _csv

    def date_range(path, date_field):
        with open(path, encoding="utf-8-sig", newline="") as f:
            dates = [row[date_field] for row in _csv.DictReader(f) if row.get(date_field)]
        if not dates:
            return None, None
        parsed = [datetime.strptime(d, "%d/%m/%Y") for d in dates]
        return min(parsed), max(parsed)

    m_start, m_end = date_range(movement_csv_path, "DepartureDate")
    e_start, e_end = date_range(event_csv_path, "EventStartDate")
    if m_start is None or e_start is None:
        return  # let downstream validation catch empty files
    overlaps = m_start <= e_end and e_start <= m_end
    if not overlaps:
        raise ImportError_(
            f"Movement report covers {m_start.date()} to {m_end.date()}, "
            f"Event report covers {e_start.date()} to {e_end.date()}, no overlap. "
            f"Tampering correlation would silently report zero confirmed cases. "
            f"Refusing to continue, check the report subscriptions are pulling matching periods."
        )


def fetch_reports(username, password, work_dir=WORK_DIR):
    """
    Connects to the mailbox and downloads all five reports into
    work_dir. Needs real network access, cannot run in this sandbox.
    Returns a dict of {report_key: local_file_path}.
    """
    os.makedirs(work_dir, exist_ok=True)
    conn = mail_reader.connect(username, password)
    paths = {}

    try:
        # Direct-attachment reports
        for key, ext in [("teletrac_offline", ".csv"), ("ft_cloud_camera", ".zip")]:
            subject = mail_reader.REPORT_SUBJECTS[key]
            folder = mail_reader.REPORT_FOLDERS[key]
            msg = mail_reader.find_latest_message(conn, subject, mailbox=folder)
            if msg is None:
                raise ImportError_(f"No email found matching subject '{subject}' in '{folder}'")
            filename, content = mail_reader.extract_attachment(msg)
            if content is None:
                raise ImportError_(f"Email for '{subject}' had no attachment")
            path = os.path.join(work_dir, key + ext)
            with open(path, "wb") as f:
                f.write(content)
            paths[key] = path

        # Signed-link reports
        import requests
        for key in ("mix_movement", "mix_mobile_status", "mix_power_events"):
            subject = mail_reader.REPORT_SUBJECTS[key]
            folder = mail_reader.REPORT_FOLDERS[key]
            msg = mail_reader.find_latest_message(conn, subject, mailbox=folder)
            if msg is None:
                raise ImportError_(f"No email found matching subject '{subject}' in '{folder}'")
            html = mail_reader._get_html_body(msg)
            link = mail_reader.extract_download_link(html)
            if link is None:
                raise ImportError_(f"Could not find a download link in the email for '{subject}'")
            resp = requests.get(link, timeout=60)
            resp.raise_for_status()
            path = os.path.join(work_dir, key + ".csv")
            with open(path, "wb") as f:
                f.write(resp.content)
            paths[key] = path
    finally:
        conn.logout()

    return paths


def process_reports(paths, settings_path=None, feedback_rows=None):
    """
    Pure processing, no network. paths is the dict returned by
    fetch_reports() (or, for testing, pointed at the known-good local
    sample files). Returns the same data dict the dashboard serves.
    """
    check_periods_overlap(paths["mix_movement"], paths["mix_power_events"])

    settings = load_settings(settings_path or os.path.join(DATA_DIR, "settings.ini"))
    feedback = feedback_rows or {}

    all_rows = []
    all_rows += teletrac_csv.parse(paths["teletrac_offline"])
    all_rows += mix_mobile_status.parse(paths["mix_mobile_status"])
    all_rows += mix_movement.parse(paths["mix_movement"])
    all_rows += mix_power_events.parse(paths["mix_power_events"])
    all_rows += ft_cloud_camera.parse(paths["ft_cloud_camera"])

    grouped, skipped = group_by_plate(all_rows)
    timestamps = [r.last_report_time for r in all_rows if r.last_report_time]
    now = max(timestamps) if timestamps else datetime.now()
    results = classify_fleet(grouped, settings, feedback, now=now)

    tamper_result = run_tamper_analysis(paths["mix_movement"], paths["mix_power_events"])
    tampering = {
        "confirmed": [_tamper_case_to_loader_shape(g) for g in tamper_result["confirmed"]],
        "unconfirmed": [_tamper_case_to_loader_shape(g) for g in tamper_result["unconfirmed"]],
        "summary": {
            "gaps_checked": len(tamper_result["gaps"]),
            "mismatches": len(tamper_result["mismatches"]),
            "confirmed": len(tamper_result["confirmed"]),
            "unconfirmed": len(tamper_result["unconfirmed"]),
            "null_gps_excluded": len(tamper_result["skipped"]),
            "period_label": "",
        },
        "severity_bands": _severity_bands_from_tamper_result(tamper_result),
        "top_vehicles": _top_vehicles_from_tamper_result(tamper_result),
        "quality_log": [_quality_skip_to_loader_shape(s) for s in tamper_result["skipped"]],
    }

    # Generate the actual downloadable .xlsx files - same formatting as
    # the original standalone tool (report_writer.write_report() for
    # Fleet Integrity, tamper_engine.build_workbook() for Tampering Risk)
    # - then hand their paths to _build_data(), which embeds them via
    # its own _b64_file() helper.
    os.makedirs(DATA_DIR, exist_ok=True)
    integrity_path = os.path.join(DATA_DIR, "GTL_integrity_report.xlsx")
    tamper_path = os.path.join(DATA_DIR, "GTL_tampering_report.xlsx")
    report_writer.write_report(results, settings, recovered=[], newly_offline=[],
                                output_path=integrity_path, report_date=now, history_available=False)
    build_tamper_workbook(tamper_result, tamper_path)

    data = _build_data(results, settings, tampering, [], [], now, False, integrity_path, tamper_path)
    data["_skipped_plates"] = sorted(set(skipped))
    # Stashed so run_import() can react to Offline->Online transitions
    # (see notifications.check_reconnections) without this function - kept
    # deliberately network-free per its own docstring - doing any Sheets
    # reads or sending mail itself. Popped back out before the dashboard
    # JSON is written; it's classify_fleet()'s raw internal shape, not
    # something the frontend has any use for.
    data["_classification"] = results
    return data


def _quality_skip_to_loader_shape(s):
    from schema import normalize_plate
    return {
        "Plate": normalize_plate(s["AssetName"]), "Vehicle": s["AssetName"], "FleetNumber": s["FleetNumber"],
        "ArrivalDate": s["ArrivalDate"], "ArrivalTime": s["ArrivalTime"], "RawEndCoordinate": s["RawEndLatLong"],
        "NextDepartureDate": s["NextDepartureDate"], "NextDepartureTime": s["NextDepartureTime"],
        "RawStartCoordinate": s["RawStartLatLong"],
    }


def _tamper_case_to_loader_shape(g):
    """
    tamper_engine.analyse() returns cases keyed like AssetName/ArriveAt/
    DepartFrom/GapMinutes/HasDisconnect/HasReconnect (the shape that made
    sense while building the analysis). _build_data() expects the shape
    tampering_loader.py produces when reading the standalone tool's real
    .xlsx output (Plate/AtLocation/FromLocation/GapDuration/PowerEventInGap).
    This is the one conversion step between them, so both code paths feed
    _build_data() identically regardless of where the case came from.
    """
    from schema import normalize_plate
    from tamper_engine import severity, readable_duration

    if g["HasDisconnect"] and g["HasReconnect"]:
        power_event = "Disconnect + Reconnect logged (Power Cycle)"
    elif g["HasDisconnect"]:
        power_event = "Disconnect logged, no Reconnect"
    elif g["HasReconnect"]:
        power_event = "Reconnect only, no Disconnect"
    else:
        power_event = "None logged"

    return {
        "AssetID": g["AssetID"], "Plate": normalize_plate(g["AssetName"]), "Vehicle": g["AssetName"],
        "FleetNumber": g["FleetNumber"], "Severity": severity(g["DistanceKm"]),
        "ArrivalDate": g["ArrivalDate"], "ArrivalTime": g["ArrivalTime"], "AtLocation": g["ArriveAt"],
        "NextDate": g["NextDepartureDate"], "NextTime": g["NextDepartureTime"], "FromLocation": g["DepartFrom"],
        "DistanceKm": g["DistanceKm"], "GapDuration": readable_duration(g["GapMinutes"]),
        "ImpliedSpeedKmh": g["ImpliedSpeedKmh"], "PowerEventInGap": power_event,
    }


def _severity_bands_from_tamper_result(result):
    from tamper_engine import severity
    bands = []
    for sname, rule in [("Critical", "> 100 km"), ("High", "10 - 100 km"), ("Moderate", "2 - 10 km")]:
        confirmed = sum(1 for g in result["confirmed"] if severity(g["DistanceKm"]) == sname)
        unconfirmed = sum(1 for g in result["unconfirmed"] if severity(g["DistanceKm"]) == sname)
        bands.append({"severity": sname, "rule": rule, "confirmed": confirmed, "unconfirmed": unconfirmed})
    return bands


def _top_vehicles_from_tamper_result(result):
    stats = {}
    for g in result["confirmed"]:
        k = g["AssetID"]
        stats.setdefault(k, {"assetId": k, "vehicle": g["AssetName"], "fleetNumber": g["FleetNumber"],
                              "confirmedCases": 0, "worstGapKm": 0})
        stats[k]["confirmedCases"] += 1
        stats[k]["worstGapKm"] = max(stats[k]["worstGapKm"], g["DistanceKm"])
    return sorted(stats.values(), key=lambda v: -v["worstGapKm"])


def already_imported_today(data_path=None):
    data_path = data_path or os.path.join(DATA_DIR, "fleet_today.json")
    if not os.path.exists(data_path):
        return False
    with open(data_path) as f:
        existing = json.load(f)
    generated = existing.get("meta", {}).get("generated", "")
    today_str = datetime.now().strftime("%d %B %Y")
    return generated.startswith(today_str)


def run_import(username=None, password=None, force=False):
    if not force and already_imported_today():
        return {"status": "skipped", "reason": "already imported today"}

    username = username or os.environ.get("EMAIL_ADDRESS")
    password = password or os.environ.get("EMAIL_PASSWORD")
    if not username or not password:
        raise ImportError_("EMAIL_ADDRESS / EMAIL_PASSWORD not set")

    paths = fetch_reports(username, password)

    try:
        import sheets_store
        feedback_rows = sheets_store.load_feedback()
    except RuntimeError as e:
        print(f"WARNING: feedback unavailable this run ({e}), continuing without it.")
        feedback_rows = {}

    data = process_reports(paths, feedback_rows=feedback_rows)
    classification = data.pop("_classification", {})

    # meta.generated reflects the latest timestamp found IN the report
    # data itself, which naturally lags real time (assets report
    # periodically, not continuously) - that's fine for "as of" display,
    # but wrong for detecting "did the scheduled import actually run
    # recently", which needs real wall-clock time instead. Kept
    # separate rather than overloading one field for two questions.
    data["meta"]["importedAt"] = datetime.now().strftime("%d %B %Y, %H:%M")

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "fleet_today.json"), "w") as f:
        json.dump(data, f, default=str)

    # Best-effort and last: the dashboard's own data is already safely
    # written above regardless of anything below. A vehicle whose status
    # just flipped Offline->Online, with an unanswered follow-up still on
    # file, gets asked (not told) whether that resolves it - see
    # notifications.check_reconnections for why this is a question, not
    # an automatic close.
    base_url = _public_base_url()
    try:
        import notifications
        prompted = notifications.check_reconnections(classification, base_url=base_url)
        if prompted:
            print(f"Reconnect-check sent for: {', '.join(prompted)}")
    except Exception as e:
        print(f"Reconnect-check pass failed: {e}")

    # Weekly (configurable) rollups - snapshots of current state, not
    # tied to this cycle's transitions, so they're independent of
    # check_reconnections above and of each other.
    try:
        digest_settings = load_settings(os.path.join(DATA_DIR, "settings.ini"))
        import notifications
        pending_result = notifications.send_pending_confirmation_digest(
            classification, base_url,
            digest_settings["PENDING_DIGEST_INTERVAL_DAYS"],
            digest_settings["PENDING_CONFIRMATION_OVERDUE_DAYS"])
        if pending_result["sent"]:
            print(f"Pending-confirmation digest sent for {pending_result['count']} vehicle(s)")
        escalation_result = notifications.send_technical_escalation_digest(
            classification, base_url, digest_settings["ESCALATION_DIGEST_INTERVAL_DAYS"])
        if escalation_result["sent"]:
            print(f"Technical-escalation digest sent for {escalation_result['count']} vehicle(s)")
    except Exception as e:
        print(f"Digest pass failed: {e}")

    return {"status": "ok", "generated": data["meta"]["generated"]}


def _public_base_url():
    """
    Same purpose as app.py's public_base_url(), deliberately reimplemented
    rather than imported: this module runs standalone (its own load_dotenv
    call, no Flask app object) as often as it runs inside a request, and
    the /api/refresh-if-stale trigger specifically calls it from a
    background thread with no active Flask request context at all -
    touching flask.request there would raise, not just return the wrong
    value. Render injects RENDER_EXTERNAL_URL automatically, so production
    needs no extra configuration; local runs without PUBLIC_BASE_URL set
    just won't get working respond links in the reconnect-check email,
    same tradeoff every other local-dev email test in this codebase makes.
    """
    return (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
