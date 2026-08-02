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
from schema import AssetReport, normalize_plate, utc_to_eat

logger = logging.getLogger(__name__)


def _parse_timestamp(ts):
    """MiX's Timestamp field is UTC (the 'Z' suffix) - converted to EAT
    here, at the one place this raw value enters the app, so every
    AssetReport.last_report_time in the whole system is EAT regardless
    of source (see schema.py's utc_to_eat() docstring)."""
    if not ts:
        return None
    try:
        return utc_to_eat(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def _asset_plate(asset):
    return normalize_plate(
        asset.get("RegistrationNumber") or asset.get("Description") or asset.get("FleetNumber") or ""
    )


DECOMMISSIONED_STATE = "Decommissioned"


def is_decommissioned(asset):
    """
    MiX marks retired assets with UserState="Decommissioned" on the asset
    record itself. Verified against real data that this is exactly the
    line the client draws: GTL 191 assets -> 128 active (63 decommissioned)
    and AGL 109 -> 103 active (6 decommissioned), both matching the counts
    the client reports independently.

    Note this is per-ASSET, not per-site, even though decommissioned
    assets do cluster into a retirement site (GTL's site
    6856334112108248987 holds 50 assets, all of them decommissioned).
    Filtering by that site would have missed the other 13 spread across
    five still-active sites, so the asset's own state is the reliable
    signal and the site clustering is just a convention on top of it.
    """
    return str(asset.get("UserState", "")).strip().lower() == DECOMMISSIONED_STATE.lower()


def _plates(assets, decommissioned):
    return {normalize_plate(a.get("RegistrationNumber") or a.get("Description") or "")
            for a in assets if is_decommissioned(a) is decommissioned} - {""}


def decommissioned_plates(assets):
    """
    Plates of vehicles that are RETIRED - meaning every asset record
    bearing that plate is decommissioned.

    Needed as a separate, exported list because dropping decommissioned
    assets from THIS adapter's own output isn't enough to keep them off
    the dashboard: the mailed-report adapters (teletrac_csv,
    mix_movement, ft_cloud_camera...) know nothing about MiX's
    UserState, so a retired vehicle still sitting in yesterday's CSV
    walks straight back in through that door. Confirmed live - 8
    decommissioned GTL assets reappeared exactly this way.

    Subtracting the active plates is the essential part, not a
    refinement. A plate routinely has BOTH a decommissioned record and
    an active one, because replacing a truck's tracker retires the old
    asset and creates a new one under the same registration - 30 of
    GTL's 128 active vehicles look like this. Excluding on "has any
    decommissioned record" therefore deletes live trucks: it cut GTL
    from 128 to 99 before this subtraction existed. A vehicle is only
    genuinely retired when nothing active still carries its plate.
    """
    return _plates(assets, True) - _plates(assets, False)


def build_reports(org_id, assets, positions):
    """
    org_id: the organisation these assets/positions belong to.
    assets / positions: raw lists from MixApiClient.
    Returns a list of AssetReport, one per position whose AssetId
    matched a known, non-decommissioned asset in this org.
    """
    assets_by_id = {a["AssetId"]: a for a in assets if not is_decommissioned(a)}
    decommissioned_ids = {a["AssetId"] for a in assets if is_decommissioned(a)}
    reports = []
    skipped = 0
    decommissioned = 0
    for pos in positions:
        asset = assets_by_id.get(pos.get("AssetId"))
        if asset is None:
            # Either genuinely unknown, or filtered out above as
            # decommissioned - separated so the log doesn't report a
            # routine retirement as a data-quality problem.
            if pos.get("AssetId") in decommissioned_ids:
                decommissioned += 1
            else:
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
    if decommissioned:
        logger.info(f"MiX API org {org_id}: {decommissioned} decommissioned asset(s) excluded")
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


def fetch_all_reports_and_decommissioned(client, org_ids):
    """
    Same as fetch_all_reports(), but also returns the union of every
    org's decommissioned plates so the caller can exclude them from
    OTHER platforms' rows too (see decommissioned_plates()).
    Returns (reports, decommissioned_plates_set).
    """
    all_reports = []
    retired, active = set(), set()
    fetched = client.get_assets_and_positions(org_ids)
    for org_id, data in fetched.items():
        all_reports += build_reports(org_id, data["assets"], data["positions"])
        retired |= _plates(data["assets"], True)
        active |= _plates(data["assets"], False)
    # Subtract active across ALL orgs, not per-org: a vehicle can be
    # retired under one organisation and active under another (moved
    # between depots/entities), and it's still on the road, so one
    # org's retirement record must not delete it from the other's.
    return all_reports, retired - active
