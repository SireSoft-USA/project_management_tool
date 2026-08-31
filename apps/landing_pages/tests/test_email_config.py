"""Tests for email sender identity and notification settings (Steps 0.2 / 0.3).

These pin down configuration that is easy to break silently: a wrong "From"
address is only noticed once mail is already in someone's inbox, and an
unbounded bulk threshold is only noticed when 200 emails go out at once.
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.core.mail import mail_admins, send_mail
from django.test import SimpleTestCase, TestCase, override_settings

from apps.landing_pages.checks import W003_FROM_ADDRESS, check_default_from_email
from apps.users.factories import UserFactory
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.invitations import send_invitation
from apps.workspaces.models import Invitation


class FromAddressTests(TestCase):
    """The "From" header users actually see."""

    def test_default_from_email_is_configured(self):
        self.assertTrue(settings.DEFAULT_FROM_EMAIL, "DEFAULT_FROM_EMAIL must not be empty")

    def test_default_from_email_is_not_upstream_author(self):
        """Regression: upstream shipped the original author's personal Gmail."""
        self.assertNotIn("matagus@gmail.com", settings.DEFAULT_FROM_EMAIL)

    def test_server_email_is_not_upstream_author(self):
        self.assertNotIn("matagus@gmail.com", settings.SERVER_EMAIL)

    def test_admins_does_not_contain_upstream_author(self):
        """Error mail must not be routed to a stranger."""
        self.assertNotIn("matagus@gmail.com", list(settings.ADMINS))

    def test_default_from_email_contains_an_at_sign(self):
        """Cheap sanity check: catches a name-only value that would break sending."""
        self.assertIn("@", settings.DEFAULT_FROM_EMAIL)

    def test_sent_mail_uses_default_from_email(self):
        """End-to-end: the configured address lands on the actual message."""
        send_mail("Subject", "Body", None, ["someone@example.com"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].from_email, settings.DEFAULT_FROM_EMAIL)

    @override_settings(DEFAULT_FROM_EMAIL="Siresoft <admin@siresoft.com>")
    def test_display_name_format_is_preserved(self):
        """'Name <addr>' must survive intact — it is what recipients see."""
        send_mail("Subject", "Body", None, ["someone@example.com"])
        self.assertEqual(mail.outbox[0].from_email, "Siresoft <admin@siresoft.com>")

    def test_explicit_from_address_overrides_default(self):
        send_mail("Subject", "Body", "explicit@example.com", ["someone@example.com"])
        self.assertEqual(mail.outbox[0].from_email, "explicit@example.com")


class AdminsSettingTests(TestCase):
    """ADMINS drives mail_admins(); Django 6 requires plain strings."""

    def test_admins_is_a_list_of_strings(self):
        """Django 6 raises ImproperlyConfigured on (name, address) pairs."""
        for entry in settings.ADMINS:
            self.assertIsInstance(entry, str, f"ADMINS entry {entry!r} must be a plain address string")

    @override_settings(ADMINS=["ops@siresoft.com"])
    def test_mail_admins_delivers_to_configured_addresses(self):
        mail_admins("Alert", "Something broke")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@siresoft.com"])

    @override_settings(ADMINS=[])
    def test_mail_admins_is_a_no_op_when_unset(self):
        """An empty ADMINS must not raise — it is the default."""
        mail_admins("Alert", "Something broke")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(ADMINS=["ops@siresoft.com"], EMAIL_SUBJECT_PREFIX="[Siresoft] ")
    def test_subject_prefix_applies_to_admin_mail(self):
        mail_admins("Alert", "Something broke")
        self.assertTrue(mail.outbox[0].subject.startswith("[Siresoft] "))

    def test_subject_prefix_does_not_apply_to_ordinary_mail(self):
        """Notification subjects are self-describing and must stay unprefixed."""
        send_mail("You have a new task", "Body", None, ["someone@example.com"])
        self.assertEqual(mail.outbox[0].subject, "You have a new task")


class EmailTransportTests(SimpleTestCase):
    """Transport-level guard rails."""

    def test_email_timeout_is_set(self):
        """Without a timeout, a hung SMTP server pins a worker indefinitely."""
        self.assertIsNotNone(settings.EMAIL_TIMEOUT)

    def test_email_timeout_is_a_sane_positive_number(self):
        self.assertGreater(settings.EMAIL_TIMEOUT, 0)
        self.assertLessEqual(settings.EMAIL_TIMEOUT, 60)

    def test_backend_is_not_smtp_during_tests(self):
        """Tests must never open a real SMTP connection."""
        self.assertIn("locmem", settings.EMAIL_BACKEND)


class NotificationSettingsTests(SimpleTestCase):
    """Step 0.3 — the knobs the notification app will read."""

    def test_notifications_enabled_exists_and_is_boolean(self):
        self.assertIsInstance(settings.NOTIFICATIONS_ENABLED, bool)

    def test_notifications_enabled_defaults_to_true(self):
        self.assertTrue(settings.NOTIFICATIONS_ENABLED)

    def test_bulk_threshold_exists_and_is_an_int(self):
        self.assertIsInstance(settings.NOTIFICATION_BULK_THRESHOLD, int)

    def test_bulk_threshold_is_positive(self):
        """A zero/negative threshold would make every assignment a 'digest'."""
        self.assertGreater(settings.NOTIFICATION_BULK_THRESHOLD, 0)

    @override_settings(NOTIFICATIONS_ENABLED=False)
    def test_notifications_can_be_disabled(self):
        """The kill switch must be overridable — used by staging restores."""
        self.assertFalse(settings.NOTIFICATIONS_ENABLED)

    @override_settings(NOTIFICATION_BULK_THRESHOLD=3)
    def test_bulk_threshold_can_be_overridden(self):
        self.assertEqual(settings.NOTIFICATION_BULK_THRESHOLD, 3)


class FromAddressCheckTests(SimpleTestCase):
    """W003 catches an undeliverable From address before the first real send."""

    SMTP = "django.core.mail.backends.smtp.EmailBackend"
    CONSOLE = "django.core.mail.backends.console.EmailBackend"

    def _ids(self):
        return [w.id for w in check_default_from_email(None)]

    @override_settings(EMAIL_BACKEND=SMTP, DEFAULT_FROM_EMAIL="Siresoft <admin@siresoft.com>")
    def test_valid_display_name_address_passes(self):
        self.assertEqual(self._ids(), [])

    @override_settings(EMAIL_BACKEND=SMTP, DEFAULT_FROM_EMAIL="admin@siresoft.com")
    def test_valid_bare_address_passes(self):
        self.assertEqual(self._ids(), [])

    @override_settings(EMAIL_BACKEND=SMTP, DEFAULT_FROM_EMAIL="Siresoft <noreply@10.0.2.11:8000>")
    def test_host_with_port_is_flagged(self):
        """The real trap: the SITE_DOMAIN-derived default includes a port."""
        self.assertEqual(self._ids(), [W003_FROM_ADDRESS])

    @override_settings(EMAIL_BACKEND=SMTP, DEFAULT_FROM_EMAIL="not-an-address")
    def test_missing_at_sign_is_flagged(self):
        self.assertEqual(self._ids(), [W003_FROM_ADDRESS])

    @override_settings(EMAIL_BACKEND=SMTP, DEFAULT_FROM_EMAIL="")
    def test_empty_address_is_flagged(self):
        self.assertEqual(self._ids(), [W003_FROM_ADDRESS])

    @override_settings(EMAIL_BACKEND=CONSOLE, DEFAULT_FROM_EMAIL="Siresoft <noreply@10.0.2.11:8000>")
    def test_console_backend_is_not_flagged(self):
        """Local development must not be nagged about an address nothing delivers."""
        self.assertEqual(self._ids(), [])

    @override_settings(EMAIL_BACKEND=SMTP, DEFAULT_FROM_EMAIL="  Siresoft <admin@siresoft.com>  ")
    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(self._ids(), [])


class InvitationEmailContentTests(TestCase):
    """The one email the app already sends — what a recipient actually receives."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.admin = UserFactory(email="admin@siresoft.com")
        self.workspace = WorkspaceFactory(name="Siresoft", slug="siresoft")
        self.invitation = Invitation.objects.create(
            workspace=self.workspace,
            email="newperson@example.com",
            invited_by=self.admin,
        )

    def _send_and_get(self):
        # send_invitation() queues on transaction commit, which TestCase never
        # reaches on its own; captureOnCommitCallbacks runs the callback.
        with self.captureOnCommitCallbacks(execute=True):
            send_invitation(self.invitation)
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0]

    def _html_body(self, message):
        for content, mimetype in message.alternatives:
            if mimetype == "text/html":
                return content
        self.fail("email has no text/html alternative")

    def test_from_address_is_configured_sender(self):
        self.assertEqual(self._send_and_get().from_email, settings.DEFAULT_FROM_EMAIL)

    def test_sends_to_the_invited_address(self):
        self.assertEqual(self._send_and_get().to, ["newperson@example.com"])

    def test_has_both_text_and_html_parts(self):
        """Text-only clients and spam filters both expect a plain-text part."""
        message = self._send_and_get()
        self.assertTrue(message.body.strip(), "plain-text body must not be empty")
        self.assertTrue(self._html_body(message).strip())

    def test_accept_link_uses_configured_domain(self):
        message = self._send_and_get()
        self.assertIn("10.0.2.11:8000", message.body)
        self.assertIn("10.0.2.11:8000", self._html_body(message))

    def test_contains_no_placeholder_domain(self):
        """The whole point of Step 0.1 — no example.com anywhere."""
        message = self._send_and_get()
        self.assertNotIn("example.com", message.body)
        self.assertNotIn("example.com", self._html_body(message))

    def test_header_shows_site_name_not_upstream_brand(self):
        """Regression: the shared base template hardcoded 'Matorral'."""
        self.assertNotIn("Matorral", self._html_body(self._send_and_get()))

    def test_footer_link_has_a_host(self):
        """Regression: current_site was missing, so the footer emitted href="https://"."""
        html = self._html_body(self._send_and_get())
        self.assertNotIn('href="https://"', html)

    def test_footer_link_scheme_matches_environment(self):
        """Local runs are http; a hardcoded https link would not open."""
        html = self._html_body(self._send_and_get())
        self.assertIn("http://10.0.2.11:8000", html)


class SiteDerivedDefaultsTests(SimpleTestCase):
    """The From address defaults are derived from SITE_NAME/SITE_DOMAIN.

    This only works because the Sites block is defined *above* the Email block in
    settings.py. If someone reorders them, settings import raises NameError — these
    tests would fail to even load, which is the intended alarm.
    """

    def test_site_domain_and_name_are_defined(self):
        self.assertTrue(settings.SITE_DOMAIN)
        self.assertTrue(settings.SITE_NAME)

    def test_subject_prefix_is_derived_from_site_name(self):
        self.assertIn(settings.SITE_NAME, settings.EMAIL_SUBJECT_PREFIX)
