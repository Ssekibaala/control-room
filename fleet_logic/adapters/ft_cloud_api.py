"""
Joins FT Cloud OpenAPI vehicles + device infos + last positions into
the canonical AssetReport shape (see fleet_logic/schema.py) - the same
shape ft_cloud_camera.py produces from the mailed zip, so downstream
code (classify_fleet, report_writer, ...) doesn't need to know or care
that this data came from a live API poll instead of a report
attachment.

Vehicles carry vehicleNumber (the plate, prefixed "GTL-" on this
tenant) and deviceList[].uniqueId; device infos carry the same
uniqueId plus lastOnlineTime/lastOfflineTime; positions are keyed by
uniqueId. So uniqueId is the join key across all three, and a vehicle
with no device attached has no last-seen signal at all - see
build_reports()'s skipped counter for that case.
"""

import logging
from datetime import datetime
from schema import AssetReport, normalize_plate

logger = logging.getLogger(__name__)


def _parse_timestamp(ts):
    """FT returns RFC3339 with a literal Z. Parsed to a naive UTC
    datetime to match every other adapter in this codebase - classifier
    compares these against each other and against datetime.now(), so a
    tz-aware value here would raise on subtraction."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def _last_seen(info):
    """
    When this device was last in contact with FT. Confirmed against
    real tenant-1144 data that lastOnlineTime and lastOfflineTime are
    mutually exclusive, never both: a connected device carries
    lastOnlineTime and a null lastOfflineTime, a disconnected one the
    reverse. So neither field alone answers "when was this last seen"
    for the whole fleet - reading only lastOnlineTime silently loses
    the timestamp for every offline vehicle, which is precisely the
    set this dashboard exists to report on. Together they are the same
    pair the mailed report exposed as its "Last Online Time" and
    "Offline Time" columns.

    updateTime is the last resort for the handful of devices with
    neither (never-connected or freshly-registered units); it is when
    FT last touched the record at all, which is weaker but still
    better than reporting No Data.
    """
    return (_parse_timestamp(info.get("lastOnlineTime"))
            or _parse_timestamp(info.get("lastOfflineTime"))
            or _parse_timestamp(info.get("updateTime")))


def _status_note(vehicle, info):
    """Free-text note, same slot the mailed adapter filled with its
    "Offline Time" column - FT's own ONLINE/OFFLINE word plus, when
    offline, since when."""
    state = vehicle.get("onlineState") or ""
    offline_since = info.get("lastOfflineTime")
    if state == "OFFLINE" and offline_since:
        return f"OFFLINE since {offline_since}"
    return state or None


def build_reports(fleet_id, vehicles, device_infos, positions=None):
    """
    fleet_id: the FT fleet these vehicles belong to.
    vehicles / device_infos / positions: raw values from FtCloudApiClient.
    Returns a list of AssetReport, one per vehicle with a usable plate
    and an attached device.
    """
    positions = positions or {}
    info_by_uid = {i["uniqueId"]: i for i in device_infos if i.get("uniqueId")}
    reports = []
    skipped_no_plate = 0
    skipped_no_device = 0

    for v in vehicles:
        plate = normalize_plate(v.get("vehicleNumber", ""))
        if not plate:
            skipped_no_plate += 1
            continue
        unique_id = next(
            (d.get("uniqueId") for d in (v.get("deviceList") or []) if d.get("uniqueId")), None)
        if unique_id is None:
            skipped_no_device += 1
            continue

        info = info_by_uid.get(unique_id, {})
        gps = positions.get(unique_id) or {}
        last_report_time = _last_seen(info)

        reports.append(AssetReport(
            asset_plate=plate,
            source_platform="FT Cloud Camera",
            report_type="camera_status",
            last_report_time=last_report_time,
            last_lat=gps.get("lat"),
            last_lon=gps.get("lng"),
            status_note=_status_note(v, info),
            organisation_id=str(fleet_id),
            raw_row={"vehicle": v, "deviceInfo": info, "position": gps},
        ))

    if skipped_no_plate:
        logger.warning(f"FT Cloud fleet {fleet_id}: {skipped_no_plate} vehicle(s) had no usable plate, skipped")
    if skipped_no_device:
        logger.warning(f"FT Cloud fleet {fleet_id}: {skipped_no_device} vehicle(s) had no attached device, skipped")
    return reports


def fetch_all_reports(client, fleet_ids, fetch_positions=True, position_lookback_days=3,
                       preset_positions=None):
    """
    client: an FtCloudApiClient. fleet_ids: list of FT fleet ids to poll.
    Returns a flat list of AssetReport across every fleet, each still
    tagged with organisation_id so downstream code can sort/group
    per-fleet even after everything's been appended together.

    preset_positions is the webhook path: {uniqueId: {lat, lng, ...}}
    already pushed to us by FT, supplied by the caller instead of
    being fetched. When given, fetch_positions should be False - that
    combination is what removes the one-call-per-device trips loop
    entirely. The two sources share a shape deliberately, so which one
    is in play changes nothing below this line.
    """
    all_reports = []
    fetched = client.get_fleet_snapshot(
        fleet_ids, fetch_positions=fetch_positions, position_lookback_days=position_lookback_days)
    for fleet_id, data in fetched.items():
        positions = data["positions"] or {}
        if preset_positions:
            # Anything freshly fetched still wins; preset only fills gaps.
            positions = {**preset_positions, **positions}
        all_reports += build_reports(fleet_id, data["vehicles"], data["deviceInfos"], positions)
    return all_reports
