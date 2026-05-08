from django.urls import path
from .views import RouteView, HealthView, CitiesView

urlpatterns = [
    path("route/", RouteView.as_view(), name="route"),
    path("health/", HealthView.as_view(), name="health"),
    path("cities/", CitiesView.as_view(), name="cities"),
]
