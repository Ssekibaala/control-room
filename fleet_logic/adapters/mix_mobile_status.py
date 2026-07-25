"""
Adapter for: Current Mobile Status Report.csv (MiX Unity / Powerfleet)

Real-file discovery: this CSV contains TWO stacked report blocks, not one.
Block 1 (rows 1 to ~180): headers "MovementStatus,Description"
    - color-coded status (Black/Green/Red - Moving/Not Moving), asset name only,
      no timestamp. Useful only as a supplementary movement-color flag.
Block 2 (from the row where header repeats as
    "VehicleDesc,Reg,LastDriver,LastTrip,LastPositionDate,TimeZone,Location"):
    - this is the real data: plate (Reg), LastPositionDate (the timestamp we
      need for offline detection), and Location.

Strategy: scan for the second header row, treat everything from there on
as the authoritative source. Block 1's color status gets attached to the
matching plate as a bonus field if present.
"""

import csv
import re
from datetime import datetime
from schema import AssetReport, normalize_plate

BLOCK2_HEADER = "VehicleDesc,Reg,LastDriver,LastTrip,LastPositionDate,TimeZone,Location"


def _parse_timestamp(ts_str: str):
    # format: "14/07/2026 20:26"
    try:
        return datetime.strptime(ts_str.strip(), "%d/%m/%Y %H:%M")
    except (ValueError, AttributeError):
        return None


def _parse_movement_colors(lines):
    """Block 1: MovementStatus,Description -> {normalized_plate: color_status}"""
    colors = {}
    reader = csv.reader(lines)
    next(reader, None)  # header
    for row in reader:
        if len(row) < 2:
            continue
        status, desc = row[0].strip(), row[1].strip()
        if status.startswith("VehicleDesc"):
            break
        plate = normalize_plate(desc)
        if plate:
            colors[plate] = status
    return colors


def parse(csv_path: str):
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        raw_text = f.read()

    lines = raw_text.splitlines()
    split_idx = next((i for i, l in enumerate(lines) if l.startswith("VehicleDesc,Reg")), None)
    if split_idx is None:
        raise ValueError("Expected block-2 header 'VehicleDesc,Reg,...' not found in file")

    colors = _parse_movement_colors(lines[:split_idx])

    block2_text = "\n".join(lines[split_idx:])
    reader = csv.DictReader(block2_text.splitlines())

    rows = []
    for raw in reader:
        plate = normalize_plate(raw.get('Reg', ''))
        if not plate:
            continue
        rows.append(AssetReport(
            asset_plate=plate,
            source_platform="MiX Unity",
            report_type="mobile_status",
            last_report_time=_parse_timestamp(raw.get('LastPositionDate', '')),
            last_location_text=raw.get('Location', '').strip(),
            status_note=colors.get(plate),
            raw_row=raw,
        ))
    return rows
