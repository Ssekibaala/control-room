"""
Proves the security boundary at the only place that matters: the
actual bytes of the HTTP response. Uses Flask's real test client
(no mocking of the request/response cycle) to log in as each role
and inspect exactly what came back over the wire.
"""

import json
from app import app


def login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def _reset_role_matrix():
    """Back to the shipped defaults. Run at BOTH ends of the suite: a
    previous run that died mid-way (or a save made through the UI while
    developing) leaves database/role_panels.json behind, and every
    client-visibility assertion below is written against the defaults -
    so without this the suite reports leaks that are really just
    yesterday's settings."""
    import os
    import permissions as perms
    path = os.path.join(os.path.dirname(__file__), "database", "role_panels.json")
    if os.path.exists(path):
        os.remove(path)
    perms.PANEL_ACCESS.clear()
    perms.PANEL_ACCESS.update(perms._build_panel_access())


def run():
    client = app.test_client()
    failures = []
    _reset_role_matrix()

    print("=== Wrong password is rejected ===")
    r = login(client, "justin", "wrong-password")
    ok = r.status_code == 401
    print(f"  [{'PASS' if ok else 'FAIL'}] status {r.status_code}")
    if not ok:
        failures.append("wrong password not rejected")

    print()
    print("=== Admin login and data access ===")
    r = login(client, "justin", "TestPass123!")
    print(f"  login status: {r.status_code}, role: {r.get_json().get('role')}")
    r = client.get("/api/dashboard-data")
    admin_data = r.get_json()
    print(f"  dashboard-data status: {r.status_code}")
    print(f"  keys present: {sorted(admin_data.keys())}")
    checks = [
        ("tamperConfirmed" in admin_data, "admin sees tamperConfirmed"),
        ("xlsxB64" not in admin_data, "xlsxB64 never sent in JSON payload, even to admin (export endpoint handles it)"),
        (len(admin_data.get("critical", [])) > 0, "admin sees critical rows"),
    ]
    for passed, label in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        if not passed:
            failures.append(label)
    r = client.get("/api/export/integrity")
    print(f"  [{'PASS' if r.status_code == 200 else 'FAIL'}] admin can actually download the real xlsx (status {r.status_code}, {len(r.data)} bytes)")
    if r.status_code != 200:
        failures.append("admin export endpoint broken")
    client.post("/api/logout")

    print()
    print("=== Technician login and data access ===")
    login(client, "brandon.b", "TestPass123!")
    r = client.get("/api/dashboard-data")
    tech_data = r.get_json()
    checks = [
        ("tamperConfirmed" in tech_data, "technician sees tamperConfirmed"),
        ("border" in tech_data, "technician sees border"),
    ]
    for passed, label in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        if not passed:
            failures.append(label)
    r = client.get("/api/export/tampering")
    print(f"  [{'PASS' if r.status_code == 200 else 'FAIL'}] technician can export tampering xlsx (status {r.status_code})")
    if r.status_code != 200:
        failures.append("technician export blocked incorrectly")
    client.post("/api/logout")

    print()
    print("=== Client login: THE actual security proof ===")
    login(client, "gtl-client", "TestPass123!")
    r = client.get("/api/dashboard-data")
    client_data = r.get_json()
    raw_body = r.get_data(as_text=True)

    print(f"  Response keys sent to client role: {sorted(client_data.keys())}")

    must_not_appear = ["tamperConfirmed", "tamperUnconfirmed", "qualityLog", "xlsxB64",
                        "tamperB64", "doubleFlagged", "settingsRows", "severityBands"]
    for key in must_not_appear:
        absent_from_dict = key not in client_data
        absent_from_raw_bytes = f'"{key}"' not in raw_body
        passed = absent_from_dict and absent_from_raw_bytes
        print(f"  [{'PASS' if passed else 'FAIL'}] '{key}' absent from actual response bytes")
        if not passed:
            failures.append(f"client response leaks '{key}'")

    # Check row-level field stripping on a panel the client CAN see
    if client_data.get("critical"):
        row = client_data["critical"][0]
        has_reasons = "reasons" in row
        print(f"  [{'PASS' if not has_reasons else 'FAIL'}] 'reasons' field stripped from critical rows for client")
        if has_reasons:
            failures.append("client sees internal 'reasons' field")

    # Client should be blocked from both exports outright
    r1 = client.get("/api/export/integrity")
    r2 = client.get("/api/export/tampering")
    print(f"  [{'PASS' if r1.status_code == 403 else 'FAIL'}] client blocked from integrity export (status {r1.status_code})")
    print(f"  [{'PASS' if r2.status_code == 403 else 'FAIL'}] client blocked from tampering export (status {r2.status_code})")
    if r1.status_code != 403:
        failures.append("client can export integrity xlsx")
    if r2.status_code != 403:
        failures.append("client can export tampering xlsx")

    # Client should still get the panels they ARE allowed
    print(f"  [{'PASS' if 'exec' in json.dumps(client_data) or client_data.get('kpi') else 'FAIL'}] client still receives Executive Dashboard KPIs")

    print()
    print("=== Feedback: client can submit, validation holds, history readable ===")
    login(client, "gtl-client", "TestPass123!")

    sess = client.get("/api/session").get_json()
    known_in_panels = "p-known" in sess.get("panels", [])
    print(f"  [{'PASS' if known_in_panels else 'FAIL'}] client's allowed_panels includes p-known (Known Issues)")
    if not known_in_panels:
        failures.append("client cannot see Known Issues panel")

    activity_in_panels = "p-activity" in sess.get("panels", [])
    print(f"  [{'PASS' if activity_in_panels else 'FAIL'}] client's allowed_panels includes p-activity (Recent Activity)")
    if not activity_in_panels:
        failures.append("client cannot see Recent Activity panel")

    TEST_PLATE = "ZZZTEST1"

    r = client.post("/api/feedback", json={"plate": TEST_PLATE, "comment": "x"})  # missing reportedBy/requiresFollowup
    print(f"  [{'PASS' if r.status_code == 400 else 'FAIL'}] missing required fields rejected (status {r.status_code})")
    if r.status_code != 400:
        failures.append("feedback missing-field validation broken")

    r = client.post("/api/feedback", json={"plate": TEST_PLATE, "comment": "x", "reportedBy": "Tester", "requiresFollowup": "yes"})
    print(f"  [{'PASS' if r.status_code == 400 else 'FAIL'}] non-boolean requiresFollowup rejected (status {r.status_code})")
    if r.status_code != 400:
        failures.append("feedback requiresFollowup type validation broken")

    r = client.post("/api/feedback", json={
        "plate": TEST_PLATE, "comment": "Automated test - safe to ignore/delete",
        "reportedBy": "test_permissions.py", "requiresFollowup": False,
    })
    sheets_configured = r.status_code != 503
    if sheets_configured:
        print(f"  [{'PASS' if r.status_code == 200 else 'FAIL'}] client CAN submit feedback now (status {r.status_code}) - was admin/technician-only before")
        if r.status_code != 200:
            failures.append("client cannot submit feedback")

        r = client.get(f"/api/feedback-history/{TEST_PLATE}")
        history = r.get_json().get("history", []) if r.status_code == 200 else []
        found = any(h.get("addedBy") == "test_permissions.py" for h in history)
        print(f"  [{'PASS' if found else 'FAIL'}] just-submitted feedback appears in /api/feedback-history/{TEST_PLATE}")
        if not found:
            failures.append("submitted feedback not readable back from history")

        r = client.get("/api/feedback-activity")
        activity = r.get_json() if r.status_code == 200 else {}
        activity_entries = activity.get("entries", [])
        found_in_activity = any(e.get("plate") == TEST_PLATE and e.get("addedBy") == "test_permissions.py" for e in activity_entries)
        print(f"  [{'PASS' if found_in_activity else 'FAIL'}] just-submitted feedback appears in /api/feedback-activity (global feed)")
        if not found_in_activity:
            failures.append("submitted feedback not visible in the global activity feed")
        counted_today = activity.get("todayCount", 0) >= 1
        print(f"  [{'PASS' if counted_today else 'FAIL'}] /api/feedback-activity todayCount reflects the new comment")
        if not counted_today:
            failures.append("feedback-activity todayCount did not include the just-submitted entry")

        # Clean up: this test data has no business staying in the real
        # Sheet permanently, same principle as never leaving fabricated
        # entries against a real vehicle plate.
        try:
            import sheets_store
            sh_client, sheet_id = sheets_store._get_client()
            ws = sh_client.open_by_key(sheet_id).worksheet("Feedback")
            cell_matches = ws.findall(TEST_PLATE)
            for cell in sorted(cell_matches, key=lambda c: -c.row):
                ws.delete_rows(cell.row)
            # Submitting feedback now also opens/updates an EmailThreads
            # row for the plate (see notifications.on_comment_added) -
            # clean that up too, or it leaks a "ZZZTEST1" case forever.
            ws_threads = sheets_store._get_or_create_threads_tab(sh_client, sheet_id)
            thread_matches = ws_threads.findall(TEST_PLATE)
            for cell in sorted(thread_matches, key=lambda c: -c.row):
                ws_threads.delete_rows(cell.row)
            print(f"  (cleaned up {len(cell_matches)} feedback row(s) and {len(thread_matches)} thread ledger row(s))")
        except Exception as e:
            print(f"  (could not clean up test row automatically: {e})")
    else:
        print("  [SKIP] Sheets not configured in this environment (503) - validation-only checks above still count")

    client.post("/api/logout")
    r = client.post("/api/feedback", json={"plate": TEST_PLATE, "comment": "x", "reportedBy": "x", "requiresFollowup": True})
    print(f"  [{'PASS' if r.status_code == 401 else 'FAIL'}] logged-out feedback submission rejected (status {r.status_code})")
    if r.status_code != 401:
        failures.append("logged-out user can submit feedback")
    r = client.get(f"/api/feedback-history/{TEST_PLATE}")
    print(f"  [{'PASS' if r.status_code == 401 else 'FAIL'}] logged-out feedback history read rejected (status {r.status_code})")
    if r.status_code != 401:
        failures.append("logged-out user can read feedback history")
    r = client.get("/api/feedback-activity")
    print(f"  [{'PASS' if r.status_code == 401 else 'FAIL'}] logged-out feedback-activity read rejected (status {r.status_code})")
    if r.status_code != 401:
        failures.append("logged-out user can read the global activity feed")

    print()
    print("=== Feedback overlay: marking no-follow-up takes effect immediately ===")
    # The defect this covers: feedback used to be applied only during the
    # daily email import, so an asset marked "no follow-up needed" kept
    # sitting in Critical and Priority Watch until the next morning's run.
    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "fleet_logic"))
    from feedback_overlay import apply_feedback
    from datetime import datetime as _dt

    base = {
        "full": [
            {"plate": "AAA111", "status": "Technical Escalation", "severity": "Critical - Long-term Fault",
             "days": 40, "feedback": "", "action": "Recover device", "border": "No"},
            {"plate": "BBB222", "status": "Technical Escalation", "severity": "Elevated - Monitor",
             "days": 5, "feedback": "", "action": "Contact customer", "border": "No"},
        ],
        "critical": [], "criticalCards": [], "pending": [], "knownIssues": [],
        "healthy": [], "border": [], "doubleFlagged": [],
        "kpi": {"escalations": 2, "knownIssues": 0, "total": 2, "online": 0},
        "sevCounts": {}, "meta": {},
    }

    def _entry(plate, comment, followup, who, role, etype):
        e = {"plate": plate, "comment": comment, "requiresFollowup": followup, "date": _dt.now(),
             "addedBy": who, "role": role, "entryType": etype,
             "status": "Known Issue - No Follow-up Needed" if followup is False else "Follow-up Requested"}
        return e

    fb_entry = _entry("AAA111", "parked in the workshop", False, "Glenn", "client", "feedback")
    overlaid = apply_feedback(base, {"AAA111": {
        "latest": fb_entry, "latestFeedback": fb_entry, "latestAction": None, "history": [fb_entry]}})

    row = next(r for r in overlaid["full"] if r["plate"] == "AAA111")
    checks = [
        (row["status"] == "Known Issue", "no-follow-up flips the asset to Known Issue"),
        (row["severity"] == "No Follow-up Needed", "severity follows the status change"),
        ("parked in the workshop" in row["feedback"], "the comment reaches the Customer Feedback column"),
        (not any(r["plate"] == "AAA111" for r in overlaid["critical"]), "asset leaves Critical Assets"),
        (not any(r["plate"] == "AAA111" for r in overlaid["criticalCards"]), "asset leaves Priority Watch"),
        (any(r["plate"] == "AAA111" for r in overlaid["knownIssues"]), "asset appears under Known Issues"),
        (overlaid["kpi"]["escalations"] == 1, "escalation count drops"),
        (overlaid["kpi"]["knownIssues"] == 1, "known-issue count rises"),
        (base["full"][0]["status"] == "Technical Escalation", "the cached import payload is never mutated"),
    ]
    for passed, label in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        if not passed:
            failures.append(f"overlay: {label}")

    # A technician's written action outranks the computed suggestion, and
    # both entry types land in the same trail for the same asset.
    act = _entry("BBB222", "Dispatch Kelvin Monday", True, "Kelvin", "technician", "action")
    fb2 = _entry("BBB222", "driver says it is at the border", True, "Glenn", "client", "feedback")
    overlaid2 = apply_feedback(base, {"BBB222": {
        "latest": fb2, "latestFeedback": fb2, "latestAction": act, "history": [act, fb2]}})
    row2 = next(r for r in overlaid2["full"] if r["plate"] == "BBB222")
    checks2 = [
        (row2["action"] == "Dispatch Kelvin Monday", "technician action overrides the computed recommendation"),
        ("border" in row2["feedback"], "client feedback fills its own column, separately"),
        (row2["status"] == "Technical Escalation", "requiresFollowup=True keeps the asset in the active queue"),
    ]
    for passed, label in checks2:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        if not passed:
            failures.append(f"overlay: {label}")

    login(client, "gtl-client", "TestPass123!")
    r = client.post("/api/feedback", json={"plate": "ZZZTEST1", "comment": "x", "reportedBy": "t",
                                           "requiresFollowup": True, "entryType": "action"})
    print(f"  [{'PASS' if r.status_code == 403 else 'FAIL'}] client cannot author a Recommended Action (status {r.status_code})")
    if r.status_code != 403:
        failures.append("client can write the technician's action field")
    client.post("/api/logout")

    print()
    print("=== Roles & Visibility editor drives the actual payload ===")
    import permissions as perms

    login(client, "justin", "TestPass123!")
    r = client.get("/api/role-panels")
    print(f"  [{'PASS' if r.status_code == 200 else 'FAIL'}] admin can read the role matrix (status {r.status_code})")
    if r.status_code != 200:
        failures.append("admin cannot read role matrix")

    # Granting a panel must deliver that panel's DATA too, not just show
    # an empty nav item - this is the whole point of the matrix being the
    # single source of truth rather than two lists that can disagree.
    granted = {p: list(roles) for p, roles in perms.PANEL_ACCESS.items()}
    granted["p-tconfirmed"] = ["admin", "technician", "client"]
    r = client.post("/api/role-panels", json={"panels": granted})
    print(f"  [{'PASS' if r.status_code == 200 else 'FAIL'}] granting client a tampering panel is accepted (status {r.status_code})")
    if r.status_code != 200:
        failures.append("role matrix save rejected a legitimate grant")
    client.post("/api/logout")

    login(client, "gtl-client", "TestPass123!")
    granted_data = client.get("/api/dashboard-data").get_json()
    got_rows = len(granted_data.get("tamperConfirmed", []))
    print(f"  [{'PASS' if got_rows > 0 else 'FAIL'}] client now actually receives tamperConfirmed data ({got_rows} rows)")
    if got_rows == 0:
        failures.append("granted panel delivered no data - matrix and payload disagree")
    still_blocked = "tamperUnconfirmed" not in granted_data
    print(f"  [{'PASS' if still_blocked else 'FAIL'}] panels NOT granted stay blocked (tamperUnconfirmed absent)")
    if not still_blocked:
        failures.append("ungranted panel leaked data")
    no_blob = "xlsxB64" not in granted_data and "tamperB64" not in granted_data
    print(f"  [{'PASS' if no_blob else 'FAIL'}] raw xlsx blobs still never sent, whatever the matrix says")
    if not no_blob:
        failures.append("xlsx blob leaked into JSON payload")
    client.post("/api/logout")

    # Revoking must take the data away again, symmetrically.
    login(client, "justin", "TestPass123!")
    revoked = {p: list(roles) for p, roles in perms.PANEL_ACCESS.items()}
    revoked["p-tconfirmed"] = ["admin", "technician"]
    client.post("/api/role-panels", json={"panels": revoked})
    client.post("/api/logout")
    login(client, "gtl-client", "TestPass123!")
    revoked_data = client.get("/api/dashboard-data").get_json()
    gone = "tamperConfirmed" not in revoked_data
    print(f"  [{'PASS' if gone else 'FAIL'}] revoking the panel removes its data again")
    if not gone:
        failures.append("revoked panel still sending data")
    client.post("/api/logout")

    # The one refusal left: stripping Manage Users from every role would
    # make the editor permanently unreachable.
    login(client, "justin", "TestPass123!")
    lockout = {p: list(roles) for p, roles in perms.PANEL_ACCESS.items()}
    lockout["p-users"] = []
    r = client.post("/api/role-panels", json={"panels": lockout})
    print(f"  [{'PASS' if r.status_code == 400 else 'FAIL'}] refuses to leave nobody with Manage Users (status {r.status_code})")
    if r.status_code != 400:
        failures.append("editor allowed an unrecoverable lockout")

    r = client.get("/api/users")
    print(f"  [{'PASS' if r.status_code == 200 else 'FAIL'}] user list readable by admin (status {r.status_code})")
    has_last_login = all("lastLogin" in u for u in r.get_json().get("users", []))
    print(f"  [{'PASS' if has_last_login else 'FAIL'}] every account reports a lastLogin field")
    if not has_last_login:
        failures.append("lastLogin missing from user list")
    client.post("/api/logout")

    login(client, "gtl-client", "TestPass123!")
    r1 = client.get("/api/users")
    r2 = client.get("/api/role-panels")
    r3 = client.post("/api/users", json={"username": "x", "role": "admin", "password": "12345678"})
    for resp, label in ((r1, "list users"), (r2, "read role matrix"), (r3, "create a user")):
        print(f"  [{'PASS' if resp.status_code == 403 else 'FAIL'}] client cannot {label} (status {resp.status_code})")
        if resp.status_code != 403:
            failures.append(f"client can {label}")
    client.post("/api/logout")

    _reset_role_matrix()
    print("  (reset role matrix back to shipped defaults)")

    print()
    print("=== Email-response flow: token security holds ===")
    from app import make_respond_token
    tok = make_respond_token("ZZTOKTEST", "no_followup")

    r = client.get("/feedback/respond?token=not-a-real-token")
    print(f"  [{'PASS' if r.status_code == 400 else 'FAIL'}] garbage token rejected on the confirm page (status {r.status_code})")
    if r.status_code != 400:
        failures.append("respond_page accepts a garbage token")

    tampered = tok[:-2] + ("a" if tok[-2] != "a" else "b") + tok[-1]
    r = client.get(f"/feedback/respond?token={tampered}")
    print(f"  [{'PASS' if r.status_code == 400 else 'FAIL'}] tampered token rejected, signature check holds (status {r.status_code})")
    if r.status_code != 400:
        failures.append("respond_page accepts a tampered token")

    r = client.get(f"/feedback/respond?token={tok}")
    plate_shown = b"ZZTOKTEST" in r.data
    print(f"  [{'PASS' if r.status_code == 200 and plate_shown else 'FAIL'}] valid token renders the confirm page for the right plate (status {r.status_code})")
    if not (r.status_code == 200 and plate_shown):
        failures.append("respond_page doesn't render the plate for a valid token")

    r = client.post("/feedback/respond", json={"token": tok, "requiresFollowup": "false"})
    print(f"  [{'PASS' if r.status_code == 400 else 'FAIL'}] submitting with no name is rejected (status {r.status_code})")
    if r.status_code != 400:
        failures.append("respond_submit accepts a response with no name")

    r = client.post("/feedback/respond", json={"token": tok, "name": "Tester"})
    print(f"  [{'PASS' if r.status_code == 400 else 'FAIL'}] submitting with no follow-up choice is rejected (status {r.status_code})")
    if r.status_code != 400:
        failures.append("respond_submit accepts a response with no explicit follow-up choice")

    # This is the actual security boundary worth proving: the token grants
    # answering ONE plate, nothing else - it carries no session and needs
    # no login, so if it leaked scope beyond its own plate that would be a
    # real hole, not a cosmetic one.
    r = client.post("/feedback/respond", json={
        "token": tok, "name": "AUTOTEST_CLEANUP tester",
        "comment": "AUTOTEST token-scope probe - safe to delete", "requiresFollowup": "false",
    })
    ok = r.status_code == 200 and r.get_json().get("plate") == "ZZTOKTEST"
    print(f"  [{'PASS' if ok else 'FAIL'}] valid token submits successfully and only for its own plate (status {r.status_code})")
    if not ok:
        failures.append("respond_submit didn't accept a fully valid submission")

    try:
        import sheets_store as _ss
        fb = _ss.load_feedback().get("ZZTOKTEST", {})
        recorded = any(h["addedBy"] == "AUTOTEST_CLEANUP tester" for h in fb.get("history", []))
        print(f"  [{'PASS' if recorded else 'FAIL'}] the email-link response lands in the SAME trail as in-app feedback")
        if not recorded:
            failures.append("email-link response didn't land in the unified feedback trail")
        client_sheet, sheet_id = _ss._get_client()
        ws = _ss._get_or_create_feedback_tab(client_sheet, sheet_id)
        cell_matches = ws.findall("ZZTOKTEST")
        for cell in sorted(cell_matches, key=lambda c: -c.row):
            ws.delete_rows(cell.row)
        ws2 = _ss._get_or_create_threads_tab(client_sheet, sheet_id)
        thread_matches = ws2.findall("ZZTOKTEST")
        for cell in sorted(thread_matches, key=lambda c: -c.row):
            ws2.delete_rows(cell.row)
        print(f"  (cleaned up {len(cell_matches)} feedback row(s) and {len(thread_matches)} thread ledger row(s))")
    except Exception as e:
        print(f"  (could not clean up ZZTOKTEST automatically: {e})")

    print()
    print("=== Logged out: no access at all ===")
    client.post("/api/logout")
    r = client.get("/api/dashboard-data")
    print(f"  [{'PASS' if r.status_code == 401 else 'FAIL'}] logged-out request rejected (status {r.status_code})")
    if r.status_code != 401:
        failures.append("logged-out user can still fetch data")

    print()
    if failures:
        print(f"RESULT: FAILED -> {failures}")
        return False
    print("RESULT: PASSED - role boundaries hold at the actual HTTP response level.")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
