from __future__ import annotations

import logging
from dataclasses import asdict

from django.conf import settings
from django.shortcuts import render
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FuelStation
from .serializers import RouteRequestSerializer
from .services.optimizer import optimize
from .services.ors_client import ORSClient, ORSError

log = logging.getLogger(__name__)


def _parse_point(s: str, ors: ORSClient) -> tuple[float, float]:
    """Accept "lat,lon" or a free-form address (geocoded via ORS, cached)."""
    s = s.strip()
    if "," in s:
        a, b = (t.strip() for t in s.split(",", 1))
        try:
            return float(a), float(b)
        except ValueError:
            pass
    return ors.geocode(s)


class RouteView(APIView):
    """POST /api/route/  body: {"start": "...", "finish": "..."}"""

    def post(self, request):
        ser = RouteRequestSerializer(data=request.data)
        if not ser.is_valid():
            return Response(
                {"error": "invalid_request", "details": ser.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start_text = ser.validated_data["start"]
        finish_text = ser.validated_data["finish"]

        station_count = FuelStation.objects.exclude(latitude__isnull=True).count()
        if station_count == 0:
            return Response(
                {
                    "error": "no_fuel_data",
                    "hint": "Run `python manage.py load_fuel_prices` first.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            ors = ORSClient()
        except ORSError as e:
            return Response(
                {"error": "ors_not_configured", "detail": str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            start = _parse_point(start_text, ors)
            finish = _parse_point(finish_text, ors)
        except ORSError as e:
            return Response(
                {"error": "geocode_failed", "detail": str(e)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        try:
            geojson = ors.directions(start, finish)
        except ORSError as e:
            return Response(
                {"error": "directions_failed", "detail": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            stations = FuelStation.objects.exclude(latitude__isnull=True)
            result = optimize(geojson, stations)
        except ValueError as e:
            return Response(
                {
                    "error": "no_reachable_station",
                    "detail": str(e),
                    "hint": (
                        "The 500-mile range can't reach any station from this point. "
                        "Try a route that stays inside the contiguous US, or reload "
                        "the dataset with `python manage.py load_fuel_prices`."
                    ),
                    "stations_loaded": station_count,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except Exception as e:  # safety net — never bubble a 500 to the client
            log.exception("optimize failed")
            return Response(
                {"error": "internal_error", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            "start": {"input": start_text, "lat": start[0], "lon": start[1]},
            "finish": {"input": finish_text, "lat": finish[0], "lon": finish[1]},
            "vehicle": {
                "mpg": settings.VEHICLE_MPG,
                "range_miles": settings.VEHICLE_RANGE_MILES,
            },
            **asdict(result),
        })


class HealthView(APIView):
    def get(self, request):
        total = FuelStation.objects.count()
        geocoded = FuelStation.objects.exclude(latitude__isnull=True).count()
        return Response({
            "ok": True,
            "stations": total,
            "geocoded": geocoded,
            "ors_configured": bool(settings.ORS_API_KEY),
        })


class CitiesView(APIView):
    """GET /api/cities/ – CONUS cities for the frontend typeahead.

    Cached at module level so the CSV is parsed once per worker process.
    """
    _cache: list[dict] | None = None

    def get(self, request):
        if CitiesView._cache is None:
            CitiesView._cache = _load_cities()
        return Response({"cities": CitiesView._cache})


def _load_cities() -> list[dict]:
    """Read us_cities.csv → [{label, lat, lon, state}], CONUS only, deduped."""
    import csv
    path = settings.FUEL_DATA_DIR / "us_cities.csv"
    non_conus = {"AK", "HI", "PR", "VI", "GU", "AS", "MP"}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            state = (row.get("STATE_CODE") or "").strip()
            city = (row.get("CITY") or "").strip()
            if not state or not city or state in non_conus:
                continue
            key = (city.lower(), state)
            if key in seen:
                continue
            seen.add(key)
            try:
                lat = float(row["LATITUDE"])
                lon = float(row["LONGITUDE"])
            except (KeyError, ValueError):
                continue
            out.append({
                "label": f"{city}, {state}",
                "lat": lat,
                "lon": lon,
                "state": state,
            })
    out.sort(key=lambda c: c["label"])
    return out


def home(request):
    total = FuelStation.objects.count()
    return render(request, "index.html", {
        "stations": total,
        "ors_configured": bool(settings.ORS_API_KEY),
        "mpg": settings.VEHICLE_MPG,
        "range_mi": int(settings.VEHICLE_RANGE_MILES),
    })
