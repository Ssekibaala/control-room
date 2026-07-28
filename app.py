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
from respond_tokens import make_respond_token, read_respond_token

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "importer"))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")


def public_base_url():
    """
    The base URL for links that leave the app in an email, which must
    never depend on which machine happened to be running Flask when the
    notification was sent. request.url_root reflects THAT REQUEST's
    host - correct for a real visitor Browse the deployed site, but
    "http://localhost:5000/" for any local dev run or test script, and a
    link like that is dead for anyone who isn't the exact machine that
    sent it. PUBLIC_BASE_URL is an explicit override; RENDER_EXTERNAL_URL
    is injected automatically by Render for every web service, so
    production needs no manual configuration at all. request.url_root is
    the last resort, kept only so interactive local testing still works.
    """
    configured = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if configured:
        return configured.rstrip("/")
    return request.url_root.rstrip("/")


def _notify_async(plate, comment, added_by, role, entry_type, requires_followup, respond_urls):
    """
    Fires notifications.on_comment_added() on a background thread. The
    Sheets write it follows is already durable by the time this runs -
    email is a courtesy the requester was never waiting on, so a slow or
    blocked SMTP host (see mailer.py's port 587->465 fallback, which by
    itself can take several seconds) must not hold the HTTP response
    open. Errors are only logged here; there is no request left to
    report them to.
    """
    def _run():
        try:
            import notifications
            result = notifications.on_comment_added(
                plate, comment, added_by, role, entry_type, requires_followup, respond_urls)
            if not result["sent"]:
                print(f"Notification email not sent for {plate}: {result['reason']}")
        except Exception as e:
            print(f"Notification email failed for {plate}: {e}")
    threading.Thread(target=_run, daemon=True).start()


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


def _valid_emails(raw):
    """raw is a comma-separated string from the UI. Returns
    (clean_list, error_message) - error_message is None when every
    address present is well-formed, so a single typo doesn't silently
    drop the rest of a client's contacts."""
    emails = [e.strip() for e in raw.split(",") if e.strip()]
    bad = [e for e in emails if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e)]
    if bad:
        return None, f"These don't look like valid email addresses: {', '.join(bad)}"
    return emails, None


@app.route("/api/clients", methods=["GET"])
@login_required
def api_list_clients():
    """Every client is visible to any logged-in role - unlike accounts,
    there's nothing sensitive in an organisation's name and contact
    emails, and technicians need to see this list just as much as admins
    do when they're the ones adding vehicle updates."""
    try:
        import sheets_store
        return jsonify({"clients": sheets_store.load_clients()})
    except Exception as e:
        return jsonify({"error": f"Could not load clients: {e}"}), 503


@app.route("/api/clients", methods=["POST"])
@login_required
def api_create_client():
    if session["role"] not in MANAGE_USERS_ROLES:
        return jsonify({"error": "Not permitted for your role"}), 403
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "'name' is required"}), 400
    emails, err = _valid_emails(body.get("emails") or "")
    if err:
        return jsonify({"error": err}), 400
    try:
        import sheets_store
        sheets_store.add_client(name, emails)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": f"Could not save the client: {e}"}), 503
    return jsonify({"name": name, "emails": emails}), 201


@app.route("/api/clients/<name>/emails", methods=["PUT"])
@login_required
def api_set_client_emails(name):
    if session["role"] not in MANAGE_USERS_ROLES:
        return jsonify({"error": "Not permitted for your role"}), 403
    body = request.get_json(force=True, silent=True) or {}
    emails, err = _valid_emails(body.get("emails") or "")
    if err:
        return jsonify({"error": err}), 400
    try:
        import sheets_store
        found = sheets_store.set_client_emails(name, emails)
    except Exception as e:
        return jsonify({"error": f"Could not save: {e}"}), 503
    if not found:
        return jsonify({"error": f"No client called '{name}'"}), 404
    return jsonify({"name": name, "emails": emails})


@app.route("/api/clients/<name>", methods=["DELETE"])
@login_required
def api_delete_client(name):
    if session["role"] not in MANAGE_USERS_ROLES:
        return jsonify({"error": "Not permitted for your role"}), 403
    try:
        import sheets_store
        found = sheets_store.delete_client(name)
    except Exception as e:
        return jsonify({"error": f"Could not delete: {e}"}), 503
    if not found:
        return jsonify({"error": f"No client called '{name}'"}), 404
    return jsonify({"ok": True})


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

    # The comment is already safely saved above - everything from here is
    # best-effort AND off the request thread. SMTP itself can take several
    # seconds (a blocked port timing out before mailer.py's fallback picks
    # up, or just a slow mail host), and there is nothing in that send the
    # browser needs before it can show "Saved" - the sheet write already
    # succeeded. Blocking the response on it turned every submit into a
    # multi-second wait for something the user was never looking at.
    base_url = public_base_url()
    respond_urls = None
    if role in ("admin", "technician"):
        respond_urls = {
            "no_followup": base_url + url_for("respond_page",
                token=make_respond_token(plate, "no_followup")),
            "needs_attention": base_url + url_for("respond_page",
                token=make_respond_token(plate, "needs_attention")),
        }
    _notify_async(plate, comment, reported_by, role, entry_type, requires_followup_raw, respond_urls)

    return jsonify({"ok": True, "plate": plate, "status": status})


@app.route("/feedback/respond")
def respond_page():
    """
    The page an email button lands on. Rendering it is a GET and changes
    nothing - the pre-selected answer only takes effect once the visitor
    reviews it and presses Confirm, which is a POST. This is deliberate:
    corporate mail scanners (Safe Links, Mimecast, Proofpoint...) prefetch
    every link in an email to scan it, and a GET that acted immediately
    would let a scanner silently answer on the client's behalf before
    they ever opened the message.
    """
    token = request.args.get("token", "")
    payload, error = read_respond_token(token)
    if error:
        return render_template("respond.html", error=error), 400
    return render_template(
        "respond.html", plate=payload["plate"], action=payload["action"], token=token, error=None,
    )


@app.route("/feedback/respond", methods=["POST"])
def respond_submit():
    """
    Commits the answer from the confirm page. No login: the signed,
    expiring token IS the credential, scoped to exactly one plate and
    one pre-chosen action - it can set feedback for that vehicle and
    nothing else, no session, no fleet data, no other vehicle.
    """
    token = request.form.get("token", "") or (request.get_json(silent=True) or {}).get("token", "")
    payload, error = read_respond_token(token)
    if error:
        return jsonify({"error": error}), 400

    # The token proves WHICH vehicle this link may answer for - that's the
    # actual security boundary. The pre-selected action is only a default;
    # the visitor can still switch it on the page before confirming, the
    # same free choice the in-app form gives a logged-in user.
    plate = payload["plate"]
    body = request.get_json(silent=True) or request.form
    comment = (body.get("comment") or "").strip()
    name = (body.get("name") or "").strip()
    requires_followup_raw = body.get("requiresFollowup")
    if isinstance(requires_followup_raw, str):
        requires_followup_raw = requires_followup_raw.lower() == "true"

    if not name:
        return jsonify({"error": "Please tell us who's responding."}), 400
    if not isinstance(requires_followup_raw, bool):
        return jsonify({"error": "Please choose whether this needs follow-up."}), 400
    if not comment:
        comment = "No follow-up needed." if not requires_followup_raw else "Please look into this."

    requires_followup = requires_followup_raw
    try:
        import sheets_store
        sheets_store.add_feedback(
            plate, comment, added_by=name, requires_followup=requires_followup,
            role="client", entry_type="feedback",
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": f"Could not save your response right now: {e}"}), 503

    _notify_async(plate, comment, name, "client", "feedback", requires_followup, None)

    return jsonify({"ok": True, "plate": plate})


@app.route("/api/feedback-history/<plate>")
@login_required
def api_feedback_history(plate):
    """
    The full trail for one vehicle, oldest first - what the "Customer
    Feedback" column on every table can't show (just the latest). Any
    logged-in role can read this: it's the same feedback the client
    themselves can already submit, not privileged data.

    Uses the cached read (same as /api/dashboard-data's overlay) rather
    than a fresh Sheets API call - the drill-down modal was paying a
    full ~1-4s Google Sheets round trip on every open, when the same
    30s-TTL cache everything else already relies on is fresh enough
    here too. A submit on this exact plate still lands instantly for
    the submitter: add_feedback() patches this worker's cache in place
    before this ever runs (see sheets_store._patch_cache_with).
    """
    try:
        import sheets_store
        all_feedback = sheets_store.load_feedback_cached()
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
