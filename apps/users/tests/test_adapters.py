"""Tests for UserEmailAsUsernameAdapter (Phase 4: logo branding).

allauth's own emails (password reset, email confirmation) are sent through
DefaultAccountAdapter.send_mail(), a separate path from send_notification().
logo_url has to be injected here too, or those emails would keep the
text-only header while every notification email shows the real logo.
"""

from unittest.mock import patch

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.test import RequestFactory, TestCase

from apps.users.adapters import UserEmailAsUsernameAdapter
from apps.users.factories import UserFactory


class SendMailAddsLogoUrlTests(TestCase):
    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)
        self.request = RequestFactory().get("/")

    @patch("allauth.account.adapter.DefaultAccountAdapter.send_mail")
    def test_injects_logo_url_into_the_context(self, mock_send_mail):
        adapter = UserEmailAsUsernameAdapter(request=self.request)
        context = {"activate_url": "http://x/activate/"}

        adapter.send_mail("account/email/email_confirmation", "user@siresoft.com", context)

        sent_context = mock_send_mail.call_args.args[2]
        self.assertIn("logo_url", sent_context)
        self.assertTrue(sent_context["logo_url"].startswith("http://10.0.2.11:8000/"))

    @patch("allauth.account.adapter.DefaultAccountAdapter.send_mail")
    def test_does_not_override_an_explicitly_provided_logo_url(self, mock_send_mail):
        """setdefault(), not assignment: a caller that already set logo_url wins."""
        adapter = UserEmailAsUsernameAdapter(request=self.request)
        context = {"logo_url": "http://custom.example.com/logo.png"}

        adapter.send_mail("account/email/email_confirmation", "user@siresoft.com", context)

        sent_context = mock_send_mail.call_args.args[2]
        self.assertEqual(sent_context["logo_url"], "http://custom.example.com/logo.png")

    @patch("allauth.account.adapter.DefaultAccountAdapter.send_mail")
    def test_delegates_to_the_parent_send_mail(self, mock_send_mail):
        """The override must still actually send — not just build context."""
        adapter = UserEmailAsUsernameAdapter(request=self.request)
        adapter.send_mail("account/email/email_confirmation", "user@siresoft.com", {})
        mock_send_mail.assert_called_once()

    def test_real_send_mail_reaches_the_rendered_email(self):
        """End-to-end: no mocking, confirm the actual rendered HTML gets the logo."""
        adapter = UserEmailAsUsernameAdapter(request=self.request)
        user = UserFactory(email="user@siresoft.com")

        adapter.send_mail(
            "account/email/email_confirmation",
            "user@siresoft.com",
            {"activate_url": "http://x/activate/", "user": user},
        )

        sent = mail.outbox[-1]
        html_body = sent.alternatives[0][0] if sent.alternatives else sent.body
        self.assertIn("header-logo2.png", html_body)
