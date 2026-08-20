"""Tests for the unsubscribe endpoint.

This view is deliberately unauthenticated, so its security properties matter:
a token must only affect its own owner, and a GET must never mutate anything.
"""

import uuid

from django.test import TestCase
from django.urls import reverse

from apps.notifications.models import NotificationPreference
from apps.users.factories import UserFactory


class UnsubscribeViewTests(TestCase):
    def setUp(self):
        self.user = UserFactory(email="member@siresoft.com")
        self.preference = NotificationPreference.objects.for_user(self.user)
        self.url = reverse("notifications:unsubscribe", kwargs={"token": self.preference.unsubscribe_token})

    def test_get_shows_confirmation_without_login(self):
        """Recipients must be able to opt out without remembering a password."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_get_does_not_change_anything(self):
        """Mail clients and scanners prefetch links; a mutating GET would
        unsubscribe people who never clicked."""
        self.client.get(self.url)
        self.preference.refresh_from_db()
        self.assertTrue(self.preference.notify_on_assignment)
        self.assertTrue(self.preference.notify_on_membership)

    def test_post_disables_all_notifications(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)

        self.preference.refresh_from_db()
        self.assertFalse(self.preference.notify_on_assignment)
        self.assertFalse(self.preference.notify_on_membership)

    def test_post_is_idempotent(self):
        """One-click unsubscribe may be delivered more than once."""
        self.client.post(self.url)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)

        self.preference.refresh_from_db()
        self.assertFalse(self.preference.notify_on_assignment)

    def test_unknown_token_returns_404(self):
        url = reverse("notifications:unsubscribe", kwargs={"token": uuid.uuid4()})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_malformed_token_returns_404(self):
        """The uuid converter rejects junk before any query runs."""
        self.assertEqual(self.client.get("/notifications/unsubscribe/not-a-uuid/").status_code, 404)

    def test_token_only_affects_its_owner(self):
        """The core security property: one user cannot mute another."""
        other = UserFactory(email="other@siresoft.com")
        other_preference = NotificationPreference.objects.for_user(other)

        self.client.post(self.url)

        other_preference.refresh_from_db()
        self.assertTrue(other_preference.notify_on_assignment)

    def test_confirmation_page_shows_the_affected_address(self):
        """Users must see which address they are unsubscribing."""
        response = self.client.get(self.url)
        self.assertContains(response, "member@siresoft.com")

    def test_disallowed_methods_are_rejected(self):
        self.assertEqual(self.client.delete(self.url).status_code, 405)
        self.assertEqual(self.client.put(self.url).status_code, 405)

    def test_works_while_logged_in_as_someone_else(self):
        """The token, not the session, decides whose preferences change."""
        self.client.force_login(UserFactory(email="someone@siresoft.com"))
        self.client.post(self.url)

        self.preference.refresh_from_db()
        self.assertFalse(self.preference.notify_on_assignment)


class PreferencesViewTests(TestCase):
    """The signed-in settings page for turning notification types on and off."""

    def setUp(self):
        self.user = UserFactory(email="member@siresoft.com")
        self.url = reverse("notifications:preferences")

    def test_requires_login(self):
        """Unlike unsubscribe, this page edits settings and must be protected."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_renders_for_a_logged_in_user(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_creates_preferences_on_first_visit(self):
        """Existing users have no row until they touch the feature."""
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())
        self.client.force_login(self.user)
        self.client.get(self.url)
        self.assertTrue(NotificationPreference.objects.filter(user=self.user).exists())

    def test_can_turn_a_notification_off(self):
        self.client.force_login(self.user)
        self.client.post(self.url, {"notify_on_membership": "on"})

        preference = NotificationPreference.objects.for_user(self.user)
        self.assertFalse(preference.notify_on_assignment)
        self.assertTrue(preference.notify_on_membership)

    def test_can_turn_notifications_back_on(self):
        preference = NotificationPreference.objects.for_user(self.user)
        preference.disable_all()

        self.client.force_login(self.user)
        self.client.post(self.url, {"notify_on_assignment": "on", "notify_on_membership": "on"})

        preference.refresh_from_db()
        self.assertTrue(preference.notify_on_assignment)
        self.assertTrue(preference.notify_on_membership)

    def test_unchecking_everything_disables_everything(self):
        self.client.force_login(self.user)
        self.client.post(self.url, {})

        preference = NotificationPreference.objects.for_user(self.user)
        self.assertFalse(preference.notify_on_assignment)
        self.assertFalse(preference.notify_on_membership)

    def test_post_redirects_after_saving(self):
        """Redirect-after-POST so a refresh does not resubmit."""
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"notify_on_assignment": "on"})
        self.assertEqual(response.status_code, 302)

    def test_one_user_cannot_edit_anothers_preferences(self):
        other = UserFactory(email="other@siresoft.com")
        other_preference = NotificationPreference.objects.for_user(other)

        self.client.force_login(self.user)
        self.client.post(self.url, {})

        other_preference.refresh_from_db()
        self.assertTrue(other_preference.notify_on_assignment)
