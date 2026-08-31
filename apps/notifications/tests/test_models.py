"""Tests for NotificationPreference and its manager (Step 1.2)."""

import uuid

from django.db import IntegrityError
from django.test import TestCase

from apps.notifications.models import NotificationKind, NotificationPreference
from apps.users.factories import UserFactory


class NotificationPreferenceDefaultsTests(TestCase):
    """Defaults decide what happens to users who never touch their settings."""

    def test_all_notifications_default_to_on(self):
        """Opt-out, not opt-in: a new user receives notifications."""
        preference = NotificationPreference.objects.create(user=UserFactory())
        self.assertTrue(preference.notify_on_assignment)
        self.assertTrue(preference.notify_on_membership)

    def test_unsubscribe_token_is_generated(self):
        preference = NotificationPreference.objects.create(user=UserFactory())
        self.assertIsInstance(preference.unsubscribe_token, uuid.UUID)

    def test_unsubscribe_tokens_are_unique_per_user(self):
        """A shared token would let one user unsubscribe another."""
        first = NotificationPreference.objects.create(user=UserFactory())
        second = NotificationPreference.objects.create(user=UserFactory())
        self.assertNotEqual(first.unsubscribe_token, second.unsubscribe_token)

    def test_one_preference_row_per_user(self):
        user = UserFactory()
        NotificationPreference.objects.create(user=user)
        with self.assertRaises(IntegrityError):
            NotificationPreference.objects.create(user=user)

    def test_str_is_readable(self):
        preference = NotificationPreference.objects.create(user=UserFactory(email="a@b.com"))
        self.assertIn("a@b.com", str(preference))


class ForUserManagerTests(TestCase):
    """for_user() is how every caller reaches preferences."""

    def test_creates_row_on_first_access(self):
        """No backfill migration is needed because rows are made on demand."""
        user = UserFactory()
        self.assertFalse(NotificationPreference.objects.filter(user=user).exists())

        preference = NotificationPreference.objects.for_user(user)

        self.assertTrue(preference.notify_on_assignment)
        self.assertEqual(NotificationPreference.objects.filter(user=user).count(), 1)

    def test_returns_existing_row_without_duplicating(self):
        user = UserFactory()
        first = NotificationPreference.objects.for_user(user)
        second = NotificationPreference.objects.for_user(user)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(NotificationPreference.objects.filter(user=user).count(), 1)

    def test_preserves_existing_opt_out(self):
        """Regression guard: for_user() must never reset a user's choice."""
        user = UserFactory()
        preference = NotificationPreference.objects.for_user(user)
        preference.notify_on_assignment = False
        preference.save()

        self.assertFalse(NotificationPreference.objects.for_user(user).notify_on_assignment)


class IsEnabledForTests(TestCase):
    """The single lookup the email layer uses to honour opt-outs."""

    def setUp(self):
        self.preference = NotificationPreference.objects.create(user=UserFactory())

    def test_enabled_by_default(self):
        self.assertTrue(self.preference.is_enabled_for(NotificationKind.ASSIGNMENT))
        self.assertTrue(self.preference.is_enabled_for(NotificationKind.MEMBERSHIP))

    def test_reflects_assignment_opt_out(self):
        self.preference.notify_on_assignment = False
        self.assertFalse(self.preference.is_enabled_for(NotificationKind.ASSIGNMENT))

    def test_opting_out_of_one_kind_leaves_others_on(self):
        """Kinds must be independent — muting assignments must not mute invites."""
        self.preference.notify_on_assignment = False
        self.assertTrue(self.preference.is_enabled_for(NotificationKind.MEMBERSHIP))

    def test_unknown_kind_raises_rather_than_defaulting_to_send(self):
        """Failing loudly beats silently emailing someone who opted out."""
        with self.assertRaises(ValueError):
            self.preference.is_enabled_for("no_such_kind")


class DisableAllTests(TestCase):
    """Used by the unsubscribe endpoint."""

    def test_turns_every_kind_off(self):
        preference = NotificationPreference.objects.create(user=UserFactory())
        preference.disable_all()
        self.assertFalse(preference.notify_on_assignment)
        self.assertFalse(preference.notify_on_membership)

    def test_persists_to_the_database(self):
        preference = NotificationPreference.objects.create(user=UserFactory())
        preference.disable_all()
        preference.refresh_from_db()
        self.assertFalse(preference.notify_on_assignment)

    def test_is_idempotent(self):
        preference = NotificationPreference.objects.create(user=UserFactory())
        preference.disable_all()
        preference.disable_all()
        self.assertFalse(preference.notify_on_assignment)


class NotificationKindTests(TestCase):
    """Every kind must map to a real field, or opt-outs silently fail."""

    def test_every_kind_maps_to_an_existing_model_field(self):
        field_names = {f.name for f in NotificationPreference._meta.get_fields()}
        for kind in NotificationKind.values:
            with self.subTest(kind=kind):
                self.assertIn(NotificationKind.preference_field(kind), field_names)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            NotificationKind.preference_field("nope")
