"""Single-call wrapper around ORS Directions API."""
from __future__ import annotations

import requests
from django.conf import settings


class RoutingError(Exception):
    pass


def get_route(start_lonlat: list[float], end_lonlat: list[float]) -> dict:
    """
    ONE call to ORS. Returns:
        {
          "geometry": [[lon, lat], ...],   # full polyline, decoded
          "distance_miles": float,
          "duration_seconds": float,
          "bbox": [...],
        }
    """
    if not settings.ORS_API_KEY:
        raise RoutingError("ORS_API_KEY not configured.")

    url = f"{settings.ORS_BASE_URL}/v2/directions/driving-car/geojson"
    body = {"coordinates": [start_lonlat, end_lonlat], "units": "mi"}
    headers = {"Authorization": settings.ORS_API_KEY,
               "Content-Type": "application/json"}
    r = requests.post(url, json=body, headers=headers, timeout=20)
    if r.status_code != 200:
        raise RoutingError(f"ORS error {r.status_code}: {r.text[:200]}")

    data = r.json()
    feat = data["features"][0]
    summary = feat["properties"]["summary"]
    return {
        "geometry": feat["geometry"]["coordinates"],
        "distance_miles": float(summary["distance"]),
        "duration_seconds": float(summary["duration"]),
        "bbox": data.get("bbox"),
    }
