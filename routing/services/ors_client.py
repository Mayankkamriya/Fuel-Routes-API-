"""OpenRouteService client. Centralizes all external HTTP calls."""
from __future__ import annotations
import requests
from django.conf import settings


class ORSError(Exception):
    pass


class ORSClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.ORS_API_KEY
        if not self.api_key:
            raise ORSError("ORS_API_KEY is not configured. Add it to .env")
        self.base = settings.ORS_BASE_URL

    def geocode(self, text: str) -> tuple[float, float]:
        """Return (lat, lon) for a free-form address using ORS Pelias."""
        r = requests.get(
            f"{self.base}/geocode/search",
            params={"api_key": self.api_key, "text": text, "boundary.country": "US", "size": 1},
            timeout=20,
        )
        if not r.ok:
            raise ORSError(f"Geocode failed: {r.status_code} {r.text[:200]}")
        feats = r.json().get("features") or []
        if not feats:
            raise ORSError(f"No geocode result for: {text}")
        lon, lat = feats[0]["geometry"]["coordinates"]
        return float(lat), float(lon)

    def directions(self, start: tuple[float, float], finish: tuple[float, float]) -> dict:
        """Return ORS GeoJSON for driving-car between (lat,lon) endpoints. ONE call per request."""
        body = {
            "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],
            "instructions": False,
            "units": "mi",
        }
        r = requests.post(
            f"{self.base}/v2/directions/driving-car/geojson",
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        if not r.ok:
            raise ORSError(f"Directions failed: {r.status_code} {r.text[:200]}")
        return r.json()
