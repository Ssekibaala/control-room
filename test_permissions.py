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


def run():
    client = app.test_client()
    failures = []

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
