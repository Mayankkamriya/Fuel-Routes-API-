from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="FuelStation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("opis_id", models.IntegerField(db_index=True)),
                ("name", models.CharField(max_length=255)),
                ("address", models.CharField(max_length=255)),
                ("city", models.CharField(max_length=128)),
                ("state", models.CharField(max_length=8, db_index=True)),
                ("rack_id", models.IntegerField(blank=True, null=True)),
                ("retail_price", models.FloatField()),
                ("latitude", models.FloatField(blank=True, db_index=True, null=True)),
                ("longitude", models.FloatField(blank=True, db_index=True, null=True)),
            ],
            options={"indexes": [models.Index(fields=["latitude", "longitude"], name="routing_fue_latitud_idx")]},
        ),
    ]
