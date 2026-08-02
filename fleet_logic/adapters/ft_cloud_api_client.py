"""
Thin client for the FT Cloud (FTCloud OpenAPI v3) camera platform.
This is the API-based replacement for the mailed "Offline Vehicle
Reports" zip the ft_cloud_camera adapter reads: instead of a daily
xlsx attachment, this polls the fleet's vehicles and their devices
directly on a timer.

Auth is unlike the other two platforms in this codebase: there is no
token exchange. FT issues a long-lived signature string per tenant and
every request carries it as the _sign header alongside the numeric
_tenantid. So there is no refresh logic here - just two headers.

Three behaviours below were confirmed by trial against the real API
(tenant 1144), not documented assumptions:
  - af- and eur- regional hosts both serve the SAME tenant and return
    the same fleets, so the region choice is about latency, not data.
  - the vehicle list is where plates live (vehicleNumber); the device
    list has no plate at all, only uniqueId - so the offline report's
    "plate + last seen" pairing needs BOTH calls joined on uniqueId.
  - there is no current-position endpoint. The freshest known
    coordinate is the endGpsInfo of a device's most recent trip, which
    is why get_last_positions() exists and why it is one call per
    device rather than one call per fleet.
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta, timezone

BASE_URL = "https://af-ftcloud.ifleetvision.com:20501/openapi"

logger = logging.getLogger(__name__)


class FtCloudApiError(Exception):
    pass


class FtCloudApiClient:
    def __init__(self, sign=None, tenant_id=None, base_url=None, inter_call_delay_seconds=None):
        self.sign = sign or os.environ.get("FT_CLOUD_API_SIGN")
        self.tenant_id = tenant_id or os.environ.get("FT_CLOUD_TENANT_ID")
        self.base_url = (base_url or os.environ.get("FT_CLOUD_BASE_URL") or BASE_URL).rstrip("/")
        self.inter_call_delay_seconds = (
            inter_call_delay_seconds if inter_call_delay_seconds is not None
            else float(os.environ.get("FT_CLOUD_INTER_CALL_DELAY_SECONDS", "0.2"))
        )

    def is_configured(self):
        return all([self.sign, self.tenant_id])

    def _headers(self):
        if not self.is_configured():
            raise FtCloudApiError("FT_CLOUD_API_SIGN/FT_CLOUD_TENANT_ID not fully set")
        return {"_sign": self.sign, "_tenantid": str(self.tenant_id), "Content-Type": "application/json"}

    def _paged(self, path, params, page_size=200):
        """Walks every page of a PageRes endpoint. Stops on a short page
        rather than trusting a total count, which these endpoints don't
        return."""
        results = []
        page = 1
        while True:
            q = dict(params, page=page, pageSize=page_size)
            resp = requests.get(f"{self.base_url}{path}", headers=self._headers(), params=q, timeout=60)
            resp.raise_for_status()
            batch = resp.json().get("data") or []
            results += batch
            if len(batch) < page_size:
                return results
            page += 1
            time.sleep(self.inter_call_delay_seconds)

    def get_fleets(self):
        """Every fleet visible to this tenant. Used to look up the GTL
        fleetId for settings.ini, not part of the polling path."""
        return self._paged("/v2/fleets", {})

    def get_vehicles(self, fleet_id):
        """Vehicles for one fleet - this is the only place plates
        (vehicleNumber) and the plate->uniqueId link exist."""
        return self._paged("/v2/vehicles", {"fleetIds": fleet_id})

    def get_device_infos(self, unique_ids):
        """lastOnlineTime/lastOfflineTime per device. The endpoint caps
        uniqueIds at 50 per call, so this chunks and concatenates."""
        infos = []
        for i in range(0, len(unique_ids), 50):
            chunk = unique_ids[i:i + 50]
            if i > 0:
                time.sleep(self.inter_call_delay_seconds)
            resp = requests.post(f"{self.base_url}/v2/devices/infos", headers=self._headers(),
                                  json={"uniqueIds": ",".join(chunk)}, timeout=60)
            resp.raise_for_status()
            infos += resp.json().get("data") or []
        return infos

    def get_last_position(self, unique_id, lookback_days=3):
        """
        Freshest known coordinate for one device, taken from the
        endGpsInfo of its most recent trip - FT exposes no
        current-position endpoint, so this is the closest equivalent.
        Returns None when the device has not moved within the lookback
        window (a long-parked or long-offline vehicle), which the
        caller treats as "no coordinate", not as an error.
        """
        now = datetime.now(timezone.utc)
        params = {
            "uniqueId": unique_id,
            "startTime": (now - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        resp = requests.get(f"{self.base_url}/v2/trips", headers=self._headers(), params=params, timeout=60)
        resp.raise_for_status()
        trips = resp.json().get("data") or []
        if not trips:
            return None
        return (trips[-1].get("endGpsInfo") or {}) or None

    def get_last_positions(self, unique_ids, lookback_days=3):
        """
        One trips call per device, spaced by inter_call_delay_seconds -
        the endpoint is documented at 10 requests/second and this is
        the only per-device call in the poll, so it dominates the
        cycle's wall time. A failure on one device is logged and
        omitted rather than aborting every other device's lookup.
        Returns {unique_id: {"lat":, "lng":, "time":}}.
        """
        positions = {}
        for i, unique_id in enumerate(unique_ids):
            if i > 0:
                time.sleep(self.inter_call_delay_seconds)
            try:
                gps = self.get_last_position(unique_id, lookback_days=lookback_days)
            except requests.exceptions.RequestException as e:
                logger.error(f"FT Cloud trips lookup failed for {unique_id}: {e}")
                continue
            if gps:
                positions[unique_id] = gps
        return positions

    # ---- Webhook subscription management ------------------------------
    # FT pushes GPS rather than exposing a bulk position endpoint, so
    # these three are what turn the expensive per-device trips loop
    # into a push stream. See fleet_logic/adapters/ft_cloud_webhook.py
    # for the receiving half.

    def subscribe_webhook(self, message_type, callback_url, unique_ids):
        """
        POST /v2/webhook. unique_ids is REQUIRED here even though FT
        treats it as optional: the documented default is "subscribe to
        all devices", and this credential is a Teletrac reseller tenant
        covering 11 client companies - omitting it would fire every
        other company's vehicle positions at GTL's endpoint. Passing an
        empty list is therefore refused rather than silently sent.
        """
        if not unique_ids:
            raise FtCloudApiError(
                "refusing to subscribe with an empty uniqueIds list - FT reads that as "
                "'every device on the tenant', which on this reseller account means "
                "10 other companies' vehicles")
        body = {"type": message_type, "callbackUrl": callback_url, "uniqueIds": ",".join(unique_ids)}
        resp = requests.post(f"{self.base_url}/v2/webhook", headers=self._headers(), json=body, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def unsubscribe_webhook(self, message_type):
        """DELETE /v2/webhook. message_type may be "ALL"."""
        resp = requests.delete(f"{self.base_url}/v2/webhook", headers=self._headers(),
                                json={"type": message_type}, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def get_webhook_subscription(self, message_type):
        """
        Which devices are subscribed for one type. FT answers a plain
        400 ("Message type has not been subscribed") rather than an
        empty result when nothing is subscribed, so that specific case
        is translated into a normal "not subscribed" answer instead of
        being raised as an error.
        """
        resp = requests.get(f"{self.base_url}/v2/webhook/subscribe-type/{message_type}",
                             headers=self._headers(), timeout=60)
        if resp.status_code == 400 and "not been subscribed" in resp.text:
            return {"subscribed": False, "uniqueIds": ""}
        resp.raise_for_status()
        return {"subscribed": True, **(resp.json().get("data") or {})}

    def get_fleet_unique_ids(self, fleet_ids):
        """Every device serial in the given fleets - the scoping list
        for subscribe_webhook()."""
        unique_ids = set()
        for fleet_id in fleet_ids:
            for v in self.get_vehicles(fleet_id):
                for d in (v.get("deviceList") or []):
                    if d.get("uniqueId"):
                        unique_ids.add(d["uniqueId"])
        return sorted(unique_ids)

    def get_fleet_snapshot(self, fleet_ids, fetch_positions=True, position_lookback_days=3):
        """
        Full per-fleet fetch: vehicles (plates), then their devices'
        last-online times, then optionally last known coordinates.
        A failure on one fleet is logged and yields empty lists for
        that fleet rather than aborting the others - same pattern as
        the MiX and Teletrac clients.
        Returns {fleet_id: {"vehicles": [...], "deviceInfos": [...], "positions": {...}}}.
        """
        result = {}
        for i, fleet_id in enumerate(fleet_ids):
            if i > 0:
                time.sleep(self.inter_call_delay_seconds)
            try:
                vehicles = self.get_vehicles(fleet_id)
            except requests.exceptions.RequestException as e:
                logger.error(f"FT Cloud vehicles fetch failed for fleet {fleet_id}: {e}")
                result[fleet_id] = {"vehicles": [], "deviceInfos": [], "positions": {}}
                continue

            unique_ids = sorted({
                d.get("uniqueId") for v in vehicles for d in (v.get("deviceList") or []) if d.get("uniqueId")
            })
            try:
                device_infos = self.get_device_infos(unique_ids)
            except requests.exceptions.RequestException as e:
                logger.error(f"FT Cloud device-info fetch failed for fleet {fleet_id}: {e}")
                device_infos = []

            positions = {}
            if fetch_positions and unique_ids:
                positions = self.get_last_positions(unique_ids, lookback_days=position_lookback_days)

            result[fleet_id] = {"vehicles": vehicles, "deviceInfos": device_infos, "positions": positions}
        return result
