"""
User accounts. Local JSON file for now (zero setup cost, matches the
"no infrastructure" constraint), same load_users()/save_users()
interface so this can be swapped for a Google Sheets tab later
without touching app.py or permissions.py at all.

Passwords are never stored in plain text. Run this file directly to
create or reset a user:
    python users.py add brandon.b technician "a-real-password"
    python users.py add justin admin "a-real-password"
    python users.py add gtl-client client "a-real-password"
"""

import json
import os
import sys
from werkzeug.security import generate_password_hash, check_password_hash

USERS_PATH = os.path.join(os.path.dirname(__file__), "database", "users.json")


def load_users():
    if not os.path.exists(USERS_PATH):
        return {}
    with open(USERS_PATH) as f:
        return json.load(f)


def save_users(users):
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)
    with open(USERS_PATH, "w") as f:
        json.dump(users, f, indent=2)


def add_user(username, role, password):
    from permissions import ROLES
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    users = load_users()
    users[username] = {"password_hash": generate_password_hash(password), "role": role}
    save_users(users)
    return users[username]


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
    return user["role"]


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "add":
        _, _, username, role, password = sys.argv
        add_user(username, role, password)
        print(f"User '{username}' saved with role '{role}'.")
    else:
        print("Usage: python users.py add <username> <role> <password>")
        print("Roles: admin, technician, client")
