from types import SimpleNamespace
from routing.services.optimizer import optimize


def _line(start_lon, end_lon, lat=39.0, n=200):
    """Synthetic east-west line."""
    coords = []
    for i in range(n):
        t = i / (n - 1)
        coords.append([start_lon + (end_lon - start_lon) * t, lat])
    return {
        "features": [{
            "geometry": {"coordinates": coords},
            "properties": {"summary": {"distance": 1200.0, "duration": 0}},
        }]
    }


def test_short_route_no_stop():
    geo = _line(-100.0, -99.0)
    geo["features"][0]["properties"]["summary"]["distance"] = 100.0
    res = optimize(geo, [])
    assert res.stops == []


def test_picks_cheapest_in_far_half():
    geo = _line(-100.0, -90.0)  # ~864 mi at lat=39
    stations = [
        SimpleNamespace(opis_id=1, name="A", address="", city="", state="", retail_price=4.0, latitude=39.0, longitude=-99.0),
        SimpleNamespace(opis_id=2, name="B", address="", city="", state="", retail_price=3.0, latitude=39.0, longitude=-95.0),
        SimpleNamespace(opis_id=3, name="C", address="", city="", state="", retail_price=3.5, latitude=39.0, longitude=-93.0),
    ]
    res = optimize(geo, stations)
    assert any(s["name"] == "B" for s in res.stops)
