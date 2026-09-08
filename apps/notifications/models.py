import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.notifications.managers import NotificationPreferenceManager
from apps.utils.models import BaseModel


class NotificationPreference(BaseModel):
    """Per-user opt-out flags for notification email.

    Rows are created on demand (see ``objects.for_user``) rather than backfilled,
    so existing users need no data migration: absent row == all defaults on.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("User"),
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )
    notify_on_assignment = models.BooleanField(
        _("Notify on assignment"),
        default=True,
        help_text=_("Email me when someone assigns an issue to me."),
    )
    notify_on_membership = models.BooleanField(
        _("Notify on workspace membership"),
        default=True,
        help_text=_("Email me when I am added to a workspace."),
    )
    notify_on_epic_inactivity = models.BooleanField(
        _("Notify on epic inactivity"),
        default=True,
        help_text=_("Email me when an epic assigned to me has had no story activity for a week."),
    )
    notify_on_epic_activity = models.BooleanField(
        _("Notify on epic activity"),
        default=True,
        help_text=_("Email me when a story, bug or chore under an epic I own is created or edited."),
    )
    # Unguessable, per-user, and stable: it goes in the List-Unsubscribe header and
    # the footer link, both of which must work without an authenticated session.
    unsubscribe_token = models.UUIDField(
        _("Unsubscribe token"),
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )

    objects = NotificationPreferenceManager()

    class Meta:
        verbose_name = _("Notification Preference")
        verbose_name_plural = _("Notification Preferences")

    def __str__(self):
        return f"Notification preferences for {self.user}"

    def is_enabled_for(self, kind: str) -> bool:
        """Return whether ``kind`` (a NotificationKind value) is opted in."""
        return getattr(self, NotificationKind.preference_field(kind), True)

    def disable_all(self) -> None:
        """Opt out of every notification type. Used by the unsubscribe endpoint."""
        self.notify_on_assignment = False
        self.notify_on_membership = False
        self.notify_on_epic_inactivity = False
        self.notify_on_epic_activity = False
        self.save(
            update_fields=[
                "notify_on_assignment",
                "notify_on_membership",
                "notify_on_epic_inactivity",
                "notify_on_epic_activity",
                "updated_at",
            ]
        )


class NotificationKind(models.TextChoices):
    """The notification types the app can send.

    Each kind maps to exactly one boolean on NotificationPreference. Keeping that
    mapping here means adding a kind is a single-file change, and an unmapped kind
    fails loudly instead of silently emailing an opted-out user.
    """

    ASSIGNMENT = "assignment", _("Issue assigned to me")
    MEMBERSHIP = "membership", _("Added to a workspace")
    EPIC_INACTIVITY = "epic_inactivity", _("Epic with no recent activity")
    EPIC_ACTIVITY = "epic_activity", _("Activity on an epic I own")

    @staticmethod
    def preference_field(kind: str) -> str:
        try:
            return _PREFERENCE_FIELDS[kind]
        except KeyError:
            raise ValueError(f"Unknown notification kind: {kind!r}") from None


_PREFERENCE_FIELDS = {
    NotificationKind.ASSIGNMENT: "notify_on_assignment",
    NotificationKind.MEMBERSHIP: "notify_on_membership",
    NotificationKind.EPIC_INACTIVITY: "notify_on_epic_inactivity",
    NotificationKind.EPIC_ACTIVITY: "notify_on_epic_activity",
}
