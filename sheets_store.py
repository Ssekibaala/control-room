"""
Feedback and history now live in a Google Sheet, not a local CSV or a
database file, per the decision to drop SQLite and Drive entirely.
Your team opens the same spreadsheet directly to read or edit an entry,
no separate admin tool needed, and Sheets keeps its own revision
history for free.

SETUP (one-time, needed before this can actually run):
  1. Google Cloud Console -> create a project (or reuse one) ->
     enable the "Google Sheets API".
  2. Create a Service Account, download its JSON key.
  3. Create a Google Sheet, share it with the service account's email
     (found inside the JSON key, ends in @...iam.gserviceaccount.com)
     with Editor access.
  4. Put the JSON key contents in the GOOGLE_SERVICE_ACCOUNT_JSON
     environment variable (the whole file, as one line) and the
     spreadsheet's ID (from its URL) in FEEDBACK_SHEET_ID.
  5. First run auto-creates the "Feedback" tab with headers if it
     doesn't already exist.

Until those environment variables are set, every function here raises
a clear RuntimeError rather than failing silently or writing to a
local file nobody's watching.
"""

import os
import json
from datetime import datetime

FEEDBACK_HEADERS = ["Plate", "Comment", "RequiresFollowup", "Status", "DateAdded", "AddedBy", "Role"]

CLOSED_KEYWORDS = ("sold", "decommission", "written off", "write off", "scrapped")
IGNORE_KEYWORDS = ("monitoring", "fine", "no action", "parked", "ignore")
PENDING_KEYWORDS = ("workshop", "garage", "repair", "removed", "awaiting")


def infer_status(comment: str, requires_followup=None) -> str:
    """
    RequiresFollowup (an explicit choice made when the feedback is
    submitted, see add_feedback()) is the primary signal now, not
    guesswork from wording. Keyword inference only fills in a more
    specific label, and only ever runs on top of an explicit "yes".
    """
    text = (comment or "").lower()
    if requires_followup is False:
        return "Known Issue - No Follow-up Needed"
    if any(k in text for k in CLOSED_KEYWORDS):
        return "Closed - Do Not Chase"
    if any(k in text for k in IGNORE_KEYWORDS):
        return "Acknowledged - Monitoring"
    if any(k in text for k in PENDING_KEYWORDS):
        return "Pending - In Workshop"
    if requires_followup is True:
        return "Follow-up Requested"
    return "Noted"


def _get_client():
    """
    Deliberately imported here, not at module load time, so the rest
    of the app works fine (and test_permissions.py etc keep passing)
    even before gspread and credentials are set up.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        raise RuntimeError(
            "Google Sheets support needs 'gspread' and 'google-auth'. "
            "Run: pip install gspread google-auth"
        ) from e

    raw_creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("FEEDBACK_SHEET_ID")
    if not raw_creds or not sheet_id:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON and FEEDBACK_SHEET_ID must both be set "
            "as environment variables before Sheets-backed feedback will work. "
            "See the setup steps at the top of sheets_store.py."
        )

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(json.loads(raw_creds), scopes=scopes)
    client = gspread.authorize(creds)
    return client, sheet_id


def _get_or_create_feedback_tab(client, sheet_id):
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet("Feedback")
    except Exception:
        ws = sh.add_worksheet(title="Feedback", rows=1000, cols=len(FEEDBACK_HEADERS))
        ws.append_row(FEEDBACK_HEADERS)
        return ws
    # Migrate an older header (from before RequiresFollowup/Role existed)
    # to the current schema. Only ever touches row 1 - existing data rows
    # are never rewritten, older rows just read back with those two
    # fields blank (_parse_bool below treats that as "not specified").
    if ws.row_values(1) != FEEDBACK_HEADERS:
        ws.update("A1", [FEEDBACK_HEADERS])
    return ws


USER_HEADERS = ["Username", "PasswordHash", "Role", "Clients", "LastLogin", "CreatedAt"]


def _get_or_create_users_tab(client, sheet_id):
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet("Users")
    except Exception:
        ws = sh.add_worksheet(title="Users", rows=200, cols=len(USER_HEADERS))
        ws.append_row(USER_HEADERS)
        return ws
    if ws.row_values(1) != USER_HEADERS:
        ws.update("A1", [USER_HEADERS])
    return ws


def load_users_sheet():
    """
    Every account, keyed by username. Accounts live here rather than in
    a local JSON file because Render's filesystem is ephemeral: every
    deploy re-clones the repo, so a file-backed account list silently
    reverts to whatever was committed and every last-login stamp is
    lost. The Sheet survives deploys, restarts and redeploys.
    """
    client, sheet_id = _get_client()
    ws = _get_or_create_users_tab(client, sheet_id)
    result = {}
    for row in ws.get_all_records():
        username = str(row.get("Username", "")).strip()
        if not username:
            continue
        clients_raw = str(row.get("Clients", "")).strip()
        result[username] = {
            "password_hash": str(row.get("PasswordHash", "")).strip(),
            "role": str(row.get("Role", "")).strip(),
            "clients": [c.strip() for c in clients_raw.split(",") if c.strip()],
            "last_login": str(row.get("LastLogin", "")).strip(),
            "created_at": str(row.get("CreatedAt", "")).strip(),
        }
    return result


def add_user_sheet(username, password_hash, role, clients=None):
    client, sheet_id = _get_client()
    ws = _get_or_create_users_tab(client, sheet_id)
    ws.append_row([
        username, password_hash, role, ", ".join(clients or []), "",
        datetime.now().strftime("%d/%m/%Y %H:%M"),
    ])


def record_login_sheet(username):
    """
    Stamps LastLogin in place for one account. Updates only that single
    cell, so a concurrent write to another user's row can't clobber it.
    """
    client, sheet_id = _get_client()
    ws = _get_or_create_users_tab(client, sheet_id)
    cell = ws.find(username, in_column=1)
    if cell is None:
        return
    ws.update_cell(cell.row, USER_HEADERS.index("LastLogin") + 1,
                   datetime.now().strftime("%d/%m/%Y %H:%M"))


def _parse_bool(value):
    text = str(value).strip().lower()
    if text in ("yes", "true", "1"):
        return True
    if text in ("no", "false", "0"):
        return False
    return None


def _parse_date(date_str):
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return datetime.min


def _parsed_feedback_rows():
    """
    Every raw Sheet row, parsed into one flat list of {plate, comment,
    status, requiresFollowup, date, addedBy, role} dicts - the shared
    parsing step both load_feedback() (grouped per plate, for
    classify_fleet()) and load_all_feedback_entries() (flat activity
    feed) build on, so there's exactly one place that knows how to
    read a Sheet row.
    """
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fleet_logic"))
    from schema import normalize_plate  # local import, avoids a circular dependency at module load

    client, sheet_id = _get_client()
    ws = _get_or_create_feedback_tab(client, sheet_id)
    rows = ws.get_all_records()  # list of dicts keyed by header row

    parsed = []
    for row in rows:
        plate = normalize_plate(str(row.get("Plate", "")))
        if not plate:
            continue
        comment = str(row.get("Comment", "")).strip()
        requires_followup = _parse_bool(row.get("RequiresFollowup"))
        parsed.append({
            "plate": plate,
            "comment": comment,
            "status": row.get("Status") or infer_status(comment, requires_followup),
            "requiresFollowup": requires_followup,
            "date": _parse_date(str(row.get("DateAdded", "")).strip()),
            "addedBy": str(row.get("AddedBy", "")).strip(),
            "role": str(row.get("Role", "")).strip(),
        })
    return parsed


def load_feedback():
    """
    Returns {normalized_plate: {"latest": {...}, "history": [...]}}.
    The Sheet is append-only (rows are never edited or deleted here), so
    "history" is the complete, permanent trail for that plate, oldest
    first - "latest" is just history[-1], kept separate so
    classify_fleet() and the dashboard's "Customer Feedback" column
    don't each need to know how to find the newest entry themselves.
    """
    by_plate = {}
    for entry in _parsed_feedback_rows():
        by_plate.setdefault(entry["plate"], []).append(entry)

    result = {}
    for plate, entries in by_plate.items():
        entries.sort(key=lambda e: e["date"])
        result[plate] = {"latest": entries[-1], "history": entries}
    return result


def load_all_feedback_entries(limit=200):
    """
    Every feedback entry across every vehicle, newest first - the
    activity feed (POST /api/feedback-activity), not the per-plate view
    load_feedback() returns. Capped at `limit` so this stays a bounded
    payload even after months of daily use, 200 is generous for a
    "what's been reported today/this week" view.
    """
    entries = _parsed_feedback_rows()
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries[:limit]


def add_feedback(plate: str, comment: str, added_by: str = "", requires_followup=None, role: str = ""):
    """Called from the dashboard's POST /api/feedback route."""
    client, sheet_id = _get_client()
    ws = _get_or_create_feedback_tab(client, sheet_id)
    followup_text = "" if requires_followup is None else ("Yes" if requires_followup else "No")
    ws.append_row([
        plate, comment, followup_text, infer_status(comment, requires_followup),
        datetime.now().strftime("%d/%m/%Y %H:%M"), added_by, role,
    ])
