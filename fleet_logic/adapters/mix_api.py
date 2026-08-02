"""
Joins MiX Integrate API assets + latest positions into the canonical
AssetReport shape (see fleet_logic/schema.py) - the same shape every
mailed-report adapter produces, so downstream code (classify_fleet,
report_writer, ...) doesn't need to know or care that this data came
from a live API poll instead of a report attachment.

Positions carry AssetId + Latitude/Longitude/Timestamp; Assets carry
the same AssetId plus RegistrationNumber (the plate) and FleetNumber.
Confirmed against real data (orgs 8221725616980128168 and
750564533632365070) that every position's AssetId is present in that
org's own asset list, so the join is a plain dict lookup, never a
fuzzy match - see build_reports()'s skipped-position warning for the
case where that ever isn't true (stale/removed asset, race between
the two calls, etc).
"""

import logging
from datetime import datetime
from schema import AssetReport, normalize_plate

logger = logging.getLogger(__name__)


def _parse_timestamp(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _asset_plate(asset):
    return normalize_plate(
        asset.get("RegistrationNumber") or asset.get("Description") or asset.get("FleetNumber") or ""
    )


def build_reports(org_id, assets, positions):
    """
    org_id: the organisation these assets/positions belong to.
    assets / positions: raw lists from MixApiClient.
    Returns a list of AssetReport, one per position whose AssetId
    matched a known asset in this org.
    """
    assets_by_id = {a["AssetId"]: a for a in assets}
    reports = []
    skipped = 0
    for pos in positions:
        asset = assets_by_id.get(pos.get("AssetId"))
        if asset is None:
            skipped += 1
            continue
        plate = _asset_plate(asset)
        if not plate:
            continue
        reports.append(AssetReport(
            asset_plate=plate,
            source_platform="MiX API",
            report_type="position_api",
            last_report_time=_parse_timestamp(pos.get("Timestamp")),
            last_lat=pos.get("Latitude"),
            last_lon=pos.get("Longitude"),
            last_location_text=pos.get("FormattedAddress"),
            organisation_id=str(org_id),
            raw_row={"asset": asset, "position": pos},
        ))
    if skipped:
        logger.warning(f"MiX API org {org_id}: {skipped} position(s) had no matching asset, skipped")
    return reports


def fetch_all_reports(client, org_ids):
    """
    client: a MixApiClient. org_ids: list of organisation ids to poll.
    Returns a flat list of AssetReport across every organisation, each
    still tagged with organisation_id so downstream code can sort/group
    per-org even after everything's been appended together.
    """
    all_reports = []
    fetched = client.get_assets_and_positions(org_ids)
    for org_id, data in fetched.items():
        all_reports += build_reports(org_id, data["assets"], data["positions"])
    return all_reports
