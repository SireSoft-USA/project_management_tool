from django.apps import AppConfig


class IssuesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.issues"
    label = "issues"

    def ready(self):
        # Deferred to ready(): signals.py imports models, which requires a
        # populated app registry. This is the documented place for it.
        from apps.issues import signals  # noqa: PLC0415

        signals.register()
