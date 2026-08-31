from django.db import models


class NotificationPreferenceQuerySet(models.QuerySet):
    """Custom QuerySet for NotificationPreference."""

    def opted_in_to(self, field_name: str) -> NotificationPreferenceQuerySet:
        """Filter to preferences with the given boolean flag enabled."""
        return self.filter(**{field_name: True})


class NotificationPreferenceManager(models.Manager.from_queryset(NotificationPreferenceQuerySet)):
    """Manager for NotificationPreference."""

    def for_user(self, user):
        """Return this user's preferences, creating defaults on first access.

        Preferences are created lazily so that existing users need no backfill
        migration and new users need no signal — an absent row simply means
        "everything enabled", which is the default we want.
        """
        preference, _created = self.get_or_create(user=user)
        return preference
