"""Tests for the epic-activity opt-out.

A notification kind is only safe to send if a recipient can turn it off, and if
"unsubscribe from everything" really means everything. These cover both, plus the
structural guard that stops a future kind being added without a matching flag.
"""

from django.test import TestCase
from django.urls import reverse

from apps.notifications.forms import NotificationPreferenceForm
from apps.notifications.models import NotificationKind, NotificationPreference
from apps.users.factories import UserFactory


class EpicActivityPreferenceTest(TestCase):
    """The flag itself: default, mapping, and opt-out behaviour."""

    @classmethod
    def setUpTestData(cls):
        cls.user = UserFactory()

    def test_defaults_to_enabled(self):
        """An absent or fresh row means "everything on", matching the other kinds."""
        preference = NotificationPreference.objects.for_user(self.user)

        self.assertTrue(preference.notify_on_epic_activity)
        self.assertTrue(preference.is_enabled_for(NotificationKind.EPIC_ACTIVITY))

    def test_opting_out_is_respected(self):
        preference = NotificationPreference.objects.for_user(self.user)
        preference.notify_on_epic_activity = False
        preference.save(update_fields=["notify_on_epic_activity"])

        self.assertFalse(preference.is_enabled_for(NotificationKind.EPIC_ACTIVITY))

    def test_disable_all_covers_epic_activity(self):
        """The unsubscribe endpoint calls disable_all; a kind it misses keeps mailing."""
        preference = NotificationPreference.objects.for_user(self.user)

        preference.disable_all()
        preference.refresh_from_db()

        self.assertFalse(preference.notify_on_epic_activity)

    def test_every_kind_maps_to_a_real_field(self):
        """Guards the next kind added: an unmapped or misspelled field fails here.

        is_enabled_for falls back to True for an unknown attribute, so a typo in
        the mapping would silently ignore the user's opt-out rather than error.
        """
        preference = NotificationPreference.objects.for_user(self.user)

        for kind in NotificationKind.values:
            field = NotificationKind.preference_field(kind)
            self.assertTrue(
                hasattr(preference, field),
                f"{kind} maps to {field!r}, which is not a field on NotificationPreference",
            )

    def test_disable_all_turns_off_every_kind(self):
        """Stronger than the single-field check: no kind may survive an unsubscribe."""
        preference = NotificationPreference.objects.for_user(self.user)

        preference.disable_all()
        preference.refresh_from_db()

        for kind in NotificationKind.values:
            self.assertFalse(
                preference.is_enabled_for(kind),
                f"{kind} is still enabled after disable_all()",
            )


class EpicActivityPreferenceFormTest(TestCase):
    """The preferences page must expose the new toggle, not just store it."""

    @classmethod
    def setUpTestData(cls):
        cls.user = UserFactory()

    def test_form_exposes_the_toggle(self):
        self.assertIn("notify_on_epic_activity", NotificationPreferenceForm().fields)

    def test_form_covers_every_kind(self):
        """A flag with no form field cannot be turned off by the person receiving it."""
        fields = set(NotificationPreferenceForm().fields)

        for kind in NotificationKind.values:
            self.assertIn(NotificationKind.preference_field(kind), fields)

    def test_page_saves_an_opt_out(self):
        self.client.force_login(self.user)
        url = reverse("notifications:preferences")

        response = self.client.post(
            url,
            {"notify_on_assignment": "on", "notify_on_membership": "on", "notify_on_epic_inactivity": "on"},
        )

        self.assertEqual(response.status_code, 302)
        preference = NotificationPreference.objects.for_user(self.user)
        self.assertFalse(preference.notify_on_epic_activity)
        # Unchecked only the new one; the others must be untouched.
        self.assertTrue(preference.notify_on_assignment)
