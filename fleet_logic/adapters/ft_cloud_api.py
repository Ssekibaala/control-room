"""
Joins FT Cloud OpenAPI vehicles + device infos + last positions into
the canonical AssetReport shape (see fleet_logic/schema.py) - the same
shape ft_cloud_camera.py produces from the mailed zip, so downstream
code (classify_fleet, report_writer, ...) doesn't need to know or care
that this data came from a live API poll instead of a report
attachment.

Vehicles carry vehicleNumber (the plate, prefixed "GTL-" on this
tenant) and deviceList[].uniqueId; device infos carry the same
uniqueId plus updateTime; positions are keyed by uniqueId. So uniqueId
is the join key across all three, and a vehicle with no device attached
has no last-seen signal at all - see build_reports()'s skipped counter
for that case.
"""

import logging
from datetime import datetime
from schema import AssetReport, normalize_plate, utc_to_eat

logger = logging.getLogger(__name__)


def _parse_timestamp(ts):
    """FT returns RFC3339 with a literal Z (real UTC, confirmed against
    tenant-1144 data), converted to EAT here via utc_to_eat() - stays a
    naive datetime (classifier compares these against each other and
    against now_eat(), so a tz-aware value here would raise on
    subtraction), but now correctly EAT rather than 3 hours behind."""
    if not ts:
        return None
    try:
        return utc_to_eat(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _last_seen(info, webhook_last_seen=None):
    """
    When this device was last VERIFIABLY in contact with FT.

    webhook_last_seen, when given, is the timestamp of the last webhook
    event FT actually pushed us for this device (see
    ft_cloud_webhook.last_seen_by_unique_id) - a real report, not merely
    a connectivity flag - and it wins whenever present, however old.

    Otherwise, updateTime ("the time when the device last reported
    status", per FT's own field description) - not lastOnlineTime or
    lastOfflineTime. Those two are transition timestamps, not
    last-report timestamps: they flip which one is populated based on
    the device's CURRENT state, so a device that just went offline shows
    a lastOfflineTime of right now regardless of how stale its actual
    data is - RAI932W's own FT Cloud page still read "off-line" while
    this fallback made it look freshly seen, for the same reason
    lastOnlineTime made it look Online in the first place: neither field
    answers "when did this device last actually report", only "when did
    its connectivity state last change". updateTime does answer that -
    confirmed against real tenant-1144 data, where it lines up with the
    device's true last full report regardless of whether it's currently
    ONLINE or OFFLINE.
    """
    if webhook_last_seen is not None:
        return webhook_last_seen
    return _parse_timestamp(info.get("updateTime"))


def _status_note(vehicle, info):
    """Free-text note, same slot the mailed adapter filled with its
    "Offline Time" column - FT's own ONLINE/OFFLINE word plus, when
    offline, since when."""
    state = vehicle.get("onlineState") or ""
    offline_since = info.get("lastOfflineTime")
    if state == "OFFLINE" and offline_since:
        return f"OFFLINE since {offline_since}"
    return state or None


def _reported_offline(vehicle):
    """
    FT's own connectivity verdict for this vehicle (the same onlineState
    that drives the "off-line" badge on FT's Device List page), so the
    dashboard's Online/Offline can match FT's directly rather than
    re-derive it from last_report_time's age.

    This matters specifically for devices FT has JUST flagged offline:
    _last_seen()'s fallback for that case is lastOfflineTime, which is
    the transition moment itself and therefore always looks fresh - a
    device that went offline an hour ago would otherwise pass the
    staleness threshold and still show "Online" for up to
    OFFLINE_THRESHOLD_DAYS. Confirmed against RAI932W/Gezira: FT's own
    Device List already read "off-line" while this app still said
    Online, purely because "recently went offline" and "recently
    reported" produce the same fresh timestamp.

    Only returns True (a confirmed OFFLINE) or None - never False. FT
    reporting ONLINE is only a connectivity heartbeat, not proof of a
    real report, and must not suppress the opposite finding produced by
    webhook staleness (see _last_seen()'s webhook_last_seen branch,
    which exists for exactly that case).
    """
    return True if vehicle.get("onlineState") == "OFFLINE" else None


def build_reports(fleet_id, vehicles, device_infos, positions=None, webhook_last_seen=None):
    """
    fleet_id: the FT fleet these vehicles belong to.
    vehicles / device_infos / positions: raw values from FtCloudApiClient.
    webhook_last_seen: {uniqueId: datetime}, see
    ft_cloud_webhook.last_seen_by_unique_id - unlike positions, not
    freshness-filtered, since _last_seen() needs the true last-report
    time even when it's too stale for positions to have kept it.
    Returns a list of AssetReport, one per vehicle with a usable plate
    and an attached device.
    """
    positions = positions or {}
    webhook_last_seen = webhook_last_seen or {}
    info_by_uid = {i["uniqueId"]: i for i in device_infos if i.get("uniqueId")}
    reports = []
    skipped_no_plate = 0
    skipped_no_device = 0
    skipped_disabled = 0

    for v in vehicles:
        # FT's equivalent of MiX's UserState=Decommissioned (see
        # mix_api.is_decommissioned). Every GTL/AGL vehicle is currently
        # ENABLE so this filters nothing today - it's here so a vehicle
        # retired on FT later drops out on its own, the same way a
        # decommissioned MiX asset does, rather than silently padding
        # the fleet count until someone notices.
        if str(v.get("vehicleState", "")).strip().upper() not in ("", "ENABLE"):
            skipped_disabled += 1
            continue
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
        last_report_time = _last_seen(info, webhook_last_seen.get(unique_id))

        reports.append(AssetReport(
            asset_plate=plate,
            source_platform="FT Cloud Camera",
            report_type="camera_status",
            last_report_time=last_report_time,
            last_lat=gps.get("lat"),
            last_lon=gps.get("lng"),
            status_note=_status_note(v, info),
            reported_offline=_reported_offline(v),
            organisation_id=str(fleet_id),
            raw_row={"vehicle": v, "deviceInfo": info, "position": gps},
        ))

    if skipped_no_plate:
        logger.warning(f"FT Cloud fleet {fleet_id}: {skipped_no_plate} vehicle(s) had no usable plate, skipped")
    if skipped_no_device:
        logger.warning(f"FT Cloud fleet {fleet_id}: {skipped_no_device} vehicle(s) had no attached device, skipped")
    if skipped_disabled:
        logger.info(f"FT Cloud fleet {fleet_id}: {skipped_disabled} disabled vehicle(s) excluded")
    return reports


def fetch_all_reports(client, fleet_ids, fetch_positions=True, position_lookback_days=3,
                       preset_positions=None, webhook_last_seen=None):
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

    webhook_last_seen: {uniqueId: datetime}, passed straight through to
    build_reports() - see its docstring and _last_seen().
    """
    all_reports = []
    fetched = client.get_fleet_snapshot(
        fleet_ids, fetch_positions=fetch_positions, position_lookback_days=position_lookback_days)
    for fleet_id, data in fetched.items():
        positions = data["positions"] or {}
        if preset_positions:
            # Anything freshly fetched still wins; preset only fills gaps.
            positions = {**preset_positions, **positions}
        all_reports += build_reports(fleet_id, data["vehicles"], data["deviceInfos"], positions,
                                      webhook_last_seen=webhook_last_seen)
    return all_reports
