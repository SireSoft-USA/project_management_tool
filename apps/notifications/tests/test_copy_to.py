"""Tests for copying notification email to a monitoring address.

NOTIFICATION_COPY_TO puts a fixed address on every notification and invitation,
so the team keeps a record of what the system sent.
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.test import TestCase, override_settings

from apps.notifications.emails import copy_recipients, send_notification
from apps.notifications.models import NotificationKind
from apps.users.factories import UserFactory
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.invitations import send_invitation
from apps.workspaces.models import Invitation

TEMPLATE = "notifications/email/membership"
MONITOR = "flone@siresoft.com"


class CopyRecipientsHelperTests(TestCase):
    """The helper that decides Cc vs Bcc vs nothing."""

    @override_settings(NOTIFICATION_COPY_TO=[])
    def test_returns_nothing_when_unconfigured(self):
        """Empty config must splat into EmailMultiAlternatives harmlessly."""
        self.assertEqual(copy_recipients(), {})

    @override_settings(NOTIFICATION_COPY_TO=[MONITOR], NOTIFICATION_COPY_MODE="cc")
    def test_cc_mode_uses_the_cc_field(self):
        self.assertEqual(copy_recipients(), {"cc": [MONITOR]})

    @override_settings(NOTIFICATION_COPY_TO=[MONITOR], NOTIFICATION_COPY_MODE="bcc")
    def test_bcc_mode_uses_the_bcc_field(self):
        self.assertEqual(copy_recipients(), {"bcc": [MONITOR]})

    @override_settings(NOTIFICATION_COPY_TO=[MONITOR], NOTIFICATION_COPY_MODE="nonsense")
    def test_unrecognised_mode_falls_back_to_bcc(self):
        """Hiding the address is the safe default if the setting is mistyped."""
        self.assertEqual(copy_recipients(), {"bcc": [MONITOR]})

    @override_settings(NOTIFICATION_COPY_TO=[MONITOR, "ops@siresoft.com"], NOTIFICATION_COPY_MODE="cc")
    def test_supports_multiple_addresses(self):
        self.assertEqual(copy_recipients(), {"cc": [MONITOR, "ops@siresoft.com"]})

    @override_settings(NOTIFICATION_COPY_TO=[MONITOR], NOTIFICATION_COPY_MODE="cc")
    def test_excludes_the_recipient_to_avoid_duplicates(self):
        """Mailing the monitor itself must not put it in both To and Cc."""
        self.assertEqual(copy_recipients(exclude=[MONITOR]), {})

    @override_settings(NOTIFICATION_COPY_TO=[MONITOR], NOTIFICATION_COPY_MODE="cc")
    def test_exclusion_is_case_insensitive(self):
        self.assertEqual(copy_recipients(exclude=["FLONE@Siresoft.com"]), {})

    @override_settings(NOTIFICATION_COPY_TO=["  ", ""], NOTIFICATION_COPY_MODE="cc")
    def test_blank_entries_are_ignored(self):
        self.assertEqual(copy_recipients(), {})


@override_settings(NOTIFICATION_COPY_TO=[MONITOR], NOTIFICATION_COPY_MODE="cc")
class NotificationEmailCopyTests(TestCase):
    """Assignment/membership notifications carry the copy."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)
        self.user = UserFactory(email="member@siresoft.com")
        self.workspace = WorkspaceFactory(name="Acme")

    def _send(self, recipient=None):
        return send_notification(
            recipient=recipient or self.user,
            kind=NotificationKind.MEMBERSHIP,
            subject="Subject",
            template=TEMPLATE,
            context={"workspace": self.workspace, "workspace_url": "http://x/w/acme/"},
        )

    def test_notification_is_copied_to_the_monitor(self):
        self._send()
        self.assertEqual(mail.outbox[0].cc, [MONITOR])

    def test_the_real_recipient_still_receives_it(self):
        self._send()
        self.assertEqual(mail.outbox[0].to, ["member@siresoft.com"])

    def test_monitor_is_an_actual_delivery_target(self):
        """locmem records every envelope recipient, To and Cc alike."""
        self._send()
        self.assertIn(MONITOR, mail.outbox[0].recipients())

    def test_no_duplicate_when_the_monitor_is_the_recipient(self):
        self._send(recipient=UserFactory(email=MONITOR))
        message = mail.outbox[0]
        self.assertEqual(message.to, [MONITOR])
        self.assertEqual(message.cc, [])

    @override_settings(NOTIFICATION_COPY_TO=[])
    def test_no_copy_when_unconfigured(self):
        self._send()
        self.assertEqual(mail.outbox[0].cc, [])

    @override_settings(NOTIFICATION_COPY_MODE="bcc")
    def test_bcc_mode_hides_the_monitor_from_the_header(self):
        self._send()
        message = mail.outbox[0]
        self.assertEqual(message.cc, [])
        self.assertEqual(message.bcc, [MONITOR])
        self.assertIn(MONITOR, message.recipients())


@override_settings(NOTIFICATION_COPY_TO=[MONITOR], NOTIFICATION_COPY_MODE="cc")
class InvitationEmailCopyTests(TestCase):
    """The "new member added" email you asked to have copied."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)
        self.workspace = WorkspaceFactory(name="Acme", slug="acme")
        self.admin = UserFactory(email="admin@siresoft.com")

    def _invite(self, email="newcomer@siresoft.com"):
        invitation = Invitation.objects.create(workspace=self.workspace, email=email, invited_by=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            send_invitation(invitation)
        return mail.outbox[0]

    def test_invitation_is_copied_to_the_monitor(self):
        self.assertEqual(self._invite().cc, [MONITOR])

    def test_invitee_still_receives_it(self):
        self.assertEqual(self._invite().to, ["newcomer@siresoft.com"])

    def test_no_duplicate_when_inviting_the_monitor(self):
        message = self._invite(email=MONITOR)
        self.assertEqual(message.to, [MONITOR])
        self.assertEqual(message.cc, [])

    @override_settings(NOTIFICATION_COPY_TO=[])
    def test_no_copy_when_unconfigured(self):
        self.assertEqual(self._invite().cc, [])
