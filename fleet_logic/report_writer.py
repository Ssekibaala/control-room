"""
Generates the two downloadable .xlsx reports (Fleet Integrity, Device
Tampering Risk) from the exact same rows _build_data() already computed
for the dashboard, so there is no separate calculation path that could
drift from what's shown on screen. control_room.py's own docstring
says these files get "embedded as base64... downloaded byte-for-byte
identical to the originals" - this module is what actually produces
that byte-for-byte source, since nothing else in this pipeline does.

The Summary sheet layout in write_tamper_report intentionally matches
what importer/test_tamper_engine.py's read_reference_summary() expects
to read back (a "Consecutive Trip Gaps Checked" header row followed by
a values row, and a "N trip boundaries were excluded..." line), so a
future run against real reference data stays comparable.
"""

import openpyxl
from openpyxl.styles import Font, PatternFill

HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)

INTEGRITY_COLS = [
    ("plate", "Plate"), ("status", "Status"), ("severity", "Severity"), ("days", "Days Offline"),
    ("lastPosition", "Last Position Time"), ("TLT", "Teletrac"), ("MIX", "MiX Unity"), ("CAM", "FT Cloud"),
    ("border", "Border Risk"), ("borderDetail", "Border Detail"), ("feedback", "Customer Feedback"),
    ("action", "Recommended Action"), ("reasons", "Investigation Reasons"), ("location", "Last Known Location"),
]

TAMPER_COLS = [
    ("plate", "Plate"), ("vehicle", "Vehicle"), ("fleetNumber", "Fleet Number"), ("severity", "Severity"),
    ("arrivalDate", "Arrival Date"), ("arrivalTime", "Arrival Time"), ("atLocation", "At Location"),
    ("nextDate", "Next Date"), ("nextTime", "Next Time"), ("fromLocation", "From Location"),
    ("distanceKm", "Distance (km)"), ("gapDuration", "Gap Duration"), ("impliedSpeed", "Implied Speed (km/h)"),
    ("powerEvent", "Power Event In Gap"),
]

QUALITY_COLS = [
    ("plate", "Plate"), ("vehicle", "Vehicle"), ("fleetNumber", "Fleet Number"),
    ("arrivalDate", "Arrival Date"), ("arrivalTime", "Arrival Time"), ("rawEnd", "Raw End Coordinate"),
    ("nextDate", "Next Departure Date"), ("nextTime", "Next Departure Time"), ("rawStart", "Raw Start Coordinate"),
]


def _write_table(ws, columns, rows):
    headers = [label for _, label in columns]
    keys = [key for key, _ in columns]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    for row in rows:
        ws.append([row.get(k, "") for k in keys])
    for col_cells in ws.columns:
        length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 45)


def write_integrity_report(data, path):
    """data is the dict _build_data() returns (before xlsxB64/tamperB64 are set)."""
    wb = openpyxl.Workbook()

    summary = wb.active
    summary.title = "Summary"
    summary.append(["GTL Fleet Integrity Report"])
    summary.append(["Generated", data["meta"]["generated"]])
    summary.append([])
    summary.append(["Metric", "Value"])
    for k, v in data["kpi"].items():
        summary.append([k, v])

    _write_table(wb.create_sheet("Full Data"), INTEGRITY_COLS, data["full"])
    _write_table(wb.create_sheet("Critical Assets"), INTEGRITY_COLS, data["critical"])
    _write_table(wb.create_sheet("Border Risk"), INTEGRITY_COLS, data["border"])
    _write_table(wb.create_sheet("Healthy Fleet"), INTEGRITY_COLS, data["healthy"])

    wb.save(path)


def write_tamper_report(data, tamper_summary, path):
    """data is the same _build_data() dict; tamper_summary is tampering['summary']."""
    wb = openpyxl.Workbook()

    summary = wb.active
    summary.title = "Summary"
    summary.append(["GTL Device Tampering Risk Report"])
    summary.append(["Generated", data["meta"]["generated"]])
    summary.append([])
    summary.append(["Consecutive Trip Gaps Checked", "Location Mismatches", "Confirmed", "Unconfirmed"])
    summary.append([
        tamper_summary.get("gaps_checked", 0), tamper_summary.get("mismatches", 0),
        tamper_summary.get("confirmed", 0), tamper_summary.get("unconfirmed", 0),
    ])
    summary.append([])
    summary.append([f"{tamper_summary.get('null_gps_excluded', 0)} trip boundaries were excluded for null GPS fix"])

    _write_table(wb.create_sheet("Confirmed Cases"), TAMPER_COLS, data["tamperConfirmed"])
    _write_table(wb.create_sheet("Unconfirmed Cases"), TAMPER_COLS, data["tamperUnconfirmed"])
    _write_table(wb.create_sheet("Data Quality Log"), QUALITY_COLS, data["qualityLog"])

    wb.save(path)
