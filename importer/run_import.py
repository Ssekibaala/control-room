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
import threading
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
from schema import now_eat
import report_writer

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
WORK_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "_incoming")


def _write_json_atomic(path, data):
    """
    Write-then-rename instead of writing fleet_today.json directly. Every
    dashboard request reads this file (app.py's load_dashboard_data())
    from its own thread with no coordination with the writer beyond
    _FLEET_TODAY_LOCK (which only serializes writer-vs-writer, not
    writer-vs-reader) - a plain open(path, "w") + json.dump() is visible
    to a concurrent reader WHILE it's still being written, which would
    intermittently serve a truncated/invalid JSON response. os.replace()
    is atomic on both POSIX and Windows, so a reader only ever sees the
    fully-old or fully-new file.
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, default=str)
    os.replace(tmp_path, path)


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


def _parse_gap_date(g):
    """The gap's own date, for comparing against a plate's latest tamper
    check - ArrivalDate/ArrivalTime is when the vehicle actually went
    quiet (the start of the unexplained gap), the more meaningful anchor
    than NextDate (when it reappeared)."""
    try:
        return datetime.strptime(f"{g['ArrivalDate']} {g['ArrivalTime']}", "%d/%m/%Y %H:%M:%S")
    except (ValueError, KeyError):
        try:
            return datetime.strptime(g["ArrivalDate"], "%d/%m/%Y")
        except (ValueError, KeyError):
            return None


def _filter_checked_gaps(cases, tamper_checks):
    """Drops any gap already explained by a physical check dated on or
    after it - see sheets_store.load_tamper_checks()'s docstring for
    why the boundary is a check date, not a blanket per-plate
    suppression. A plate with no check record at all is untouched."""
    kept = []
    for g in cases:
        checks = tamper_checks.get(g["Plate"])
        if not checks:
            kept.append(g)
            continue
        latest_check = checks[0]["checkedAt"]  # load_tamper_checks() sorts newest first
        gap_date = _parse_gap_date(g)
        if latest_check and gap_date and gap_date <= latest_check:
            continue  # already physically explained
        kept.append(g)
    return kept


def _checked_assets_summary(tamper_checks):
    """Flat list for the dashboard's Checked Assets section and the
    tamper risk report email - every check record, newest first
    overall (not just newest-first per plate)."""
    rows = [
        {"plate": plate, "checkedBy": c["checkedBy"],
         "checkedAt": c["checkedAt"].strftime("%d %b %Y, %H:%M") if c["checkedAt"] else "",
         "comment": c["comment"]}
        for plate, checks in tamper_checks.items() for c in checks
    ]
    rows.sort(key=lambda r: r["checkedAt"], reverse=True)
    return rows


def _day_over_day(results, previous_status):
    """
    The one place classify_fleet()'s fresh per-plate status gets diffed
    against what was recorded last cycle. Returns (recovered,
    newly_offline, current_status) - current_status is what the caller
    should persist (sheets_store.save_vehicle_status) so the NEXT
    cycle has something to diff against.

    previous_status=None means there's nothing to compare against yet
    (the very first import ever run, or last cycle's read failed) -
    recovered/newly_offline come back empty rather than guessed at, the
    same "no history yet" case report_writer.write_report() already
    has copy for via history_available.

    A plate absent from previous_status (new to the fleet this cycle)
    is excluded from both lists - it has no prior state to have
    transitioned FROM, "recovered"/"newly offline" wouldn't be a
    meaningful thing to say about it yet.
    """
    current_status = {
        plate: ("Online" if info.get("status") == "Online" else "Offline")
        for plate, info in results.items()
    }
    if previous_status is None:
        return [], [], current_status
    recovered, newly_offline = [], []
    for plate, status in current_status.items():
        if plate not in previous_status:
            continue
        was_online = previous_status.get(plate) == "Online"
        is_online = status == "Online"
        if is_online and not was_online:
            recovered.append(plate)
        elif was_online and not is_online:
            newly_offline.append(plate)
    return recovered, newly_offline, current_status


MIX_API_SNAPSHOT_PATH = os.path.join(DATA_DIR, "mix_api_snapshot.json")


def _load_mix_api_reports(snapshot_path=None, max_age_minutes=90, now=None):
    """
    Folds the live MiX API poller's most recent snapshot (app.py's
    background thread, see _mix_api_poll_once - runs in the SAME
    process as this import on the single Heroku web dyno, so the file
    is normally at most MIX_API_POLL_INTERVAL_MINUTES old) into this
    cycle's AssetReport rows, tagged source_platform="MiX Unity" so it
    competes on an equal footing with the mailed mix_movement/
    mix_mobile_status reports in classify_fleet's freshest-per-platform
    selection - being fresher (minutes old vs a day old), it will
    normally win, which is the point: MiX Unity's Online/Offline signal
    becomes API-driven in practice without the mailed reports having to
    be ripped out first.

    Purely additive and best-effort: if the poller hasn't produced a
    snapshot yet (not configured, or this process doesn't run the
    poller at all - run_import() is documented as also runnable
    standalone, outside app.py), this returns [] and behaviour is
    identical to before the API integration existed.

    max_age_minutes guards against folding in a snapshot from a poller
    that silently died - a stale-but-present snapshot would otherwise
    look like a fresh MiX Unity signal to classify_fleet.
    """
    snapshot_path = snapshot_path or MIX_API_SNAPSHOT_PATH
    if not os.path.exists(snapshot_path):
        return []
    try:
        with open(snapshot_path) as f:
            snapshot = json.load(f)
        fetched_at = datetime.fromisoformat(snapshot["fetchedAt"])
    except (ValueError, KeyError, json.JSONDecodeError):
        print(f"WARNING: MiX API snapshot at {snapshot_path} is unreadable, skipping.")
        return []
    age_minutes = ((now or now_eat()) - fetched_at).total_seconds() / 60
    if age_minutes > max_age_minutes:
        print(f"WARNING: MiX API snapshot is {age_minutes:.0f} min old (> {max_age_minutes}), "
              f"skipping - looks like the poller stopped running.")
        return []

    from schema import AssetReport, normalize_plate
    rows = []
    for org_id, entries in snapshot.get("byOrg", {}).items():
        for e in entries:
            plate = normalize_plate(e.get("plate", ""))
            if not plate:
                continue
            last_report_time = None
            if e.get("lastReportTime"):
                try:
                    last_report_time = datetime.fromisoformat(e["lastReportTime"])
                except ValueError:
                    pass
            rows.append(AssetReport(
                asset_plate=plate,
                source_platform="MiX Unity",
                report_type="position_api",
                last_report_time=last_report_time,
                last_lat=e.get("lat"),
                last_lon=e.get("lon"),
                last_location_text=e.get("locationText"),
                organisation_id=org_id,
                raw_row=e,
            ))
    return rows


TELETRAC_API_SNAPSHOT_PATH = os.path.join(DATA_DIR, "teletrac_api_snapshot.json")


def _load_teletrac_api_reports(snapshot_path=None, max_age_minutes=90, now=None):
    """
    Same idea as _load_mix_api_reports() above, mirrored for the live
    Teletrac API poller's snapshot (app.py's _teletrac_api_poll_once) -
    folded in tagged source_platform="Teletrac", the SAME name the
    mailed teletrac_csv report uses, so the two compete for the
    freshest-per-platform slot rather than counting as two independent
    platforms in _investigation_reasons' offline-platform tally.
    Being fresher, the API rows normally win. Purely additive
    and best-effort: no snapshot yet, or too old (poller stopped
    running), returns [] and behaviour is identical to before this
    integration existed.
    """
    snapshot_path = snapshot_path or TELETRAC_API_SNAPSHOT_PATH
    if not os.path.exists(snapshot_path):
        return []
    try:
        with open(snapshot_path) as f:
            snapshot = json.load(f)
        fetched_at = datetime.fromisoformat(snapshot["fetchedAt"])
    except (ValueError, KeyError, json.JSONDecodeError):
        print(f"WARNING: Teletrac API snapshot at {snapshot_path} is unreadable, skipping.")
        return []
    age_minutes = ((now or now_eat()) - fetched_at).total_seconds() / 60
    if age_minutes > max_age_minutes:
        print(f"WARNING: Teletrac API snapshot is {age_minutes:.0f} min old (> {max_age_minutes}), "
              f"skipping - looks like the poller stopped running.")
        return []

    from schema import AssetReport, normalize_plate
    rows = []
    for client_id, entries in snapshot.get("byClient", {}).items():
        for e in entries:
            plate = normalize_plate(e.get("plate", ""))
            if not plate:
                continue
            last_report_time = None
            if e.get("lastReportTime"):
                try:
                    last_report_time = datetime.fromisoformat(e["lastReportTime"])
                except ValueError:
                    pass
            rows.append(AssetReport(
                asset_plate=plate,
                source_platform="Teletrac",
                report_type="position_api",
                last_report_time=last_report_time,
                last_lat=e.get("lat"),
                last_lon=e.get("lon"),
                last_location_text=e.get("locationText"),
                imei=e.get("imei"),
                organisation_id=client_id,
                raw_row=e,
            ))
    return rows


FT_CLOUD_API_SNAPSHOT_PATH = os.path.join(DATA_DIR, "ft_cloud_api_snapshot.json")


def _load_ft_cloud_api_reports(snapshot_path=None, max_age_minutes=90, now=None):
    """
    Same idea as _load_mix_api_reports() above, mirrored for the live
    FT Cloud API poller's snapshot (app.py's _ft_cloud_api_poll_once) -
    folded in tagged source_platform="FT Cloud Camera", the SAME name
    the mailed ft_cloud_camera report uses, so the two compete for the
    freshest-per-platform slot rather than counting as two independent
    platforms in _investigation_reasons' offline-platform tally.
    Being fresher, the API rows normally win. Purely additive and
    best-effort: no snapshot yet, or too old (poller stopped running),
    returns [] and behaviour is identical to before this integration
    existed.
    """
    snapshot_path = snapshot_path or FT_CLOUD_API_SNAPSHOT_PATH
    if not os.path.exists(snapshot_path):
        return []
    try:
        with open(snapshot_path) as f:
            snapshot = json.load(f)
        fetched_at = datetime.fromisoformat(snapshot["fetchedAt"])
    except (ValueError, KeyError, json.JSONDecodeError):
        print(f"WARNING: FT Cloud API snapshot at {snapshot_path} is unreadable, skipping.")
        return []
    age_minutes = ((now or now_eat()) - fetched_at).total_seconds() / 60
    if age_minutes > max_age_minutes:
        print(f"WARNING: FT Cloud API snapshot is {age_minutes:.0f} min old (> {max_age_minutes}), "
              f"skipping - looks like the poller stopped running.")
        return []

    from schema import AssetReport, normalize_plate
    rows = []
    for fleet_id, entries in snapshot.get("byFleet", {}).items():
        for e in entries:
            plate = normalize_plate(e.get("plate", ""))
            if not plate:
                continue
            last_report_time = None
            if e.get("lastReportTime"):
                try:
                    last_report_time = datetime.fromisoformat(e["lastReportTime"])
                except ValueError:
                    pass
            rows.append(AssetReport(
                asset_plate=plate,
                source_platform="FT Cloud Camera",
                report_type="camera_status",
                last_report_time=last_report_time,
                last_lat=e.get("lat"),
                last_lon=e.get("lon"),
                status_note=e.get("statusNote"),
                organisation_id=fleet_id,
                raw_row=e,
            ))
    return rows


def process_reports(paths, settings_path=None, feedback_rows=None, tamper_checks=None, previous_status=None,
                     mix_api_reports=None, teletrac_api_reports=None, ft_cloud_api_reports=None):
    """
    Pure processing, no network. paths is the dict returned by
    fetch_reports() (or, for testing, pointed at the known-good local
    sample files). Returns the same data dict the dashboard serves.

    tamper_checks (sheets_store.load_tamper_checks()'s shape) and
    previous_status (sheets_store.load_vehicle_status()'s shape) are
    both loaded by the caller, same reason feedback_rows is: this
    function stays Sheets/network-free per its own docstring, the
    caller does the one read and hands the result in as plain data.
    mix_api_reports is the same idea - see _load_mix_api_reports(),
    which run_import() calls and passes in here; defaults to [] so
    calling process_reports() directly (as the tests do) is unaffected.
    teletrac_api_reports and ft_cloud_api_reports mirror
    mix_api_reports for the Teletrac and FT Cloud API pollers - see
    _load_teletrac_api_reports() and _load_ft_cloud_api_reports().
    """
    check_periods_overlap(paths["mix_movement"], paths["mix_power_events"])

    settings = load_settings(settings_path or os.path.join(DATA_DIR, "settings.ini"))
    feedback = feedback_rows or {}
    tamper_checks = tamper_checks or {}

    all_rows = []
    all_rows += teletrac_csv.parse(paths["teletrac_offline"])
    all_rows += mix_mobile_status.parse(paths["mix_mobile_status"])
    all_rows += mix_movement.parse(paths["mix_movement"])
    all_rows += mix_power_events.parse(paths["mix_power_events"])
    all_rows += ft_cloud_camera.parse(paths["ft_cloud_camera"])
    all_rows += mix_api_reports or []
    all_rows += teletrac_api_reports or []
    all_rows += ft_cloud_api_reports or []

    grouped, skipped = group_by_plate(all_rows)
    timestamps = [r.last_report_time for r in all_rows if r.last_report_time]
    now = max(timestamps) if timestamps else now_eat()
    results = classify_fleet(grouped, settings, feedback, now=now)

    tamper_result = run_tamper_analysis(paths["mix_movement"], paths["mix_power_events"])
    confirmed_cases = _filter_checked_gaps(
        [_tamper_case_to_loader_shape(g) for g in tamper_result["confirmed"]], tamper_checks)
    unconfirmed_cases = _filter_checked_gaps(
        [_tamper_case_to_loader_shape(g) for g in tamper_result["unconfirmed"]], tamper_checks)
    tampering = {
        "confirmed": confirmed_cases,
        "unconfirmed": unconfirmed_cases,
        "summary": {
            "gaps_checked": len(tamper_result["gaps"]),
            "mismatches": len(tamper_result["mismatches"]),
            "confirmed": len(confirmed_cases),
            "unconfirmed": len(unconfirmed_cases),
            "null_gps_excluded": len(tamper_result["skipped"]),
            "period_label": "",
        },
        "severity_bands": _severity_bands_from_tamper_result(confirmed_cases, unconfirmed_cases),
        "top_vehicles": _top_vehicles_from_tamper_result(confirmed_cases),
        "quality_log": [_quality_skip_to_loader_shape(s) for s in tamper_result["skipped"]],
    }
    checked_assets = _checked_assets_summary(tamper_checks)
    tampering["checked_assets"] = checked_assets

    recovered, newly_offline, current_status = _day_over_day(results, previous_status)
    history_available = previous_status is not None

    # Generate the actual downloadable .xlsx files - same formatting as
    # the original standalone tool (report_writer.write_report() for
    # Fleet Integrity, tamper_engine.build_workbook() for Tampering Risk)
    # - then hand their paths to _build_data(), which embeds them via
    # its own _b64_file() helper.
    os.makedirs(DATA_DIR, exist_ok=True)
    integrity_path = os.path.join(DATA_DIR, "GTL_integrity_report.xlsx")
    tamper_path = os.path.join(DATA_DIR, "GTL_tampering_report.xlsx")
    report_writer.write_report(results, settings, recovered=recovered, newly_offline=newly_offline,
                                output_path=integrity_path, report_date=now, history_available=history_available)
    build_tamper_workbook(tamper_result, tamper_path)

    data = _build_data(results, settings, tampering, recovered, newly_offline, now, history_available,
                        integrity_path, tamper_path, checked_assets=checked_assets)
    data["_skipped_plates"] = sorted(set(skipped))
    # Stashed so run_import() can react to Offline->Online transitions
    # (see notifications.check_reconnections) without this function - kept
    # deliberately network-free per its own docstring - doing any Sheets
    # reads or sending mail itself. Popped back out before the dashboard
    # JSON is written; it's classify_fleet()'s raw internal shape, not
    # something the frontend has any use for.
    data["_classification"] = results
    # Same reasoning, for the weekly tamper risk report email.
    data["_tampering"] = tampering
    # current_status is what the caller persists for NEXT cycle's
    # comparison (sheets_store.save_vehicle_status) - recovered is
    # reused as-is by notifications.check_reconnections so it isn't
    # recomputed a second time just to decide who to email.
    data["_current_status"] = current_status
    data["_recovered"] = recovered
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


def _severity_bands_from_tamper_result(confirmed_cases, unconfirmed_cases):
    """Takes the already loader-shaped, already checked-gap-filtered
    lists (see process_reports) rather than the raw tamper_result, so a
    physically-checked gap is excluded from these counts too, not just
    from the confirmed/unconfirmed tables themselves."""
    from tamper_engine import severity
    bands = []
    for sname, rule in [("Critical", "> 100 km"), ("High", "10 - 100 km"), ("Moderate", "2 - 10 km")]:
        confirmed = sum(1 for g in confirmed_cases if severity(g["DistanceKm"]) == sname)
        unconfirmed = sum(1 for g in unconfirmed_cases if severity(g["DistanceKm"]) == sname)
        bands.append({"severity": sname, "rule": rule, "confirmed": confirmed, "unconfirmed": unconfirmed})
    return bands


def _top_vehicles_from_tamper_result(confirmed_cases):
    """Same note as above - takes the filtered, loader-shaped confirmed
    list (Plate/Vehicle/FleetNumber/DistanceKm/AssetID), not the raw
    tamper_result."""
    stats = {}
    for g in confirmed_cases:
        k = g["AssetID"]
        stats.setdefault(k, {"assetId": k, "vehicle": g["Vehicle"], "fleetNumber": g["FleetNumber"],
                              "confirmedCases": 0, "worstGapKm": 0})
        stats[k]["confirmedCases"] += 1
        stats[k]["worstGapKm"] = max(stats[k]["worstGapKm"], g["DistanceKm"])
    return sorted(stats.values(), key=lambda v: -v["worstGapKm"])


def already_imported_today(data_path=None):
    """
    Gates the once-daily mail-fetch pipeline (run_import(), below) - NOT
    a general "is the dashboard fresh" check, that's meta.generated's
    job (see _build_data() and refresh_live_snapshot()). Deliberately
    checks meta.importedAt (wall-clock time the real mail-based pipeline
    last actually ran) rather than meta.generated (the freshest
    timestamp found IN the report data): now that live API polling
    folds today-dated positions into every cycle's data regardless of
    whether the daily mail fetch has run yet, meta.generated is
    effectively always "today" - using it here would make this gate
    permanently true and silently skip the real mail fetch. importedAt
    is only ever written by run_import() itself, never by
    refresh_live_snapshot()'s lightweight regenerations, so it stays an
    accurate answer to "did the real pipeline run today."
    """
    data_path = data_path or os.path.join(DATA_DIR, "fleet_today.json")
    if not os.path.exists(data_path):
        return False
    with open(data_path) as f:
        existing = json.load(f)
    imported_at = existing.get("meta", {}).get("importedAt", "")
    today_str = now_eat().strftime("%d %B %Y")
    return imported_at.startswith(today_str)


# Guards fleet_today.json's read-merge-write critical section - the once-
# daily run_import() and the ~5-min refresh_live_snapshot() run on
# independent timers/threads (see app.py's pollers) and both write this
# same file; without this, two overlapping writes could interleave and
# corrupt the JSON rather than just one of them winning cleanly.
_FLEET_TODAY_LOCK = threading.Lock()


def _cached_report_paths():
    """
    The paths fetch_reports() would have written today's (or the last
    run's) mail reports to. Reused by refresh_live_snapshot() so a
    lightweight API-driven reclassification never touches the mailbox -
    only files already on disk from the last real mail fetch. Returns
    None if any expected file is missing (most commonly: no mail-based
    import has ever run in this environment yet), which the caller
    treats as "nothing to refresh from yet."
    """
    paths = {
        "teletrac_offline": os.path.join(WORK_DIR, "teletrac_offline.csv"),
        "mix_mobile_status": os.path.join(WORK_DIR, "mix_mobile_status.csv"),
        "mix_movement": os.path.join(WORK_DIR, "mix_movement.csv"),
        "mix_power_events": os.path.join(WORK_DIR, "mix_power_events.csv"),
        "ft_cloud_camera": os.path.join(WORK_DIR, "ft_cloud_camera.zip"),
    }
    if not all(os.path.exists(p) for p in paths.values()):
        return None
    return paths


def refresh_live_snapshot():
    """
    Lightweight companion to run_import(): reclassifies against the SAME
    mail report files already on disk from the last real import, but
    with freshly-polled MiX/Teletrac/FT Cloud API snapshots folded in -
    so the dashboard's Online/Offline status and last-seen times advance
    on the API poll cadence (minutes, not once-daily) instead of waiting for tomorrow's
    mail fetch. Called from app.py after each successful platform poll
    (see _mix_api_poll_once and its Teletrac/FT Cloud counterparts).

    Deliberately does NOT do what run_import() does beyond writing
    fleet_today.json:
      - no mail fetch (reuses cached files, see _cached_report_paths())
      - does not call sheets_store.save_vehicle_status() - that write is
        the day-over-day anchor Recovered/Newly-Offline and
        check_reconnections compare against; updating it every 30
        minutes would compare each cycle to the PREVIOUS cycle instead
        of to yesterday, making assets look like they "recovered" or
        "went offline" every half hour instead of once a day
      - sends no notifications or digests
      - does not touch meta.importedAt - already_imported_today()'s
        gate and app.py's /api/refresh-if-stale staleness check both
        key off that field meaning "the real mail-based pipeline ran";
        a lightweight refresh must never be mistaken for one of those
    """
    paths = _cached_report_paths()
    if paths is None:
        return {"status": "skipped", "reason": "no cached mail reports yet - real import hasn't run"}

    try:
        import sheets_store
        feedback_rows = sheets_store.load_feedback()
    except Exception:
        feedback_rows = {}
    try:
        tamper_checks = sheets_store.load_tamper_checks()
    except Exception:
        tamper_checks = {}
    try:
        previous_status = sheets_store.load_vehicle_status()
    except Exception:
        previous_status = None

    mix_api_reports = _load_mix_api_reports()
    teletrac_api_reports = _load_teletrac_api_reports()
    ft_cloud_api_reports = _load_ft_cloud_api_reports()

    try:
        data = process_reports(paths, feedback_rows=feedback_rows, tamper_checks=tamper_checks,
                               previous_status=previous_status, mix_api_reports=mix_api_reports,
                               teletrac_api_reports=teletrac_api_reports,
                               ft_cloud_api_reports=ft_cloud_api_reports)
    except ImportError_ as e:
        # e.g. check_periods_overlap() tripping on a cached mail-file
        # pairing issue - not something retrying an API poll fixes.
        return {"status": "error", "reason": str(e)}

    data.pop("_classification", None)
    data.pop("_tampering", None)
    data.pop("_current_status", None)
    data.pop("_recovered", None)

    existing_path = os.path.join(DATA_DIR, "fleet_today.json")
    with _FLEET_TODAY_LOCK:
        # Preserve whatever the last REAL run_import() wrote here - see
        # this function's own docstring for why this must not become "now".
        if os.path.exists(existing_path):
            with open(existing_path) as f:
                data["meta"]["importedAt"] = json.load(f).get("meta", {}).get("importedAt", "")
        data["meta"]["liveRefreshedAt"] = now_eat().strftime("%d %B %Y, %H:%M")

        os.makedirs(DATA_DIR, exist_ok=True)
        _write_json_atomic(existing_path, data)

    return {"status": "ok", "generated": data["meta"]["generated"]}


def run_import(username=None, password=None, force=False, force_digests=False, prompted_by=None):
    """
    force_digests + prompted_by back the dashboard's "Send check-in now"
    button (app.py's /api/checkin/trigger): force_digests bypasses each
    digest's own interval gate for this one run only (the gate itself,
    and every other call site, is untouched), and prompted_by is who
    clicked it, recorded via sheets_store.record_manual_checkin() so
    the dashboard can show "last sent by X on Y" back to everyone, not
    just whoever happened to trigger it. A no-op for the ordinary
    scheduled import (both default to "don't force, nobody prompted").
    """
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

    try:
        tamper_checks = sheets_store.load_tamper_checks()
    except Exception as e:
        print(f"WARNING: tamper checks unavailable this run ({e}), continuing without them.")
        tamper_checks = {}

    try:
        previous_status = sheets_store.load_vehicle_status()
    except Exception as e:
        print(f"WARNING: previous vehicle status unavailable this run ({e}), "
              f"Recovered/New will show empty and no reconnect-check can fire this cycle.")
        previous_status = None

    mix_api_reports = _load_mix_api_reports()
    if mix_api_reports:
        print(f"Folded in {len(mix_api_reports)} MiX API position(s) from the live poller")

    teletrac_api_reports = _load_teletrac_api_reports()
    if teletrac_api_reports:
        print(f"Folded in {len(teletrac_api_reports)} Teletrac API position(s) from the live poller")

    ft_cloud_api_reports = _load_ft_cloud_api_reports()
    if ft_cloud_api_reports:
        print(f"Folded in {len(ft_cloud_api_reports)} FT Cloud API status row(s) from the live poller")

    data = process_reports(paths, feedback_rows=feedback_rows, tamper_checks=tamper_checks,
                           previous_status=previous_status, mix_api_reports=mix_api_reports,
                           teletrac_api_reports=teletrac_api_reports,
                           ft_cloud_api_reports=ft_cloud_api_reports)
    classification = data.pop("_classification", {})
    tampering = data.pop("_tampering", {})
    current_status = data.pop("_current_status", {})
    recovered = data.pop("_recovered", [])

    # meta.generated reflects the latest timestamp found IN the report
    # data itself, which naturally lags real time (assets report
    # periodically, not continuously) - that's fine for "as of" display,
    # but wrong for detecting "did the scheduled import actually run
    # recently", which needs real wall-clock time instead. Kept
    # separate rather than overloading one field for two questions.
    data["meta"]["importedAt"] = now_eat().strftime("%d %B %Y, %H:%M")

    with _FLEET_TODAY_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        _write_json_atomic(os.path.join(DATA_DIR, "fleet_today.json"), data)

    # This cycle's status becomes NEXT cycle's "previous_status" - saved
    # regardless of what happens below, since losing this write would
    # mean the day after this one starts back at "no history yet"
    # rather than one bad cycle just being skipped.
    try:
        sheets_store.save_vehicle_status(current_status)
    except Exception as e:
        print(f"Could not persist this cycle's vehicle status: {e}")

    # Best-effort and last: the dashboard's own data is already safely
    # written above regardless of anything below. A vehicle whose status
    # just flipped Offline->Online, with an unanswered follow-up still on
    # file, gets asked (not told) whether that resolves it - see
    # notifications.check_reconnections for why this is a question, not
    # an automatic close. `recovered` is already the exact same
    # Offline->Online list computed above - check_reconnections only
    # has to decide, per plate in it, whether there's anything worth
    # emailing about, not recompute the transition itself.
    base_url = _public_base_url()
    try:
        import notifications
        prompted = notifications.check_reconnections(classification, recovered, base_url=base_url)
        if prompted:
            print(f"Reconnect-check sent for: {', '.join(prompted)}")
    except Exception as e:
        print(f"Reconnect-check pass failed: {e}")

    # Separate from the reconnect-check above: a plain FYI for EVERY
    # recovery this cycle, not just the ones with an unanswered
    # follow-up. No interval gate - see send_recovery_notice's own
    # docstring for why a schedule doesn't fit this one.
    try:
        recovery_result = notifications.send_recovery_notice(classification, recovered)
        if recovery_result["sent"]:
            print(f"Recovery notice sent for {recovery_result['count']} vehicle(s)")
    except Exception as e:
        print(f"Recovery notice failed: {e}")

    # Weekly (configurable) rollups - snapshots of current state, not
    # tied to this cycle's transitions, so they're independent of
    # check_reconnections above and of each other.
    digest_results = {}
    try:
        digest_settings = load_settings(os.path.join(DATA_DIR, "settings.ini"))
        import notifications

        def _interval(key):
            return 0 if force_digests else digest_settings[key]

        pending_result = notifications.send_pending_confirmation_digest(
            classification, base_url,
            _interval("PENDING_DIGEST_INTERVAL_DAYS"),
            digest_settings["PENDING_CONFIRMATION_OVERDUE_DAYS"])
        digest_results["pending_confirmation"] = pending_result
        if pending_result["sent"]:
            print(f"Pending-confirmation digest sent for {pending_result['count']} vehicle(s)")
        escalation_result = notifications.send_technical_escalation_digest(
            classification, base_url, _interval("ESCALATION_DIGEST_INTERVAL_DAYS"))
        digest_results["technical_escalation"] = escalation_result
        if escalation_result["sent"]:
            print(f"Technical-escalation digest sent for {escalation_result['count']} vehicle(s)")
        checkin_result = notifications.send_known_issues_checkin_digest(
            classification, base_url, _interval("KNOWN_ISSUES_CHECKIN_INTERVAL_DAYS"))
        digest_results["known_issues_checkin"] = checkin_result
        if checkin_result["sent"]:
            print(f"Known-issues check-in digest sent for {checkin_result['count']} vehicle(s)")

        # Not part of the manual "check-in" button (that's specifically
        # pending/escalation/known-issues, see app.py's route) - stays on
        # its own weekly schedule regardless of force_digests.
        tamper_result = notifications.send_tamper_risk_report_digest(
            tampering, base_url, digest_settings["TAMPER_REPORT_INTERVAL_DAYS"])
        digest_results["tamper_risk_report"] = tamper_result
        if tamper_result["sent"]:
            print(f"Tamper risk report sent for {tamper_result['count']} vehicle(s)")

        if force_digests:
            try:
                sheets_store.record_manual_checkin(prompted_by or "unknown")
            except Exception as e:
                print(f"Manual check-in triggered but could not record who prompted it: {e}")
    except Exception as e:
        print(f"Digest pass failed: {e}")

    return {"status": "ok", "generated": data["meta"]["generated"], "digests": digest_results}


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

    Same guard as app.py's public_base_url(): a PUBLIC_BASE_URL that
    got left set to a local-dev value (this exact incident happened -
    "http://localhost:5000" ended up configured on the live Render
    service) must not win. Unlike app.py, there's no live request to
    fall back to here, so a rejected value becomes "" rather than a
    wrong-but-present one - check_reconnections/send_*_digest already
    skip sending rather than mail out a dead link when base_url is empty.
    """
    configured = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    if configured and any(marker in configured.lower() for marker in ("localhost", "127.0.0.1", "0.0.0.0")):
        print(f"WARNING: ignoring PUBLIC_BASE_URL/RENDER_EXTERNAL_URL={configured!r} - "
              f"looks like a local-dev leftover, not a real public domain.")
        return ""
    return configured.rstrip("/")
