import hashlib
import json
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.notifications.managers import NotificationDeliveryManager, NotificationPreferenceManager
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


class NotificationDelivery(BaseModel):
    """A record that one notification reached one person, so a retry does not resend it.

    Celery retries the whole task. That is right for a transport failure - a mail
    server that was briefly down should not cost somebody their notification -
    but it means a task that failed *partway* runs again from the top. With one
    email per recipient, a failure on the third of five re-sends to the first
    two. The people who already got the mail get it twice.

    So each (event, recipient) pair is claimed here before the message is built.
    A claim that already exists means the mail went out on an earlier attempt and
    this one skips it. The unique constraint is what makes that safe under
    concurrency: two workers racing on the same pair, only one insert survives,
    and the loser declines rather than duplicating.

    Rows are a delivery log, not queue state. They are written once and never
    updated, which is what lets ``sent_at`` be trusted as "this actually left".
    """

    # Identifies the event *and* the recipient. Built by build_idempotency_key()
    # rather than assembled at call sites, so every caller hashes the same fields
    # in the same order - two spellings of "the same event" would defeat the
    # whole mechanism.
    idempotency_key = models.CharField(
        _("Idempotency Key"),
        max_length=64,
        unique=True,
        editable=False,
        help_text=_("Hash identifying one notification event for one recipient."),
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Recipient"),
        on_delete=models.CASCADE,
        related_name="notification_deliveries",
    )
    kind = models.CharField(
        _("Kind"),
        max_length=32,
        help_text=_("A NotificationKind value. Stored as text so an old row survives a renamed kind."),
    )
    # NULL until the send is confirmed. A row with no timestamp means a claim was
    # taken but the send did not complete - which is exactly the state a retry
    # needs to see in order to try again rather than skip.
    sent_at = models.DateTimeField(_("Sent At"), null=True, blank=True, db_index=True)

    objects = NotificationDeliveryManager()

    class Meta:
        indexes = [
            models.Index(fields=["recipient", "kind"]),
        ]
        ordering = ["-created_at"]
        verbose_name = _("Notification Delivery")
        verbose_name_plural = _("Notification Deliveries")

    def __str__(self):
        state = "sent" if self.sent_at else "pending"
        return f"{self.kind} to {self.recipient} ({state})"


def build_idempotency_key(*, kind: str, recipient_id: int, **parts) -> str:
    """Return the stable key identifying one notification event for one person.

    Two attempts at the same notification must produce the same key, and two
    genuinely different notifications must not. So the inputs are sorted and
    JSON-encoded before hashing: a dict that happens to iterate in a different
    order, or an int where a string was used last time, would otherwise look like
    a different event and let a duplicate through.

    Hashed rather than stored raw because the change list is part of the identity
    - it is what distinguishes "set status to Done" from "set status to Blocked"
    on the same issue - and is far too long for an indexed column.

    Args:
        kind: A NotificationKind value.
        recipient_id: The user being notified. Part of the key, so fanning one
            event out to five people yields five distinct claims.
        **parts: Whatever else identifies the event - issue and epic ids, the
            actor, the rendered changes.
    """
    payload = {"kind": str(kind), "recipient_id": recipient_id, **parts}
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
