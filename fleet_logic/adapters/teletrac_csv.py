"""
Adapter for: Bulk - Teletrac platform offline report.csv
Arrives as a CSV attachment on a scheduled email, subject line
"Bulk - Teletrac platform offline report.csv"

Confirmed real columns (from sample):
total_number_of_devices, company_name, asset_name, imei_number, sim_card,
network_provider, last_reported_date, last_known_coordinates, last_known_location

Only rows where company_name == 'Globe Trotters Ltd' belong to GTL; the
file contains every client Teletrac supports, so filtering is mandatory.
"""

import csv
import re
from datetime import datetime
from schema import AssetReport, normalize_plate

GTL_COMPANY_NAME = "Globe Trotters Ltd"


def _parse_coords(coord_str: str):
    # format: "Lat: -4.043715 Lon: 39.627088"
    if not coord_str:
        return None, None
    m = re.search(r'Lat:\s*(-?\d+\.?\d*)\s*Lon:\s*(-?\d+\.?\d*)', coord_str)
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def _parse_timestamp(ts_str: str):
    # format: "14 Jul 2026 23:19:24"
    try:
        return datetime.strptime(ts_str.strip(), "%d %b %Y %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def parse(csv_path: str, client_filter: str = GTL_COMPANY_NAME):
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for raw in reader:
            if raw.get('company_name', '').strip() != client_filter:
                continue
            lat, lon = _parse_coords(raw.get('last_known_coordinates', ''))
            rows.append(AssetReport(
                asset_plate=normalize_plate(raw.get('asset_name', '')),
                source_platform="Teletrac",
                report_type="offline_bulk",
                last_report_time=_parse_timestamp(raw.get('last_reported_date', '')),
                last_lat=lat,
                last_lon=lon,
                last_location_text=raw.get('last_known_location', '').strip(),
                imei=raw.get('imei_number', '').strip(),
                sim_card=raw.get('sim_card', '').strip(),
                network_provider=raw.get('network_provider', '').strip(),
                raw_row=raw,
            ))
    return rows
