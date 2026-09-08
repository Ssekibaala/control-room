"""
One-time data migration: reads every tab out of the Google Sheet (via the
OLD sheets_store.py - its last intended use anywhere in this codebase) and
writes it into the new MySQL tables (via db.py/db_store.py).

Usage:
    python scripts/init_db.py                      # once, creates tables
    python scripts/export_sheets_to_mysql.py --dry-run   # see what it would do
    python scripts/export_sheets_to_mysql.py             # actually migrate

Idempotent by design: every table this writes to is cleared (its own rows
only, nothing else) before being repopulated, so running this twice against
the same Sheet produces the same result rather than duplicate rows. Run it
against a STAGING database first and check the printed row counts against
what you expect from the live Sheet before trusting it for the real cutover
- see the migration plan's Section 6.

GOOGLE_SERVICE_ACCOUNT_JSON and FEEDBACK_SHEET_ID must still be set for
this one run (same as they were for the live app before this migration);
MYSQL_HOST/MYSQL_DATABASE/MYSQL_USER/MYSQL_PASSWORD must be set for the
destination.
"""

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fleet_logic"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import db
import db_store
import sheets_store


def _parse_sheet_date(raw):
    """sheets_store.py wrote every timestamp as "%d/%m/%Y %H:%M" text -
    parse it back for a real MySQL DATETIME column. Blank/unparseable
    dates fall back to None (NULL), which every db_store.py reader
    already treats safely (e.g. _fmt(None) -> "")."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def export_users(dry_run):
    users = sheets_store.load_users_sheet()
    print(f"Users: {len(users)} account(s) found in the Sheet.")
    if dry_run:
        for username, info in users.items():
            print(f"  would migrate: {username} ({info['role']}, clients={info['clients']})")
        return
    with db.cursor() as cur:
        cur.execute("DELETE FROM users")  # cascades to user_clients
        for username, info in users.items():
            cur.execute(
                "INSERT INTO users (username, password_hash, role, email, active, last_login, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (username, info["password_hash"], info["role"], info["email"], info["active"],
                 _parse_sheet_date(info["last_login"]), _parse_sheet_date(info["created_at"]) or datetime.now()),
            )
            for client_name in info["clients"]:
                cur.execute(
                    "INSERT IGNORE INTO user_clients (username, client_name) VALUES (%s, %s)",
                    (username, client_name),
                )
    print(f"  migrated {len(users)} account(s).")


def export_feedback(dry_run):
    rows = sheets_store._parsed_feedback_rows()
    print(f"Feedback: {len(rows)} entr(y/ies) found in the Sheet.")
    if dry_run:
        return
    with db.cursor() as cur:
        cur.execute("DELETE FROM feedback")
        for r in rows:
            requires_followup = None if r["requiresFollowup"] is None else (1 if r["requiresFollowup"] else 0)
            cur.execute(
                "INSERT INTO feedback (plate, comment, requires_followup, status, date_added, added_by, role, entry_type) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (r["plate"], r["comment"], requires_followup, r["status"],
                 r["date"] if r["date"] and r["date"].year > 1 else datetime.now(),
                 r["addedBy"], r["role"], r["entryType"]),
            )
    print(f"  migrated {len(rows)} entr(y/ies).")


def export_tamper_checks(dry_run):
    by_plate = sheets_store.load_tamper_checks()
    total = sum(len(v) for v in by_plate.values())
    print(f"TamperChecks: {total} record(s) across {len(by_plate)} plate(s) found in the Sheet.")
    if dry_run:
        return
    with db.cursor() as cur:
        cur.execute("DELETE FROM tamper_checks")
        for plate, checks in by_plate.items():
            for c in checks:
                cur.execute(
                    "INSERT INTO tamper_checks (plate, checked_by, checked_at, comment) VALUES (%s, %s, %s, %s)",
                    (plate, c["checkedBy"], c["checkedAt"] or datetime.now(), c["comment"]),
                )
    print(f"  migrated {total} record(s).")


def export_clients(dry_run):
    clients = sheets_store.load_clients()
    print(f"Clients: {len(clients)} found in the Sheet.")
    corrupted_total = 0
    if dry_run:
        for c in clients:
            corrupted = c.get("corruptedIds") or []
            corrupted_total += len(corrupted)
            print(f"  would migrate: {c['name']} (emails={len(c['emails'])}, "
                  f"mix={len(c['mixOrgIds'])}, teletrac={len(c['teletracClientIds'])}, "
                  f"ftCloud={len(c['ftCloudFleetIds'])})" + (f" -- {len(corrupted)} CORRUPTED id(s) dropped, "
                  f"re-pick manually after migration" if corrupted else ""))
        if corrupted_total:
            print(f"  WARNING: {corrupted_total} platform id(s) across all clients were already mangled by "
                  f"Sheets' float-coercion bug and cannot be recovered - re-pick them from the mapping "
                  f"dropdowns in the app after cutover.")
        return
    with db.cursor() as cur:
        # Order matters: children before the parent, since the parent
        # delete would otherwise cascade the children away first anyway -
        # explicit is simpler to reason about here than relying on that.
        cur.execute("DELETE FROM client_emails")
        cur.execute("DELETE FROM client_mix_org_ids")
        cur.execute("DELETE FROM client_teletrac_client_ids")
        cur.execute("DELETE FROM client_ftcloud_fleet_ids")
        cur.execute("DELETE FROM clients")
        for c in clients:
            cur.execute("INSERT INTO clients (name, created_at) VALUES (%s, %s)",
                        (c["name"], _parse_sheet_date(c["createdAt"]) or datetime.now()))
            for email in c["emails"]:
                cur.execute("INSERT IGNORE INTO client_emails (client_name, email) VALUES (%s, %s)",
                            (c["name"], email))
            for mix_id in c["mixOrgIds"]:
                cur.execute("INSERT IGNORE INTO client_mix_org_ids (mix_org_id, client_name) VALUES (%s, %s)",
                            (mix_id, c["name"]))
            for teletrac_id in c["teletracClientIds"]:
                cur.execute("INSERT IGNORE INTO client_teletrac_client_ids (teletrac_client_id, client_name) VALUES (%s, %s)",
                            (teletrac_id, c["name"]))
            for ft_id in c["ftCloudFleetIds"]:
                cur.execute("INSERT IGNORE INTO client_ftcloud_fleet_ids (ftcloud_fleet_id, client_name) VALUES (%s, %s)",
                            (ft_id, c["name"]))
            corrupted_total += len(c.get("corruptedIds") or [])
    print(f"  migrated {len(clients)} client(s).")
    if corrupted_total:
        print(f"  WARNING: {corrupted_total} platform id(s) across all clients were already mangled by "
              f"Sheets' float-coercion bug and could not be migrated - re-pick them from the mapping "
              f"dropdowns in the app after cutover (see sheets_store.py's _looks_corrupted_id docstring).")


def export_vehicle_status(dry_run):
    detail = sheets_store.load_vehicle_status_detail()
    print(f"VehicleStatus: {len(detail)} plate(s) found in the Sheet.")
    if dry_run:
        return
    status_by_plate = {p: d["status"] for p, d in detail.items()}
    db_store.save_vehicle_status(status_by_plate, detail)
    print(f"  migrated {len(detail)} plate(s).")


def export_digest_state(dry_run):
    client, sheet_id = sheets_store._get_client()
    ws = sheets_store._get_or_create_digest_state_tab(client, sheet_id)
    rows = ws.get_all_records()
    print(f"DigestState: {len(rows)} row(s) found in the Sheet.")
    if dry_run:
        return
    with db.cursor() as cur:
        cur.execute("DELETE FROM digest_state")
        for row in rows:
            key = str(row.get("DigestKey", "")).strip()
            if not key:
                continue
            last_sent = _parse_sheet_date(row.get("LastSentAt"))
            prompted_by = str(row.get("PromptedBy", "")).strip()
            cur.execute(
                "INSERT INTO digest_state (digest_key, last_sent_at, prompted_by) VALUES (%s, %s, %s)",
                (key, last_sent, prompted_by),
            )
    print(f"  migrated {len(rows)} row(s).")


def export_used_tokens(dry_run):
    client, sheet_id = sheets_store._get_client()
    ws = sheets_store._get_or_create_used_tokens_tab(client, sheet_id)
    rows = ws.get_all_records()
    print(f"UsedRespondTokens: {len(rows)} row(s) found in the Sheet.")
    if dry_run:
        return
    with db.cursor() as cur:
        cur.execute("DELETE FROM used_respond_tokens")
        for row in rows:
            token_hash = str(row.get("TokenHash", "")).strip()
            if not token_hash:
                continue
            used_at = _parse_sheet_date(row.get("UsedAt")) or datetime.now()
            cur.execute(
                "INSERT IGNORE INTO used_respond_tokens (token_hash, used_at) VALUES (%s, %s)",
                (token_hash, used_at),
            )
    print(f"  migrated {len(rows)} row(s).")


def export_email_threads(dry_run):
    client, sheet_id = sheets_store._get_client()
    ws = sheets_store._get_or_create_threads_tab(client, sheet_id)
    rows = ws.get_all_records()
    print(f"EmailThreads: {len(rows)} case(s) found in the Sheet.")
    if dry_run:
        return
    with db.cursor() as cur:
        cur.execute("DELETE FROM email_thread_references")
        cur.execute("DELETE FROM email_threads")
        for row in rows:
            plate = str(row.get("Plate", "")).strip()
            if not plate:
                continue
            case_id = int(row.get("CaseId") or 0)
            subject = str(row.get("Subject", "")).strip()
            root_message_id = str(row.get("RootMessageId", "")).strip()
            status = "closed" if str(row.get("Status", "")).strip().lower() == "closed" else "open"
            created_at = _parse_sheet_date(row.get("CreatedAt")) or datetime.now()
            last_sent_at = _parse_sheet_date(row.get("LastSentAt"))
            cur.execute(
                "INSERT INTO email_threads (plate, case_id, subject, root_message_id, status, created_at, last_sent_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (plate, case_id, subject, root_message_id, status, created_at, last_sent_at),
            )
            thread_id = cur.lastrowid
            references = [m.strip() for m in str(row.get("ReferencesChain", "")).split(" ") if m.strip()]
            for order, message_id in enumerate(references):
                cur.execute(
                    "INSERT INTO email_thread_references (thread_id, message_id, sent_order) VALUES (%s, %s, %s)",
                    (thread_id, message_id, order),
                )
    print(f"  migrated {len(rows)} case(s).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be migrated without writing anything to MySQL.")
    args = parser.parse_args()

    if not args.dry_run and not db.is_configured():
        print("MYSQL_HOST, MYSQL_DATABASE and MYSQL_USER must all be set before this can write "
              "anything. Re-run with --dry-run to preview against the Sheet alone, or set those "
              "first. See .env.example.")
        sys.exit(1)

    print("=== Exporting Google Sheet -> MySQL ===")
    if args.dry_run:
        print("(dry run - nothing will be written)")
    print()

    # Order matters for the real run: clients/users before anything that
    # references them isn't actually enforced by foreign keys here (the
    # child tables reference clients.name/users.username, and every
    # export function fully replaces its own tables), but reads happen in
    # this order for a readable, deterministic printout.
    export_users(args.dry_run)
    export_clients(args.dry_run)
    export_feedback(args.dry_run)
    export_tamper_checks(args.dry_run)
    export_vehicle_status(args.dry_run)
    export_digest_state(args.dry_run)
    export_used_tokens(args.dry_run)
    export_email_threads(args.dry_run)

    print()
    print("Done." if not args.dry_run else "Dry run complete - re-run without --dry-run to actually migrate.")


if __name__ == "__main__":
    main()
