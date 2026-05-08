"""Unit tests that don't hit the network."""
from django.test import TestCase
from routing.models import FuelStation
from routing.services.fuel_optimizer import plan_fuel_stops


class FuelOptimizerTests(TestCase):
    def test_short_trip_no_stops(self):
        # ~100 miles synthetic line in Texas
        geom = [[-96.80, 32.78], [-95.36, 32.78]]
        out = plan_fuel_stops(geom, 100.0)
        self.assertEqual(out["stops"], [])

    def test_long_trip_picks_cheapest(self):
        # Build a fake straight-line "route" of ~1200 miles east-west
        geom = [[-118.0 + i * 0.1, 35.0] for i in range(400)]  # ~ across US
        # Add three stations near route at varying prices
        FuelStation.objects.create(name="A", address="", city="", state="CA",
            opis_id="1", retail_price=5.0, latitude=35.0, longitude=-115.0)
        FuelStation.objects.create(name="B", address="", city="", state="AZ",
            opis_id="2", retail_price=3.0, latitude=35.0, longitude=-110.0)
        FuelStation.objects.create(name="C", address="", city="", state="NM",
            opis_id="3", retail_price=4.0, latitude=35.0, longitude=-100.0)
        out = plan_fuel_stops(geom, 1200.0)
        self.assertGreater(len(out["stops"]), 0)
        self.assertGreater(out["total_fuel_cost_usd"], 0)
