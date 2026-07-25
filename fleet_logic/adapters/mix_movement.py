"""
Adapter for: Daily Movement Report.csv (MiX Unity)
Real file is large (8.8MB, ~27k trip rows), one row per trip leg, not
one row per asset. We stream it and keep only the most recent arrival
per plate, since that is what matters for offline/last-seen detection.

Confirmed real columns include AssetExtra (plate-bearing free text),
SiteName (used for GTL filtering), ArrivalDate, ArrivalTime, ArriveAt,
EndLatLong.
"""

import csv
from datetime import datetime
from schema import AssetReport, normalize_plate

GTL_SITE_MARKERS = ("GTL", "Globe Trotters")


def _is_gtl(site_name: str) -> bool:
    return any(marker.lower() in site_name.lower() for marker in GTL_SITE_MARKERS)


def _parse_datetime(date_str: str, time_str: str):
    try:
        return datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%d/%m/%Y %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def _parse_latlong(s: str):
    try:
        lat, lon = s.split('/')
        return float(lat.strip()), float(lon.strip())
    except (ValueError, AttributeError):
        return None, None


def parse(csv_path: str):
    latest_by_plate = {}  # plate -> raw dict of the most recent arrival row

    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for raw in reader:
            if not _is_gtl(raw.get('SiteName', '')):
                continue
            plate = normalize_plate(raw.get('AssetExtra', '') or raw.get('AssetHostID', ''))
            if not plate:
                continue
            arrival = _parse_datetime(raw.get('ArrivalDate', ''), raw.get('ArrivalTime', ''))
            if arrival is None:
                continue
            current = latest_by_plate.get(plate)
            if current is None or arrival > current[0]:
                latest_by_plate[plate] = (arrival, raw)

    rows = []
    for plate, (arrival, raw) in latest_by_plate.items():
        lat, lon = _parse_latlong(raw.get('EndLatLong', ''))
        rows.append(AssetReport(
            asset_plate=plate,
            source_platform="MiX Unity",
            report_type="movement",
            last_report_time=arrival,
            last_lat=lat,
            last_lon=lon,
            last_location_text=raw.get('ArriveAt', '').strip(),
            raw_row=raw,
        ))
    return rows
