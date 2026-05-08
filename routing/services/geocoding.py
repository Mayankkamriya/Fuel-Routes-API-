"""Thin wrapper over OpenRouteService Pelias geocoder."""
from __future__ import annotations

import re
import requests
from django.conf import settings

_LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def parse_latlon(text: str) -> tuple[float, float] | None:
    m = _LATLON_RE.match(text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return lon, lat  # ORS uses [lon, lat]


def geocode_address(query: str) -> list[float] | None:
    """Return [lon, lat] or None. Restricted to USA."""
    if not settings.ORS_API_KEY:
        raise RuntimeError("ORS_API_KEY not configured.")
    coords = parse_latlon(query)
    if coords:
        return list(coords)
    r = requests.get(
        f"{settings.ORS_BASE_URL}/geocode/search",
        params={
            "api_key": settings.ORS_API_KEY,
            "text": query,
            "boundary.country": "USA",
            "size": 1,
        },
        timeout=10,
    )
    if r.status_code != 200:
        return None
    feats = r.json().get("features") or []
    if not feats:
        return None
    return feats[0]["geometry"]["coordinates"]  # [lon, lat]
