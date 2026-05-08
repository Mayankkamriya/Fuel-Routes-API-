from django.db import models


class FuelStation(models.Model):
    """A truck-stop fuel station with geocoded location and retail price."""

    opis_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=8, db_index=True)
    rack_id = models.IntegerField(null=True, blank=True)
    retail_price = models.FloatField()
    latitude = models.FloatField(null=True, blank=True, db_index=True)
    longitude = models.FloatField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["latitude", "longitude"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price:.3f}"
