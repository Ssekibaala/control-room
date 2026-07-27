"""
GTL Control Room - Flask backend.

Job 1: login/logout, session-based.
Job 2: serve the dashboard shell (any logged-in role).
Job 3: /api/dashboard-data - the ONLY place role filtering matters.
       Every byte sent to the browser passes through
       permissions.filter_payload_for_role() first. A client-role
       session literally cannot receive tampering data in the HTTP
       response, not "won't show it", cannot.
Job 4: /api/export/<name> - gated by permissions.EXPORT_ACCESS.

Run locally:
    python app.py
Then open http://127.0.0.1:5000
"""

import os
import re
import sys
import json
import time
import base64
import threading
import functools
from datetime import datetime
from flask import Flask, request, session, jsonify, redirect, url_for, Response, render_template

from dotenv import load_dotenv
load_dotenv()  # loads .env for local dev; no-op on Render, which injects real env vars directly

import users as users_store
from users import verify_login
import permissions
from permissions import (
    filter_payload_for_role, allowed_panels, EXPORT_ACCESS,
    MANAGE_USERS_ROLES, ROLES, PANEL_LABELS, LockoutError,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "importer"))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "fleet_today.json")


def load_dashboard_data():
    if not os.path.exists(DATA_PATH):
        return {}
    with open(DATA_PATH) as f:
        return json.load(f)


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            return jsonify({"error": "not authenticated"}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(force=True, silent=True) or {}
    username = body.get("username", "")
    password = body.get("password", "")
    role = verify_login(username, password)
    if role is None:
        return jsonify({"error": "Invalid username or password"}), 401
    session["username"] = username
    session["role"] = role
    return jsonify({"username": username, "role": role, "panels": allowed_panels(role)})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/session")
def api_session():
    if "username" not in session:
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "username": session["username"],
        "role": session["role"],
        "panels": allowed_panels(session["role"]),
    })


@app.route("/api/users", methods=["GET"])
@login_required
def api_list_users():
    """
    Admin and technician accounts can see who has a login and what role
    they hold - never password hashes, those never leave users.json.
    Client role is deliberately excluded: it's an external account
    (GTL themselves), not part of your team's account administration.
    """
    if session["role"] not in MANAGE_USERS_ROLES:
        return jsonify({"error": "Not permitted for your role"}), 403
    all_users = users_store.load_users()
    return jsonify({
        "users": [
            {
                "username": name,
                "role": info.get("role", ""),
                # The address notifications go to. Blank means this person
                # simply isn't reachable by email yet - the UI says so rather
                # than letting someone assume they're being kept informed.
                "email": info.get("email", ""),
                # A user can be assigned more than one client, so this is
                # always a list even when there's only one today.
                "clients": info.get("clients", []),
                # Blank for anyone who hasn't signed in since last-login
                # tracking was added - the UI shows "Never" rather than
                # inventing a date that was never recorded.
                "lastLogin": info.get("last_login", ""),
                "createdAt": info.get("created_at", ""),
            }
            for name, info in sorted(all_users.items())
        ],
        # Tells the UI whether accounts are in the durable store or the
        # ephemeral local fallback, so it can warn rather than quietly
        # letting someone create accounts that a deploy will erase.
        "durable": users_store._sheets_available(),
    })


@app.route("/api/users", methods=["POST"])
@login_required
def api_create_user():
    """
    Lets admin and technician accounts provision new logins from the
    UI instead of needing shell access to run users.py by hand - the
    same add_user() call, just reachable without a terminal. Refuses
    to silently overwrite an existing username: that's still a job for
    users.py directly, a deliberate, explicit action, not a UI click.
    """
    if session["role"] not in MANAGE_USERS_ROLES:
        return jsonify({"error": "Not permitted for your role"}), 403

    body = request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    role = (body.get("role") or "").strip()
    password = body.get("password") or ""
    email = (body.get("email") or "").strip()
    clients_raw = body.get("clients") or []
    if isinstance(clients_raw, str):
        clients_raw = [c.strip() for c in clients_raw.split(",")]
    clients = [str(c).strip() for c in clients_raw if str(c).strip()]

    if not username or not role or not password:
        return jsonify({"error": "'username', 'role', and 'password' are all required"}), 400
    if role not in ROLES:
        return jsonify({"error": f"'role' must be one of {list(ROLES)}"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "That doesn't look like a valid email address"}), 400
    if username in users_store.load_users():
        return jsonify({"error": f"'{username}' already has an account"}), 409

    try:
        users_store.add_user(username, role, password, clients, email)
    except Exception as e:
        return jsonify({"error": f"Could not save the account: {e}"}), 503
    return jsonify({"username": username, "role": role, "clients": clients, "email": email}), 201


@app.route("/api/users/<username>/email", methods=["PUT"])
@login_required
def api_set_user_email(username):
    """Backfills an address for an account created before emails were
    collected, so existing users become reachable without being recreated."""
    if session["role"] not in MANAGE_USERS_ROLES:
        return jsonify({"error": "Not permitted for your role"}), 403

    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip()
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "That doesn't look like a valid email address"}), 400

    try:
        found = users_store.set_email(username, email)
    except Exception as e:
        return jsonify({"error": f"Could not save the address: {e}"}), 503
    if not found:
        return jsonify({"error": f"No account called '{username}'"}), 404
    return jsonify({"username": username, "email": email})


@app.route("/api/role-panels", methods=["GET"])
@login_required
def api_role_panels():
    """The current panel->roles matrix. Every cell is freely tickable -
    the only rule the server still enforces is that Manage Users can't
    be taken away from every role at once, which is checked on save."""
    if session["role"] not in MANAGE_USERS_ROLES:
        return jsonify({"error": "Not permitted for your role"}), 403
    return jsonify({
        "roles": list(ROLES),
        "panels": [
            {
                "id": panel,
                "label": PANEL_LABELS.get(panel, panel),
                "roles": list(roles),
            }
            for panel, roles in permissions.PANEL_ACCESS.items()
        ],
    })


@app.route("/api/role-panels", methods=["POST"])
@login_required
def api_save_role_panels():
    if session["role"] not in MANAGE_USERS_ROLES:
        return jsonify({"error": "Not permitted for your role"}), 403

    body = request.get_json(force=True, silent=True) or {}
    new_access = body.get("panels")
    if not isinstance(new_access, dict):
        return jsonify({"error": "'panels' must be an object of panelId -> [roles]"}), 400

    try:
        updated = permissions.save_panel_access(new_access)
    except LockoutError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"Could not save role settings: {e}"}), 503

    return jsonify({"ok": True, "panels": {p: list(r) for p, r in updated.items()}})


def _overlay_feedback(raw):
    """
    Applies the human layer on top of the imported telemetry. Returns
    (payload, overlay_ok) - on failure the telemetry still goes out and
    the caller flags the payload as stale, because a dashboard that
    silently drops feedback is worse than one that admits it couldn't
    load it.
    """
    try:
        import sheets_store
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fleet_logic"))
        from feedback_overlay import apply_feedback
        return apply_feedback(raw, sheets_store.load_feedback_cached()), True
    except Exception as e:
        print(f"Feedback overlay unavailable this request: {e}")
        return raw, False


@app.route("/api/dashboard-data")
@login_required
def api_dashboard_data():
    role = session["role"]
    raw = load_dashboard_data()
    raw, overlay_ok = _overlay_feedback(raw)
    filtered = filter_payload_for_role(raw, role)
    filtered = dict(filtered)
    filtered["meta"] = {**filtered.get("meta", {}), "feedbackApplied": overlay_ok}
    # xlsxB64/tamperB64 never need to reach the browser at all, any role,
    # since /api/export/<name> reads them straight from disk on demand.
    filtered = {k: v for k, v in filtered.items() if k not in ("xlsxB64", "tamperB64")}
    return jsonify(filtered)


@app.route("/api/export/<name>")
@login_required
def api_export(name):
    role = session["role"]
    key_map = {"integrity": "integrity_xlsx", "tampering": "tampering_xlsx"}
    perm_key = key_map.get(name)
    if perm_key is None or role not in EXPORT_ACCESS.get(perm_key, ()):
        return jsonify({"error": "not permitted for your role"}), 403

    raw = load_dashboard_data()
    b64_key = "xlsxB64" if name == "integrity" else "tamperB64"
    b64 = raw.get(b64_key)
    if not b64:
        return jsonify({"error": "report not available"}), 404
    return Response(
        base64.b64decode(b64),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=GTL_{name}_report.xlsx"},
    )


@app.route("/api/feedback", methods=["POST"])
@login_required
def api_feedback():
    """
    All three roles can submit: this is a two-way log between whoever's
    watching the vehicle (client) and whoever acts on it (technician/
    admin), not a one-way form. requires_followup is an explicit choice
    made here, not guessed from wording - "No" is what pulls an asset
    out of the active Critical/Pending queues into Known Issues (see
    classifier.py), so it has to be a deliberate answer, not inferred.
    """
    role = session["role"]
    body = request.get_json(force=True, silent=True) or {}
    plate = (body.get("plate") or "").strip()
    comment = (body.get("comment") or "").strip()
    reported_by = (body.get("reportedBy") or "").strip()
    requires_followup_raw = body.get("requiresFollowup")
    entry_type = (body.get("entryType") or "feedback").strip().lower()

    if not plate or not comment or not reported_by:
        return jsonify({"error": "'plate', 'comment', and 'reportedBy' are required"}), 400
    if not isinstance(requires_followup_raw, bool):
        return jsonify({"error": "'requiresFollowup' must be true or false, an explicit choice, not optional"}), 400
    if entry_type not in ("feedback", "action"):
        return jsonify({"error": "'entryType' must be 'feedback' or 'action'"}), 400
    # Recommended Action is the technician's operational instruction to
    # the field - a client stating their own next step would be writing
    # your team's job card for them, so that field stays internal.
    if entry_type == "action" and role == "client":
        return jsonify({"error": "Only technicians and admins can set the Recommended Action"}), 403

    try:
        import sheets_store
        sheets_store.add_feedback(
            plate, comment, added_by=reported_by,
            requires_followup=requires_followup_raw, role=role,
            entry_type=entry_type,
        )
        status = sheets_store.infer_status(comment, requires_followup_raw)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        # Anything from gspread/Google's side (rate limit, timeout,
        # transient API error) landed here before as an unhandled
        # exception - Flask's HTML error page isn't valid JSON, so the
        # frontend's fetch().then(r => r.json()) throws and shows a
        # misleading "could not reach the server" for what was actually
        # a real, specific failure that reached this code just fine.
        return jsonify({"error": f"Could not save to Google Sheets right now: {e}"}), 503

    return jsonify({"ok": True, "plate": plate, "status": status})


@app.route("/api/feedback-history/<plate>")
@login_required
def api_feedback_history(plate):
    """
    The full trail for one vehicle, oldest first - what the "Customer
    Feedback" column on every table can't show (just the latest). Any
    logged-in role can read this: it's the same feedback the client
    themselves can already submit, not privileged data.
    """
    try:
        import sheets_store
        all_feedback = sheets_store.load_feedback()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": f"Could not read from Google Sheets right now: {e}"}), 503

    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fleet_logic"))
    from schema import normalize_plate
    entry = all_feedback.get(normalize_plate(plate))
    history = entry["history"] if entry else []
    return jsonify({
        "plate": plate,
        "history": [
            {
                "comment": h["comment"], "status": h["status"], "requiresFollowup": h["requiresFollowup"],
                "date": h["date"].strftime("%d %b %Y, %H:%M") if h["date"].year > 1 else "",
                "addedBy": h["addedBy"], "role": h["role"], "entryType": h.get("entryType", "feedback"),
            }
            for h in history
        ],
    })


@app.route("/api/feedback-activity")
@login_required
def api_feedback_activity():
    """
    The global activity feed: every comment across every vehicle,
    newest first, so anyone with the dashboard open can see what's been
    reported today (or any other day) without opening each vehicle one
    at a time. Same data class as api_feedback_history, every role can
    read this.
    """
    try:
        import sheets_store
        entries = sheets_store.load_all_feedback_entries()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": f"Could not read from Google Sheets right now: {e}"}), 503

    # dateISO (YYYY-MM-DD) is what the frontend actually filters/counts
    # on - unambiguous and locale-independent, unlike matching "26 Jul
    # 2026"-style strings between Python and JS, which breaks the
    # moment either side's month abbreviation or locale differs.
    # "date"/"dateOnly" stay as display-formatted strings for showing
    # in the UI, never for comparison.
    today_iso = datetime.now().strftime("%Y-%m-%d")
    serialized = [
        {
            "plate": e["plate"], "comment": e["comment"], "requiresFollowup": e["requiresFollowup"],
            "date": e["date"].strftime("%d %b %Y, %H:%M") if e["date"].year > 1 else "",
            "dateOnly": e["date"].strftime("%d %b %Y") if e["date"].year > 1 else "",
            "dateISO": e["date"].strftime("%Y-%m-%d") if e["date"].year > 1 else "",
            "addedBy": e["addedBy"], "role": e["role"], "entryType": e.get("entryType", "feedback"),
        }
        for e in entries
    ]
    today_count = sum(1 for e in serialized if e["dateISO"] == today_iso)

    return jsonify({"todayCount": today_count, "todayISO": today_iso, "entries": serialized})


REFRESH_LOCK_PATH = os.path.join(os.path.dirname(__file__), "data", "_refresh_lock.json")
STALE_THRESHOLD_HOURS = 2
LOCK_COOLDOWN_MINUTES = 10


def _claim_refresh_lock():
    """
    True if this call just claimed the right to trigger a refresh,
    False if someone else already holds it. Uses O_CREAT|O_EXCL, which
    is atomic at the OS level, so this is safe across gunicorn's
    multiple worker *processes* on Render, not just threads in one -
    a plain in-memory flag would only ever be seen by one worker.
    """
    try:
        fd = os.open(REFRESH_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            json.dump({"triggered_at": time.time()}, f)
        return True
    except FileExistsError:
        try:
            with open(REFRESH_LOCK_PATH) as f:
                held = json.load(f)
            age_minutes = (time.time() - held.get("triggered_at", 0)) / 60
        except (OSError, ValueError, json.JSONDecodeError):
            age_minutes = LOCK_COOLDOWN_MINUTES  # unreadable lock, treat as stale, safe to steal
        if age_minutes < LOCK_COOLDOWN_MINUTES:
            return False
        try:
            os.remove(REFRESH_LOCK_PATH)
        except OSError:
            pass
        return _claim_refresh_lock()  # one retry now that the stale lock is cleared


@app.route("/api/refresh-if-stale", methods=["POST"])
@login_required
def api_refresh_if_stale():
    """
    Backup for when the Apps Script scheduler doesn't fire. Any logged-
    in user's dashboard load can call this, but staleness and the
    trigger lock are both decided here, server-side - never trusted
    from the client (clocks lie, and coordinating "who goes first"
    across browser tabs from different users isn't something the
    client can do safely anyway). At most one real import gets kicked
    off per LOCK_COOLDOWN_MINUTES no matter how many requests hit this
    at once.
    """
    try:
        with open(DATA_PATH) as f:
            meta = json.load(f).get("meta", {})
        # importedAt (wall-clock time the import last actually ran) is
        # the right signal here, not "generated" (the latest timestamp
        # found IN the report data, which lags real time by design -
        # see run_import.py). Older data written before this field
        # existed won't have it; treat that as stale too, correctly,
        # since it means no import has run since this feature shipped.
        imported_str = meta.get("importedAt", "")
        imported_at = datetime.strptime(imported_str, "%d %B %Y, %H:%M")
        age_hours = (datetime.now() - imported_at).total_seconds() / 3600
    except (FileNotFoundError, ValueError, KeyError):
        age_hours = STALE_THRESHOLD_HOURS + 1  # no readable/importedAt-less data - treat as stale

    if age_hours < STALE_THRESHOLD_HOURS:
        return jsonify({"status": "fresh", "ageHours": round(age_hours, 1)})

    if not _claim_refresh_lock():
        return jsonify({"status": "refresh_recently_triggered", "ageHours": round(age_hours, 1)})

    def _background_import():
        try:
            from run_import import run_import as do_import
            do_import(force=True)
        except Exception as e:
            print(f"Background auto-refresh failed: {e}")

    threading.Thread(target=_background_import, daemon=True).start()
    return jsonify({"status": "refresh_triggered", "ageHours": round(age_hours, 1)})


@app.route("/api/import", methods=["GET", "POST"])
def api_import():
    """
    Called ONLY by the Apps Script scheduler, never by a browser.
    Protected by a shared secret in the X-API-Key header, not a user
    session, since there's no logged-in user at 4:15 AM.
    """
    expected_key = os.environ.get("IMPORT_API_KEY")
    provided_key = request.headers.get("X-API-Key")
    if not expected_key or provided_key != expected_key:
        return jsonify({"error": "unauthorized"}), 403

    force = request.args.get("force") == "true"
    try:
        from run_import import run_import as do_import
        result = do_import(force=force)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    if "username" not in session:
        return redirect(url_for("login_page"))
    role = session["role"]
    return render_template(
        "dashboard.html", allowed_panels=allowed_panels(role),
        can_export_integrity=role in EXPORT_ACCESS["integrity_xlsx"],
        can_export_tampering=role in EXPORT_ACCESS["tampering_xlsx"],
        can_manage_users=role in MANAGE_USERS_ROLES,
        # Built from the sheet ID rather than stored as a second env var,
        # so there's only ever one place the spreadsheet is identified.
        # Empty when Sheets isn't configured, and the button hides itself.
        sheet_url=(
            f"https://docs.google.com/spreadsheets/d/{os.environ['FEEDBACK_SHEET_ID']}/edit"
            if os.environ.get("FEEDBACK_SHEET_ID") else ""
        ),
    )


@app.route("/login")
def login_page():
    if "username" in session:
        return redirect(url_for("index"))
    return render_template("login.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
