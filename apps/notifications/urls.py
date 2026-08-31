from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("preferences/", views.preferences, name="preferences"),
    # Unauthenticated by design — see views.unsubscribe. The uuid converter
    # rejects malformed tokens with a 404 before any query runs.
    path("unsubscribe/<uuid:token>/", views.unsubscribe, name="unsubscribe"),
]
