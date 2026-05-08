"""OpenRouteService client. Centralizes all external HTTP calls.

Caches geocode + directions responses in Django's cache backend so repeated
queries (Postman, frontend retries) skip the network entirely. Keys are
deterministic over input so cold-start is never penalized twice.
"""
from __future__ import annotations

import hashlib
import json

import requests
from django.conf import settings
from django.core.cache import cache


class ORSError(Exception):
    pass


_GEOCODE_TTL = 60 * 60 * 24 * 7    # 7 days: city/address → lat/lon is stable
_DIRECTIONS_TTL = 60 * 60 * 6      # 6 hours: route geometry rarely shifts


def _key(prefix: str, payload) -> str:
    h = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return f"ors:{prefix}:{h}"


class ORSClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.ORS_API_KEY
        if not self.api_key:
            raise ORSError("ORS_API_KEY is not configured. Add it to .env")
        self.base = settings.ORS_BASE_URL

    def geocode(self, text: str) -> tuple[float, float]:
        """Return (lat, lon) for a free-form address. USA-only. Cached."""
        text_norm = (text or "").strip().lower()
        if not text_norm:
            raise ORSError("Empty geocode query.")

        ck = _key("geo", text_norm)
        hit = cache.get(ck)
        if hit:
            return tuple(hit)

        r = requests.get(
            f"{self.base}/geocode/search",
            params={
                "api_key": self.api_key,
                "text": text,
                "boundary.country": "US",
                "size": 1,
            },
            timeout=15,
        )
        if not r.ok:
            raise ORSError(f"Geocode failed: {r.status_code} {r.text[:200]}")
        feats = r.json().get("features") or []
        if not feats:
            raise ORSError(f"No geocode result for: {text}")
        lon, lat = feats[0]["geometry"]["coordinates"]
        latlon = (float(lat), float(lon))
        cache.set(ck, latlon, _GEOCODE_TTL)
        return latlon

    def directions(self, start: tuple[float, float], finish: tuple[float, float]) -> dict:
        """Driving-car GeoJSON between (lat,lon) endpoints. Cached.

        Coords are rounded to 4 decimals (~11 m) before hashing the cache key
        so trivial input drift hits the same entry.
        """
        s = (round(start[0], 4), round(start[1], 4))
        f = (round(finish[0], 4), round(finish[1], 4))
        ck = _key("dir", [s, f])
        hit = cache.get(ck)
        if hit:
            return hit

        body = {
            "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],
            "instructions": False,
            "units": "mi",
            # City-centroid geocodes can sit miles from the nearest road; ORS's
            # default 350 m snap radius rejects them. Allow up to 10 km.
            "radiuses": [10000, 10000],
        }
        r = requests.post(
            f"{self.base}/v2/directions/driving-car/geojson",
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        if not r.ok:
            raise ORSError(f"Directions failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        cache.set(ck, data, _DIRECTIONS_TTL)
        return data
