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

from users import verify_login
from permissions import filter_payload_for_role, allowed_panels, EXPORT_ACCESS

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


@app.route("/api/dashboard-data")
@login_required
def api_dashboard_data():
    role = session["role"]
    raw = load_dashboard_data()
    filtered = filter_payload_for_role(raw, role)
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

    if not plate or not comment or not reported_by:
        return jsonify({"error": "'plate', 'comment', and 'reportedBy' are required"}), 400
    if not isinstance(requires_followup_raw, bool):
        return jsonify({"error": "'requiresFollowup' must be true or false, an explicit choice, not optional"}), 400

    try:
        import sheets_store
        sheets_store.add_feedback(
            plate, comment, added_by=reported_by,
            requires_followup=requires_followup_raw, role=role,
        )
        status = sheets_store.infer_status(comment, requires_followup_raw)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

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
                "addedBy": h["addedBy"], "role": h["role"],
            }
            for h in history
        ],
    })


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
    )


@app.route("/login")
def login_page():
    if "username" in session:
        return redirect(url_for("index"))
    return render_template("login.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
