"""
Who the clients are, and which platform account belongs to which one.

The same real-world client is named differently on every platform - AGL
is "AGL" on MiX, "AFRICA GLOBAL LOGISTICS" on Teletrac and "Africa Global
Logistics(AGL)" on FT Cloud - so nothing can merge them automatically.
The mapping is therefore explicit and human-maintained (admin UI -> the
MySQL "clients" table and its platform-id child tables, see
db_store.CLIENT_HEADERS / db.SCHEMA_STATEMENTS).

Every read goes through load_registry(), which tries three sources in
order and always returns something usable:

  1. MySQL - the live, authoritative copy.
  2. data/client_registry.json - a local cache rewritten on every
     successful DB read. The database being briefly unreachable must not
     stop a poll cycle or blank the dashboard's client filter, and
     without this it would: the pollers run every few minutes and each
     one needs this mapping to know which orgs to fetch at all.
  3. settings.ini's legacy flat [mix_api]/[teletrac_api]/[ft_cloud_api]
     id lists, surfaced as one synthetic "Unassigned" client. This is
     the pre-multi-client behaviour, kept as a floor so a brand-new
     deployment with no DB access still polls and still shows data
     rather than silently doing nothing.
"""

import os
import json
import time
import logging

import atomic_json

logger = logging.getLogger(__name__)

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "client_registry.json")

# The client every asset falls into when no mapping claims its platform
# account. Deliberately visible rather than silently dropped: an
# unmapped org is a configuration gap someone needs to notice and fix,
# and hiding those assets would look identical to them not existing.
UNASSIGNED = "Unassigned"


def _write_cache(clients, cache_path=None):
    """All three platform pollers refresh this same cache from their own
    threads - see fleet_logic/atomic_json.py. Failing to cache is never
    fatal: the caller already has the live data it just fetched, and the
    cache only matters for a LATER run that can't reach the database."""
    path = cache_path or CACHE_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_json.write_json_atomic(path, {"clients": clients}, indent=2)
    except OSError as e:
        logger.warning(f"Could not cache the client registry to {path}: {e}")


def _read_cache(cache_path=None):
    path = cache_path or CACHE_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f).get("clients")
    except (ValueError, OSError):
        logger.warning(f"Client registry cache at {path} is unreadable, ignoring it")
        return None


def _from_settings(settings):
    """Legacy fallback - the flat per-platform id lists that predate
    clients existing at all, presented as one unnamed client."""
    if settings is None:
        return []
    mix = settings.get("MIX_API_ORG_IDS") or []
    teletrac = settings.get("TELETRAC_API_CLIENT_IDS") or []
    ft = settings.get("FT_CLOUD_API_FLEET_IDS") or []
    if not (mix or teletrac or ft):
        return []
    return [{
        "name": UNASSIGNED, "emails": [],
        "mixOrgIds": list(mix), "teletracClientIds": list(teletrac), "ftCloudFleetIds": list(ft),
    }]


# The registry is read by all three platform pollers on every cycle and
# changes only when an admin edits it, so hitting the database each time
# is pure waste. (Historically this also mattered a lot more: when this
# was backed by Google Sheets, going to it every cycle helped push this
# project over Sheets' "read requests per minute" quota, which failed
# unrelated requests app-wide - a MySQL query has no such quota, but the
# cache is still worth keeping for the same "why hit the DB every 5
# minutes for data that changes rarely" reason.) Cached in memory,
# invalidated explicitly on write (see invalidate_cache).
_MEMO_TTL_SECONDS = 300
_memo = {"clients": None, "fetched_at": 0.0}


def invalidate_cache():
    """Call after any write to the Clients registry so the next read
    reflects it immediately rather than up to _MEMO_TTL_SECONDS later."""
    _memo["clients"] = None
    _memo["fetched_at"] = 0.0


def load_registry(settings=None, cache_path=None, allow_db=True, force=False):
    """
    The client list, newest-known-good. Never raises: a caller in the
    middle of a poll cycle needs an answer, not an exception.
    """
    if allow_db and not force:
        memoized = _memo["clients"]
        if memoized is not None and (time.time() - _memo["fetched_at"]) < _MEMO_TTL_SECONDS:
            return memoized

    if allow_db:
        try:
            import db_store
            clients = db_store.load_clients()
            # An empty clients table is a legitimate "no clients
            # configured yet" answer on a fresh install, but it's
            # indistinguishable from a misconfigured database - and
            # caching it would overwrite a good cache with nothing. Fall
            # through instead.
            if clients:
                _memo.update({"clients": clients, "fetched_at": time.time()})
                _write_cache(clients, cache_path)
                return clients
        except Exception as e:
            logger.warning(f"Client registry unavailable from the database ({e}), falling back to the local cache")

    cached = _read_cache(cache_path)
    if cached:
        return cached
    return _from_settings(settings)


def _index(clients, key):
    """{platform_id: client_name} for one platform's id column."""
    out = {}
    for c in clients:
        for pid in c.get(key) or []:
            out[str(pid)] = c["name"]
    return out


def platform_index(clients):
    """
    All three lookups at once: {"mix": {...}, "teletrac": {...},
    "ftCloud": {...}}, each mapping a platform account id to the
    canonical client name that owns it.
    """
    return {
        "mix": _index(clients, "mixOrgIds"),
        "teletrac": _index(clients, "teletracClientIds"),
        "ftCloud": _index(clients, "ftCloudFleetIds"),
    }


def ids_for_platform(clients, platform):
    """Every account id to poll on one platform, across all clients.
    platform is "mix" | "teletrac" | "ftCloud"."""
    key = {"mix": "mixOrgIds", "teletrac": "teletracClientIds", "ftCloud": "ftCloudFleetIds"}[platform]
    seen, out = set(), []
    for c in clients:
        for pid in c.get(key) or []:
            pid = str(pid)
            if pid not in seen:
                seen.add(pid)
                out.append(pid)
    return out


def client_names(clients):
    return sorted({c["name"] for c in clients if c.get("name")})
