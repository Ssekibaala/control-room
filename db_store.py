"""
Feedback and history live in MySQL (see db.py for the connection layer and
schema) - the durable replacement for the Google Sheet sheets_store.py used
to manage. That Sheet is retired as of this module existing: it is still
readable one last time by scripts/export_sheets_to_mysql.py for the one-time
data migration, and nowhere else.

This module is a drop-in replacement for sheets_store.py: every public
function here has the exact same name and signature as its sheets_store.py
counterpart, so every call site elsewhere in this codebase (app.py, users.py,
notifications.py, fleet_logic/client_registry.py, importer/run_import.py)
only needed a mechanical `import sheets_store` -> `import db_store` /
`sheets_store.` -> `db_store.` rename, not a rewrite.

Until MYSQL_HOST/MYSQL_DATABASE/MYSQL_USER are all set, every function here
raises db.NotConfigured (a RuntimeError subclass) - the exact same contract
sheets_store.py had for its own "not configured" case, so every existing
`except RuntimeError` handler in app.py keeps working unchanged.
"""

import os
import sys
import hashlib
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fleet_logic"))

import db
from schema import now_eat

FEEDBACK_HEADERS = ["Plate", "Comment", "RequiresFollowup", "Status", "DateAdded", "AddedBy", "Role", "EntryType"]

# Which field an entry authors. Both land in the same append-only trail and
# both show in one chronological history per asset - EntryType only decides
# which column on the dashboard the newest entry drives. Unchanged from
# sheets_store.py: this is pure logic, not storage.
ENTRY_FEEDBACK = "feedback"   # the client's account of the asset
ENTRY_ACTION = "action"       # the technician's operational instruction
ENTRY_TYPES = (ENTRY_FEEDBACK, ENTRY_ACTION)

CLOSED_KEYWORDS = ("sold", "decommission", "written off", "write off", "scrapped")
IGNORE_KEYWORDS = ("monitoring", "fine", "no action", "parked", "ignore")
PENDING_KEYWORDS = ("workshop", "garage", "repair", "removed", "awaiting")


def infer_status(comment: str, requires_followup=None) -> str:
    """Unchanged from sheets_store.py - pure logic, no storage dependency."""
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


def _fmt(dt):
    return dt.strftime("%d/%m/%Y %H:%M") if dt else ""


def _bool_to_tinyint(value):
    if value is None:
        return None
    return 1 if value else 0


def _tinyint_to_bool(value):
    if value is None:
        return None
    return bool(value)


def log_change(cur, table_name, row_key, field, old_value, new_value, changed_by):
    """
    Records one field-level change - the generic replacement for what
    Sheets' own revision history used to give us for free on tables that
    aren't already append-only (Feedback/TamperChecks are their own audit
    trail by design; this is for users/clients/vehicle_status/digest_state).

    Takes an existing cursor rather than opening its own connection, so the
    log entry commits atomically with the change it's describing - a
    connection failure between the two would otherwise leave a change with
    no record of it, exactly the gap this exists to close.
    """
    if old_value == new_value:
        return  # nothing actually changed - no point logging a no-op
    cur.execute(
        "INSERT INTO audit_log (table_name, row_key, field, old_value, new_value, changed_by, changed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (table_name, row_key, field,
         "" if old_value is None else str(old_value),
         "" if new_value is None else str(new_value),
         changed_by or "", now_eat().replace(microsecond=0)),
    )


def log_activity(username, role, method, path, status_code, ip_address="", duration_ms=0):
    """
    Records one system-activity entry - who did what, from where, and
    what the server answered. Written centrally from app.py's
    after_request hook for every state-changing request (POST/PUT/
    DELETE), not from individual routes - so it's a single, comprehensive
    trail covering logins, feedback, exports, admin changes, and cron/
    webhook triggers alike, rather than depending on each route author
    remembering to log their own action.

    Opens its own connection (unlike log_change above) since callers here
    are never already inside another transaction - the request has
    already been fully handled by the time this runs.
    """
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO activity_log (occurred_at, username, role, method, path, status_code, ip_address, duration_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (now_eat().replace(microsecond=0), username or "", role or "", method, path,
             status_code, ip_address or "", int(duration_ms)),
        )


def load_activity_log(limit=200, username=None):
    """Most recent activity, newest first - optionally scoped to one
    account. Backs the admin-only Activity & Audit Log panel."""
    with db.cursor() as cur:
        if username:
            cur.execute(
                "SELECT * FROM activity_log WHERE username=%s ORDER BY occurred_at DESC, id DESC LIMIT %s",
                (username, limit),
            )
        else:
            cur.execute("SELECT * FROM activity_log ORDER BY occurred_at DESC, id DESC LIMIT %s", (limit,))
        return cur.fetchall()


def load_audit_log(limit=200, table_name=None):
    """Most recent field-level changes, newest first - optionally scoped
    to one table (e.g. 'users', 'clients', 'feedback', 'tamper_checks').
    Backs the same admin panel as load_activity_log() above, as the
    "what changed" half of it - see log_change()'s docstring for how
    this differs from activity_log."""
    with db.cursor() as cur:
        if table_name:
            cur.execute(
                "SELECT * FROM audit_log WHERE table_name=%s ORDER BY changed_at DESC, id DESC LIMIT %s",
                (table_name, limit),
            )
        else:
            cur.execute("SELECT * FROM audit_log ORDER BY changed_at DESC, id DESC LIMIT %s", (limit,))
        return cur.fetchall()


# ======================================================================
# Feedback
# ----------------------------------------------------------------------
# Append-only, same as the old Feedback tab: add_feedback() only ever
# INSERTs. update_feedback()/delete_feedback() below are new - Sheets let
# your team fix a typo or remove a bad entry by hand in the sheet itself;
# nothing in the app replaced that until now. Both are soft (edited_at/
# deleted_at columns, never a real UPDATE-away-the-old-value or DELETE),
# so the append-only audit property survives being made correctable, and
# every edit/delete is also mirrored into audit_log.
# ======================================================================


def _parsed_feedback_rows(include_deleted=False):
    """Every row, parsed into the same flat {plate, comment, status,
    requiresFollowup, date, addedBy, role, entryType, id} shape
    sheets_store.py's version returned (plus "id", needed by the new
    edit/delete routes - existing consumers that don't know about it
    simply never look at it)."""
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    with db.cursor() as cur:
        cur.execute(f"SELECT * FROM feedback {where} ORDER BY date_added, id")
        rows = cur.fetchall()

    parsed = []
    for row in rows:
        entry_type = (row.get("entry_type") or "").strip().lower()
        if entry_type not in ENTRY_TYPES:
            entry_type = ENTRY_FEEDBACK
        parsed.append({
            "id": row["id"],
            "plate": row["plate"],
            "comment": row["comment"] or "",
            "status": row["status"] or infer_status(row["comment"], _tinyint_to_bool(row["requires_followup"])),
            "requiresFollowup": _tinyint_to_bool(row["requires_followup"]),
            "date": row["date_added"] or datetime.min,
            "addedBy": row["added_by"] or "",
            "role": row["role"] or "",
            "entryType": entry_type,
            "editedAt": row.get("edited_at"),
            "editedBy": row.get("edited_by") or "",
        })
    return parsed


def load_feedback():
    """
    Returns {plate: {"latest", "latestFeedback", "latestAction", "history"}}
    - identical shape to sheets_store.py's version.
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


def load_feedback_cached():
    """
    sheets_store.py cached this behind a 30s cross-process stamp file
    because a Sheets read cost ~1-4s. A MySQL SELECT over an indexed table
    costs single-digit milliseconds, so that whole cache-invalidation
    machinery (a stamp file every worker had to stat(), a per-worker patch-
    the-cache-with-what-we-just-wrote step) bought nothing here except
    complexity - this is now a direct passthrough. Kept as its own function,
    not an alias, so call sites and their comments (which explain WHY they
    use the cached variant) still read sensibly without editing every one
    of them.
    """
    return load_feedback()


def load_all_feedback_entries(limit=200):
    """Every feedback entry across every vehicle, newest first - the
    activity feed. Capped at `limit`, same as sheets_store.py."""
    entries = _parsed_feedback_rows()
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries[:limit]


def add_feedback(plate: str, comment: str, added_by: str = "", requires_followup=None,
                 role: str = "", entry_type: str = ENTRY_FEEDBACK):
    """Called from the dashboard's POST /api/feedback route. Append-only:
    nothing here ever edits or removes an existing row."""
    if entry_type not in ENTRY_TYPES:
        entry_type = ENTRY_FEEDBACK
    status = infer_status(comment, requires_followup)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO feedback (plate, comment, requires_followup, status, date_added, added_by, role, entry_type) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (plate, comment, _bool_to_tinyint(requires_followup), status,
             now_eat().replace(microsecond=0), added_by, role, entry_type),
        )


def get_feedback_plate(feedback_id: int):
    """The plate a feedback entry belongs to, or None if it doesn't exist
    (or was already deleted) - lets a caller scope-check an edit/delete
    request (app.py's _plate_allowed()) before acting on an id alone,
    the same isolation every other route in this app already enforces
    by plate."""
    with db.cursor() as cur:
        cur.execute("SELECT plate FROM feedback WHERE id=%s AND deleted_at IS NULL", (feedback_id,))
        row = cur.fetchone()
    return row["plate"] if row else None


def update_feedback(feedback_id: int, comment=None, requires_followup=None, edited_by: str = ""):
    """
    Admin-only correction of an existing entry (a typo, a wrongly-worded
    comment) - see app.py's PUT /api/feedback/<id>. Soft: the row is
    updated in place (comment/requires_followup/status), but edited_at/
    edited_by record that it happened and who did it, and every changed
    field is mirrored into audit_log, so this is a correction with a
    trail, not a silent rewrite of history.

    Returns True if the entry was found (and not already deleted).
    """
    with db.cursor() as cur:
        cur.execute("SELECT * FROM feedback WHERE id = %s AND deleted_at IS NULL", (feedback_id,))
        row = cur.fetchone()
        if not row:
            return False

        new_comment = row["comment"] if comment is None else comment
        new_followup = row["requires_followup"] if requires_followup is None else _bool_to_tinyint(requires_followup)
        new_status = infer_status(new_comment, _tinyint_to_bool(new_followup))

        log_change(cur, "feedback", str(feedback_id), "comment", row["comment"], new_comment, edited_by)
        log_change(cur, "feedback", str(feedback_id), "requires_followup",
                   _tinyint_to_bool(row["requires_followup"]), _tinyint_to_bool(new_followup), edited_by)

        cur.execute(
            "UPDATE feedback SET comment=%s, requires_followup=%s, status=%s, "
            "edited_at=%s, edited_by=%s WHERE id=%s",
            (new_comment, new_followup, new_status, now_eat().replace(microsecond=0), edited_by, feedback_id),
        )
        return cur.rowcount > 0


def delete_feedback(feedback_id: int, deleted_by: str = ""):
    """Soft-deletes an entry (see update_feedback's docstring for why soft,
    not a real DELETE) - excluded from every read function above, but still
    in the table and in audit_log for anyone who needs to know it existed.
    Returns True if the entry was found (and not already deleted)."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM feedback WHERE id = %s AND deleted_at IS NULL", (feedback_id,))
        if not cur.fetchone():
            return False
        log_change(cur, "feedback", str(feedback_id), "deleted", "", "true", deleted_by)
        cur.execute(
            "UPDATE feedback SET deleted_at=%s, deleted_by=%s WHERE id=%s",
            (now_eat().replace(microsecond=0), deleted_by, feedback_id),
        )
        return cur.rowcount > 0


# ======================================================================
# Users
# ======================================================================


def load_users_sheet():
    """Every account, keyed by username - identical shape to
    sheets_store.py's version (name kept for a purely mechanical rename at
    every call site; the "_sheet" suffix is a historical artifact of what
    this used to be backed by, not a claim about what backs it now)."""
    with db.cursor() as cur:
        cur.execute("SELECT * FROM users")
        users = cur.fetchall()
        cur.execute("SELECT username, client_name FROM user_clients")
        client_rows = cur.fetchall()

    clients_by_user = {}
    for row in client_rows:
        clients_by_user.setdefault(row["username"], []).append(row["client_name"])

    result = {}
    for u in users:
        result[u["username"]] = {
            "password_hash": u["password_hash"],
            "role": u["role"],
            "clients": sorted(clients_by_user.get(u["username"], [])),
            "last_login": _fmt(u["last_login"]),
            "created_at": _fmt(u["created_at"]),
            "email": u["email"] or "",
            "active": bool(u["active"]),
        }
    return result


def add_user_sheet(username, password_hash, role, clients=None, email=""):
    clients = [str(c).strip() for c in (clients or []) if str(c).strip()]
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, role, email, active, created_at) "
            "VALUES (%s, %s, %s, %s, TRUE, %s)",
            (username, password_hash, role, email, now_eat().replace(microsecond=0)),
        )
        if clients:
            cur.executemany(
                "INSERT INTO user_clients (username, client_name) VALUES (%s, %s)",
                [(username, c) for c in clients],
            )


def set_user_email(username, email):
    with db.cursor() as cur:
        cur.execute("SELECT email FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        if not row:
            return False
        log_change(cur, "users", username, "email", row["email"], email, username)
        cur.execute("UPDATE users SET email=%s WHERE username=%s", (email, username))
        return cur.rowcount > 0


def set_user_clients(username, clients):
    """Updates the client assignments for a user. Clients is a comma-
    separated string or list."""
    if isinstance(clients, (list, tuple)):
        clean = [str(c).strip() for c in clients if str(c).strip()]
    else:
        clean = [c.strip() for c in str(clients or "").split(",") if c.strip()]
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
        if not cur.fetchone():
            return False
        cur.execute("SELECT client_name FROM user_clients WHERE username=%s", (username,))
        old = sorted(r["client_name"] for r in cur.fetchall())
        log_change(cur, "users", username, "clients", ", ".join(old), ", ".join(sorted(clean)), username)
        cur.execute("DELETE FROM user_clients WHERE username=%s", (username,))
        if clean:
            cur.executemany(
                "INSERT INTO user_clients (username, client_name) VALUES (%s, %s)",
                [(username, c) for c in clean],
            )
        return True


def set_user_role(username, role):
    with db.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        if not row:
            return False
        log_change(cur, "users", username, "role", row["role"], role, username)
        cur.execute("UPDATE users SET role=%s WHERE username=%s", (role, username))
        return cur.rowcount > 0


def set_user_active(username, active):
    with db.cursor() as cur:
        cur.execute("SELECT active FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        if not row:
            return False
        log_change(cur, "users", username, "active", bool(row["active"]), bool(active), username)
        cur.execute("UPDATE users SET active=%s WHERE username=%s", (bool(active), username))
        return cur.rowcount > 0


def delete_user_sheet(username):
    """Removes one account entirely (cascades to user_clients via FK).
    Returns True if it was found."""
    with db.cursor() as cur:
        cur.execute("DELETE FROM users WHERE username=%s", (username,))
        return cur.rowcount > 0


def record_login_sheet(username):
    """Stamps last_login. Deliberately non-fatal at the caller
    (users.record_login already wraps this in try/except) and a silent
    no-op if the account doesn't exist."""
    with db.cursor() as cur:
        cur.execute("UPDATE users SET last_login=%s WHERE username=%s",
                    (now_eat().replace(microsecond=0), username))


# ======================================================================
# Email thread ledger
# ----------------------------------------------------------------------
# One vehicle can have many cases over its life; each case is one mail
# thread, opened by a new comment and closed once the client marks "no
# follow-up needed" for it.
# ======================================================================

THREAD_HEADERS = ["Plate", "CaseId", "Subject", "RootMessageId", "ReferencesChain",
                   "Status", "CreatedAt", "LastSentAt"]


def get_or_create_open_case(plate):
    """
    Returns the currently open case for this plate, creating one if none is
    open. Shape unchanged from sheets_store.py: {plate, caseId, subject,
    rootMessageId, references (list), row, reopened, previousClosedAt}.
    "row" now holds the DB row's id rather than a Sheet row number - nothing
    outside this module and record_sent_message()/close_case() ever reads
    it, so the rename is purely internal.
    """
    with db.cursor() as cur:
        cur.execute("SELECT * FROM email_threads WHERE plate=%s ORDER BY case_id", (plate,))
        plate_rows = cur.fetchall()

        open_row = next((r for r in plate_rows if r["status"] == "open"), None)
        if open_row:
            cur.execute(
                "SELECT message_id FROM email_thread_references WHERE thread_id=%s ORDER BY sent_order",
                (open_row["id"],),
            )
            refs = [r["message_id"] for r in cur.fetchall()]
            return {"plate": plate, "caseId": open_row["case_id"], "subject": open_row["subject"],
                    "rootMessageId": open_row["root_message_id"] or "", "references": refs,
                    "row": open_row["id"], "reopened": False, "previousClosedAt": None}

        next_case_id = max((r["case_id"] for r in plate_rows), default=0) + 1
        # Every case-thread subject used to say "GTL" regardless of which
        # client actually owned the plate - a real problem once AGL/ADT
        # threads started going out under someone else's name. This
        # function only has the plate to work with (no client lookup here
        # by design - see its docstring), so the fix is to not name any
        # client in the subject at all, rather than guess.
        subject = f"{plate} — Case {next_case_id}"
        now = now_eat().replace(microsecond=0)
        cur.execute(
            "INSERT INTO email_threads (plate, case_id, subject, root_message_id, status, created_at) "
            "VALUES (%s, %s, %s, '', 'open', %s)",
            (plate, next_case_id, subject, now),
        )
        new_id = cur.lastrowid

        last_closed = max(plate_rows, key=lambda r: r["case_id"], default=None)
        previous_closed_at = None
        if last_closed:
            previous_closed_at = _fmt(last_closed["last_sent_at"] or last_closed["created_at"])
        return {"plate": plate, "caseId": next_case_id, "subject": subject,
                "rootMessageId": "", "references": [], "row": new_id,
                "reopened": bool(plate_rows), "previousClosedAt": previous_closed_at or None}


def record_sent_message(plate, case, message_id):
    """Call after successfully sending one email in this case's thread. The
    first message sent becomes the root every later one threads against;
    every message after that appends to the references chain."""
    root = case["rootMessageId"] or message_id
    references = case["references"] + [message_id]
    with db.cursor() as cur:
        cur.execute(
            "UPDATE email_threads SET root_message_id=%s, last_sent_at=%s WHERE id=%s",
            (root, now_eat().replace(microsecond=0), case["row"]),
        )
        cur.execute(
            "INSERT INTO email_thread_references (thread_id, message_id, sent_order) VALUES (%s, %s, %s)",
            (case["row"], message_id, len(references) - 1),
        )
    case["rootMessageId"], case["references"] = root, references
    return case


def close_case(plate, case_id):
    """Marks a case closed - the NEXT comment on this plate opens a fresh
    case and a fresh thread, rather than reopening a stale one."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE email_threads SET status='closed' WHERE plate=%s AND case_id=%s",
            (plate, case_id),
        )
        return cur.rowcount > 0


# ---- Clients registry ---------------------------------------------------
# A client here is the ORGANISATION (e.g. "Globe Trotters Ltd"), not a
# login - see sheets_store.py's original docstring for the full reasoning,
# unchanged by this migration. The three platform-id child tables are what
# make the mapping possible; their columns are PRIMARY KEYS, so "a platform
# account belongs to exactly one client" is now enforced by MySQL itself,
# not only by app.py's pre-check.
CLIENT_HEADERS = ["Name", "ContactEmails", "CreatedAt", "MixOrgIds", "TeletracClientIds", "FtCloudFleetIds"]

_PLATFORM_TABLES = {
    "mixOrgIds": ("client_mix_org_ids", "mix_org_id"),
    "teletracClientIds": ("client_teletrac_client_ids", "teletrac_client_id"),
    "ftCloudFleetIds": ("client_ftcloud_fleet_ids", "ftcloud_fleet_id"),
}


def load_clients():
    with db.cursor() as cur:
        cur.execute("SELECT * FROM clients ORDER BY name")
        clients = cur.fetchall()
        cur.execute("SELECT client_name, email FROM client_emails")
        email_rows = cur.fetchall()
        platform_rows = {}
        for key, (table, id_col) in _PLATFORM_TABLES.items():
            cur.execute(f"SELECT client_name, {id_col} AS id FROM {table}")
            platform_rows[key] = cur.fetchall()

    emails_by_client = {}
    for r in email_rows:
        emails_by_client.setdefault(r["client_name"], []).append(r["email"])
    ids_by_client = {key: {} for key in _PLATFORM_TABLES}
    for key, rows in platform_rows.items():
        for r in rows:
            ids_by_client[key].setdefault(r["client_name"], []).append(str(r["id"]))

    out = []
    for c in clients:
        name = c["name"]
        out.append({
            "name": name,
            "emails": sorted(emails_by_client.get(name, [])),
            "mixOrgIds": ids_by_client["mixOrgIds"].get(name, []),
            "teletracClientIds": ids_by_client["teletracClientIds"].get(name, []),
            "ftCloudFleetIds": ids_by_client["ftCloudFleetIds"].get(name, []),
            "createdAt": _fmt(c["created_at"]),
        })
    return out


def add_client(name, emails=None, mix_org_ids=None, teletrac_client_ids=None, ft_cloud_fleet_ids=None):
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM clients WHERE LOWER(name)=LOWER(%s)", (name,))
        if cur.fetchone():
            raise ValueError(f"A client named '{name}' already exists")
        cur.execute("INSERT INTO clients (name, created_at) VALUES (%s, %s)",
                    (name, now_eat().replace(microsecond=0)))
        if emails:
            cur.executemany("INSERT INTO client_emails (client_name, email) VALUES (%s, %s)",
                            [(name, e) for e in emails])
        for key, ids in (("mixOrgIds", mix_org_ids), ("teletracClientIds", teletrac_client_ids),
                         ("ftCloudFleetIds", ft_cloud_fleet_ids)):
            if not ids:
                continue
            table, id_col = _PLATFORM_TABLES[key]
            cur.executemany(f"INSERT INTO {table} ({id_col}, client_name) VALUES (%s, %s)",
                            [(str(i), name) for i in ids])


def set_client_emails(name, emails):
    """Replaces the full contact list for one client. Returns False if no
    client by that name exists."""
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM clients WHERE name=%s", (name,))
        if not cur.fetchone():
            return False
        cur.execute("DELETE FROM client_emails WHERE client_name=%s", (name,))
        if emails:
            cur.executemany("INSERT INTO client_emails (client_name, email) VALUES (%s, %s)",
                            [(name, e) for e in emails])
        return True


def set_client_platforms(name, mix_org_ids=None, teletrac_client_ids=None, ft_cloud_fleet_ids=None):
    """
    Replaces this client's platform-account mapping. Passing None for a
    platform leaves that platform's existing mapping alone; passing an
    empty list clears it - same semantics as sheets_store.py. Returns
    False if no client by that name exists.
    """
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM clients WHERE name=%s", (name,))
        if not cur.fetchone():
            return False
        for key, ids in (("mixOrgIds", mix_org_ids), ("teletracClientIds", teletrac_client_ids),
                         ("ftCloudFleetIds", ft_cloud_fleet_ids)):
            if ids is None:
                continue
            table, id_col = _PLATFORM_TABLES[key]
            cur.execute(f"DELETE FROM {table} WHERE client_name=%s", (name,))
            if ids:
                cur.executemany(f"INSERT INTO {table} ({id_col}, client_name) VALUES (%s, %s)",
                                [(str(i), name) for i in ids])
        return True


def delete_client(name):
    with db.cursor() as cur:
        cur.execute("DELETE FROM clients WHERE name=%s", (name,))
        return cur.rowcount > 0


# ---- Vehicle status memory ----------------------------------------------
# One row per plate, overwritten wholesale every import - see
# sheets_store.py's original docstring for the full reasoning (unchanged).
VEHICLE_STATUS_HEADERS = ["Plate", "Status", "UpdatedAt", "OfflineSince", "OfflinePlatforms", "LastSeenAt"]


def load_vehicle_status():
    """{plate: "Online"/"Offline"} as of the last time this was saved."""
    return {plate: detail["status"] for plate, detail in load_vehicle_status_detail().items()}


def load_vehicle_status_detail():
    """{plate: {status, offlineSince, offlinePlatforms, lastSeenAt}} as of
    the previous cycle."""
    with db.cursor() as cur:
        cur.execute("SELECT * FROM vehicle_status")
        rows = cur.fetchall()
    out = {}
    for row in rows:
        platforms = [p.strip() for p in (row["offline_platforms"] or "").split(",") if p.strip()]
        out[row["plate"]] = {
            "status": row["status"] or "",
            "offlineSince": row["offline_since"] or "",
            "offlinePlatforms": platforms,
            "lastSeenAt": row["last_seen_at"] or "",
        }
    return out


def save_vehicle_status(status_by_plate, detail_by_plate=None):
    """Overwrites the whole table with this cycle's status for every plate
    - deliberately a full replace, not a per-row patch, same reasoning as
    sheets_store.py's version."""
    detail_by_plate = detail_by_plate or {}
    now = now_eat().replace(microsecond=0)
    rows = []
    for plate, status in sorted(status_by_plate.items()):
        d = detail_by_plate.get(plate) or {}
        rows.append((plate, status, now, d.get("offlineSince") or "",
                     ", ".join(d.get("offlinePlatforms") or []), d.get("lastSeenAt") or ""))
    with db.cursor() as cur:
        cur.execute("DELETE FROM vehicle_status")
        if rows:
            cur.executemany(
                "INSERT INTO vehicle_status (plate, status, updated_at, offline_since, offline_platforms, last_seen_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                rows,
            )


# ---- Digest state ---------------------------------------------------
DIGEST_STATE_HEADERS = ["DigestKey", "LastSentAt", "PromptedBy"]


def get_digest_last_sent(digest_key):
    """Returns the datetime it last went out, or None if it never has."""
    with db.cursor() as cur:
        cur.execute("SELECT last_sent_at FROM digest_state WHERE digest_key=%s", (digest_key,))
        row = cur.fetchone()
    return row["last_sent_at"] if row else None


def set_digest_last_sent(digest_key, when=None, prompted_by=None):
    when = when or datetime.now()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO digest_state (digest_key, last_sent_at, prompted_by) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE last_sent_at=VALUES(last_sent_at), "
            "prompted_by=IF(VALUES(prompted_by)<>'', VALUES(prompted_by), prompted_by)",
            (digest_key, when.replace(microsecond=0), prompted_by or ""),
        )


def record_manual_checkin(prompted_by, when=None):
    """Records that a human (not the daily import cycle) pressed "Send
    check-in now"."""
    set_digest_last_sent("manual_checkin", when=when, prompted_by=prompted_by)


def get_last_manual_checkin():
    """Returns {"by": str, "at": datetime} or None if never triggered."""
    with db.cursor() as cur:
        cur.execute("SELECT last_sent_at, prompted_by FROM digest_state WHERE digest_key='manual_checkin'")
        row = cur.fetchone()
    if not row or not row["last_sent_at"]:
        return None
    return {"by": row["prompted_by"] or "", "at": row["last_sent_at"]}


# ---- Tamper checks ---------------------------------------------------
# A physical inspection record for a vehicle that tripped the tampering
# detector - append-only, same shape as Feedback, for the same reasons.
# update_tamper_check()/delete_tamper_check() are new, same soft-
# correction/audit-logged design as update_feedback()/delete_feedback().
TAMPER_CHECK_HEADERS = ["Plate", "CheckedBy", "CheckedAt", "Comment"]


def add_tamper_check(plate, checked_by, comment):
    """Appends one physical-check record. Never overwrites a previous check
    on the same plate."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO tamper_checks (plate, checked_by, checked_at, comment) VALUES (%s, %s, %s, %s)",
            (plate, checked_by, now_eat().replace(microsecond=0), comment),
        )


def load_tamper_checks(include_deleted=False):
    """Returns {plate: [{"checkedBy", "checkedAt" (datetime), "comment",
    "id"}, ...]}, newest first per plate."""
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    with db.cursor() as cur:
        cur.execute(f"SELECT * FROM tamper_checks {where}")
        rows = cur.fetchall()
    out = {}
    for row in rows:
        out.setdefault(row["plate"], []).append({
            "id": row["id"],
            "checkedBy": row["checked_by"] or "",
            "checkedAt": row["checked_at"],
            "comment": row["comment"] or "",
        })
    for plate in out:
        out[plate].sort(key=lambda e: e["checkedAt"] or datetime.min, reverse=True)
    return out


def get_tamper_check_plate(check_id: int):
    """The plate a tamper-check record belongs to, or None if it doesn't
    exist (or was already deleted) - same purpose as
    get_feedback_plate() above."""
    with db.cursor() as cur:
        cur.execute("SELECT plate FROM tamper_checks WHERE id=%s AND deleted_at IS NULL", (check_id,))
        row = cur.fetchone()
    return row["plate"] if row else None


def update_tamper_check(check_id: int, comment=None, edited_by: str = ""):
    """Admin-only correction of an existing physical-check record. Soft,
    same reasoning as update_feedback(). Returns True if found."""
    with db.cursor() as cur:
        cur.execute("SELECT comment FROM tamper_checks WHERE id=%s AND deleted_at IS NULL", (check_id,))
        row = cur.fetchone()
        if not row:
            return False
        new_comment = row["comment"] if comment is None else comment
        log_change(cur, "tamper_checks", str(check_id), "comment", row["comment"], new_comment, edited_by)
        cur.execute(
            "UPDATE tamper_checks SET comment=%s, edited_at=%s, edited_by=%s WHERE id=%s",
            (new_comment, now_eat().replace(microsecond=0), edited_by, check_id),
        )
        return cur.rowcount > 0


def delete_tamper_check(check_id: int, deleted_by: str = ""):
    """Soft-deletes a physical-check record. Returns True if found."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM tamper_checks WHERE id=%s AND deleted_at IS NULL", (check_id,))
        if not cur.fetchone():
            return False
        log_change(cur, "tamper_checks", str(check_id), "deleted", "", "true", deleted_by)
        cur.execute(
            "UPDATE tamper_checks SET deleted_at=%s, deleted_by=%s WHERE id=%s",
            (now_eat().replace(microsecond=0), deleted_by, check_id),
        )
        return cur.rowcount > 0


# ---- Respond-token single-use tracking -----------------------------
USED_TOKEN_HEADERS = ["TokenHash", "UsedAt"]


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_token_used(token):
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM used_respond_tokens WHERE token_hash=%s", (_hash_token(token),))
        return cur.fetchone() is not None


def mark_token_used(token):
    with db.cursor() as cur:
        # INSERT IGNORE: a token being marked used twice in a race is
        # harmless (the row already says what it needs to), and this
        # avoids surfacing a duplicate-key error to a caller that just
        # wants "this token is now spent" to be true.
        cur.execute(
            "INSERT IGNORE INTO used_respond_tokens (token_hash, used_at) VALUES (%s, %s)",
            (_hash_token(token), now_eat().replace(microsecond=0)),
        )
