"""
Device Tampering Risk Report - core engine, ported verbatim.

This is the exact analyse() logic from tampering_report.py, the
standalone GUI/CLI tool. Every constant, every threshold, every
classification rule is unchanged. The only thing that's different is
where the input CSVs come from (Gmail import vs. manual file picker)
and where the output goes (JSON for the dashboard API instead of only
an .xlsx file) - the detection logic itself is untouched, byte-for-byte
the same decisions.

If this file's output ever needs to be re-verified against the
original tool, run test_tamper_engine.py, which diffs this against a
real Device_Tampering_Risk_Report_v2.xlsx.
"""

import csv
import math
from datetime import datetime
from collections import defaultdict

# ----------------------------------------------------------------------
# Tunable settings - identical to tampering_report.py
# ----------------------------------------------------------------------
GAP_DISTANCE_THRESHOLD_KM = 2.0
MAX_PLAUSIBLE_SPEED_KMH = 140.0
NULL_FIX_TOLERANCE_DEG = 0.001
DATE_TIME_FORMAT = "%d/%m/%Y %H:%M:%S"

REQUIRED_MOVEMENT_COLS = [
    "AssetID", "AssetExtra", "FleetNumber", "DepartureDate", "DepartureTime",
    "ArrivalDate", "ArrivalTime", "StartLatLong", "EndLatLong",
    "DepartFrom", "ArriveAt", "StartOdoMeter", "EndOdoMeter",
]
REQUIRED_EVENT_COLS = ["AssetID", "EventDescription", "EventStartDate", "EventStartTime"]


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def parse_latlong(raw):
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    parts = [p.strip() for p in s.replace(",", "/").split("/")]
    if len(parts) < 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if abs(lat) < NULL_FIX_TOLERANCE_DEG and abs(lon) < NULL_FIX_TOLERANCE_DEG:
        return None
    return lat, lon


def parse_dt(date_str, time_str):
    return datetime.strptime(date_str.strip() + " " + time_str.strip(), DATE_TIME_FORMAT)


def parse_odo(raw):
    if not raw or not raw.strip():
        return None
    try:
        return float(raw.strip().replace(",", ""))
    except ValueError:
        return None


def clean_fleet_number(raw):
    return (raw or "").strip().strip(",")


def readable_duration(total_minutes):
    if total_minutes is None:
        return ""
    total_min = int(round(total_minutes))
    days, rem = divmod(total_min, 1440)
    hrs, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hrs:
        parts.append(f"{hrs}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


def severity(distance_km):
    if distance_km > 100:
        return "Critical"
    if distance_km >= 10:
        return "High"
    return "Moderate"


def read_csv_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def validate_columns(rows, required, label, path):
    if not rows:
        raise ValueError(f"{label} file is empty: {path}")
    missing = [c for c in required if c not in rows[0]]
    if missing:
        raise ValueError(
            f"{label} file '{path}' is missing expected column(s): {', '.join(missing)}\n"
            f"Columns found: {', '.join(rows[0].keys())}"
        )


def analyse(movement_csv_path, event_csv_path):
    """Identical logic and return shape to tampering_report.py's analyse()."""
    trips = read_csv_rows(movement_csv_path)
    validate_columns(trips, REQUIRED_MOVEMENT_COLS, "Daily Movement Report", movement_csv_path)

    events = read_csv_rows(event_csv_path)
    validate_columns(events, REQUIRED_EVENT_COLS, "Detailed Event Report", event_csv_path)

    by_asset = defaultdict(list)
    bad_trip_rows = 0
    for t in trips:
        try:
            dep_dt = parse_dt(t["DepartureDate"], t["DepartureTime"])
            arr_dt = parse_dt(t["ArrivalDate"], t["ArrivalTime"])
        except Exception:
            bad_trip_rows += 1
            continue
        by_asset[t["AssetID"]].append((dep_dt, arr_dt, t))

    events_by_asset = defaultdict(list)
    bad_event_rows = 0
    for e in events:
        try:
            edt = parse_dt(e["EventStartDate"], e["EventStartTime"])
        except Exception:
            bad_event_rows += 1
            continue
        events_by_asset[e["AssetID"]].append((edt, e))
    for a in events_by_asset:
        events_by_asset[a].sort(key=lambda x: x[0])

    def power_events_in_window(asset_id, start_dt, end_dt):
        return [(edt, e["EventDescription"]) for edt, e in events_by_asset.get(asset_id, [])
                if start_dt <= edt <= end_dt]

    gaps = []
    skipped = []

    for aid, tlist in by_asset.items():
        tlist.sort(key=lambda x: x[0])
        fleet = clean_fleet_number(tlist[0][2]["FleetNumber"])

        for i in range(len(tlist) - 1):
            dep_i, arr_i, trip_i = tlist[i]
            dep_j, arr_j, trip_j = tlist[i + 1]

            loc_end = parse_latlong(trip_i["EndLatLong"])
            loc_start = parse_latlong(trip_j["StartLatLong"])

            if loc_end is None or loc_start is None:
                skipped.append({
                    "AssetID": aid,
                    "AssetName": trip_i["AssetExtra"].strip(),
                    "FleetNumber": fleet,
                    "ArrivalDate": trip_i["ArrivalDate"],
                    "ArrivalTime": trip_i["ArrivalTime"],
                    "RawEndLatLong": trip_i["EndLatLong"],
                    "NextDepartureDate": trip_j["DepartureDate"],
                    "NextDepartureTime": trip_j["DepartureTime"],
                    "RawStartLatLong": trip_j["StartLatLong"],
                })
                continue

            dist = haversine(loc_end[0], loc_end[1], loc_start[0], loc_start[1])
            gap_minutes = (dep_j - arr_i).total_seconds() / 60.0
            if gap_minutes < 0:
                continue

            odo_end_i = parse_odo(trip_i["EndOdoMeter"])
            odo_start_j = parse_odo(trip_j["StartOdoMeter"])
            odo_jump = (odo_start_j - odo_end_i) if (odo_end_i is not None and odo_start_j is not None) else None

            implied_speed = dist / (gap_minutes / 60.0) if gap_minutes > 0 else None

            hits = power_events_in_window(aid, arr_i, dep_j)
            has_disconnect = any(desc == "Power Disconnect" for _, desc in hits)
            has_reconnect = any(desc == "Power Reconnect" for _, desc in hits)

            gaps.append({
                "AssetID": aid,
                "AssetName": trip_i["AssetExtra"].strip(),
                "FleetNumber": fleet,
                "ArrivalDate": trip_i["ArrivalDate"],
                "ArrivalTime": trip_i["ArrivalTime"],
                "ArriveAt": trip_i["ArriveAt"],
                "NextDepartureDate": trip_j["DepartureDate"],
                "NextDepartureTime": trip_j["DepartureTime"],
                "DepartFrom": trip_j["DepartFrom"],
                "DistanceKm": round(dist, 2),
                "GapMinutes": round(gap_minutes, 1),
                "OdoJumpKm": round(odo_jump, 2) if odo_jump is not None else None,
                "ImpliedSpeedKmh": round(implied_speed, 1) if implied_speed is not None else None,
                "HasDisconnect": has_disconnect,
                "HasReconnect": has_reconnect,
                "PowerEventCount": len(hits),
                "PlausibleSpeed": (implied_speed is None) or (implied_speed <= MAX_PLAUSIBLE_SPEED_KMH),
            })

    mismatches = [g for g in gaps if g["DistanceKm"] > GAP_DISTANCE_THRESHOLD_KM]
    confirmed_power_cycle = [g for g in mismatches if g["HasDisconnect"] and g["HasReconnect"]]
    confirmed_no_reconnect = [g for g in mismatches if g["HasDisconnect"] and not g["HasReconnect"]]
    confirmed = confirmed_power_cycle + confirmed_no_reconnect
    unconfirmed = [g for g in mismatches if not g["HasDisconnect"]]

    return {
        "total_trip_records": len(trips),
        "total_assets": len(by_asset),
        "bad_trip_rows": bad_trip_rows,
        "bad_event_rows": bad_event_rows,
        "gaps": gaps,
        "mismatches": mismatches,
        "confirmed": confirmed,
        "confirmed_power_cycle": confirmed_power_cycle,
        "confirmed_no_reconnect": confirmed_no_reconnect,
        "unconfirmed": unconfirmed,
        "skipped": skipped,
    }
