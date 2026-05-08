"""
Fuel-stop optimizer.

Approach (no extra routing-API calls):
  1. Take the polyline returned by ORS (list of [lon, lat] vertices).
  2. Compute cumulative miles along the polyline (haversine between vertices).
  3. Build a KDTree over polyline vertices in a local equirectangular
     projection (good enough at these scales). For each candidate fuel
     station within a bounding box around the route, find its nearest
     polyline vertex => its "mile marker" along the route and its
     perpendicular distance (corridor filter).
  4. Greedy refueling:
       tank = 500 mi, position = 0
       while position + tank < total_distance:
           candidates = stations with marker in (position, position + tank]
                        and corridor <= FUEL_CORRIDOR_MILES
           pick the cheapest among the FAR HALF of the window
             (so we don't waste range refueling too early); fall back to
             cheapest in full window if far-half empty.
           refuel, advance position to that station.
  5. Total cost = sum(gallons_for_each_leg * price_at_station_used_for_leg).
     The first leg uses the price of the first refuel station; the final
     leg from the last refuel to the destination uses that last station's
     price. (Standard interpretation of the brief.)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Iterable

import numpy as np
from scipy.spatial import cKDTree

from django.conf import settings

from routing.models import FuelStation


# ---------- geo helpers ----------
EARTH_R_MI = 3958.7613


def _haversine_miles(lon1, lat1, lon2, lat2) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R_MI * math.asin(math.sqrt(a))


def _cumulative_miles(coords: np.ndarray) -> np.ndarray:
    """coords shape (N,2) as [lon,lat]. Returns shape (N,) cumulative miles."""
    if len(coords) < 2:
        return np.zeros(len(coords))
    lon1 = coords[:-1, 0]
    lat1 = coords[:-1, 1]
    lon2 = coords[1:, 0]
    lat2 = coords[1:, 1]
    rlat1 = np.radians(lat1)
    rlat2 = np.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(rlat1) * np.cos(rlat2) * np.sin(dlon / 2) ** 2
    seg = 2 * EARTH_R_MI * np.arcsin(np.sqrt(a))
    return np.concatenate(([0.0], np.cumsum(seg)))


# ---------- output dataclasses ----------
@dataclass
class FuelStop:
    name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price_per_gallon: float
    miles_from_start: float
    gallons_purchased: float
    leg_cost_usd: float


# ---------- main ----------
def plan_fuel_stops(geometry: list[list[float]], total_distance_miles: float) -> dict:
    """
    geometry: [[lon, lat], ...] from the ORS call
    Returns: { "stops": [...], "total_fuel_cost_usd": float,
               "total_gallons": float, "warnings": [...] }
    """
    coords = np.asarray(geometry, dtype=float)
    cum = _cumulative_miles(coords)
    total = float(total_distance_miles or cum[-1])

    warnings: list[str] = []
    range_mi = settings.VEHICLE_RANGE_MILES
    mpg = settings.VEHICLE_MPG
    corridor = settings.FUEL_CORRIDOR_MILES

    # Trip fits in one tank: no fuel stop needed. Brief says still report cost.
    if total <= range_mi:
        gallons = total / mpg
        # No station => use national avg fallback only if DB empty;
        # otherwise use cheapest station near start as a reasonable proxy.
        avg_price = _nearest_price(coords[0], radius_mi=50) or _global_avg_price()
        return {
            "stops": [],
            "total_fuel_cost_usd": round(gallons * (avg_price or 0.0), 2),
            "total_gallons": round(gallons, 2),
            "assumed_price_per_gallon": avg_price,
            "warnings": ["Trip is within one tank; no refuel required."],
        }

    # ---- bounding box prefilter on stations ----
    min_lon, min_lat = coords.min(axis=0)
    max_lon, max_lat = coords.max(axis=0)
    pad = 0.5  # ~30mi
    qs = FuelStation.objects.filter(
        longitude__gte=min_lon - pad, longitude__lte=max_lon + pad,
        latitude__gte=min_lat - pad, latitude__lte=max_lat + pad,
    ).only("id", "name", "address", "city", "state",
           "latitude", "longitude", "retail_price")
    stations = list(qs)
    if not stations:
        warnings.append("No fuel stations found near route in DB.")
        return {"stops": [], "total_fuel_cost_usd": 0.0,
                "total_gallons": round(total / mpg, 2), "warnings": warnings}

    # ---- project to local equirectangular for KDTree ----
    lat0 = float(coords[:, 1].mean())
    cos_lat0 = math.cos(math.radians(lat0))
    deg_to_mi_lat = 69.0
    deg_to_mi_lon = 69.0 * cos_lat0

    route_xy = np.column_stack([
        (coords[:, 0]) * deg_to_mi_lon,
        (coords[:, 1]) * deg_to_mi_lat,
    ])
    tree = cKDTree(route_xy)

    st_arr = np.array([[s.longitude * deg_to_mi_lon, s.latitude * deg_to_mi_lat]
                       for s in stations])
    dists_mi, idxs = tree.query(st_arr, k=1)

    # Build candidate list: (marker_mile, corridor_mi, station)
    candidates = []
    for s, d, i in zip(stations, dists_mi, idxs):
        if d > corridor:
            continue
        candidates.append((float(cum[i]), float(d), s))
    candidates.sort(key=lambda t: t[0])

    if not candidates:
        warnings.append(f"No fuel stations within {corridor} mi corridor of route.")
        return {"stops": [], "total_fuel_cost_usd": 0.0,
                "total_gallons": round(total / mpg, 2), "warnings": warnings}

    # ---- greedy refuel ----
    stops: list[FuelStop] = []
    position = 0.0
    last_price = None
    safety = 0
    while position + range_mi < total:
        safety += 1
        if safety > 200:
            warnings.append("Safety stop: more than 200 refuels — aborting.")
            break

        window_min = position
        window_max = position + range_mi
        in_window = [(m, d, s) for (m, d, s) in candidates if window_min < m <= window_max]
        if not in_window:
            warnings.append(
                f"No reachable fuel station within {range_mi} mi after mile {position:.1f}."
            )
            return {"stops": [asdict(x) for x in stops],
                    "total_fuel_cost_usd": round(sum(x.leg_cost_usd for x in stops), 2),
                    "total_gallons": round(sum(x.gallons_purchased for x in stops), 2),
                    "warnings": warnings}

        # Prefer "far half": stations in (position + range/2, position + range]
        far_half = [c for c in in_window if c[0] >= window_min + range_mi * 0.5]
        pool = far_half if far_half else in_window
        chosen = min(pool, key=lambda c: c[2].retail_price)
        marker, _d, s = chosen

        leg_miles = marker - position
        gallons = leg_miles / mpg
        # Cost for THIS leg uses the price we paid most recently. For the
        # very first leg (no prior refuel), the brief is silent; we charge
        # at the first stop's price (i.e. you fueled up there).
        price_for_leg = last_price if last_price is not None else s.retail_price
        leg_cost = gallons * price_for_leg

        stops.append(FuelStop(
            name=s.name, address=s.address, city=s.city, state=s.state,
            latitude=s.latitude, longitude=s.longitude,
            price_per_gallon=round(s.retail_price, 3),
            miles_from_start=round(marker, 2),
            gallons_purchased=round(gallons, 2),
            leg_cost_usd=round(leg_cost, 2),
        ))
        position = marker
        last_price = s.retail_price

    # Final leg: from last stop to destination
    final_leg = total - position
    final_gallons = final_leg / mpg
    final_price = last_price if last_price is not None else 0.0
    final_cost = final_gallons * final_price
    if stops:
        # Attribute the final-leg cost to the last refuel record for clarity.
        stops[-1].gallons_purchased = round(stops[-1].gallons_purchased + final_gallons, 2)
        stops[-1].leg_cost_usd = round(stops[-1].leg_cost_usd + final_cost, 2)

    total_cost = sum(x.leg_cost_usd for x in stops)
    total_gal = sum(x.gallons_purchased for x in stops)

    return {
        "stops": [asdict(x) for x in stops],
        "total_fuel_cost_usd": round(total_cost, 2),
        "total_gallons": round(total_gal, 2),
        "warnings": warnings,
    }


def _global_avg_price() -> float | None:
    from django.db.models import Avg
    v = FuelStation.objects.aggregate(a=Avg("retail_price"))["a"]
    return float(v) if v else None


def _nearest_price(lonlat, radius_mi: float) -> float | None:
    """Cheap fallback: cheapest station within bounding-box ~radius_mi of point."""
    lon, lat = float(lonlat[0]), float(lonlat[1])
    pad_lat = radius_mi / 69.0
    pad_lon = radius_mi / (69.0 * max(0.1, math.cos(math.radians(lat))))
    qs = FuelStation.objects.filter(
        latitude__gte=lat - pad_lat, latitude__lte=lat + pad_lat,
        longitude__gte=lon - pad_lon, longitude__lte=lon + pad_lon,
    ).order_by("retail_price").values_list("retail_price", flat=True)[:1]
    return float(qs[0]) if qs else None
