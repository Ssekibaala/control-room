"""
Proves per-client data isolation at the only place that matters: the
actual bytes of the HTTP response.

The check is deliberately not "does the row list look right" but "does
another client's plate appear ANYWHERE in the serialized payload".
That distinction caught a real leak while this was being written: the
asset rows were correctly scoped, but the tampering sections
(tamperConfirmed/tamperUnconfirmed/qualityLog/tamperChecked) identify
vehicles by plate and carry no client field, so they were still handing
an unassigned technician 73 GTL plates. A row-shaped assertion would
have passed.

Run: python test_client_isolation.py
Needs data/fleet_today.json to contain rows for at least two clients.
"""

import os
import json

# Must be set before app is imported - importing it starts the live API
# pollers otherwise, which this test has no need for.
for _var in ("DISABLE_MIX_API_POLLER", "DISABLE_TELETRAC_API_POLLER", "DISABLE_FT_CLOUD_API_POLLER"):
    os.environ.setdefault(_var, "true")

import app as A  # noqa: E402
import permissions  # noqa: E402

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "fleet_today.json")


def _plates_by_client():
    with open(DATA_PATH) as f:
        data = json.load(f)
    out = {}
    for row in data.get("full", []):
        client = str(row.get("client", "")).strip()
        if client and row.get("plate"):
            out.setdefault(client, set()).add(row["plate"])
    return out


def _payload_for(role, assigned_clients):
    """One real request through the real route, with the Sheets-backed
    client lookup stubbed so the test needs no live accounts."""
    original = A._assigned_clients
    A._assigned_clients = lambda _username: assigned_clients
    A._user_clients_cache.clear()
    try:
        with A.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["username"] = "isolation-test"
                sess["role"] = role
            return client.get("/api/dashboard-data").get_json()
    finally:
        A._assigned_clients = original
        A._user_clients_cache.clear()


def _plates_present(payload, plates):
    """How many of `plates` appear anywhere in the serialized response."""
    raw = json.dumps(payload)
    return sum(1 for p in plates if f'"{p}"' in raw)


def run():
    A.app.config["TESTING"] = True
    by_client = _plates_by_client()
    if len(by_client) < 2:
        print(f"SKIPPED - need two clients' data in {DATA_PATH}, found: {sorted(by_client)}")
        return True

    names = sorted(by_client)
    first, second = names[0], names[1]
    failures = []

    print(f"=== Client isolation ({first} vs {second}) ===")

    cases = [
        ("admin", [], {first, second}, "admin sees every client"),
        ("technician", [first], {first}, f"technician assigned {first}"),
        ("client", [second], {second}, f"client assigned {second}"),
        ("technician", [], set(), "technician with no assignment sees nothing"),
    ]

    for role, assigned, should_see, label in cases:
        payload = _payload_for(role, assigned)
        for name in names:
            found = _plates_present(payload, by_client[name])
            expected_any = name in should_see
            ok = (found > 0) if expected_any else (found == 0)
            verdict = "PASS" if ok else "FAIL"
            print(f"  [{verdict}] {label}: {name} plates in response = {found} "
                  f"({'expected' if expected_any else 'expected none'})")
            if not ok:
                failures.append(f"{label}: {name} plates present={found}, should_see={expected_any}")

        advertised = set(payload.get("meta", {}).get("clients") or [])
        if advertised != should_see:
            print(f"  [FAIL] {label}: meta.clients={sorted(advertised)}, expected {sorted(should_see)}")
            failures.append(f"{label}: meta.clients mismatch")
        else:
            print(f"  [PASS] {label}: meta.clients={sorted(advertised)}")

    print()
    print("=== Nobody can grant access they don't hold ===")
    grant_cases = [
        ("technician", [first], [second], False, f"technician with {first} granting {second}"),
        ("technician", [first], [first], True, f"technician with {first} granting {first}"),
        ("admin", [], [first, second], True, "admin granting anything"),
    ]
    for role, held, requested, should_allow, label in grant_cases:
        ok, disallowed = permissions.can_grant_clients(role, held, requested)
        verdict = "PASS" if ok is should_allow else "FAIL"
        print(f"  [{verdict}] {label}: allowed={ok} refused={disallowed}")
        if ok is not should_allow:
            failures.append(label)

    print()
    print("=== An empty assignment means nothing, never everything ===")
    if permissions.visible_clients("client", []) != set():
        failures.append("empty assignment did not resolve to an empty client set")
        print("  [FAIL] empty assignment should resolve to no clients")
    else:
        print("  [PASS] empty assignment resolves to no clients")

    print()
    if failures:
        print(f"RESULT: FAILED -> {failures}")
        return False
    print("RESULT: PASSED - client isolation holds in the actual HTTP response bytes.")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
