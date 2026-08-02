"""
Turns Teletrac Integrate API "current data" rows into the canonical
AssetReport shape (see fleet_logic/schema.py) - the same shape
teletrac_csv.py produces from the mailed report, so downstream code
(classify_fleet, report_writer, ...) doesn't need to know or care that
this data came from a live API poll instead of a report attachment.

Unlike MiX, Teletrac's GetDevicesCurrentData already returns
everything needed per device in one call (DeviceName, ImeiNumber,
Lat/Lon, Location, GPSDateTime) - no separate assets-list join
required.
"""

import logging
from datetime import datetime
from schema import AssetReport, normalize_plate

logger = logging.getLogger(__name__)


def _parse_timestamp(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _parse_float(v):
    """Lat/Lon come back from GetDevicesCurrentData as strings, not
    numbers - confirmed against real GTL data (client RGYn7A==)."""
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def build_reports(client_id, devices):
    """
    client_id: the Teletrac client these devices belong to.
    devices: raw list from TeletracApiClient.get_devices_current_data().
    Returns a list of AssetReport, one per device with a usable plate.
    """
    reports = []
    skipped = 0
    for d in devices:
        plate = normalize_plate(d.get("DeviceName", ""))
        if not plate:
            skipped += 1
            continue
        reports.append(AssetReport(
            asset_plate=plate,
            source_platform="Teletrac",
            report_type="position_api",
            last_report_time=_parse_timestamp(d.get("GPSDateTime")),
            last_lat=_parse_float(d.get("Lat")),
            last_lon=_parse_float(d.get("Lon")),
            last_location_text=d.get("Location"),
            imei=str(d["ImeiNumber"]) if d.get("ImeiNumber") is not None else None,
            organisation_id=str(client_id),
            raw_row=d,
        ))
    if skipped:
        logger.warning(f"Teletrac API client {client_id}: {skipped} device(s) had no usable name/plate, skipped")
    return reports


def fetch_all_reports(client, client_ids):
    """
    client: a TeletracApiClient. client_ids: list of Teletrac client ids to poll.
    Returns a flat list of AssetReport across every client, each still
    tagged with organisation_id so downstream code can sort/group
    per-client even after everything's been appended together.
    """
    all_reports = []
    fetched = client.get_devices_current_data_for_clients(client_ids)
    for client_id, devices in fetched.items():
        all_reports += build_reports(client_id, devices)
    return all_reports
