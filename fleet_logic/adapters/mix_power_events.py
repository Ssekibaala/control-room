"""
Adapter for: Detailed Event Report.csv (MiX Unity Power disconnections)
Confirmed real columns include AssetName, AssetExtra, EventDescription
("Power Disconnect" / "Power Reconnect"), EventStartDate, EventStartTime,
StartLocation / F_Start* fields.

This feeds the tampering-adjacent power event trail, distinct from your
existing EXE tampering tool, this is the raw power on/off log per asset
that your tool can also consume if you want to cross-check it later.
"""

import csv
from datetime import datetime
from schema import AssetReport, normalize_plate


def _parse_datetime(date_str: str, time_str: str):
    try:
        return datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%d/%m/%Y %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def parse(csv_path: str, event_types=("Power Disconnect", "Power Reconnect")):
    rows = []
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for raw in reader:
            if raw.get('EventDescription', '').strip() not in event_types:
                continue
            plate = normalize_plate(raw.get('AssetExtra') or raw.get('AssetName', ''))
            if not plate:
                continue
            rows.append(AssetReport(
                asset_plate=plate,
                source_platform="MiX Unity",
                report_type="power_event",
                last_report_time=_parse_datetime(raw.get('EventStartDate', ''), raw.get('EventStartTime', '')),
                last_location_text=raw.get('EventLocation', '').strip() or raw.get('StartLocation', '').strip(),
                status_note=raw.get('EventDescription', '').strip(),
                raw_row=raw,
            ))
    return rows
