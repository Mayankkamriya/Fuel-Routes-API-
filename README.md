# Fuel Route API

A Django + Django REST Framework service that finds the most cost‑efficient
fuel stops for a long‑haul route inside the continental United States.

Given a `start` and `finish` (address or `lat,lon`), it returns:

- the driving route geometry (single OpenRouteService call),
- the optimal sequence of refuel stops from a real US truck‑stop price dataset,
- per‑stop gallons + cost, the total fuel cost, and a Google Maps URL for the
  full plan.

It also ships an interactive single‑page frontend (Leaflet map + city
typeahead) at `/`, so you can drive the API from a browser without writing a
single line of JavaScript yourself.

**Vehicle profile:** 10 MPG, 500‑mile range per tank (configurable in
`fuel_route_api/settings.py`).

---

## What's in the box

```
Fuel-Routes-API-/
├── data/
│   ├── fuel-prices-for-be-assessment.csv   # full dataset, bundled
│   ├── fuel-prices-sample.csv              # tiny subset for smoke tests
│   └── us_cities.csv                       # offline geocoder + typeahead source
├── fuel_route_api/                         # Django project (settings, urls, wsgi)
├── routing/                                # the app
│   ├── management/commands/load_fuel_prices.py
│   ├── migrations/0001_initial.py
│   ├── services/
│   │   ├── ors_client.py                   # OpenRouteService client (cached)
│   │   └── optimizer.py                    # KDTree + greedy stop selection
│   ├── templates/index.html                # Leaflet-based SPA at /
│   ├── tests/test_optimizer.py             # pytest unit tests, no network/DB
│   ├── models.py · serializers.py · urls.py · views.py
├── postman/
│   ├── FuelRoute.postman_collection.json
│   └── FuelRouteAPI.postman_collection.json
├── .env.example
├── requirements.txt
└── manage.py
```

---

## Quick start

> Requires Python 3.10+ and an
> [OpenRouteService](https://openrouteservice.org/dev/#/signup) API key
> (free tier is fine).

### macOS / Linux

```bash
cd Fuel-Routes-API-

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                                  # then edit .env and set ORS_API_KEY=...

python manage.py migrate
python manage.py load_fuel_prices                     # loads ~8,151 stations from the bundled CSV
python manage.py runserver
```

### Windows (PowerShell)

```powershell
cd Fuel-Routes-API-

python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env                           # then edit .env and set ORS_API_KEY=...

python manage.py migrate
python manage.py load_fuel_prices
python manage.py runserver
```

Open <http://127.0.0.1:8000/> — you'll see the interactive route planner with
a city‑autocomplete form and a live Leaflet map. The first request for a given
route will hit OpenRouteService; subsequent identical requests are served from
the in‑process cache.

---

## Endpoints

### `GET /` — interactive web UI
Single‑page Leaflet app. Pick a `start` and `finish` from the city typeahead
(or paste `"lat,lon"`), submit, and watch the optimal route + refuel stops
render on the map. Reads `/api/cities/` for the typeahead source and
`POST /api/route/` for the plan.

### `GET /api/health/`
```json
{
  "ok": true,
  "stations": 8151,
  "geocoded": 8151,
  "ors_configured": true
}
```

### `GET /api/cities/`
Returns the deduplicated CONUS city list used by the frontend typeahead. Cached
in‑process per worker, so the CSV is parsed once.

```json
{
  "cities": [
    { "label": "Abbeville, AL", "lat": 31.57, "lon": -85.25, "state": "AL" },
    "..."
  ]
}
```

### `POST /api/route/`
**Body:** `{ "start": "...", "finish": "..." }`. Either field accepts a
free‑form US address *or* a `"lat,lon"` pair.

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start":"Los Angeles, CA","finish":"New York, NY"}'
```

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start":"34.05,-118.24","finish":"40.71,-74.00"}'
```

**Response (truncated):**
```json
{
  "start":  { "input": "Los Angeles, CA", "lat": 34.05, "lon": -118.24 },
  "finish": { "input": "New York, NY",     "lat": 40.71, "lon":  -74.00 },
  "vehicle": { "mpg": 10.0, "range_miles": 500.0 },
  "total_distance_mi": 2789.4,
  "total_fuel_cost": 853.98,
  "stops": [
    {
      "name": "PILOT TRAVEL CENTER #...",
      "address": "...",
      "city": "...",
      "state": "AZ",
      "opis_id": 12345,
      "latitude": 35.20,
      "longitude": -111.65,
      "price_per_gallon": 3.099,
      "route_mile": 372.10,
      "gallons": 37.21,
      "cost": 115.32
    }
  ],
  "polyline": [[34.05, -118.24], "..."],
  "bbox": [33.74, -118.41, 41.05, -73.97],
  "map_url": "https://www.google.com/maps/dir/34.05,-118.24/.../40.71,-74.00"
}
```

`polyline` is `[[lat, lon], ...]` ready to drop into Leaflet/Mapbox. `bbox`
is `[min_lat, min_lon, max_lat, max_lon]`. `map_url` opens the full plan
(start → every refuel stop → finish) in Google Maps.

---

## Error responses

Every failure mode returns a clear JSON payload, usually with a `hint`:

| Status | `error`                | When |
|-------:|------------------------|------|
| 400 | `invalid_request`         | Missing `start` or `finish` |
| 422 | `geocode_failed`          | ORS could not geocode an address |
| 422 | `no_reachable_station`    | No station within tank range of the route |
| 500 | `internal_error`          | Unexpected exception (safety net — never bubbles a Django 500) |
| 502 | `directions_failed`       | ORS Directions API error |
| 503 | `ors_not_configured`      | `ORS_API_KEY` is empty |
| 503 | `no_fuel_data`            | DB is empty — run `load_fuel_prices` |

---

## How the optimizer works

1. **One** ORS Directions call returns the full driving polyline (cached for
   6 hours, keyed on the rounded coordinate pair).
2. A bbox prefilter narrows the `FuelStation` queryset to stations near the
   route corridor (~80 mi pad).
3. A KDTree projects every candidate station onto its nearest polyline vertex
   in a local equirectangular frame — one `O(N log V)` query for the lot.
4. Stations within a 30‑mile corridor (75 mi fallback) are kept and sorted by
   route mile.
5. A textbook gas‑station greedy walks the route:
   - From the current station, look ahead within `range`.
   - If a strictly cheaper station is reachable, buy just enough fuel to
     reach the *first* such station.
   - Otherwise, fill up at the cheapest reachable station.

   This is provably cost‑optimal for the deterministic‑prices case.
6. The final tank is sized exactly to reach the destination — no waste.

API cost: **1 directions call + at most 2 geocoding calls per request**, and
both are cached.

---

## Tests

The optimizer has no‑network, no‑DB unit tests (pure dataclasses + numpy):

```bash
python manage.py test          # via Django's runner
# OR, equivalently:
pytest                         # pytest reads routing/tests/test_optimizer.py
```

What's covered:
- short routes that fit in one tank → zero refuel stops
- first refuel picks the cheapest station in the first tank window
- greedy correctly jumps to the *first strictly cheaper* station, not the
  globally cheapest in the window
- unreachable‑station edge case raises `ValueError` (mapped to 422)

---

## Configuration (`.env`)

```
ORS_API_KEY=your_openrouteservice_api_key_here
DJANGO_SECRET_KEY=dev-secret-change-me
DJANGO_DEBUG=True
```

Other knobs live in `fuel_route_api/settings.py`:

- `VEHICLE_MPG` (default `10.0`)
- `VEHICLE_RANGE_MILES` (default `500.0`)
- `CACHES` — in‑process LocMem with a 6‑hour TTL for ORS responses. Swap for
  `django_redis` in production if multiple workers need to share the cache.

---

## Data

`data/fuel-prices-for-be-assessment.csv` is the bundled assessment dataset
(8,151 rows). Geocoding is **offline** — done against the bundled
`us_cities.csv` — so loading is instant and uses zero ORS quota:

- exact `(city, state)` matches use the city centroid,
- unmatched cities fall back to the **state centroid** (logged as `fallback`),
- rows with an unknown state are skipped.

```bash
python manage.py load_fuel_prices                   # full dataset
python manage.py load_fuel_prices --limit 500       # quick smoke test
python manage.py load_fuel_prices --csv data/fuel-prices-sample.csv
```

The command wipes `FuelStation` first, so it's idempotent — re‑run it any
time the CSV changes.

---

## Postman

`postman/` ships two ready‑to‑import collections covering health, cities, and
route requests for both address and `lat,lon` inputs. Import either file in
Postman and hit Send.