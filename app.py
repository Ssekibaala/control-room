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
import base64
import functools
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
    Only admin/technician submit feedback, matching the intent that
    this records what your team was told by the customer, not a
    self-service form for the client role to edit their own record.
    """
    role = session["role"]
    if role not in ("admin", "technician"):
        return jsonify({"error": "not permitted for your role"}), 403

    body = request.get_json(force=True, silent=True) or {}
    plate = (body.get("plate") or "").strip()
    comment = (body.get("comment") or "").strip()
    if not plate or not comment:
        return jsonify({"error": "both 'plate' and 'comment' are required"}), 400

    try:
        import sheets_store
        sheets_store.add_feedback(plate, comment, added_by=session["username"])
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"ok": True, "plate": plate, "status": sheets_store.infer_status(comment)})


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
