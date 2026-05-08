"""Fuel-stop optimization.

Strategy
--------
Vehicle: 10 mpg, 500-mile range, fully fueled at the start.
We make ONE ORS directions call. From the returned polyline we:

1. Project every fuel station within a corridor (~1.5 mi) onto the route via KDTree
   in O(N log N). For each in-corridor station we know its cumulative
   route-mile position.
2. Walk the route greedily: from the current position, pick the cheapest station
   within the *next* (range_miles) miles, but bias toward the far half so we
   refuel less often (cheaper per-mile). If no station is reachable before the
   tank runs out, raise. If the finish is reachable on remaining fuel, stop.
3. Cost = sum(price * gallons_to_fill_to_reach_next_stop_or_finish).
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Iterable
import math
import numpy as np
from scipy.spatial import KDTree

EARTH_R_MI = 3958.7613
# Stations are geocoded at city level (offline DB), so the effective offset
# from the actual interstate exit can be 5-25 mi. Use a generous corridor.
CORRIDOR_MI = 30.0
# Fallback corridor used only if no station is found inside the primary corridor
# for a given window. Keeps the API from 422'ing on sparse states.
FALLBACK_CORRIDOR_MI = 75.0
RANGE_MI = 500.0
MPG = 10.0


def _latlon_to_xyz(lat: float, lon: float) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    return (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))


def _haversine_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * EARTH_R_MI * math.asin(math.sqrt(h))


@dataclass
class Stop:
    name: str
    address: str
    city: str
    state: str
    opis_id: int
    latitude: float
    longitude: float
    price_per_gallon: float
    route_mile: float
    gallons: float
    cost: float


@dataclass
class RouteResult:
    total_distance_mi: float
    total_fuel_cost: float
    stops: list[dict]
    map_url: str


def _route_points_with_cumdist(coords_lonlat: list[list[float]]) -> tuple[np.ndarray, np.ndarray]:
    """Return (points[N,2] lat/lon, cum_miles[N])."""
    pts = np.array([[p[1], p[0]] for p in coords_lonlat], dtype=float)
    cum = np.zeros(len(pts))
    for i in range(1, len(pts)):
        cum[i] = cum[i - 1] + _haversine_mi(tuple(pts[i - 1]), tuple(pts[i]))
    return pts, cum


def _build_station_index(stations) -> tuple[KDTree, list]:
    if not stations:
        return None, []
    xyz = np.array([_latlon_to_xyz(s.latitude, s.longitude) for s in stations])
    return KDTree(xyz), list(stations)


def optimize(geojson: dict, stations: Iterable) -> RouteResult:
    feat = geojson["features"][0]
    coords = feat["geometry"]["coordinates"]
    summary = feat["properties"]["summary"]
    total_mi = float(summary["distance"])  # already miles (units=mi requested)

    pts, cum = _route_points_with_cumdist(coords)

    stations = [s for s in stations if s.latitude is not None and s.longitude is not None]
    tree, station_list = _build_station_index(stations)

    # (route_mile, station, perpendicular_dist_mi) for stations within FALLBACK corridor
    nearby: list[tuple[float, object, float]] = []
    if tree is not None:
        chord = 2 * math.sin((FALLBACK_CORRIDOR_MI / EARTH_R_MI) / 2)
        route_xyz = np.array([_latlon_to_xyz(p[0], p[1]) for p in pts])
        seen: dict[int, tuple[float, float]] = {}  # station_idx -> (mile, dist_mi)
        for i, q in enumerate(route_xyz):
            for j in tree.query_ball_point(q, r=chord):
                d_mi = _haversine_mi(
                    (pts[i][0], pts[i][1]),
                    (station_list[j].latitude, station_list[j].longitude),
                )
                prev = seen.get(j)
                if prev is None or d_mi < prev[1]:
                    seen[j] = (cum[i], d_mi)
        for j, (mile, d_mi) in seen.items():
            nearby.append((mile, station_list[j], d_mi))
        nearby.sort(key=lambda x: x[0])

    # Greedy fueling. Tank starts full.
    fuel_mi = RANGE_MI
    pos = 0.0
    stops: list[Stop] = []

    while pos + fuel_mi < total_mi:
        # Window of stations reachable on current tank.
        window = [(m, s, d) for (m, s, d) in nearby if pos < m <= pos + fuel_mi]
        if not window:
            raise ValueError(
                f"No fuel station reachable within {RANGE_MI} mi from mile {pos:.1f}."
            )
        # First try primary corridor; widen if empty.
        primary = [(m, s, d) for (m, s, d) in window if d <= CORRIDOR_MI] or window
        far_half = [(m, s, d) for (m, s, d) in primary if m >= pos + fuel_mi * 0.5] or primary
        chosen_mile, chosen, _ = min(far_half, key=lambda x: x[1].retail_price)
        # gallons needed to reach it (we go from current fuel down to ~0; refill to full)
        miles_consumed = chosen_mile - pos
        gallons_used = miles_consumed / MPG
        # Refuel enough to fill the tank back to full (range terms)
        # Cost model: pay for the miles_consumed since last fill at THIS station's price.
        # (Simple, transparent: each leg is paid at the price of the station ending it.)
        cost = gallons_used * chosen.retail_price
        stops.append(
            Stop(
                name=chosen.name,
                address=chosen.address,
                city=chosen.city,
                state=chosen.state,
                opis_id=chosen.opis_id,
                latitude=chosen.latitude,
                longitude=chosen.longitude,
                price_per_gallon=round(chosen.retail_price, 4),
                route_mile=round(chosen_mile, 2),
                gallons=round(gallons_used, 3),
                cost=round(cost, 2),
            )
        )
        pos = chosen_mile
        fuel_mi = RANGE_MI

    # Final leg from pos -> finish, paid at last station's price (or an avg if no stops).
    final_miles = total_mi - pos
    final_gallons = final_miles / MPG
    final_price = stops[-1].price_per_gallon if stops else (
        sum(s.retail_price for _, s, _ in nearby[:5]) / max(1, len(nearby[:5]))
        if nearby else 0.0
    )
    final_cost = final_gallons * final_price
    total_cost = sum(s.cost for s in stops) + final_cost

    start_lat, start_lon = pts[0]
    end_lat, end_lon = pts[-1]
    map_url = (
        f"https://www.google.com/maps/dir/{start_lat},{start_lon}/"
        + "/".join(f"{s.latitude},{s.longitude}" for s in stops)
        + (f"/{end_lat},{end_lon}" if stops else f"/{end_lat},{end_lon}")
    )

    return RouteResult(
        total_distance_mi=round(total_mi, 2),
        total_fuel_cost=round(total_cost, 2),
        stops=[asdict(s) for s in stops],
        map_url=map_url,
    )
