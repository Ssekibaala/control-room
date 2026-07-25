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

FEEDBACK_HEADERS = ["Plate", "Comment", "Status", "DateAdded", "AddedBy"]

CLOSED_KEYWORDS = ("sold", "decommission", "written off", "write off", "scrapped")
IGNORE_KEYWORDS = ("monitoring", "fine", "no action", "parked", "ignore")
PENDING_KEYWORDS = ("workshop", "garage", "repair", "removed", "awaiting")


def infer_status(comment: str) -> str:
    text = (comment or "").lower()
    if any(k in text for k in CLOSED_KEYWORDS):
        return "Closed - Do Not Chase"
    if any(k in text for k in IGNORE_KEYWORDS):
        return "Acknowledged - Monitoring"
    if any(k in text for k in PENDING_KEYWORDS):
        return "Pending - In Workshop"
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


def load_feedback():
    """
    Returns {normalized_plate: {"comment":..., "status":..., "date":...}}
    exactly like the old CSV version, most recent comment per plate wins.
    """
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fleet_logic"))
    from schema import normalize_plate  # local import, avoids a circular dependency at module load

    client, sheet_id = _get_client()
    ws = _get_or_create_feedback_tab(client, sheet_id)
    rows = ws.get_all_records()  # list of dicts keyed by header row

    feedback = {}
    for row in rows:
        plate = normalize_plate(str(row.get("Plate", "")))
        if not plate:
            continue
        comment = str(row.get("Comment", "")).strip()
        date_str = str(row.get("DateAdded", "")).strip()
        try:
            date_added = datetime.strptime(date_str, "%d/%m/%Y") if date_str else datetime.min
        except ValueError:
            date_added = datetime.min
        existing = feedback.get(plate)
        if existing is None or date_added >= existing["date"]:
            feedback[plate] = {
                "comment": comment,
                "status": row.get("Status") or infer_status(comment),
                "date": date_added,
            }
    return feedback


def add_feedback(plate: str, comment: str, added_by: str = ""):
    """Called from the dashboard's POST /api/feedback route."""
    client, sheet_id = _get_client()
    ws = _get_or_create_feedback_tab(client, sheet_id)
    ws.append_row([
        plate, comment, infer_status(comment),
        datetime.now().strftime("%d/%m/%Y"), added_by,
    ])
