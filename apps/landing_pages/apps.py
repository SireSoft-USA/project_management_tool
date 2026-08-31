from django.apps import AppConfig


class LandingPagesConfig(AppConfig):
    name = "apps.landing_pages"
    verbose_name = "Landing Pages"

    def ready(self):
        # Importing the module runs its @register decorators. This import cannot move
        # to the top of the file: checks.py imports django.contrib.sites.models, and
        # importing a model before the app registry is populated raises
        # AppRegistryNotReady. ready() is the documented place for this.
        from apps.landing_pages import checks  # noqa: F401, PLC0415  (registers system checks)
