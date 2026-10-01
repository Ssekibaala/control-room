"""
DDR (Device Diagnostic Report) - a per-client, per-device-category health
report shaped after a real report your team was already sending clients
by hand (see the AGL sample this was built from). Three categories, not
four: the sample split cameras into "MITAC" and "AD Plus AI" as two
separate brands, but this app has no reliable data source mapped to
MITAC devices at all - only the three categories below are ever
populated:

    OBC Status         <- MiX Unity platform    (PLATFORM_SHORT "MIX", mixSeen)
    AD Plus AI Camera Status <- FT Cloud platform (PLATFORM_SHORT "CAM", camSeen)
    Fuel Probe Status  <- Teletrac (white-label) platform (PLATFORM_SHORT "TLT", tltSeen)

Deliberately built from the SAME per-vehicle rows the dashboard's Full
Data table already renders (control_room._integrity_row's output, the
"full" section of a dashboard-data payload) rather than reading raw
platform snapshots directly - that's what already carries the
mixSeen/camSeen/tltSeen per-platform timestamps, the current status, and
the existing feedback/action trail for each plate, so this needs no new
data pipeline of its own and stays automatically in sync with whatever
the dashboard already shows. This is what makes it a template rather
than something built for one client: give it any client's rows and it
produces the same three-sheet report, unmodified.
"""

import io
from datetime import datetime

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# (category key, sheet title, the per-platform "seen" field that decides
# whether a row belongs in this category, the human column label for it)
CATEGORIES = [
    {"key": "obc", "title": "OBC Status", "seen_field": "mixSeen", "position_label": "Last Position"},
    {"key": "camera", "title": "AD Plus AI Camera Status", "seen_field": "camSeen", "position_label": "Positioning Time"},
    {"key": "fuel", "title": "Fuel Probe Status", "seen_field": "tltSeen", "position_label": "Last Reading"},
]

# The report's own timestamp format, from control_room._format_last_position -
# needed here to tell whether a category's own "seen" value is from today.
_SEEN_FMT = "%d %b %Y, %H:%M"

_NO_DATA = "No data"


def rows_for_category(full_rows, category):
    """Every row that actually has data from this category's platform -
    a vehicle with no MiX Unity feed at all has nothing to say in the
    OBC sheet, and belongs in neither sheet rather than as a padded-out
    blank row."""
    field = category["seen_field"]
    return [r for r in full_rows if r.get(field) and r[field] != _NO_DATA]


def _clean(value):
    """
    Some rows in fleet_today.json carry the literal string "None" here
    (confirmed on real data - an artifact of an earlier pipeline run
    stringifying a missing value upstream, not anything this module
    does), which would otherwise print the word "None" straight into a
    client-facing spreadsheet. Treated as blank, the same as a genuinely
    missing value, regardless of which upstream step produced it.
    """
    text = str(value or "").strip()
    return "" if text.lower() == "none" else text


def _platform_reported_today(seen_value, report_date):
    if not seen_value:
        return False
    try:
        return datetime.strptime(seen_value, _SEEN_FMT).date() == report_date.date()
    except ValueError:
        return False


def _platform_status_label(row, seen_value, report_date):
    """
    The row's "status" is a whole-VEHICLE verdict (classify_fleet only
    escalates to "Technical Escalation" once EVERY platform is offline;
    if even one is still reporting, the vehicle reads "Pending Customer
    Confirmation" no matter how long a DIFFERENT platform has been dead).
    That is exactly why passing it through here was wrong: a truck whose
    MiX Unity has been silent for 60 days but whose fuel probe reported
    ten minutes ago still carries vehicle-wide "Pending Customer
    Confirmation" - and this sheet is specifically about the fuel probe,
    which has nothing to be confirmed about right now.

    So this ignores the vehicle-wide status entirely and judges THIS
    category's own device on its own freshness: reported today -> Online.
    Not reported today -> Pending Customer Confirmation, needing the
    client to confirm THIS device specifically, or "Known Issue" if
    that absence was already explained by the client.

    Freshness is checked FIRST, "Known Issue" second - deliberately, and
    the order matters: a "Known Issue" feedback entry (e.g. "vehicle got
    an accident") never expires or auto-clears on its own, so once the
    device is back and reporting again, checking status before freshness
    kept showing "Known Issue" on a device that is plainly online right
    now, on the one sheet whose whole point is "is this device reporting
    today" - actively misleading, not just stale. Reported today wins:
    the device is online, full stop, whatever an old feedback note says.
    """
    if _platform_reported_today(seen_value, report_date):
        return "Online"
    if (row.get("status") or "") == "Known Issue":
        return "Known Issue"
    return "Pending Customer Confirmation"


def _sheet_rows(full_rows, category, report_date):
    out = []
    for r in rows_for_category(full_rows, category):
        position = r.get(category["seen_field"]) or ""
        status = _platform_status_label(r, position, report_date)
        # Both comment columns are what people actually wrote on the
        # platform - the latest "Recommended Action" entry and the latest
        # "Customer Feedback" entry (see feedback_overlay._apply_to_row()).
        # Only shown for a device that's offline: on an Online row an old
        # note ("asset in the workshop") describes a problem that's over.
        offline = status != "Online"
        out.append({
            "client": r.get("client") or "",
            "plate": r.get("plate") or "",
            "position": position,
            "status": status,
            "techComment": _clean(r.get("technicianComment")) if offline else "",
            "customerFeedback": _clean(r.get("customerComment")) if offline else "",
        })
    # Newest report first - the point of this column is "which of these
    # needs a second look right now", and that's the stalest entries,
    # not whichever plate happens to sort first alphabetically. A
    # position that fails to parse (shouldn't happen - every row here
    # came through rows_for_category, which already excludes "No data")
    # sorts to the very end rather than crashing the export.
    def _position_key(row):
        try:
            return datetime.strptime(row["position"], _SEEN_FMT)
        except ValueError:
            return datetime.min
    out.sort(key=_position_key, reverse=True)
    return out


_COLUMNS = [
    {"key": "client", "label": "Organization Name"},
    {"key": "plate", "label": "Registration Number"},
    {"key": "position", "label": None},  # label filled in per-sheet from category["position_label"]
    {"key": "status", "label": "Status"},
    {"key": "techComment", "label": "Technician's Comment"},
    {"key": "customerFeedback", "label": "Customer Feedback"},
]


def build_workbook(full_rows, client_name=None, report_date=None):
    """
    full_rows: the "full" list from a dashboard-data payload with
    feedback already overlaid (app.py's _overlay_feedback) - that's what
    carries the technicianComment/customerComment fields.
    client_name: restrict to one client's vehicles, or None for every
    client the caller is entitled to (the caller is responsible for
    that scoping - see app.py's /api/ddr/export, which never calls this
    with rows the session isn't allowed to see in the first place).
    report_date: defaults to now - stamped into each sheet's title row,
    same convention as report_writer.py's Fleet Integrity workbook.
    """
    report_date = report_date or datetime.now()
    if client_name:
        full_rows = [r for r in full_rows if (r.get("client") or "") == client_name]

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # replaced by one real sheet per category below

    for category in CATEGORIES:
        rows = _sheet_rows(full_rows, category, report_date)
        ws = wb.create_sheet(title=category["title"][:31])  # Excel's own 31-char sheet-name limit

        title = f"{category['title']} - {client_name or 'All Clients'}"
        ws.append([title])
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(_COLUMNS))
        ws.cell(row=1, column=1).font = Font(bold=True, size=13)
        ws.append([report_date.strftime("%d %B %Y, %H:%M")])
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(_COLUMNS))
        ws.cell(row=2, column=1).font = Font(italic=True, color="666666")
        ws.append([])

        header_row = 4
        labels = [c["label"] or category["position_label"] for c in _COLUMNS]
        ws.append(labels)
        for c in range(1, len(_COLUMNS) + 1):
            cell = ws.cell(row=header_row, column=c)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        if not rows:
            ws.append(["No vehicles reporting on this platform for this client."])
            ws.merge_cells(start_row=header_row + 1, start_column=1, end_row=header_row + 1, end_column=len(_COLUMNS))
        else:
            for r in rows:
                ws.append([r[c["key"]] for c in _COLUMNS])

        ws.freeze_panes = f"A{header_row + 1}"
        if ws.max_row > header_row:
            ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(_COLUMNS))}{ws.max_row}"
        for i, c in enumerate(_COLUMNS, start=1):
            col = get_column_letter(i)
            label = c["label"] or category["position_label"]
            cell_lens = [len(str(ws.cell(row=r, column=i).value or "")) for r in range(header_row + 1, ws.max_row + 1)]
            ws.column_dimensions[col].width = min(max([len(label)] + cell_lens) + 2, 50)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
