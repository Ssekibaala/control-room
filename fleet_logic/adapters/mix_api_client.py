"""
Thin client for MiX Telematics' Integrate API (OAuth2 password grant +
REST). This is the API-based replacement for the mailed report
subscriptions the other mix_* adapters read: instead of a daily .csv
attachment, this polls /assets and /positions directly on a timer, so
location data is only ever poll-interval-old rather than a day old.

Two behaviours below were confirmed by trial against the real API
(orgs 8221725616980128168 and 750564533632365070), not documented
assumptions:
  - the Positions endpoint requires the organisation id as a JSON
    INTEGER, not a numeric string ("GroupIds list not supplied"
    otherwise).
  - Positions must be requested ONE organisation per call - it
    rejects a request spanning more than one org ("must belong to a
    single organisation").
"""

import os
import time
import logging
import requests

TOKEN_URL = "https://identity.za.mixtelematics.com/core/connect/token"
BASE_URL = "https://integrate.za.mixtelematics.com/api"
SCOPE = "offline_access MiX.Integrate"

logger = logging.getLogger(__name__)


class MixApiError(Exception):
    pass


class MixApiClient:
    def __init__(self, client_id=None, client_secret=None, username=None, password=None,
                 inter_org_delay_seconds=None):
        self.client_id = client_id or os.environ.get("MIX_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("MIX_CLIENT_SECRET")
        self.username = username or os.environ.get("MIX_USERNAME")
        self.password = password or os.environ.get("MIX_PASSWORD")
        self.inter_org_delay_seconds = (
            inter_org_delay_seconds if inter_org_delay_seconds is not None
            else float(os.environ.get("MIX_INTER_ORG_DELAY_SECONDS", "5"))
        )
        self._token = None
        self._token_expiry = 0

    def is_configured(self):
        return all([self.client_id, self.client_secret, self.username, self.password])

    def _get_access_token(self):
        if self._token and time.time() < self._token_expiry:
            return self._token
        if not self.is_configured():
            raise MixApiError("MIX_CLIENT_ID/MIX_CLIENT_SECRET/MIX_USERNAME/MIX_PASSWORD not fully set")
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "password", "username": self.username,
            "password": self.password, "scope": SCOPE,
        }, auth=(self.client_id, self.client_secret), timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        # Refresh a little early rather than risking a live 401 mid-cycle.
        self._token_expiry = time.time() + payload.get("expires_in", 3600) - 60
        return self._token

    def _headers(self, json_body=False):
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def get_organisation_groups(self):
        """
        Every organisation these credentials can see, as
        [{"GroupId": ..., "Name": ...}]. Backs the admin UI's MiX
        dropdown so a client is mapped by picking a name rather than by
        pasting a 19-digit id - the ids are indistinguishable by eye and
        a single wrong digit silently maps a client to the wrong fleet.
        """
        url = f"{BASE_URL}/organisationgroups"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        return resp.json()

    def get_assets(self, org_id):
        """All assets for one organisation. GET /assets/group/{orgId}."""
        url = f"{BASE_URL}/assets/group/{org_id}"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        return resp.json()

    def get_latest_positions(self, org_id):
        """Latest known position per asset for one organisation."""
        url = f"{BASE_URL}/positions/groups/latest/1"
        resp = requests.post(url, headers=self._headers(json_body=True), json=[int(org_id)], timeout=60)
        resp.raise_for_status()
        return resp.json()

    def get_assets_and_positions(self, org_ids):
        """
        Loops every org, spacing calls out by inter_org_delay_seconds to
        stay well under the API's rate limit - same throttling pattern
        the mailed-report integration used between its own per-org
        calls. A failure on one org is logged and returns empty lists
        for that org rather than aborting every other org's fetch.
        Returns {org_id: {"assets": [...], "positions": [...]}}.
        """
        result = {}
        for i, org_id in enumerate(org_ids):
            if i > 0:
                time.sleep(self.inter_org_delay_seconds)
            try:
                assets = self.get_assets(org_id)
            except requests.exceptions.RequestException as e:
                logger.error(f"MiX assets fetch failed for org {org_id}: {e}")
                assets = []
            time.sleep(self.inter_org_delay_seconds)
            try:
                positions = self.get_latest_positions(org_id)
            except requests.exceptions.RequestException as e:
                logger.error(f"MiX positions fetch failed for org {org_id}: {e}")
                positions = []
            result[org_id] = {"assets": assets, "positions": positions}
        return result
