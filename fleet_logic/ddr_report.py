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


def _known_issue_comment(row):
    """The client's own explanation for a Known Issue row - genuine
    hand-written content, kept verbatim regardless of any platform's
    freshness (that's the whole point of a Known Issue: the question of
    "why is this quiet" is already answered)."""
    return _clean(row.get("feedback"))


def _platform_recommended_comment(seen_value, report_date, long_term_fault_days, high_priority_days):
    """
    Same escalation ladder as classifier._recommended_action(), but
    keyed off how long THIS platform's own position has been stale,
    not the vehicle-wide days_silent (the STALEST of all three
    platforms). Reusing the vehicle-wide action text here was exactly
    the earlier bug: a truck whose MiX unit died 40 days ago but whose
    fuel probe reported 13 hours ago showed "Recover device - long-term
    fault" right next to a Last Position from yesterday, on the sheet
    that's specifically about the fuel probe - a technician's note
    about a completely different platform. Recomputed from THIS row's
    own position instead, so the comment always matches the date next
    to it.
    """
    try:
        seen_dt = datetime.strptime(seen_value, _SEEN_FMT)
    except (ValueError, TypeError):
        return ""
    days_silent = (report_date - seen_dt).total_seconds() / 86400
    if days_silent >= long_term_fault_days:
        return "Recover device - long-term fault, schedule field recovery"
    if days_silent >= high_priority_days:
        return "Schedule physical inspection this week"
    return "Contact customer for status confirmation"


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
    client to confirm THIS device specifically. The one carry-over is
    "Known Issue" - a deliberate, already-given client explanation, which
    stays put rather than being re-opened by a freshness check.
    """
    if (row.get("status") or "") == "Known Issue":
        return "Known Issue"
    if _platform_reported_today(seen_value, report_date):
        return "Online"
    return "Pending Customer Confirmation"


def _sheet_rows(full_rows, category, report_date, long_term_fault_days, high_priority_days):
    out = []
    for r in rows_for_category(full_rows, category):
        position = r.get(category["seen_field"]) or ""
        status = _platform_status_label(r, position, report_date)
        # A healthy device (Online) has nothing here to comment on. A
        # Known Issue keeps the client's own explanation verbatim - that
        # was never platform-specific to begin with. Everything else
        # (Pending Customer Confirmation) gets a comment recomputed from
        # THIS row's own position, not the vehicle-wide action field -
        # see _platform_recommended_comment()'s docstring for why that
        # distinction matters.
        if status == "Online":
            tech_comment = ""
        elif status == "Known Issue":
            tech_comment = _known_issue_comment(r)
        else:
            tech_comment = _platform_recommended_comment(position, report_date, long_term_fault_days, high_priority_days)
        out.append({
            "client": r.get("client") or "",
            "plate": r.get("plate") or "",
            "position": position,
            "status": status,
            "techComment": tech_comment,
            "customerFeedback": "",  # blank - this is the column the client fills in
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


def build_workbook(full_rows, client_name=None, report_date=None,
                    long_term_fault_days=30, high_priority_days=7):
    """
    full_rows: the "full" list from a dashboard-data payload (or
    RAW_DATA.full client-side) - every vehicle this session can see.
    client_name: restrict to one client's vehicles, or None for every
    client the caller is entitled to (the caller is responsible for
    that scoping - see app.py's /api/ddr/export, which never calls this
    with rows the session isn't allowed to see in the first place).
    report_date: defaults to now - stamped into each sheet's title row,
    same convention as report_writer.py's Fleet Integrity workbook.
    long_term_fault_days/high_priority_days: the same two thresholds
    settings.ini's [thresholds] section feeds classifier.py - the
    caller should pass the real configured values (see app.py's
    _load_settings()); defaulted here only so this module still works
    standalone (tests, a REPL) without wiring settings through.
    """
    report_date = report_date or datetime.now()
    if client_name:
        full_rows = [r for r in full_rows if (r.get("client") or "") == client_name]

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # replaced by one real sheet per category below

    for category in CATEGORIES:
        rows = _sheet_rows(full_rows, category, report_date, long_term_fault_days, high_priority_days)
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
