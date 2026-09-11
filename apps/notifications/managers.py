from django.db import models
from django.utils import timezone


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


class NotificationDeliveryManager(models.Manager):
    """Manager for NotificationDelivery, owning the claim/confirm pair."""

    def claim(self, *, idempotency_key: str, recipient, kind: str):
        """Take ownership of sending one notification to one person.

        Returns the row to send against, or None if this notification has
        already been delivered and must not be sent again.

        The claim is a single get_or_create against a unique column, so the
        database settles the race: if two workers reach the same pair at once,
        one insert wins and the other reads back the existing row. Nothing is
        locked and nothing is read first, so there is nothing to dead-lock on.

        A row that exists but has no ``sent_at`` is *not* treated as delivered.
        That state means an earlier attempt claimed the work and then failed
        before the message left - most likely the transport error that triggered
        this retry. Declining there would turn one failed send into a permanently
        lost notification, which is worse than the duplicate this guards against.

        Returns:
            The NotificationDelivery to confirm once the message is sent, or None
            when an earlier attempt already sent it.
        """
        delivery, created = self.get_or_create(
            idempotency_key=idempotency_key,
            defaults={"recipient": recipient, "kind": kind},
        )

        if not created and delivery.sent_at is not None:
            return None

        return delivery

    def confirm(self, delivery) -> None:
        """Mark a claimed delivery as actually sent.

        Called only after the message reached the mail backend, so ``sent_at``
        means "this left" rather than "this was attempted". update_fields keeps
        it to the one column, which matters because the row is otherwise
        immutable.
        """
        delivery.sent_at = timezone.now()
        delivery.save(update_fields=["sent_at", "updated_at"])

    def release(self, delivery) -> None:
        """Discard an unsent claim so a later attempt can retry it.

        Used when the send is declined by a guard rather than failing - the
        recipient opted out, has no address, or notifications are off globally.
        Those are not deliveries, and leaving a pending row behind would make a
        later legitimate send look like a duplicate that had already happened.
        """
        if delivery.sent_at is None:
            delivery.delete()
