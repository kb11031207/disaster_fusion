"""Tiny shared helpers used across the pipeline."""

from __future__ import annotations

import uuid
from datetime import date
from math import asin, cos, radians, sin, sqrt


def generate_id(prefix: str) -> str:
    """
    Build a short unique ID like 'vf-a3f9b1c0'.

    The 8-hex-char suffix gives 16^8 = ~4 billion combinations — far
    more than the few hundred IDs a single pipeline run will produce,
    so collisions are not a real concern.
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers."""
    earth_radius_km = 6371.0
    lat1_r, lat2_r = radians(lat1), radians(lat2)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2) ** 2
    return 2 * earth_radius_km * asin(sqrt(a))


def iso_date_today() -> str:
    """Today's date as an ISO string, e.g. '2026-04-25'."""
    return date.today().isoformat()
