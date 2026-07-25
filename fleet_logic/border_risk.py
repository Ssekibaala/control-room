"""
Real distance-based border risk, not a keyword match on location text.
Border risk now only applies to OFFLINE assets whose last known GPS
position falls within the configured radius of a known crossing.
An asset that is online and merely near a border is not a risk, it's
just a truck doing its job.
"""

import math

# Approximate coordinates of GTL's known high-traffic border crossings.
# Add more here as new corridors show up in complaint patterns.
KNOWN_BORDERS = [
    ("Malaba", "Kenya/Uganda", 0.6364, 34.2733),
    ("Busia", "Kenya/Uganda", 0.4608, 34.0917),
    ("Namanga", "Kenya/Tanzania", -2.5442, 36.7897),
    ("Mutukula", "Uganda/Tanzania", -1.0167, 31.3333),
    ("Elegu", "Uganda/South Sudan", 3.5167, 32.0833),
]


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def nearest_border(lat, lon):
    """Returns (name, country, distance_km) for the closest known border, or None if no GPS."""
    if lat is None or lon is None:
        return None
    best = None
    for name, country, blat, blon in KNOWN_BORDERS:
        d = haversine_km(lat, lon, blat, blon)
        if best is None or d < best[2]:
            best = (name, country, d)
    return best


def border_risk(is_offline: bool, days_offline: float, lat, lon, radius_km: float, min_days: float):
    """
    Border risk = TRUE only if:
      - asset is currently offline, AND
      - offline for at least min_days (the configured threshold), AND
      - last known position is within radius_km of a known border.
    Online assets near a border are never flagged, that's normal operation.
    """
    if not is_offline or days_offline < min_days:
        return False, None
    nb = nearest_border(lat, lon)
    if nb is None:
        return False, None
    name, country, dist = nb
    if dist <= radius_km:
        return True, (name, country, round(dist, 1))
    return False, None
