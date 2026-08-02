"""
Thin client for Teletrac's RESTful API v3 (OAuth2 password grant +
REST, base URL https://api.gpsiot.net). This is the API-based
replacement for the mailed "Bulk - Teletrac platform offline report"
CSV the other adapter (teletrac_csv.py) reads: instead of a daily .csv
attachment, this polls /api/Asset/GetDevicesCurrentData directly on a
timer, so location/status data is only ever poll-interval-old rather
than a day old.

The grant is a "password" grant, but the username/password are
Teletrac's Web API Key / Web Secret Key (issued per account by
Teletrac support), not a real user's login - same shape as MiX's
client-credentials-flavoured password grant in mix_api_client.py.
"""

import os
import time
import logging
import requests

TOKEN_URL = "https://api.gpsiot.net/token"
BASE_URL = "https://api.gpsiot.net/api"

logger = logging.getLogger(__name__)


class TeletracApiError(Exception):
    pass


class TeletracApiClient:
    def __init__(self, api_key=None, api_secret=None, inter_client_delay_seconds=None):
        self.api_key = api_key or os.environ.get("TELETRAC_API_KEY")
        self.api_secret = api_secret or os.environ.get("TELETRAC_API_SECRET")
        self.inter_client_delay_seconds = (
            inter_client_delay_seconds if inter_client_delay_seconds is not None
            else float(os.environ.get("TELETRAC_INTER_CLIENT_DELAY_SECONDS", "5"))
        )
        self._token = None
        self._token_expiry = 0

    def is_configured(self):
        return all([self.api_key, self.api_secret])

    def _get_access_token(self):
        if self._token and time.time() < self._token_expiry:
            return self._token
        if not self.is_configured():
            raise TeletracApiError("TELETRAC_API_KEY/TELETRAC_API_SECRET not fully set")
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "password", "username": self.api_key, "password": self.api_secret,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        # Refresh a little early rather than risking a live 401 mid-cycle.
        self._token_expiry = time.time() + payload.get("expires_in", 7200) - 60
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self._get_access_token()}", "Content-Type": "application/json"}

    def get_devices_current_data(self, client_id, group_id=None, imei_nos=None):
        """Current position/status for every device under one client.
        POST /api/Asset/GetDevicesCurrentData. group_id/imei_nos are
        both optional narrowing filters - omitted here, every device
        under the client is returned."""
        url = f"{BASE_URL}/Asset/GetDevicesCurrentData"
        body = {"ClientID": client_id}
        if group_id:
            body["GroupId"] = group_id
        if imei_nos:
            body["imei_nos"] = imei_nos
        resp = requests.post(url, headers=self._headers(), json=body, timeout=60)
        resp.raise_for_status()
        return resp.json().get("Data", [])

    def get_all_clients(self, include_miniresller_clients=True):
        """GET-equivalent (POST per the docs) /api/Client/GetAllClients -
        used only to help an operator look up ClientID values to put in
        settings.ini, not part of the regular polling path."""
        url = f"{BASE_URL}/Client/GetAllClients"
        resp = requests.post(url, headers=self._headers(),
                              json={"IncludeMiniresellerClients": include_miniresller_clients}, timeout=30)
        resp.raise_for_status()
        return resp.json().get("client", [])

    def get_devices_current_data_for_clients(self, client_ids):
        """
        Loops every client, spacing calls out by
        inter_client_delay_seconds to stay well under the API's rate
        limit - same throttling pattern the MiX API client uses between
        its own per-org calls. A failure on one client is logged and
        returns an empty list for that client rather than aborting
        every other client's fetch.
        Returns {client_id: [device_current_data, ...]}.
        """
        result = {}
        for i, client_id in enumerate(client_ids):
            if i > 0:
                time.sleep(self.inter_client_delay_seconds)
            try:
                result[client_id] = self.get_devices_current_data(client_id)
            except requests.exceptions.RequestException as e:
                logger.error(f"Teletrac current-data fetch failed for client {client_id}: {e}")
                result[client_id] = []
        return result
