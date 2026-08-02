"""
Receiver-side state for FT Cloud's webhook push stream.

Why this exists at all: FT exposes NO bulk position endpoint (every
one of its ~38 REST groups was checked - GPS appears only as a webhook
type). The sole pull-based way to get a coordinate is each device's
most recent trip, which is one HTTP call PER DEVICE per cycle. For
GTL's 55 trucks that is 55 calls every poll purely for coordinates,
versus 3 calls for everything else. FT's intended design is push:
subscribe once, and positions arrive continuously.

So this module is the other half of that. app.py's public receiver
route hands raw webhook bodies to record_events(), which keeps a
rolling "latest known state per device" file that the ordinary poll
then reads instead of making those 55 calls.

Two deliberate robustness choices:

  - The payload parser is tolerant. The exact GPS field names are not
    in the shared documentation (the swagger host is not publicly
    reachable), so rather than guess one spelling and silently drop
    every event, _extract() accepts the plausible variants and
    anything it cannot parse is written to the unparsed log for a
    one-line fix later. An unrecognised payload must never 500 back
    at FT - that is how a provider disables a subscription.

  - Writes are atomic and lock-guarded. Flask serves webhook
    deliveries on multiple threads, and a torn state file would be
    read by run_import as corrupt and skipped for the whole cycle.
"""

import os
import json
import threading
import logging
from datetime import datetime
from schema import now_eat, utc_to_eat

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# Retained so an unrecognised payload shape can be diagnosed from one
# real delivery rather than by guesswork. Capped so a misconfigured
# subscription cannot fill the disk.
MAX_UNPARSED = 50


def _parse_ts(value):
    """
    Webhook timestamps arrive either as RFC3339 (matching the REST
    side) or as epoch milliseconds, depending on message type. Both are
    parsed as UTC (what they actually are) then converted to naive EAT
    via schema.utc_to_eat() - the same convention every adapter in this
    codebase uses, since classifier compares these against schema.now_eat(),
    not host-local time (a tz-aware value here would raise on subtraction).
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        ms = float(value)
        # Anything past ~2001 in seconds is milliseconds at this scale.
        if ms > 1e11:
            ms /= 1000.0
        try:
            return utc_to_eat(datetime.utcfromtimestamp(ms))
        except (ValueError, OSError):
            return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return utc_to_eat(datetime.strptime(str(value), fmt))
        except ValueError:
            continue
    return None


def _parse_stored_ts(value):
    """
    For re-reading a timestamp THIS module already wrote to the state
    file (record_events() stores new_ts.isoformat() - already EAT,
    post-_parse_ts conversion). Deliberately does NOT call _parse_ts:
    that function assumes its input is raw incoming UTC and would shift
    an already-EAT value by another +3h. Plain ISO parse, no offset.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract(event):
    """
    Pulls (uniqueId, lat, lon, timestamp, online) out of one webhook
    event, tolerating the field-name variants FT uses across message
    types. Returns None when there is no device identifier, which is
    the one thing that makes an event unusable.

    Nested 'data'/'gpsInfo' wrappers are unwrapped first: the REST
    trips endpoint nests coordinates under endGpsInfo, so the push
    side plausibly nests them too.
    """
    if not isinstance(event, dict):
        return None

    flat = dict(event)
    for wrapper in ("data", "gpsInfo", "gps", "location", "position"):
        inner = event.get(wrapper)
        if isinstance(inner, dict):
            flat = {**inner, **{k: v for k, v in flat.items() if k != wrapper}}

    unique_id = _first(flat, "uniqueId", "uniqueID", "deviceId", "deviceNo", "sn")
    if not unique_id:
        return None

    lat = _as_float(_first(flat, "lat", "latitude", "gpsLat"))
    lon = _as_float(_first(flat, "lng", "lon", "longitude", "gpsLng", "gpsLon"))
    ts = _parse_ts(_first(flat, "time", "gpsTime", "deviceTime", "createTime",
                           "timestamp", "reportTime", "utcTime"))

    online = None
    state = _first(flat, "onlineState", "onlineStatus", "state", "status")
    if isinstance(state, str) and state.upper() in ("ONLINE", "OFFLINE"):
        online = state.upper()
    elif isinstance(state, bool):
        online = "ONLINE" if state else "OFFLINE"

    # A coordinate of exactly 0,0 is FT's "no fix yet" filler, not a
    # real position in the Gulf of Guinea. Treated as absent so it can
    # never be fed to the border-risk distance check.
    if lat == 0 and lon == 0:
        lat = lon = None

    return {"uniqueId": str(unique_id), "lat": lat, "lon": lon, "time": ts, "online": online}


def _events_from_body(body):
    """FT may post a single event or a batch, and may wrap it in a
    envelope. Normalised to a flat list of candidate event dicts."""
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for key in ("data", "items", "list", "events", "messages"):
        inner = body.get(key)
        if isinstance(inner, list):
            return inner
    return [body]


def load_state(path):
    if not os.path.exists(path):
        return {"devices": {}, "updatedAt": None, "unparsed": []}
    try:
        with open(path) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning(f"FT Cloud webhook state at {path} unreadable, starting fresh")
        return {"devices": {}, "updatedAt": None, "unparsed": []}
    state.setdefault("devices", {})
    state.setdefault("unparsed", [])
    return state


def _save_state(path, state):
    """Atomic replace - a half-written file would be read as corrupt
    by the very next poll and lose the whole fleet's positions."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, default=str)
    os.replace(tmp, path)


def record_events(path, body, message_type=None, allowed_unique_ids=None):
    """
    Merges one webhook delivery into the rolling per-device state.

    allowed_unique_ids, when given, drops events for devices outside
    GTL's fleet. The subscription is already device-scoped at FT, but
    this endpoint is a public URL: it must not accept another
    company's positions just because someone posted them at it.

    Returns a small summary for the route to log/return. Never raises
    on a malformed body - the caller must always answer FT 200, since
    repeated non-200s are how a provider disables a subscription.
    """
    events = _events_from_body(body)
    accepted, rejected, unparsed = 0, 0, []

    with _LOCK:
        state = load_state(path)
        devices = state["devices"]
        for event in events:
            parsed = _extract(event)
            if parsed is None:
                unparsed.append(event)
                continue
            uid = parsed["uniqueId"]
            if allowed_unique_ids is not None and uid not in allowed_unique_ids:
                rejected += 1
                continue

            current = devices.get(uid, {})
            new_ts = parsed["time"]
            old_ts = _parse_stored_ts(current.get("time"))
            # Out-of-order deliveries are normal on a push stream;
            # never let a late older fix overwrite a newer one.
            if new_ts and old_ts and new_ts < old_ts:
                accepted += 1
                continue

            if parsed["lat"] is not None and parsed["lon"] is not None:
                current["lat"] = parsed["lat"]
                current["lon"] = parsed["lon"]
            if new_ts:
                current["time"] = new_ts.isoformat()
            if parsed["online"]:
                current["online"] = parsed["online"]
            if message_type:
                current["lastType"] = message_type
            current["receivedAt"] = now_eat().isoformat()
            devices[uid] = current
            accepted += 1

        if unparsed:
            state["unparsed"] = (state.get("unparsed", []) + unparsed)[-MAX_UNPARSED:]
        state["updatedAt"] = now_eat().isoformat()
        _save_state(path, state)

    if unparsed:
        logger.warning(f"FT Cloud webhook: {len(unparsed)} event(s) had no recognisable device id "
                       f"(kept in the state file's 'unparsed' list for inspection)")
    return {"accepted": accepted, "rejected": rejected, "unparsed": len(unparsed)}


def positions_by_unique_id(path, max_age_minutes=None, now=None):
    """
    The shape _ft_cloud_api_poll_once() wants: {uniqueId: {lat, lng,
    time}}, matching what the trips-based lookup returns so the two
    position sources are drop-in interchangeable.

    max_age_minutes drops coordinates too old to be meaningful - a
    stale-but-present fix would otherwise look like a current
    position to the border-risk check.
    """
    state = load_state(path)
    now = now or now_eat()
    out = {}
    for uid, d in state.get("devices", {}).items():
        if d.get("lat") is None or d.get("lon") is None:
            continue
        ts = _parse_stored_ts(d.get("time"))
        if max_age_minutes is not None and ts is not None:
            if (now - ts).total_seconds() / 60 > max_age_minutes:
                continue
        out[uid] = {"lat": d["lat"], "lng": d["lon"], "time": d.get("time")}
    return out
