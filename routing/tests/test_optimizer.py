"""Unit tests for the fuel-stop optimizer (no network, no DB)."""
from types import SimpleNamespace

import pytest

from routing.services.optimizer import optimize


def _line(start_lon: float, end_lon: float, lat: float = 39.0, n: int = 400, distance_mi: float | None = None):
    """Synthetic east-west polyline. distance_mi defaults to the haversine length."""
    coords = []
    for i in range(n):
        t = i / (n - 1)
        coords.append([start_lon + (end_lon - start_lon) * t, lat])

    # 1° longitude at lat=39 is ~53.7 mi; this is the actual length.
    import math
    actual = abs(end_lon - start_lon) * 69.0 * math.cos(math.radians(lat))
    return {
        "features": [{
            "geometry": {"coordinates": coords},
            "properties": {"summary": {
                "distance": float(distance_mi) if distance_mi is not None else actual,
                "duration": 0,
            }},
        }]
    }


def _station(name: str, price: float, lon: float, lat: float = 39.0, opis: int = 0):
    return SimpleNamespace(
        opis_id=opis, name=name, address="", city="", state="",
        retail_price=price, latitude=lat, longitude=lon,
    )


def test_short_route_no_stop():
    """Trip < range fits in one tank: zero refuel stops."""
    geo = _line(-100.0, -99.0, distance_mi=100.0)
    res = optimize(geo, [])
    assert res.stops == []
    assert res.total_distance_mi == 100.0


def test_picks_cheapest_first_window():
    """Across ~1075 mi (lon -100 to -80) the first refuel must be the cheapest
    station reachable from origin, regardless of how far ahead it sits."""
    geo = _line(-100.0, -80.0)  # ~1075 mi at lat 39
    stations = [
        _station("A", 4.00, -99.0),  # mile ~54
        _station("B", 3.00, -95.0),  # mile ~268 — cheapest in first tank
        _station("C", 3.50, -93.0),  # mile ~376
        _station("D", 3.20, -85.0),  # mile ~806
    ]
    res = optimize(geo, stations)
    assert res.stops, "expected at least one refuel"
    assert res.stops[0]["name"] == "B"


def test_greedy_jumps_to_first_cheaper():
    """Textbook greedy: from current station, if a cheaper station is within
    range, head there directly rather than topping off at the cheapest in
    the entire window."""
    geo = _line(-100.0, -85.0)  # ~806 mi
    stations = [
        _station("A", 5.00, -99.5),  # mile ~27, cur_price after first stop
        _station("B", 4.00, -94.0),  # mile ~322, cheaper than A
        _station("C", 3.50, -90.0),  # mile ~537, even cheaper, just past B + 500? no, 215mi from B
    ]
    res = optimize(geo, stations)
    names = [s["name"] for s in res.stops]
    # First leg picks A (only one in first window). Then from A, both B and C
    # are in the next window. Greedy says: jump to FIRST station strictly
    # cheaper than cur_price ($5) — that is B at $4. C is also cheaper but
    # we want to refuel as little as possible.
    assert names[:2] == ["A", "B"]


def test_no_reachable_station_raises():
    geo = _line(-120.0, -75.0)  # ~2400 mi
    # Only one station, sitting beyond the first 500 mi window.
    stations = [_station("Lonely", 3.0, -90.0)]  # mile ~1614
    with pytest.raises(ValueError):
        optimize(geo, stations)
