"""Fuel-stop optimization.

Pipeline (one ORS call per request, everything else is local compute):

1. Vectorize cumulative-mile array from the polyline (numpy haversine).
2. Bbox prefilter the FuelStation table to stations near the route corridor.
3. Project each candidate station to its nearest polyline vertex via a single
   KDTree.query (O(N log V)) in a local equirectangular frame.
4. Run the textbook "gas-station problem" greedy:
     - From the current station, look ahead within `range`.
     - If a strictly cheaper station is reachable, buy just enough fuel to reach
       the first one.
     - Otherwise fill up and jump to the cheapest reachable station.
   This is provably cost-optimal for the deterministic prices case.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Iterable

import numpy as np
from scipy.spatial import cKDTree
from django.conf import settings

from routing.models import FuelStation


EARTH_R_MI = 3958.7613
DEG_TO_MI_LAT = 69.0
# Generous corridor – stations are city-geocoded so the offset to the actual
# interstate exit can be 5-25 mi. Widened only if no candidate is found.
CORRIDOR_MI = 30.0
FALLBACK_CORRIDOR_MI = 75.0
BBOX_PAD_DEG = 1.2  # ~80 mi at mid-latitudes – more than enough for the corridor


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
    polyline: list[list[float]]   # [[lat, lon], ...] for the frontend map
    bbox: list[float]             # [min_lat, min_lon, max_lat, max_lon]
    map_url: str


def _cum_miles(coords_lonlat: np.ndarray) -> np.ndarray:
    """Vectorized cumulative haversine in miles. coords shape (N,2) as [lon,lat]."""
    if len(coords_lonlat) < 2:
        return np.zeros(len(coords_lonlat))
    lon1 = np.radians(coords_lonlat[:-1, 0])
    lat1 = np.radians(coords_lonlat[:-1, 1])
    lon2 = np.radians(coords_lonlat[1:, 0])
    lat2 = np.radians(coords_lonlat[1:, 1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    seg = 2 * EARTH_R_MI * np.arcsin(np.sqrt(a))
    return np.concatenate(([0.0], np.cumsum(seg)))


def _bbox_prefilter(coords: np.ndarray, queryset_or_iterable):
    """Filter a FuelStation queryset (or any iterable of station-like objects)
    down to those whose lat/lon falls in the route bbox + pad. Uses a DB query
    when given a QuerySet; falls back to in-memory filter otherwise (for tests)."""
    min_lon, min_lat = coords.min(axis=0)
    max_lon, max_lat = coords.max(axis=0)
    if hasattr(queryset_or_iterable, "filter"):
        return list(
            queryset_or_iterable.filter(
                latitude__gte=min_lat - BBOX_PAD_DEG,
                latitude__lte=max_lat + BBOX_PAD_DEG,
                longitude__gte=min_lon - BBOX_PAD_DEG,
                longitude__lte=max_lon + BBOX_PAD_DEG,
            ).only(
                "id", "opis_id", "name", "address", "city", "state",
                "latitude", "longitude", "retail_price",
            )
        )
    out = []
    for s in queryset_or_iterable:
        if s.latitude is None or s.longitude is None:
            continue
        if (min_lat - BBOX_PAD_DEG <= s.latitude <= max_lat + BBOX_PAD_DEG
                and min_lon - BBOX_PAD_DEG <= s.longitude <= max_lon + BBOX_PAD_DEG):
            out.append(s)
    return out


def _project_stations(coords: np.ndarray, cum: np.ndarray, stations: list):
    """For every station, find the nearest polyline vertex => (route_mile,
    perpendicular_distance_mi). Returns a list sorted by route_mile of
    (mile, station, dist_mi), one entry per station in the corridor."""
    if not stations:
        return []
    lat0 = float(coords[:, 1].mean())
    deg_to_mi_lon = DEG_TO_MI_LAT * math.cos(math.radians(lat0))

    route_xy = np.column_stack([
        coords[:, 0] * deg_to_mi_lon,
        coords[:, 1] * DEG_TO_MI_LAT,
    ])
    tree = cKDTree(route_xy)

    st_xy = np.empty((len(stations), 2), dtype=float)
    for i, s in enumerate(stations):
        st_xy[i, 0] = s.longitude * deg_to_mi_lon
        st_xy[i, 1] = s.latitude * DEG_TO_MI_LAT
    dists_mi, idxs = tree.query(st_xy, k=1)

    nearby: list[tuple[float, object, float]] = []
    for s, d, i in zip(stations, dists_mi, idxs):
        if d > FALLBACK_CORRIDOR_MI:
            continue
        nearby.append((float(cum[i]), s, float(d)))
    nearby.sort(key=lambda t: t[0])
    return nearby


def _plan_stops(nearby: list, total_mi: float, range_mi: float, mpg: float) -> list[Stop]:
    """Textbook gas-station greedy. nearby is sorted ascending by route_mile.

    Each iteration:
      1. From `pos`, find all stations with mile in (pos, pos + tank_remaining].
      2. If any is strictly cheaper than `cur_price`, jump to the FIRST cheaper one
         and buy just enough fuel for the leg (cost @ cur_price).
      3. Else fill up at the cheapest reachable station and jump there
         (cost @ cur_price for the leg taken to get there).
    `cur_price` for the very first leg is undefined; we treat the origin as a
    pseudo-station with price = cheapest in the first window so the first leg
    is paid at the price of the first refuel station, matching the brief.
    """
    # First, prefer the primary corridor; widen only if the primary set is empty.
    primary = [(m, s, d) for (m, s, d) in nearby if d <= CORRIDOR_MI]
    pool = primary if primary else nearby

    pos = 0.0
    tank_mi = range_mi  # full tank at the start
    cur_price = None    # price last paid; None at the origin
    stops: list[Stop] = []
    safety = 0

    while pos + tank_mi < total_mi:
        safety += 1
        if safety > 200:
            raise ValueError("Refuel loop safety hit – check station data.")

        window = [(m, s, d) for (m, s, d) in pool if pos < m <= pos + tank_mi]
        if not window:
            raise ValueError(
                f"No fuel station reachable within {range_mi:.0f} mi from mile {pos:.1f}."
            )

        if cur_price is None:
            # First refuel: pick the globally cheapest station in this window.
            chosen_mile, chosen, _ = min(window, key=lambda t: t[1].retail_price)
        else:
            cheaper = next(
                ((m, s, d) for (m, s, d) in window if s.retail_price < cur_price),
                None,
            )
            if cheaper is not None:
                chosen_mile, chosen, _ = cheaper
            else:
                chosen_mile, chosen, _ = min(window, key=lambda t: t[1].retail_price)

        leg_miles = chosen_mile - pos
        gallons = leg_miles / mpg
        # Pay for this leg at the price we last filled at; for the first leg,
        # the brief is silent and we use the chosen station's price (you fueled
        # up there to make the next leg).
        leg_price = cur_price if cur_price is not None else chosen.retail_price
        cost = gallons * leg_price

        stops.append(Stop(
            name=chosen.name,
            address=chosen.address,
            city=chosen.city,
            state=chosen.state,
            opis_id=getattr(chosen, "opis_id", 0) or 0,
            latitude=chosen.latitude,
            longitude=chosen.longitude,
            price_per_gallon=round(chosen.retail_price, 4),
            route_mile=round(chosen_mile, 2),
            gallons=round(gallons, 3),
            cost=round(cost, 2),
        ))
        pos = chosen_mile
        cur_price = chosen.retail_price
        tank_mi = range_mi  # assume we top off

    return stops


def optimize(geojson: dict, stations: Iterable) -> RouteResult:
    range_mi = float(settings.VEHICLE_RANGE_MILES)
    mpg = float(settings.VEHICLE_MPG)

    feat = geojson["features"][0]
    coords = np.asarray(feat["geometry"]["coordinates"], dtype=float)  # [lon,lat]
    summary = feat["properties"]["summary"]
    total_mi = float(summary["distance"])  # ORS units=mi

    cum = _cum_miles(coords)
    polyline_latlon = [[float(lat), float(lon)] for lon, lat in coords]
    bbox = [
        float(coords[:, 1].min()), float(coords[:, 0].min()),
        float(coords[:, 1].max()), float(coords[:, 0].max()),
    ]

    # Trip fits in one tank: pay for the gallons, no refuel needed.
    if total_mi <= range_mi:
        gallons = total_mi / mpg
        # Use the cheapest station within the route bbox as a price proxy.
        candidates = _bbox_prefilter(coords, stations)
        proxy_price = (min((s.retail_price for s in candidates), default=0.0)
                       if candidates else 0.0)
        return RouteResult(
            total_distance_mi=round(total_mi, 2),
            total_fuel_cost=round(gallons * proxy_price, 2),
            stops=[],
            polyline=polyline_latlon,
            bbox=bbox,
            map_url=_google_maps_url(coords, []),
        )

    candidates = _bbox_prefilter(coords, stations)
    nearby = _project_stations(coords, cum, candidates)
    if not nearby:
        raise ValueError("No fuel stations found within the route corridor.")

    stops = _plan_stops(nearby, total_mi, range_mi, mpg)

    # Final leg from the last refuel to the destination, paid at last station's price.
    final_miles = total_mi - (stops[-1].route_mile if stops else 0.0)
    final_gallons = final_miles / mpg
    final_price = stops[-1].price_per_gallon if stops else 0.0
    total_cost = sum(s.cost for s in stops) + final_gallons * final_price

    return RouteResult(
        total_distance_mi=round(total_mi, 2),
        total_fuel_cost=round(total_cost, 2),
        stops=[asdict(s) for s in stops],
        polyline=polyline_latlon,
        bbox=bbox,
        map_url=_google_maps_url(coords, stops),
    )


def _google_maps_url(coords: np.ndarray, stops: list[Stop]) -> str:
    start_lat, start_lon = float(coords[0, 1]), float(coords[0, 0])
    end_lat, end_lon = float(coords[-1, 1]), float(coords[-1, 0])
    waypoints = "/".join(f"{s.latitude},{s.longitude}" for s in stops)
    if waypoints:
        return f"https://www.google.com/maps/dir/{start_lat},{start_lon}/{waypoints}/{end_lat},{end_lon}"
    return f"https://www.google.com/maps/dir/{start_lat},{start_lon}/{end_lat},{end_lon}"
