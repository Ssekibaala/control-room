"""
User accounts, stored in the Google Sheet.

WHY NOT A LOCAL FILE: database/users.json is committed to git, and
Render's filesystem is ephemeral - every deploy re-clones the repo. A
file-backed account list therefore silently reverts to whatever was
committed on each deploy, taking every account created through Manage
Users and every last-login stamp with it. The Sheet already exists as
the durable store for feedback ("these comments must never be
deleted"), so accounts live there too and survive deploys, restarts
and redeploys.

The local JSON file is kept as a read-only FALLBACK, used only when
Sheets credentials aren't configured (fresh clone, offline dev, running
the test suite without secrets). Anything written while in fallback
mode is expected to be temporary.

Passwords are never stored in plain text, in either store. Create or
reset a user from the command line:
    python users.py add brandon.b technician "a-real-password"
    python users.py add gtl-client client "a-real-password" "Globe Trotters"
"""

import json
import os
import sys
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

USERS_PATH = os.path.join(os.path.dirname(__file__), "database", "users.json")


def _sheets_available():
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") and os.environ.get("FEEDBACK_SHEET_ID"))


def _load_local():
    if not os.path.exists(USERS_PATH):
        return {}
    try:
        with open(USERS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_local(users):
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)
    with open(USERS_PATH, "w") as f:
        json.dump(users, f, indent=2)


def load_users():
    """
    Every account. Reads the Sheet when configured, falling back to the
    local file otherwise. A Sheets read that errors (network blip, rate
    limit) also falls back rather than locking everyone out of login -
    a stale account list beats a dead sign-in page.
    """
    if _sheets_available():
        try:
            import sheets_store
            users = sheets_store.load_users_sheet()
            if users:
                return users
            # Empty Users tab on a Sheet that's otherwise set up means
            # this is the first run since accounts moved here. Seed it
            # from the local file so nobody is locked out mid-migration.
            local = _load_local()
            if local:
                _migrate_local_to_sheet(local)
                return sheets_store.load_users_sheet()
            return {}
        except Exception:
            return _load_local()
    return _load_local()


def _migrate_local_to_sheet(local_users):
    import sheets_store
    for username, info in local_users.items():
        sheets_store.add_user_sheet(
            username, info.get("password_hash", ""), info.get("role", ""),
            info.get("clients", []),
        )


def save_users(users):
    """Kept for the local fallback path only - the Sheet is written
    per-row by add_user()/record_login() rather than wholesale, so one
    account's update can never clobber another's."""
    _save_local(users)


def add_user(username, role, password, clients=None, email=""):
    from permissions import ROLES
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")

    email = (email or "").strip()
    password_hash = generate_password_hash(password)
    if _sheets_available():
        import sheets_store
        sheets_store.add_user_sheet(username, password_hash, role, clients or [], email)
    else:
        users = _load_local()
        users[username] = {
            "password_hash": password_hash, "role": role,
            "clients": clients or [], "email": email,
            "created_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
        _save_local(users)
    return {"username": username, "role": role, "clients": clients or [], "email": email}


def set_email(username, email):
    """Backfills the address for an account created before emails were
    collected. Returns True if the account was found."""
    email = (email or "").strip()
    if _sheets_available():
        import sheets_store
        return sheets_store.set_user_email(username, email)
    users = _load_local()
    if username not in users:
        return False
    users[username]["email"] = email
    _save_local(users)
    return True


def notification_recipients(roles=None):
    """
    Addresses to notify, drawn from the accounts themselves - a user with
    no address is simply not reachable and is skipped rather than
    silently dropping the whole send. Deduplicated, because two accounts
    can legitimately share a shared mailbox.
    """
    seen, out = set(), []
    for username, info in load_users().items():
        addr = (info.get("email") or "").strip()
        if not addr or addr.lower() in seen:
            continue
        if roles and info.get("role") not in roles:
            continue
        seen.add(addr.lower())
        out.append({"username": username, "email": addr, "role": info.get("role", "")})
    return out


def record_login(username):
    """
    Stamps the successful sign-in so Manage Users can show who's
    actually been using the platform (and, more usefully, who hasn't
    signed in for months and probably shouldn't still have an account).
    Deliberately non-fatal: failing to record this must never be able to
    block someone from logging in, so every error is swallowed.
    """
    try:
        if _sheets_available():
            import sheets_store
            sheets_store.record_login_sheet(username)
            return
        users = _load_local()
        if username not in users:
            return
        users[username]["last_login"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        _save_local(users)
    except Exception:
        pass


def verify_login(username, password):
    """Returns the role string on success, None on failure. Never
    reveals whether the failure was a bad username or bad password,
    same response either way, that's deliberate."""
    users = load_users()
    user = users.get(username)
    if not user:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    record_login(username)
    return user["role"]


def get_user(username):
    return load_users().get(username)


if __name__ == "__main__":
    if len(sys.argv) in (5, 6, 7) and sys.argv[1] == "add":
        username, role, password = sys.argv[2], sys.argv[3], sys.argv[4]
        email = sys.argv[5] if len(sys.argv) >= 6 else ""
        clients = [c.strip() for c in sys.argv[6].split(",")] if len(sys.argv) == 7 else []
        add_user(username, role, password, clients, email)
        where = "the Google Sheet" if _sheets_available() else f"{USERS_PATH} (local fallback)"
        print(f"User '{username}' saved with role '{role}' to {where}.")
    elif len(sys.argv) == 4 and sys.argv[1] == "set-email":
        ok = set_email(sys.argv[2], sys.argv[3])
        print(f"Email {'updated' if ok else 'NOT set - no such account'} for '{sys.argv[2]}'.")
    else:
        print('Usage: python users.py add <username> <role> <password> [email] ["Client A, Client B"]')
        print('       python users.py set-email <username> <email>')
        print("Roles: admin, technician, client")
