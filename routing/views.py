from __future__ import annotations
import logging
from dataclasses import asdict
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .models import FuelStation
from .serializers import RouteRequestSerializer
from .services.ors_client import ORSClient, ORSError
from .services.optimizer import optimize

log = logging.getLogger(__name__)


def _parse_point(s: str, ors: ORSClient) -> tuple[float, float]:
    """Accept "lat,lon" or a free-form address (geocoded via ORS)."""
    s = s.strip()
    if "," in s:
        a, b = [t.strip() for t in s.split(",", 1)]
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

        return Response(
            {
                "start": {"input": start_text, "lat": start[0], "lon": start[1]},
                "finish": {"input": finish_text, "lat": finish[0], "lon": finish[1]},
                **asdict(result),
            }
        )


class HealthView(APIView):
    def get(self, request):
        total = FuelStation.objects.count()
        geocoded = FuelStation.objects.exclude(latitude__isnull=True).count()
        return Response({
            "ok": True,
            "stations": total,
            "geocoded": geocoded,
            "ors_configured": bool(__import__("django").conf.settings.ORS_API_KEY),
        })


from django.shortcuts import render
from django.conf import settings as _s

def home(request):
    total = FuelStation.objects.count()
    return render(request, "index.html", {
        "stations": total,
        "ors_configured": bool(_s.ORS_API_KEY),
        "mpg": _s.VEHICLE_MPG,
        "range_mi": int(_s.VEHICLE_RANGE_MILES),
    })
