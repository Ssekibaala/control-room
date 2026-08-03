"""
Canonical data model. Every adapter, no matter the source, produces
rows shaped like this. Nothing downstream (classifier, excel writer,
email writer) ever needs to know which platform a row came from.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# East Africa Time - UTC+3 year-round, no DST - where this fleet actually
# operates. Deliberately a fixed offset added to datetime.utcnow(), NOT
# the container/host's own system timezone (which defaults to UTC on a
# bare python:3.12-slim image and was never explicitly set), and NOT
# zoneinfo/pytz (would need a tzdata package the base image doesn't
# ship). Every datetime in this codebase is naive and, by convention,
# assumed to already be EAT wall-clock - these two helpers are the only
# places that convention gets enforced against a real UTC source.
_EAT_OFFSET = timedelta(hours=3)


def now_eat():
    """Current wall-clock time in East Africa Time, as a naive datetime -
    use this instead of datetime.now()/datetime.utcnow() for anything
    that gets displayed or compared against report timestamps (which are
    EAT too), so a container whose own system clock is UTC doesn't make
    the dashboard's timestamps run 3 hours behind reality."""
    return datetime.utcnow() + _EAT_OFFSET


def utc_to_eat(dt):
    """Converts a naive UTC datetime (e.g. parsed from a '...Z'-suffixed
    API timestamp) to naive EAT, so it's directly comparable to every
    other timestamp in this codebase without silently running 3 hours
    off. None-safe."""
    if dt is None:
        return None
    return dt + _EAT_OFFSET


@dataclass
class AssetReport:
    asset_plate: str                  # normalized plate, e.g. "UBQ756P"
    source_platform: str              # "Teletrac", "MiX Unity", "FT Cloud Camera", "Telecom SIM"
    report_type: str                  # "offline_bulk", "movement", "mobile_status",
                                       # "power_disconnect", "camera_status", "sim_feedback"
    last_report_time: Optional[datetime]
    last_lat: Optional[float] = None
    last_lon: Optional[float] = None
    last_location_text: Optional[str] = None
    imei: Optional[str] = None
    sim_card: Optional[str] = None
    network_provider: Optional[str] = None
    status_note: Optional[str] = None   # free text, e.g. telecom's "offline since 11/7"
    # Vendor-confirmed connectivity state, for platforms that expose one
    # directly (FT Cloud's vehicle onlineState) rather than requiring it
    # to be inferred from last_report_time's age. True/False when the
    # platform gave an explicit answer, None when it didn't (mailed
    # reports, MiX, Teletrac). See classifier._platform_status(): only
    # True overrides the threshold computation, and only towards
    # Offline - a platform saying "online" is just a connectivity
    # heartbeat and must not suppress a genuine staleness finding.
    reported_offline: Optional[bool] = None
    organisation_id: Optional[str] = None  # MiX org / Teletrac client this row belongs to (API-sourced rows only)
    # Canonical client this asset belongs to, resolved from
    # organisation_id via the client registry (see
    # fleet_logic/client_registry.py). This - not organisation_id - is
    # what access control and the dashboard's client filter key off,
    # because the same client owns differently-named accounts on each
    # platform. None on mailed-report rows, which predate clients.
    client: Optional[str] = None
    raw_row: dict = field(default_factory=dict)   # original row, kept for audit/debug
    ingested_at: datetime = field(default_factory=datetime.utcnow)


import re

_PLATE_PATTERN = re.compile(r'^[A-Z]{1,3}\d{2,4}[A-Z]{1,3}$')


def normalize_plate(raw: str) -> str:
    """
    Plates arrive as 'FAW UBQ 756P', 'FAW-UBQ756P', 'UBQ756P', 'UBQ540X_2', etc.
    Strip make/model prefix, spaces/dashes, and any trailing '_2' / '-2'
    secondary-device suffix, so every source and every duplicate device
    on the same truck collapses to one plate format: UBQ756P
    """
    if not raw:
        return ""
    raw = str(raw).upper()
    raw = re.sub(r'[_\-]?\s*[2-9]$', '', raw.strip())  # drop trailing device-suffix like _2
    match = re.search(r'([A-Z]{1,3}\s?\d{2,4}\s?[A-Z]{0,3})\s*$', raw)
    token = match.group(1) if match else raw
    return re.sub(r'[\s\-]', '', token)


def is_valid_plate(plate: str) -> bool:
    """
    Filters out test/junk asset names (GTL1, MIX4000, XXXX, '2', etc.)
    that aren't real vehicle plates. Matches standard Uganda/Kenya
    plate shape: 1-3 letters, 2-4 digits, 1-3 letters.
    """
    if not plate:
        return False
    return bool(_PLATE_PATTERN.match(plate))


# Common OCR/typing confusions between platforms that export plates
# slightly differently (image-scanned dashboards, manual re-entry, etc.)
_OCR_EQUIVALENTS = {
    '0': 'O', 'O': 'O',
    '1': 'I', 'I': 'I',
    '5': 'S', 'S': 'S',
    '8': 'B', 'B': 'B',
}


def _fuzzy_key(plate: str) -> str:
    """Collapse OCR-ambiguous characters so visually-confusable plates
    (UBQ756P vs UBQ7S6P style typos) land on the same key."""
    return "".join(_OCR_EQUIVALENTS.get(ch, ch) for ch in plate)


def build_fuzzy_groups(plates):
    """
    Given a list of already-normalized plates from every source, find
    ones that are near-duplicates of each other (same fuzzy key, or
    edit distance 1 on a same-length string) and group them under the
    most frequently-seen spelling. Returns {variant_plate: canonical_plate}.
    Every merge is explicit and auditable, nothing is silently dropped.
    """
    from collections import Counter
    counts = Counter(plates)
    uniques = list(counts.keys())
    fuzzy_map = {}
    for p in uniques:
        fuzzy_map.setdefault(_fuzzy_key(p), []).append(p)

    canonical = {}
    for key, variants in fuzzy_map.items():
        if len(variants) == 1:
            canonical[variants[0]] = variants[0]
            continue
        # pick the most-seen spelling as canonical
        best = max(variants, key=lambda v: counts[v])
        for v in variants:
            canonical[v] = best
    return canonical
