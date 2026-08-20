from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    label = "notifications"
    verbose_name = "Notifications"

    def ready(self):
        # Deferred to ready(): signals.py imports models, which requires a
        # populated app registry. This is the documented place for it.
        from apps.notifications import signals  # noqa: PLC0415

        signals.register()
