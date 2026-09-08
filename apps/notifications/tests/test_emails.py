"""Tests for the send_notification() choke point (Step 1.3).

Every guard that stops an email lives in one function, so these tests are the
safety net for "did we email someone we shouldn't have?".
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.test import TestCase, override_settings

from apps.notifications.emails import build_logo_url, send_notification
from apps.notifications.models import NotificationKind, NotificationPreference
from apps.users.factories import UserFactory
from apps.workspaces.factories import WorkspaceFactory

TEMPLATE = "notifications/email/membership"


class ExplodingBackend:
    """Email backend that always fails, to prove errors are not swallowed."""

    def __init__(self, *args, **kwargs):
        pass

    def send_messages(self, messages):
        raise RuntimeError("smtp is down")

    def open(self):
        pass

    def close(self):
        pass


class SendNotificationGuardTests(TestCase):
    """A refused send must return False, not raise — callers are fire-and-forget."""

    def setUp(self):
        self.user = UserFactory(email="member@siresoft.com")
        self.workspace = WorkspaceFactory(name="Acme")

    def _send(self, recipient=None, kind=NotificationKind.MEMBERSHIP):
        return send_notification(
            recipient=recipient or self.user,
            kind=kind,
            subject="Subject",
            template=TEMPLATE,
            context={"workspace": self.workspace, "workspace_url": "http://x/w/acme/"},
        )

    def test_sends_to_an_eligible_user(self):
        self.assertTrue(self._send())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["member@siresoft.com"])

    def test_skips_when_recipient_is_none(self):
        """Unassignment passes None; it must be a no-op, not a crash."""
        sent = send_notification(
            recipient=None,
            kind=NotificationKind.MEMBERSHIP,
            subject="Subject",
            template=TEMPLATE,
            context={},
        )
        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_user_without_an_email_address(self):
        """Sending to "" raises in the backend; the guard must catch it first."""
        self.assertFalse(self._send(recipient=UserFactory(email="")))
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_inactive_user(self):
        """Deactivated accounts must stop receiving mail."""
        self.assertFalse(self._send(recipient=UserFactory(email="gone@x.com", is_active=False)))
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_user_who_opted_out_of_this_kind(self):
        preference = NotificationPreference.objects.for_user(self.user)
        preference.notify_on_membership = False
        preference.save()

        self.assertFalse(self._send())
        self.assertEqual(len(mail.outbox), 0)

    def test_still_sends_kinds_the_user_did_not_opt_out_of(self):
        """Opting out of assignments must not silence membership mail."""
        preference = NotificationPreference.objects.for_user(self.user)
        preference.notify_on_assignment = False
        preference.save()

        self.assertTrue(self._send(kind=NotificationKind.MEMBERSHIP))
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(NOTIFICATIONS_ENABLED=False)
    def test_kill_switch_silences_everything(self):
        """The staging-restore safety valve."""
        self.assertFalse(self._send())
        self.assertEqual(len(mail.outbox), 0)

    def test_creates_preferences_for_a_user_who_has_none(self):
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())
        self._send()
        self.assertTrue(NotificationPreference.objects.filter(user=self.user).exists())


class SendNotificationContentTests(TestCase):
    """What the recipient actually receives."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.user = UserFactory(email="member@siresoft.com")
        self.workspace = WorkspaceFactory(name="Acme")
        send_notification(
            recipient=self.user,
            kind=NotificationKind.MEMBERSHIP,
            subject="You've been added to Acme",
            template=TEMPLATE,
            context={"workspace": self.workspace, "workspace_url": "http://10.0.2.11:8000/w/acme/"},
        )
        self.message = mail.outbox[0]

    def _html(self):
        for content, mimetype in self.message.alternatives:
            if mimetype == "text/html":
                return content
        self.fail("no text/html alternative")

    def test_uses_the_configured_from_address(self):
        self.assertEqual(self.message.from_email, settings.DEFAULT_FROM_EMAIL)

    def test_subject_is_passed_through_unprefixed(self):
        self.assertEqual(self.message.subject, "You've been added to Acme")

    def test_has_a_plain_text_part(self):
        """Text-only clients and spam scoring both need this."""
        self.assertTrue(self.message.body.strip())

    def test_has_an_html_part(self):
        self.assertTrue(self._html().strip())

    def test_both_parts_contain_the_link(self):
        self.assertIn("10.0.2.11:8000", self.message.body)
        self.assertIn("10.0.2.11:8000", self._html())

    def test_contains_no_placeholder_domain(self):
        self.assertNotIn("example.com", self.message.body)

    def test_includes_an_unsubscribe_link(self):
        preference = NotificationPreference.objects.for_user(self.user)
        self.assertIn(str(preference.unsubscribe_token), self.message.body)

    def test_sets_list_unsubscribe_header(self):
        """Gmail/Outlook render a native unsubscribe control from this."""
        self.assertIn("List-Unsubscribe", self.message.extra_headers)
        self.assertTrue(self.message.extra_headers["List-Unsubscribe"].startswith("<http"))

    def test_advertises_one_click_unsubscribe(self):
        self.assertEqual(
            self.message.extra_headers["List-Unsubscribe-Post"],
            "List-Unsubscribe=One-Click",
        )

    def test_unsubscribe_url_is_absolute(self):
        self.assertIn(
            "http://10.0.2.11:8000/notifications/unsubscribe/", self.message.extra_headers["List-Unsubscribe"]
        )

    def test_html_part_references_the_embedded_logo(self):
        """The logo travels inside the message, so the HTML points at cid:
        rather than a URL the mail client would have to fetch (and block)."""
        html = self._html()
        self.assertIn('src="cid:siresoft-logo"', html)

    def test_logo_img_has_alt_text(self):
        """If image loading is blocked (the default in most mail clients),
        the alt text must still name the site instead of showing nothing."""
        html = self._html()
        self.assertIn('alt="Siresoft"', html)

    def test_header_shows_the_product_name_beside_the_logo(self):
        html = self._html()
        self.assertIn("Siresoft Project Management Tool", html)

    def test_header_does_not_leak_template_comment_text(self):
        """Regression test: a {# #} Django comment spanning multiple lines does
        not get parsed as a comment — it used to render as literal visible text
        in the header. The header must only ever use {% comment %}...{% endcomment %}
        for multi-line notes, which this asserts never leaks into output."""
        html = self._html()
        for leaked_word in ("wordmark", "masthead", "premium", "crowding"):
            self.assertNotIn(leaked_word, html)


class BuildLogoUrlTests(TestCase):
    """build_logo_url() — the helper every email context gets it from."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

    def test_returns_an_absolute_url(self):
        url = build_logo_url()
        self.assertTrue(url.startswith("http://10.0.2.11:8000/"))

    def test_is_not_a_relative_path(self):
        url = build_logo_url()
        self.assertFalse(url.startswith("/static/"))

    def test_does_not_hardcode_localhost(self):
        """The domain must come from the configured Site, not a dev-only default."""
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "app.matorral.dev"})
        Site.objects.clear_cache()
        url = build_logo_url()
        self.assertIn("app.matorral.dev", url)
        self.assertNotIn("localhost", url)

    def test_adds_no_extra_query_beyond_the_cached_site_lookup(self):
        """Site.objects.get_current() is cached process-wide after the first
        call, so a second call must add zero additional queries."""
        build_logo_url()  # warm the Site cache
        with self.assertNumQueries(0):
            build_logo_url()


class SendNotificationFailureTests(TestCase):
    """Backend failures must propagate so Celery can retry them."""

    @override_settings(EMAIL_BACKEND="apps.notifications.tests.test_emails.ExplodingBackend")
    def test_backend_error_propagates(self):
        user = UserFactory(email="member@siresoft.com")
        with self.assertRaises(RuntimeError):
            send_notification(
                recipient=user,
                kind=NotificationKind.MEMBERSHIP,
                subject="s",
                template=TEMPLATE,
                context={"workspace": WorkspaceFactory(), "workspace_url": "http://x/"},
            )
