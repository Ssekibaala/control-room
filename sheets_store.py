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
import sys
import json
import hashlib
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fleet_logic"))
from schema import now_eat

FEEDBACK_HEADERS = ["Plate", "Comment", "RequiresFollowup", "Status", "DateAdded", "AddedBy", "Role", "EntryType"]

# Which field an entry authors. Both land in the same append-only trail
# and both show in one chronological history per asset - EntryType only
# decides which column on the dashboard the newest entry drives.
ENTRY_FEEDBACK = "feedback"   # the client's account of the asset
ENTRY_ACTION = "action"       # the technician's operational instruction
ENTRY_TYPES = (ENTRY_FEEDBACK, ENTRY_ACTION)

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


_client_cache = {"client": None, "sheet_id": None}


def _get_client():
    """
    Authorizing is a full OAuth2 handshake (parse the service account
    key, exchange it for a bearer token) - real network latency that
    was previously paid on EVERY call into this module, on top of the
    Sheets API calls themselves. gspread's AuthorizedSession refreshes
    its own token internally once it's near expiry, so reusing the same
    client for this worker process's lifetime is exactly what the
    library is designed for; the only thing being skipped on repeat
    calls is redundant re-authorization, not staleness protection.
    """
    if _client_cache["client"] is not None:
        return _client_cache["client"], _client_cache["sheet_id"]

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
    _client_cache["client"], _client_cache["sheet_id"] = client, sheet_id
    return client, sheet_id


_spreadsheet_cache = {}
_verified_headers = set()


def _open_spreadsheet(client, sheet_id):
    """Same reasoning as _get_client(): open_by_key() is a real Sheets
    API round trip fetching the whole spreadsheet's metadata (including
    every tab), and nothing about that metadata changes between one
    request and the next within a worker's lifetime. Cached per
    sheet_id rather than unconditionally, since a single process could
    in principle talk to more than one spreadsheet."""
    if sheet_id not in _spreadsheet_cache:
        _spreadsheet_cache[sheet_id] = client.open_by_key(sheet_id)
    return _spreadsheet_cache[sheet_id]


def _get_or_create_feedback_tab(client, sheet_id):
    sh = _open_spreadsheet(client, sheet_id)
    try:
        ws = sh.worksheet("Feedback")
    except Exception:
        ws = sh.add_worksheet(title="Feedback", rows=1000, cols=len(FEEDBACK_HEADERS))
        ws.append_row(FEEDBACK_HEADERS)
        _verified_headers.add("Feedback")
        return ws
    # Migrate an older header (from before RequiresFollowup/Role existed)
    # to the current schema. Only ever touches row 1 - existing data rows
    # are never rewritten, older rows just read back with those two
    # fields blank (_parse_bool below treats that as "not specified").
    # Checked once per worker process, not on every single append - the
    # header row isn't going to change mid-process, and row_values(1) is
    # its own Sheets API round trip that was doubling the latency of
    # every comment submit for a check that, in practice, only ever
    # matters once, right after a deploy that changes FEEDBACK_HEADERS.
    if "Feedback" not in _verified_headers:
        if ws.row_values(1) != FEEDBACK_HEADERS:
            ws.update("A1", [FEEDBACK_HEADERS])
        _verified_headers.add("Feedback")
    return ws


# Email and Active are appended at the END, never inserted mid-list: gspread
# maps rows to headers by POSITION, so inserting a column would shift every
# existing user's data one place left and silently read their Clients value
# as their Email. Rows written before a column existed simply read back
# blank for it (Active reads as "active" - see _parse_active below).
USER_HEADERS = ["Username", "PasswordHash", "Role", "Clients", "LastLogin", "CreatedAt", "Email", "Active"]


def _parse_active(value):
    """Blank (every row written before this column existed) means
    active - defaulting an unmarked account to locked-out would silently
    sign everyone out on the day this column was added."""
    text = str(value).strip().lower()
    return text not in ("false", "no", "0", "inactive")


def _get_or_create_users_tab(client, sheet_id):
    sh = _open_spreadsheet(client, sheet_id)
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
            "email": str(row.get("Email", "")).strip(),
            "active": _parse_active(row.get("Active", "")),
        }
    return result


def add_user_sheet(username, password_hash, role, clients=None, email=""):
    client, sheet_id = _get_client()
    ws = _get_or_create_users_tab(client, sheet_id)
    ws.append_row([
        username, password_hash, role, ", ".join(clients or []), "",
        now_eat().strftime("%d/%m/%Y %H:%M"), email, "TRUE",
    ])


def set_user_email(username, email):
    """Fills in the address for an account created before the Email column
    existed. Single-cell update so a concurrent write elsewhere in the tab
    can't clobber it."""
    client, sheet_id = _get_client()
    ws = _get_or_create_users_tab(client, sheet_id)
    cell = ws.find(username, in_column=1)
    if cell is None:
        return False
    ws.update_cell(cell.row, USER_HEADERS.index("Email") + 1, email)
    return True


def set_user_clients(username, clients):
    """Updates the client assignments for a user. Clients is a comma-separated
    string or list. Single-cell update so concurrent writes can't clobber it."""
    if isinstance(clients, (list, tuple)):
        clients_str = ", ".join(str(c).strip() for c in clients if str(c).strip())
    else:
        clients_str = str(clients or "").strip()
    client, sheet_id = _get_client()
    ws = _get_or_create_users_tab(client, sheet_id)
    cell = ws.find(username, in_column=1)
    if cell is None:
        return False
    ws.update_cell(cell.row, USER_HEADERS.index("Clients") + 1, clients_str)
    return True


def set_user_role(username, role):
    """Updates the role for a user. Single-cell update so concurrent
    writes elsewhere in the tab can't clobber it."""
    client, sheet_id = _get_client()
    ws = _get_or_create_users_tab(client, sheet_id)
    cell = ws.find(username, in_column=1)
    if cell is None:
        return False
    ws.update_cell(cell.row, USER_HEADERS.index("Role") + 1, role)
    return True


def set_user_active(username, active):
    """Flips an account between active and deactivated. Deactivated
    accounts fail login (see users.verify_login) but keep their row -
    this is a suspend, not a delete."""
    client, sheet_id = _get_client()
    ws = _get_or_create_users_tab(client, sheet_id)
    cell = ws.find(username, in_column=1)
    if cell is None:
        return False
    ws.update_cell(cell.row, USER_HEADERS.index("Active") + 1, "TRUE" if active else "FALSE")
    return True


def delete_user_sheet(username):
    """Removes one account's row entirely. Returns True if it was found
    (and removed), False if there was no such account. This is the only
    way to remove an account short of editing the Sheet by hand - there
    was previously no delete path anywhere in this app."""
    client, sheet_id = _get_client()
    ws = _get_or_create_users_tab(client, sheet_id)
    cell = ws.find(username, in_column=1)
    if cell is None:
        return False
    ws.delete_rows(cell.row)
    return True


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
                   now_eat().strftime("%d/%m/%Y %H:%M"))


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
        # Rows written before EntryType existed are all client feedback -
        # that's the only kind the form could produce at the time.
        entry_type = str(row.get("EntryType", "")).strip().lower()
        if entry_type not in ENTRY_TYPES:
            entry_type = ENTRY_FEEDBACK
        parsed.append({
            "plate": plate,
            "comment": comment,
            "status": row.get("Status") or infer_status(comment, requires_followup),
            "requiresFollowup": requires_followup,
            "date": _parse_date(str(row.get("DateAdded", "")).strip()),
            "addedBy": str(row.get("AddedBy", "")).strip(),
            "role": str(row.get("Role", "")).strip(),
            "entryType": entry_type,
        })
    return parsed


def load_feedback():
    """
    Returns {normalized_plate: {"latest", "latestFeedback", "latestAction",
    "history"}}.

    The Sheet is append-only (rows are never edited or deleted here), so
    "history" is the complete permanent trail for that plate, oldest
    first, mixing both entry types - one asset, one story, whoever wrote
    it. The three "latest" keys are conveniences so callers don't each
    re-derive them:
      latest         - newest entry of any type, drives classification
      latestFeedback - newest client-authored entry ("Customer Feedback")
      latestAction   - newest technician-authored entry, which overrides
                       the system's computed recommendation when present
    """
    by_plate = {}
    for entry in _parsed_feedback_rows():
        by_plate.setdefault(entry["plate"], []).append(entry)

    def newest_of(entries, kind):
        matching = [e for e in entries if e["entryType"] == kind]
        return matching[-1] if matching else None

    result = {}
    for plate, entries in by_plate.items():
        entries.sort(key=lambda e: e["date"])
        result[plate] = {
            "latest": entries[-1],
            "latestFeedback": newest_of(entries, ENTRY_FEEDBACK),
            "latestAction": newest_of(entries, ENTRY_ACTION),
            "history": entries,
        }
    return result


# ---- Read cache -------------------------------------------------------
# The dashboard applies feedback on every request, so an uncached read
# would mean a Sheets round-trip (~1s) per page load. Cache briefly and
# invalidate on write. Invalidation has to cross PROCESSES, not just
# threads: gunicorn runs several workers on Render and a write handled
# by worker A must invalidate worker B's cache too. A tiny stamp file
# does that - every worker stat()s it (microseconds) and drops its cache
# when the mtime moves.
_CACHE_TTL_SECONDS = 30
_STAMP_PATH = os.path.join(os.path.dirname(__file__), "data", "_feedback_stamp")
_cache = {"data": None, "fetched_at": 0.0, "stamp": None}


def _current_stamp():
    try:
        return os.path.getmtime(_STAMP_PATH)
    except OSError:
        return None


def _bump_stamp():
    try:
        os.makedirs(os.path.dirname(_STAMP_PATH), exist_ok=True)
        with open(_STAMP_PATH, "w") as f:
            f.write(str(datetime.now().timestamp()))
    except OSError:
        pass  # a failed stamp only costs freshness, never correctness of the write itself


def load_feedback_cached():
    """load_feedback() behind the cache described above. Callers that
    must see a guaranteed-fresh read (the import) should keep using
    load_feedback() directly."""
    import time
    now = time.time()
    stamp = _current_stamp()
    fresh = (
        _cache["data"] is not None
        and _cache["stamp"] == stamp
        and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS
    )
    if fresh:
        return _cache["data"]
    data = load_feedback()
    _cache.update({"data": data, "fetched_at": now, "stamp": stamp})
    return data


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


def add_feedback(plate: str, comment: str, added_by: str = "", requires_followup=None,
                 role: str = "", entry_type: str = ENTRY_FEEDBACK):
    """Called from the dashboard's POST /api/feedback route. Append-only:
    nothing here ever edits or removes an existing row."""
    if entry_type not in ENTRY_TYPES:
        entry_type = ENTRY_FEEDBACK
    client, sheet_id = _get_client()
    ws = _get_or_create_feedback_tab(client, sheet_id)
    followup_text = "" if requires_followup is None else ("Yes" if requires_followup else "No")
    ws.append_row([
        plate, comment, followup_text, infer_status(comment, requires_followup),
        now_eat().strftime("%d/%m/%Y %H:%M"), added_by, role, entry_type,
    ])
    # Every OTHER worker must drop its cache, or it would keep serving
    # the pre-submit dashboard for up to the cache TTL - exactly the
    # "why hasn't it updated yet" problem this change exists to remove.
    _bump_stamp()

    # This worker, though, already knows exactly what was just written,
    # so patch it in rather than paying another full Sheet read (~4s) to
    # be told what we just said. That read was over half the wait
    # between pressing Submit and seeing the asset move.
    entry = {
        "plate": plate, "comment": comment,
        "status": infer_status(comment, requires_followup),
        "requiresFollowup": requires_followup, "date": datetime.now(),
        "addedBy": added_by, "role": role, "entryType": entry_type,
    }
    _patch_cache_with(entry)


def _patch_cache_with(entry):
    """Folds one just-written entry into the cached view, keeping the
    same shape load_feedback() returns. Leaves the cache alone if it was
    empty - the next read will fetch everything anyway."""
    cached = _cache.get("data")
    if cached is None:
        return
    plate = entry["plate"]
    existing = cached.get(plate)
    history = (existing["history"] + [entry]) if existing else [entry]
    cached[plate] = {
        "latest": entry,
        "latestFeedback": next((e for e in reversed(history) if e["entryType"] == ENTRY_FEEDBACK), None),
        "latestAction": next((e for e in reversed(history) if e["entryType"] == ENTRY_ACTION), None),
        "history": history,
    }
    # Re-stamp so this worker doesn't immediately invalidate its own
    # patch on the very next request by noticing the bump above.
    _cache["stamp"] = _current_stamp()


# ======================================================================
# Email thread ledger
# ----------------------------------------------------------------------
# Stores only what a mail THREAD needs to exist - never the message
# content itself, which already lives permanently in the Feedback tab
# above. One vehicle can have many cases over its life; each case is one
# mail thread, opened by a new comment and closed once the client marks
# "no follow-up needed" for it. The next problem on the same plate opens
# a fresh case with a new subject, rather than appending to a thread that
# has grown into an unreadable, years-long scroll.
# ======================================================================

THREAD_HEADERS = ["Plate", "CaseId", "Subject", "RootMessageId", "ReferencesChain",
                   "Status", "CreatedAt", "LastSentAt"]


def _get_or_create_threads_tab(client, sheet_id):
    sh = _open_spreadsheet(client, sheet_id)
    try:
        ws = sh.worksheet("EmailThreads")
    except Exception:
        ws = sh.add_worksheet(title="EmailThreads", rows=500, cols=len(THREAD_HEADERS))
        ws.append_row(THREAD_HEADERS)
        return ws
    if ws.row_values(1) != THREAD_HEADERS:
        ws.update("A1", [THREAD_HEADERS])
    return ws


def get_or_create_open_case(plate):
    """
    Returns the currently open case for this plate, creating one if none
    is open (either it's a brand new plate, or the last case was closed).
    Shape: {plate, caseId, subject, rootMessageId, references (list), row,
    reopened, previousClosedAt}. `row` is the 1-indexed sheet row, so
    callers can update it without a second lookup. `reopened` is True
    only when this plate already has case history and every prior case
    is closed - i.e. this comment is what's bringing it back, not the
    plate's first-ever case - so notifications.py can call that out in
    the email instead of presenting it as a routine update.
    """
    client, sheet_id = _get_client()
    ws = _get_or_create_threads_tab(client, sheet_id)
    rows = ws.get_all_records()

    plate_rows = [(i, r) for i, r in enumerate(rows, start=2) if str(r.get("Plate", "")).strip() == plate]
    open_row = next(((i, r) for i, r in plate_rows if r.get("Status") == "open"), None)
    if open_row:
        i, r = open_row
        refs = [m.strip() for m in str(r.get("ReferencesChain", "")).split(" ") if m.strip()]
        return {"plate": plate, "caseId": int(r["CaseId"]), "subject": r["Subject"],
                "rootMessageId": r.get("RootMessageId", ""), "references": refs, "row": i,
                "reopened": False, "previousClosedAt": None}

    next_case_id = max((int(r["CaseId"]) for _, r in plate_rows), default=0) + 1
    subject = f"{plate} — GTL case {next_case_id}"
    now = now_eat().strftime("%d/%m/%Y %H:%M")
    ws.append_row([plate, next_case_id, subject, "", "", "open", now, ""])
    new_row = len(ws.get_all_values())
    # plate_rows is non-empty here (every row in it is closed, or there'd
    # have been an open_row above) - the most recently closed one is what
    # "reopened" refers to. LastSentAt on that row is when its closing
    # outcome email went out, close_case() runs right after that send
    # succeeds, so it's a faithful stand-in for a "ClosedAt" the sheet
    # doesn't otherwise track.
    last_closed = max(plate_rows, key=lambda ir: int(ir[1].get("CaseId", 0)), default=None)
    previous_closed_at = last_closed[1].get("LastSentAt") or last_closed[1].get("CreatedAt") if last_closed else None
    return {"plate": plate, "caseId": next_case_id, "subject": subject,
            "rootMessageId": "", "references": [], "row": new_row,
            "reopened": bool(plate_rows), "previousClosedAt": previous_closed_at or None}


def record_sent_message(plate, case, message_id):
    """
    Call after successfully sending one email in this case's thread. The
    first message sent becomes the root that every later one threads
    against; every message after that appends to References.
    """
    client, sheet_id = _get_client()
    ws = _get_or_create_threads_tab(client, sheet_id)
    row = case["row"]
    root = case["rootMessageId"] or message_id
    references = case["references"] + [message_id]
    ws.update(f"D{row}:E{row}", [[root, " ".join(references)]])
    ws.update_cell(row, THREAD_HEADERS.index("LastSentAt") + 1,
                    now_eat().strftime("%d/%m/%Y %H:%M"))
    case["rootMessageId"], case["references"] = root, references
    return case


def close_case(plate, case_id):
    """Marks a case closed - the NEXT comment on this plate opens a fresh
    case and a fresh thread, rather than reopening a stale one."""
    client, sheet_id = _get_client()
    ws = _get_or_create_threads_tab(client, sheet_id)
    for i, r in enumerate(ws.get_all_records(), start=2):
        if str(r.get("Plate", "")).strip() == plate and int(r.get("CaseId", 0)) == case_id:
            ws.update_cell(i, THREAD_HEADERS.index("Status") + 1, "closed")
            return True
    return False


# ---- Clients registry ---------------------------------------------------
# A client here is the ORGANISATION (e.g. "Globe Trotters Ltd"), not a
# login - it exists so notifications can reach people who watch a fleet
# without needing their own account, AND (since the multi-client build)
# it is the unit of both data ownership and access control: every asset
# row carries the client it belongs to, and a user only ever receives
# rows for the clients assigned to them (see permissions.py).
#
# The three *Ids columns are what make that possible. The same real-world
# client is named differently on every platform - AGL is "AGL" on MiX but
# "AFRICA GLOBAL LOGISTICS" on Teletrac and "Africa Global Logistics(AGL)"
# on FT Cloud - so there is no reliable way to merge them automatically.
# These columns are the explicit, human-set mapping that says "these three
# platform accounts are all the same client", maintained from the admin UI
# rather than in code, so adding a client never needs a redeploy.
# Comma-separated because one client can legitimately own several
# organisations on the same platform.
# The three *Ids columns are appended at the END, never inserted before
# CreatedAt, for exactly the reason USER_HEADERS documents above:
# gspread maps rows to headers by POSITION, so inserting them mid-list
# would shift every pre-existing client row one place and silently read
# its CreatedAt value as MixOrgIds. Rows written before these columns
# existed just read back with all three blank (= not yet mapped).
CLIENT_HEADERS = ["Name", "ContactEmails", "CreatedAt", "MixOrgIds", "TeletracClientIds", "FtCloudFleetIds"]


def _split_ids(raw):
    return [p.strip() for p in str(raw or "").split(",") if p.strip()]


def _looks_corrupted_id(value):
    """
    True for a platform id Sheets has mangled into a float.

    Platform ids are up to 19 digits (MiX org ids, FT fleet ids). Sheets
    stores numbers as doubles, which hold ~15-17 significant digits, so
    letting it treat one as a NUMBER silently rewrites it - AGL's
    -4944176959690446336 came back as "-4.94418e+18", pointing at no
    real organisation. Writes now go in as RAW text to prevent it (see
    _write_ids), but a row written before that fix, or edited by hand in
    the Sheets UI, can still hold a mangled value.

    The precision is genuinely gone - the original cannot be recovered
    from the float - so this only flags it. Guessing would map a client
    to whatever organisation the rounded id happens to hit.
    """
    text = str(value or "").strip()
    return bool(text) and ("e+" in text.lower() or "E+" in text)


def _write_ids(ws, row, column, ids):
    """
    Writes one comma-separated id list as RAW text.

    RAW is the whole point: the default USER_ENTERED makes Sheets parse
    a lone 19-digit id as a number and quietly round it (see
    _looks_corrupted_id). update_cell() offers no such option, so this
    goes through update() instead.
    """
    # gspread is imported lazily elsewhere in this module (it's an
    # optional dependency until Sheets is actually configured), so it's
    # imported here too rather than at module level.
    from gspread.utils import rowcol_to_a1
    cell = rowcol_to_a1(row, CLIENT_HEADERS.index(column) + 1)
    ws.update(cell, [[", ".join(ids)]], value_input_option="RAW")


def _get_or_create_clients_tab(client, sheet_id):
    sh = _open_spreadsheet(client, sheet_id)
    try:
        ws = sh.worksheet("Clients")
    except Exception:
        ws = sh.add_worksheet(title="Clients", rows=100, cols=len(CLIENT_HEADERS))
        ws.append_row(CLIENT_HEADERS)
        return ws
    if ws.row_values(1) != CLIENT_HEADERS:
        ws.update("A1", [CLIENT_HEADERS])
    return ws


def load_clients():
    client, sheet_id = _get_client()
    ws = _get_or_create_clients_tab(client, sheet_id)
    out = []
    for row in ws.get_all_records():
        name = str(row.get("Name", "")).strip()
        if not name:
            continue
        emails_raw = str(row.get("ContactEmails", "")).strip()
        entry = {
            "name": name,
            "emails": [e.strip() for e in emails_raw.split(",") if e.strip()],
            "mixOrgIds": _split_ids(row.get("MixOrgIds")),
            "teletracClientIds": _split_ids(row.get("TeletracClientIds")),
            "ftCloudFleetIds": _split_ids(row.get("FtCloudFleetIds")),
            "createdAt": str(row.get("CreatedAt", "")).strip(),
        }
        # A mangled id is dropped rather than passed downstream: it
        # points at no real organisation, so keeping it would mean a
        # failed API call every poll cycle for a client that looks
        # correctly configured. Dropping it makes the platform show as
        # unmapped in the admin UI, which is both true and fixable by
        # re-picking it from the dropdown.
        for key in ("mixOrgIds", "teletracClientIds", "ftCloudFleetIds"):
            bad = [v for v in entry[key] if _looks_corrupted_id(v)]
            if bad:
                entry[key] = [v for v in entry[key] if not _looks_corrupted_id(v)]
                entry.setdefault("corruptedIds", []).extend(bad)
                print(f"WARNING: client {name!r} has a corrupted {key} value {bad} - Sheets stored a "
                      f"long id as a number and lost digits. Re-select it in the client mapping UI.")
        out.append(entry)
    return out


def add_client(name, emails=None, mix_org_ids=None, teletrac_client_ids=None, ft_cloud_fleet_ids=None):
    client, sheet_id = _get_client()
    ws = _get_or_create_clients_tab(client, sheet_id)
    if any(str(r.get("Name", "")).strip().lower() == name.lower() for r in ws.get_all_records()):
        raise ValueError(f"A client named '{name}' already exists")
    # Order must match CLIENT_HEADERS exactly, including the three *Ids
    # columns sitting AFTER CreatedAt - see the note on CLIENT_HEADERS.
    # RAW so Sheets stores the platform ids as text - see _write_ids.
    ws.append_row([name, ", ".join(emails or []),
                   now_eat().strftime("%d/%m/%Y %H:%M"),
                   ", ".join(mix_org_ids or []), ", ".join(teletrac_client_ids or []),
                   ", ".join(ft_cloud_fleet_ids or [])],
                  value_input_option="RAW")


def set_client_emails(name, emails):
    """Replaces the full contact list for one client. Single-row update,
    matched by name - returns False if no client by that name exists."""
    client, sheet_id = _get_client()
    ws = _get_or_create_clients_tab(client, sheet_id)
    cell = ws.find(name, in_column=1)
    if cell is None:
        return False
    ws.update_cell(cell.row, CLIENT_HEADERS.index("ContactEmails") + 1, ", ".join(emails or []))
    return True


def set_client_platforms(name, mix_org_ids=None, teletrac_client_ids=None, ft_cloud_fleet_ids=None):
    """
    Replaces this client's platform-account mapping - which MiX orgs /
    Teletrac clients / FT Cloud fleets belong to it. Passing None for a
    platform leaves that platform's existing mapping alone; passing an
    empty list clears it (i.e. "this client is no longer on that
    platform"), which is a meaningful and different instruction.
    Returns False if no client by that name exists.
    """
    client, sheet_id = _get_client()
    ws = _get_or_create_clients_tab(client, sheet_id)
    cell = ws.find(name, in_column=1)
    if cell is None:
        return False
    for column, ids in (("MixOrgIds", mix_org_ids),
                        ("TeletracClientIds", teletrac_client_ids),
                        ("FtCloudFleetIds", ft_cloud_fleet_ids)):
        if ids is None:
            continue
        _write_ids(ws, cell.row, column, ids)
    return True


def delete_client(name):
    client, sheet_id = _get_client()
    ws = _get_or_create_clients_tab(client, sheet_id)
    cell = ws.find(name, in_column=1)
    if cell is None:
        return False
    ws.delete_rows(cell.row)
    return True


# ---- Vehicle status memory ----------------------------------------------
# classify_fleet() recomputes Online/Offline fresh every import with zero
# memory of the previous run - correct for "what's the status right now",
# but it means nothing in the app can ever answer "did this vehicle just
# COME BACK online" without something remembering what it was last time.
# This tab is that memory: one row per plate, overwritten wholesale every
# import (see notifications.check_reconnections), not appended - only the
# latest status matters, there's no history value in keeping old rows.
VEHICLE_STATUS_HEADERS = ["Plate", "Status", "UpdatedAt"]


def _get_or_create_vehicle_status_tab(client, sheet_id):
    sh = _open_spreadsheet(client, sheet_id)
    try:
        ws = sh.worksheet("VehicleStatus")
    except Exception:
        ws = sh.add_worksheet(title="VehicleStatus", rows=500, cols=len(VEHICLE_STATUS_HEADERS))
        ws.append_row(VEHICLE_STATUS_HEADERS)
        return ws
    if ws.row_values(1) != VEHICLE_STATUS_HEADERS:
        ws.update("A1", [VEHICLE_STATUS_HEADERS])
    return ws


def load_vehicle_status():
    """{plate: "Online"/"Offline"} as of the last time this was saved -
    i.e. the previous import cycle, not the current one being computed
    right now. Missing entirely for a plate that's brand new."""
    client, sheet_id = _get_client()
    ws = _get_or_create_vehicle_status_tab(client, sheet_id)
    out = {}
    for row in ws.get_all_records():
        plate = str(row.get("Plate", "")).strip()
        if plate:
            out[plate] = str(row.get("Status", "")).strip()
    return out


def save_vehicle_status(status_by_plate):
    """Overwrites the whole tab with this cycle's status for every plate
    - deliberately a full replace, not a per-row patch, since every
    plate's status is recomputed every import anyway and a stale row for
    a plate that's since left the fleet is just clutter, never a value
    worth preserving."""
    client, sheet_id = _get_client()
    ws = _get_or_create_vehicle_status_tab(client, sheet_id)
    now = now_eat().strftime("%d/%m/%Y %H:%M")
    rows = [[plate, status, now] for plate, status in sorted(status_by_plate.items())]
    ws.clear()
    ws.append_row(VEHICLE_STATUS_HEADERS)
    if rows:
        ws.append_rows(rows)


# ---- Digest state ---------------------------------------------------
# The two rollup emails (Pending Customer Confirmation -> clients,
# Technical Escalation -> staff) fire on a configurable interval, not on
# every import - this is the only place that "when did each one last
# go out" is remembered, so it survives Render redeploys same as
# everything else that has to.
DIGEST_STATE_HEADERS = ["DigestKey", "LastSentAt", "PromptedBy"]


def _get_or_create_digest_state_tab(client, sheet_id):
    sh = _open_spreadsheet(client, sheet_id)
    try:
        ws = sh.worksheet("DigestState")
    except Exception:
        ws = sh.add_worksheet(title="DigestState", rows=20, cols=len(DIGEST_STATE_HEADERS))
        ws.append_row(DIGEST_STATE_HEADERS)
        return ws
    if ws.row_values(1) != DIGEST_STATE_HEADERS:
        ws.update("A1", [DIGEST_STATE_HEADERS])
    return ws


def get_digest_last_sent(digest_key):
    """Returns the datetime it last went out, or None if it never has."""
    client, sheet_id = _get_client()
    ws = _get_or_create_digest_state_tab(client, sheet_id)
    for row in ws.get_all_records():
        if str(row.get("DigestKey", "")).strip() == digest_key:
            raw = str(row.get("LastSentAt", "")).strip()
            if not raw:
                return None
            try:
                return datetime.strptime(raw, "%d/%m/%Y %H:%M")
            except ValueError:
                return None
    return None


def set_digest_last_sent(digest_key, when=None, prompted_by=None):
    """prompted_by is set only when a human triggered this send from the
    dashboard's "Send check-in now" button (see record_manual_checkin
    below, which also uses this same tab under the "manual_checkin"
    key) - left blank for the ordinary weekly automatic sends."""
    when = when or datetime.now()
    client, sheet_id = _get_client()
    ws = _get_or_create_digest_state_tab(client, sheet_id)
    stamp = when.strftime("%d/%m/%Y %H:%M")
    for i, row in enumerate(ws.get_all_records(), start=2):
        if str(row.get("DigestKey", "")).strip() == digest_key:
            ws.update_cell(i, DIGEST_STATE_HEADERS.index("LastSentAt") + 1, stamp)
            if prompted_by:
                ws.update_cell(i, DIGEST_STATE_HEADERS.index("PromptedBy") + 1, prompted_by)
            return
    row = [digest_key, stamp, prompted_by or ""]
    ws.append_row(row)


def record_manual_checkin(prompted_by, when=None):
    """Records that a human (not the daily import cycle) pressed "Send
    check-in now" - shown back in the dashboard so anyone can see who
    last triggered it and when, same DigestState tab as everything
    else here, under a dedicated key that's never used by an automatic
    send."""
    set_digest_last_sent("manual_checkin", when=when, prompted_by=prompted_by)


def get_last_manual_checkin():
    """Returns {"by": str, "at": datetime} or None if never triggered."""
    client, sheet_id = _get_client()
    ws = _get_or_create_digest_state_tab(client, sheet_id)
    for row in ws.get_all_records():
        if str(row.get("DigestKey", "")).strip() == "manual_checkin":
            raw = str(row.get("LastSentAt", "")).strip()
            by = str(row.get("PromptedBy", "")).strip()
            if not raw:
                return None
            try:
                return {"by": by, "at": datetime.strptime(raw, "%d/%m/%Y %H:%M")}
            except ValueError:
                return None
    return None


# ---- Tamper checks ---------------------------------------------------
# A physical inspection record for a vehicle that tripped the tampering
# detector (fleet_logic/tamper_engine.py) - append-only, same shape as
# the Feedback tab, because the same real-world event (someone drove
# out, looked at the vehicle, and is reporting what they found) is
# happening here, just for a different question. Once a plate has a
# check recorded, run_import.py excludes any tamper gap dated on or
# before that check from confirmed/unconfirmed - already explained,
# not still-open. A gap that starts AFTER the check is new information
# and is never suppressed by an earlier check.
TAMPER_CHECK_HEADERS = ["Plate", "CheckedBy", "CheckedAt", "Comment"]


def _get_or_create_tamper_checks_tab(client, sheet_id):
    sh = _open_spreadsheet(client, sheet_id)
    try:
        ws = sh.worksheet("TamperChecks")
    except Exception:
        ws = sh.add_worksheet(title="TamperChecks", rows=200, cols=len(TAMPER_CHECK_HEADERS))
        ws.append_row(TAMPER_CHECK_HEADERS)
        return ws
    if ws.row_values(1) != TAMPER_CHECK_HEADERS:
        ws.update("A1", [TAMPER_CHECK_HEADERS])
    return ws


def add_tamper_check(plate, checked_by, comment):
    """Appends one physical-check record. Never overwrites a previous
    check on the same plate - a vehicle can trip the detector again
    later and get checked again, and the full history of what was
    found each time is exactly what the dashboard's Checked Assets
    section (and the tamper risk report email) shows."""
    client, sheet_id = _get_client()
    ws = _get_or_create_tamper_checks_tab(client, sheet_id)
    now = now_eat().strftime("%d/%m/%Y %H:%M")
    ws.append_row([plate, checked_by, now, comment])


def load_tamper_checks():
    """Returns {plate: [{"checkedBy", "checkedAt" (datetime), "comment"}, ...]},
    newest first per plate."""
    client, sheet_id = _get_client()
    ws = _get_or_create_tamper_checks_tab(client, sheet_id)
    out = {}
    for row in ws.get_all_records():
        plate = str(row.get("Plate", "")).strip()
        if not plate:
            continue
        raw = str(row.get("CheckedAt", "")).strip()
        try:
            checked_at = datetime.strptime(raw, "%d/%m/%Y %H:%M")
        except ValueError:
            checked_at = None
        out.setdefault(plate, []).append({
            "checkedBy": str(row.get("CheckedBy", "")).strip(),
            "checkedAt": checked_at,
            "comment": str(row.get("Comment", "")).strip(),
        })
    for plate in out:
        out[plate].sort(key=lambda e: e["checkedAt"] or datetime.min, reverse=True)
    return out


# ---- Respond-token single-use tracking -----------------------------
# A respond token (respond_tokens.py) is a signed, 30-day-valid
# credential mailed to whoever needs to answer one specific question -
# there was nothing stopping the exact same link from being submitted
# repeatedly with different answers for the rest of that month (found
# during an end-to-end QA pass: resubmitting one link twice, with
# different names/comments/answers, both succeeded). Storing a hash of
# every token that's actually been submitted (never the raw token -
# no reason to keep something that doubles as a bearer credential
# lying around in plaintext) turns "valid for 30 days" into "valid for
# one use within 30 days", without touching the token format itself.
USED_TOKEN_HEADERS = ["TokenHash", "UsedAt"]


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_or_create_used_tokens_tab(client, sheet_id):
    sh = _open_spreadsheet(client, sheet_id)
    try:
        ws = sh.worksheet("UsedRespondTokens")
    except Exception:
        ws = sh.add_worksheet(title="UsedRespondTokens", rows=500, cols=len(USED_TOKEN_HEADERS))
        ws.append_row(USED_TOKEN_HEADERS)
        return ws
    if ws.row_values(1) != USED_TOKEN_HEADERS:
        ws.update("A1", [USED_TOKEN_HEADERS])
    return ws


def is_token_used(token):
    client, sheet_id = _get_client()
    ws = _get_or_create_used_tokens_tab(client, sheet_id)
    target = _hash_token(token)
    return any(str(row.get("TokenHash", "")).strip() == target for row in ws.get_all_records())


def mark_token_used(token):
    client, sheet_id = _get_client()
    ws = _get_or_create_used_tokens_tab(client, sheet_id)
    ws.append_row([_hash_token(token), now_eat().strftime("%d/%m/%Y %H:%M")])
