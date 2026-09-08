"""
MySQL connection layer for Control Room, replacing Google Sheets
(sheets_store.py) as the durable data store - see docs/SECURITY_AUDIT.md and
the cPanel/MySQL migration plan for why.

Driver: PyMySQL - pure Python, no C compiler needed to install it. That
matters specifically because this runs on shared cPanel hosting, where a
toolchain for a C-extension driver (mysqlclient) may not be available at all,
and a heavier ORM (SQLAlchemy) buys nothing at this scale (about a dozen
tables, low request volume, no cross-database portability need).

Every call opens a fresh connection and closes it before returning - no
persistent pool. This mirrors importer/mail_reader.py's connect-per-call IMAP
pattern, which is what makes both safe under a hosting model (Passenger/
mod_wsgi on cPanel) that does not guarantee a worker process survives between
requests, let alone holds a pooled connection open indefinitely.

Configuration (env vars, read via the same .env python-dotenv already loads
elsewhere in this app):
    MYSQL_HOST, MYSQL_PORT (default 3306), MYSQL_DATABASE, MYSQL_USER,
    MYSQL_PASSWORD

Call init_schema() once, from the one-time setup script
(scripts/init_db.py) - never automatically at app/worker startup, so
multiple worker processes never race to run DDL concurrently.
"""

import os
from contextlib import contextmanager


class NotConfigured(RuntimeError):
    """
    Raised when the MySQL env vars aren't set.

    Deliberately a RuntimeError subclass: every existing except RuntimeError
    handler in app.py (originally written for sheets_store.py's identical
    "not configured" RuntimeError) keeps working unchanged against db_store.py
    without needing to know db_store.py exists - it still returns the same
    503 with the same message shape.
    """


def _config():
    host = os.environ.get("MYSQL_HOST")
    database = os.environ.get("MYSQL_DATABASE")
    user = os.environ.get("MYSQL_USER")
    if not (host and database and user):
        raise NotConfigured(
            "MYSQL_HOST, MYSQL_DATABASE and MYSQL_USER must all be set as "
            "environment variables before the database-backed store will work. "
            "See .env.example."
        )
    return {
        "host": host,
        "port": int(os.environ.get("MYSQL_PORT", "3306")),
        "database": database,
        "user": user,
        "password": os.environ.get("MYSQL_PASSWORD") or "",
    }


def is_configured():
    return bool(os.environ.get("MYSQL_HOST") and os.environ.get("MYSQL_DATABASE")
                and os.environ.get("MYSQL_USER"))


def get_connection():
    """
    A fresh PyMySQL connection, autocommit off (see cursor() below for the
    commit/rollback contract). Exposed directly for the one-time schema
    setup script and for tests; ordinary db_store.py code should go through
    cursor() instead.

    client_flag=CLIENT.FOUND_ROWS makes UPDATE ... rowcount report rows
    MATCHED by the WHERE clause, not rows actually changed - without this,
    updating a field to the same value it already holds would report 0 rows
    affected even though the row exists, which would make e.g.
    set_user_role() incorrectly claim "no such user" for a value that
    happened not to change. Sheets' cell.find()-based equivalent never had
    this failure mode, so this flag keeps that contract intact.

    pymysql is imported here, not at module level, for the same reason
    sheets_store.py only imported gspread inside _get_client(): it's an
    optional dependency that only actually matters the moment something
    tries to talk to the database, so this module (and db_store.py, which
    imports it) can still be imported freely - by tests, by local dev
    without MySQL configured, or just to reach a pure-logic function like
    infer_status() - even on a machine where PyMySQL was never installed.
    """
    try:
        import pymysql
        import pymysql.cursors
        from pymysql.constants import CLIENT
    except ImportError as e:
        raise RuntimeError(
            "MySQL support needs 'PyMySQL'. Run: pip install PyMySQL"
        ) from e

    cfg = _config()
    return pymysql.connect(
        host=cfg["host"], port=cfg["port"], database=cfg["database"],
        user=cfg["user"], password=cfg["password"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
        autocommit=False, client_flag=CLIENT.FOUND_ROWS,
    )


@contextmanager
def cursor():
    """
    Opens a connection, yields a DictCursor, commits on clean exit, rolls
    back and re-raises on any exception, always closes the connection
    afterward. Every db_store.py function is a handful of lines wrapped in
    this - one round trip, one transaction, done.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Eight tables replace the eight Google Sheets tabs sheets_store.py managed,
# plus one generic audit_log (see db_store.py's log_change()) that Sheets'
# built-in revision history used to provide for free. InnoDB + utf8mb4
# throughout. Platform-id columns (client_mix_org_ids etc.) are PRIMARY KEYS,
# not just indexed - "one platform account belongs to exactly one client" is
# now a real database constraint, not just an app-level check
# (app.py's _platform_account_conflict, kept as a friendlier pre-check but no
# longer the only thing standing in the way of a duplicate mapping). This
# also permanently retires sheets_store.py's _looks_corrupted_id() workaround:
# a VARCHAR column cannot silently coerce a 19-digit id into a float the way
# a Sheets NUMBER cell did.
SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        username        VARCHAR(64)  PRIMARY KEY,
        password_hash   VARCHAR(255) NOT NULL,
        role            VARCHAR(32)  NOT NULL,
        email           VARCHAR(255) NOT NULL DEFAULT '',
        active          BOOLEAN      NOT NULL DEFAULT TRUE,
        last_login      DATETIME     NULL,
        created_at      DATETIME     NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS user_clients (
        username        VARCHAR(64)  NOT NULL,
        client_name     VARCHAR(128) NOT NULL,
        PRIMARY KEY (username, client_name),
        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
        plate              VARCHAR(32)  NOT NULL,
        comment            TEXT         NOT NULL,
        requires_followup  TINYINT(1)   NULL,
        status             VARCHAR(64)  NOT NULL,
        date_added         DATETIME     NOT NULL,
        added_by           VARCHAR(128) NOT NULL DEFAULT '',
        role               VARCHAR(32)  NOT NULL DEFAULT '',
        entry_type         VARCHAR(16)  NOT NULL DEFAULT 'feedback',
        edited_at          DATETIME     NULL,
        edited_by          VARCHAR(128) NULL,
        deleted_at         DATETIME     NULL,
        deleted_by         VARCHAR(128) NULL,
        INDEX idx_feedback_plate_date (plate, date_added)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS email_threads (
        id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
        plate              VARCHAR(32)  NOT NULL,
        case_id            INT          NOT NULL,
        subject            VARCHAR(255) NOT NULL,
        root_message_id    VARCHAR(255) NOT NULL DEFAULT '',
        status             ENUM('open','closed') NOT NULL DEFAULT 'open',
        created_at         DATETIME     NOT NULL,
        last_sent_at       DATETIME     NULL,
        UNIQUE KEY uq_email_threads_plate_case (plate, case_id),
        INDEX idx_email_threads_plate_status (plate, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS email_thread_references (
        thread_id          BIGINT      NOT NULL,
        message_id         VARCHAR(255) NOT NULL,
        sent_order         INT         NOT NULL,
        PRIMARY KEY (thread_id, sent_order),
        FOREIGN KEY (thread_id) REFERENCES email_threads(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS clients (
        name               VARCHAR(128) PRIMARY KEY,
        created_at         DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS client_emails (
        client_name        VARCHAR(128) NOT NULL,
        email              VARCHAR(255) NOT NULL,
        PRIMARY KEY (client_name, email),
        FOREIGN KEY (client_name) REFERENCES clients(name) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS client_mix_org_ids (
        mix_org_id         VARCHAR(32) PRIMARY KEY,
        client_name        VARCHAR(128) NOT NULL,
        FOREIGN KEY (client_name) REFERENCES clients(name) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS client_teletrac_client_ids (
        teletrac_client_id VARCHAR(64) PRIMARY KEY,
        client_name        VARCHAR(128) NOT NULL,
        FOREIGN KEY (client_name) REFERENCES clients(name) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS client_ftcloud_fleet_ids (
        ftcloud_fleet_id   VARCHAR(32) PRIMARY KEY,
        client_name        VARCHAR(128) NOT NULL,
        FOREIGN KEY (client_name) REFERENCES clients(name) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS vehicle_status (
        plate              VARCHAR(32) PRIMARY KEY,
        status             VARCHAR(32) NOT NULL,
        updated_at         DATETIME NOT NULL,
        offline_since      VARCHAR(64) NOT NULL DEFAULT '',
        offline_platforms  VARCHAR(255) NOT NULL DEFAULT '',
        last_seen_at       VARCHAR(64) NOT NULL DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS digest_state (
        digest_key         VARCHAR(64) PRIMARY KEY,
        last_sent_at       DATETIME NULL,
        prompted_by        VARCHAR(128) NOT NULL DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS tamper_checks (
        id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
        plate              VARCHAR(32) NOT NULL,
        checked_by         VARCHAR(128) NOT NULL DEFAULT '',
        checked_at         DATETIME NOT NULL,
        comment            TEXT NOT NULL,
        edited_at          DATETIME NULL,
        edited_by          VARCHAR(128) NULL,
        deleted_at         DATETIME NULL,
        deleted_by         VARCHAR(128) NULL,
        INDEX idx_tamper_checks_plate_date (plate, checked_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS used_respond_tokens (
        token_hash         CHAR(64) PRIMARY KEY,
        used_at            DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # Generic change log, replacing what Sheets' built-in revision history
    # used to give us for free on the tables above that aren't already
    # append-only (Feedback/TamperChecks ARE their own audit trail by
    # design - this exists for users/clients/vehicle_status/digest_state,
    # where a Sheets edit used to be recoverable from the Sheet's own
    # version history and a MySQL UPDATE otherwise leaves no trace at all).
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
        table_name         VARCHAR(64) NOT NULL,
        row_key            VARCHAR(128) NOT NULL,
        field              VARCHAR(64) NOT NULL,
        old_value          TEXT NULL,
        new_value          TEXT NULL,
        changed_by         VARCHAR(128) NOT NULL DEFAULT '',
        changed_at         DATETIME NOT NULL,
        INDEX idx_audit_log_table_row (table_name, row_key, changed_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # System-wide activity log: one row per state-changing HTTP request
    # (POST/PUT/DELETE), written centrally from app.py's after_request
    # hook rather than by each route individually - so it covers every
    # action (login, feedback, exports, cron/webhook triggers, admin
    # changes) without needing every route author to remember to log it.
    # Deliberately separate from audit_log above: this is "what happened
    # and who did it", not "what field changed from what to what" - the
    # two are complementary, not overlapping.
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
        occurred_at        DATETIME NOT NULL,
        username           VARCHAR(64) NOT NULL DEFAULT '',
        role               VARCHAR(32) NOT NULL DEFAULT '',
        method             VARCHAR(8) NOT NULL,
        path               VARCHAR(255) NOT NULL,
        status_code        INT NOT NULL,
        ip_address         VARCHAR(64) NOT NULL DEFAULT '',
        duration_ms        INT NOT NULL DEFAULT 0,
        INDEX idx_activity_log_occurred (occurred_at),
        INDEX idx_activity_log_username (username, occurred_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]


def init_schema():
    """Creates every table if it doesn't already exist. Safe to re-run any
    number of times. Run this once from scripts/init_db.py - never
    automatically at app/worker startup, so multiple Passenger worker
    processes never race to run DDL concurrently."""
    with cursor() as cur:
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)
