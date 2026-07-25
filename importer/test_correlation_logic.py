"""
The real sample CSVs on disk cover different months (movement = June,
events = July), so they can never produce a correlated match, that's
a data problem, not a code problem. This test builds a small,
controlled CSV pair with known, overlapping timestamps to prove the
power-event correlation logic itself is correct in isolation.
"""

import csv
import os
import tempfile
from tamper_engine import analyse

MOVEMENT_HEADERS = [
    "AssetID", "AssetExtra", "FleetNumber", "DepartureDate", "DepartureTime",
    "ArrivalDate", "ArrivalTime", "StartLatLong", "EndLatLong",
    "DepartFrom", "ArriveAt", "StartOdoMeter", "EndOdoMeter",
]
EVENT_HEADERS = ["AssetID", "EventDescription", "EventStartDate", "EventStartTime"]


def write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run():
    tmp = tempfile.mkdtemp()
    movement_path = os.path.join(tmp, "movement.csv")
    event_path = os.path.join(tmp, "event.csv")

    # Case A: CONFIRMED (Power Cycle) - vehicle "999" arrives at point X,
    # a Disconnect AND Reconnect both fall inside the gap, then the next
    # trip departs 300km away. Should land in confirmed_power_cycle.
    #
    # Case B: CONFIRMED (Disconnect, No Reconnect) - vehicle "888" gets a
    # Disconnect in the gap but no Reconnect. Should land in confirmed_no_reconnect.
    #
    # Case C: UNCONFIRMED - vehicle "777" has the same size location gap,
    # but no power event at all in the window. Should land in unconfirmed.
    #
    # Case D: NOT FLAGGED - vehicle "666" has a gap under 2km, should not
    # appear in mismatches at all (below threshold).
    movement_rows = [
        {"AssetID": "999", "AssetExtra": "TRUCK-999", "FleetNumber": "F999",
         "DepartureDate": "01/01/2026", "DepartureTime": "08:00:00",
         "ArrivalDate": "01/01/2026", "ArrivalTime": "10:00:00",
         "StartLatLong": "0.0 / 0.1", "EndLatLong": "0.30000 / 32.50000",
         "DepartFrom": "Depot A", "ArriveAt": "Point X", "StartOdoMeter": "1000", "EndOdoMeter": "1050"},
        {"AssetID": "999", "AssetExtra": "TRUCK-999", "FleetNumber": "F999",
         "DepartureDate": "01/01/2026", "DepartureTime": "18:00:00",
         "ArrivalDate": "01/01/2026", "ArrivalTime": "20:00:00",
         "StartLatLong": "2.90000 / 34.50000", "EndLatLong": "3.0 / 34.6",
         "DepartFrom": "Point Y (300km away)", "ArriveAt": "Point Z", "StartOdoMeter": "1050", "EndOdoMeter": "1080"},

        {"AssetID": "888", "AssetExtra": "TRUCK-888", "FleetNumber": "F888",
         "DepartureDate": "01/01/2026", "DepartureTime": "08:00:00",
         "ArrivalDate": "01/01/2026", "ArrivalTime": "10:00:00",
         "StartLatLong": "0.0 / 0.1", "EndLatLong": "0.30000 / 32.50000",
         "DepartFrom": "Depot A", "ArriveAt": "Point X", "StartOdoMeter": "1000", "EndOdoMeter": "1050"},
        {"AssetID": "888", "AssetExtra": "TRUCK-888", "FleetNumber": "F888",
         "DepartureDate": "01/01/2026", "DepartureTime": "18:00:00",
         "ArrivalDate": "01/01/2026", "ArrivalTime": "20:00:00",
         "StartLatLong": "2.90000 / 34.50000", "EndLatLong": "3.0 / 34.6",
         "DepartFrom": "Point Y (300km away)", "ArriveAt": "Point Z", "StartOdoMeter": "1050", "EndOdoMeter": "1080"},

        {"AssetID": "777", "AssetExtra": "TRUCK-777", "FleetNumber": "F777",
         "DepartureDate": "01/01/2026", "DepartureTime": "08:00:00",
         "ArrivalDate": "01/01/2026", "ArrivalTime": "10:00:00",
         "StartLatLong": "0.0 / 0.1", "EndLatLong": "0.30000 / 32.50000",
         "DepartFrom": "Depot A", "ArriveAt": "Point X", "StartOdoMeter": "1000", "EndOdoMeter": "1050"},
        {"AssetID": "777", "AssetExtra": "TRUCK-777", "FleetNumber": "F777",
         "DepartureDate": "01/01/2026", "DepartureTime": "18:00:00",
         "ArrivalDate": "01/01/2026", "ArrivalTime": "20:00:00",
         "StartLatLong": "2.90000 / 34.50000", "EndLatLong": "3.0 / 34.6",
         "DepartFrom": "Point Y (300km away)", "ArriveAt": "Point Z", "StartOdoMeter": "1050", "EndOdoMeter": "1080"},

        {"AssetID": "666", "AssetExtra": "TRUCK-666", "FleetNumber": "F666",
         "DepartureDate": "01/01/2026", "DepartureTime": "08:00:00",
         "ArrivalDate": "01/01/2026", "ArrivalTime": "10:00:00",
         "StartLatLong": "0.0 / 0.1", "EndLatLong": "0.30000 / 32.50000",
         "DepartFrom": "Depot A", "ArriveAt": "Point X", "StartOdoMeter": "1000", "EndOdoMeter": "1050"},
        {"AssetID": "666", "AssetExtra": "TRUCK-666", "FleetNumber": "F666",
         "DepartureDate": "01/01/2026", "DepartureTime": "18:00:00",
         "ArrivalDate": "01/01/2026", "ArrivalTime": "20:00:00",
         "StartLatLong": "0.30010 / 32.50010", "EndLatLong": "0.31 / 32.51",
         "DepartFrom": "Point X + 20m", "ArriveAt": "Point Z", "StartOdoMeter": "1050", "EndOdoMeter": "1080"},
    ]

    event_rows = [
        {"AssetID": "999", "EventDescription": "Power Disconnect", "EventStartDate": "01/01/2026", "EventStartTime": "10:30:00"},
        {"AssetID": "999", "EventDescription": "Power Reconnect", "EventStartDate": "01/01/2026", "EventStartTime": "17:00:00"},
        {"AssetID": "888", "EventDescription": "Power Disconnect", "EventStartDate": "01/01/2026", "EventStartTime": "10:30:00"},
        # 777 gets NO events at all
    ]

    write_csv(movement_path, MOVEMENT_HEADERS, movement_rows)
    write_csv(event_path, EVENT_HEADERS, event_rows)

    result = analyse(movement_path, event_path)

    confirmed_assets = {g["AssetID"] for g in result["confirmed"]}
    power_cycle_assets = {g["AssetID"] for g in result["confirmed_power_cycle"]}
    no_reconnect_assets = {g["AssetID"] for g in result["confirmed_no_reconnect"]}
    unconfirmed_assets = {g["AssetID"] for g in result["unconfirmed"]}
    mismatch_assets = {g["AssetID"] for g in result["mismatches"]}

    checks = [
        ("999 in confirmed_power_cycle", "999" in power_cycle_assets),
        ("888 in confirmed_no_reconnect", "888" in no_reconnect_assets),
        ("777 in unconfirmed", "777" in unconfirmed_assets),
        ("666 NOT in mismatches (gap too small)", "666" not in mismatch_assets),
        ("999 NOT in unconfirmed", "999" not in unconfirmed_assets),
        ("888 NOT in unconfirmed", "888" not in unconfirmed_assets),
    ]

    print("Synthetic correlation-logic test:")
    all_pass = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        if not passed:
            all_pass = False

    print()
    print("RESULT:", "PASSED - correlation logic is correct" if all_pass else "FAILED")
    return all_pass


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
