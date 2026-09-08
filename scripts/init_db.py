"""
One-time MySQL schema setup.

Run this once, after MYSQL_HOST/MYSQL_PORT/MYSQL_DATABASE/MYSQL_USER/
MYSQL_PASSWORD are set (in .env for local use, or however the host injects
env vars - cPanel's "Setup Python App" env var panel, or a .env file next
to this repo), and before the app is pointed at that database for the
first time.

Deliberately NOT called automatically from app.py/db.py at import time:
multiple Passenger/gunicorn worker processes would otherwise race to run
the same DDL concurrently on first request. Run it once, by hand:

    python scripts/init_db.py

Safe to re-run any number of times - every statement is
CREATE TABLE IF NOT EXISTS (see db.SCHEMA_STATEMENTS).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import db


def main():
    if not db.is_configured():
        print("MYSQL_HOST, MYSQL_DATABASE and MYSQL_USER must all be set "
              "(as environment variables, or in a .env file next to this "
              "repo) before the schema can be created. See .env.example.")
        sys.exit(1)

    print(f"Creating {len(db.SCHEMA_STATEMENTS)} table(s) if they don't already exist...")
    db.init_schema()
    print("Done.")


if __name__ == "__main__":
    main()
