"""Load fuel-prices CSV into FuelStation, geocoding via bundled offline US-cities DB.

No external API calls — uses data/us_cities.csv (city, state -> lat/lon)
so every one of the ~8,151 stations gets coordinates in seconds. If a city
isn't found, we fall back to the state centroid and record a warning.

Usage:
    python manage.py load_fuel_prices
    python manage.py load_fuel_prices --csv path/to/file.csv
    python manage.py load_fuel_prices --limit 500
"""
from __future__ import annotations
import csv
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import transaction

from routing.models import FuelStation


def _load_city_index(path: Path) -> tuple[dict, dict]:
    """Return (city_state -> (lat,lon), state -> (lat,lon) centroid)."""
    if not path.exists():
        raise CommandError(
            f"Missing city DB: {path}. It should ship with the project under data/us_cities.csv"
        )
    by_city: dict[tuple[str, str], tuple[float, float]] = {}
    state_pts: dict[str, list[tuple[float, float]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                lat = float(row["LATITUDE"])
                lon = float(row["LONGITUDE"])
            except (KeyError, ValueError):
                continue
            city = row["CITY"].strip().upper()
            state = row["STATE_CODE"].strip().upper()
            by_city.setdefault((city, state), (lat, lon))
            state_pts[state].append((lat, lon))
    state_centroid = {
        s: (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        for s, pts in state_pts.items()
    }
    return by_city, state_centroid


class Command(BaseCommand):
    help = "Load fuel prices CSV into FuelStation using offline city geocoding."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            default=str(settings.FUEL_DATA_DIR / "fuel-prices-for-be-assessment.csv"),
        )
        parser.add_argument("--cities", default=str(settings.FUEL_DATA_DIR / "us_cities.csv"))
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, **opts):
        path = Path(opts["csv"])
        if not path.exists():
            raise CommandError(f"CSV not found: {path}")

        by_city, state_centroid = _load_city_index(Path(opts["cities"]))
        self.stdout.write(
            f"City DB loaded: {len(by_city)} city/state pairs, {len(state_centroid)} states."
        )

        FuelStation.objects.all().delete()
        rows: list[FuelStation] = []
        seen: set[tuple[int, float]] = set()
        matched = fallback = skipped = 0
        missing_states: set[str] = set()

        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            required = {"OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Retail Price"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise CommandError(f"CSV missing required columns: {sorted(missing)}")

            for i, row in enumerate(reader):
                if opts["limit"] and i >= opts["limit"]:
                    break
                try:
                    opis_id = int(row["OPIS Truckstop ID"])
                    price = float(row["Retail Price"])
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                key = (opis_id, round(price, 4))
                if key in seen:
                    continue
                seen.add(key)

                city = row["City"].strip()
                state = row["State"].strip().upper()
                lookup = (city.upper(), state)
                coord = by_city.get(lookup)
                if coord:
                    matched += 1
                else:
                    coord = state_centroid.get(state)
                    if coord:
                        fallback += 1
                    else:
                        missing_states.add(state)
                        skipped += 1
                        continue

                rows.append(
                    FuelStation(
                        opis_id=opis_id,
                        name=row["Truckstop Name"].strip(),
                        address=row["Address"].strip(),
                        city=city,
                        state=state,
                        rack_id=int(row["Rack ID"]) if row.get("Rack ID", "").strip().isdigit() else None,
                        retail_price=price,
                        latitude=coord[0],
                        longitude=coord[1],
                    )
                )

        with transaction.atomic():
            FuelStation.objects.bulk_create(rows, batch_size=2000)

        self.stdout.write(self.style.SUCCESS(
            f"Loaded {len(rows)} stations | exact city match: {matched} | "
            f"state-centroid fallback: {fallback} | skipped: {skipped}"
        ))
        if missing_states:
            self.stderr.write(f"Unknown states (skipped): {sorted(missing_states)}")
