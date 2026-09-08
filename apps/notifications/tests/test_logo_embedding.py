"""Tests for embedding the logo in email as an inline (CID) image.

Linking to the logo does not survive real mail clients: Roundcube and Gmail block
remote images by default, webmail served over HTTPS refuses an http:// image as
mixed content, and a private-network host is unreachable from outside. Embedding
removes the fetch entirely, so these tests pin down that the image really travels
inside the message.
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.test import TestCase, override_settings

from apps.notifications.emails import LOGO_CID, LOGO_STATIC_PATH, LogoEmailMessage, logo_bytes, send_notification
from apps.notifications.models import NotificationKind
from apps.users.factories import UserFactory
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.invitations import send_invitation
from apps.workspaces.models import Invitation

TEMPLATE = "notifications/email/membership"


def image_parts(message):
    """Return the image sub-parts of a built MIME message."""
    return [p for p in message.message().walk() if p.get_content_maintype() == "image"]


def content_types(message):
    return [p.get_content_type() for p in message.message().walk()]


class LogoBytesTests(TestCase):
    """Reading the image off disk."""

    def test_returns_png_bytes(self):
        self.assertTrue(logo_bytes().startswith(b"\x89PNG"))

    @override_settings(STATICFILES_DIRS=[])
    def test_returns_none_when_missing(self):
        """A missing logo must degrade, not raise."""
        self.assertIsNone(logo_bytes())


class LogoEmailMessageTests(TestCase):
    """The message subclass that embeds the image."""

    def _message(self, logo=True):
        message = LogoEmailMessage(
            subject="s",
            body="b",
            from_email="a@b.com",
            to=["c@d.com"],
            logo=logo_bytes() if logo else None,
        )
        message.attach_alternative(f"<p><img src='cid:{LOGO_CID}'></p>", "text/html")
        return message.message()

    def test_html_is_wrapped_in_multipart_related(self):
        """Outlook and Roundcube need related to resolve a cid: reference."""
        self.assertIn("multipart/related", [p.get_content_type() for p in self._message().walk()])

    def test_image_carries_the_expected_content_id(self):
        """The template references cid:siresoft-logo; the two must agree."""
        cids = [p.get("Content-ID") for p in self._message().walk() if p.get("Content-ID")]
        self.assertIn(f"<{LOGO_CID}>", cids)

    def test_image_is_inline_not_an_attachment(self):
        parts = [p for p in self._message().walk() if p.get_content_maintype() == "image"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_content_disposition(), "inline")

    def test_no_image_part_without_a_logo(self):
        parts = [p for p in self._message(logo=False).walk() if p.get_content_maintype() == "image"]
        self.assertEqual(parts, [])


class NotificationLogoTests(TestCase):
    """End-to-end: what actually leaves the app."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)
        self.user = UserFactory(email="member@siresoft.com")
        self.workspace = WorkspaceFactory(name="Acme", slug="acme")

    def _send(self):
        send_notification(
            recipient=self.user,
            kind=NotificationKind.MEMBERSHIP,
            subject="Subject",
            template=TEMPLATE,
            context={"workspace": self.workspace, "workspace_url": "http://10.0.2.11/w/acme/"},
        )
        return mail.outbox[0]

    def _html(self, message):
        for content, mimetype in message.alternatives:
            if mimetype == "text/html":
                return content
        self.fail("no text/html alternative")

    def test_logo_travels_with_the_message(self):
        self.assertEqual(len(image_parts(self._send())), 1)

    def test_html_references_the_embedded_image(self):
        """The whole point: cid:, not an http:// URL the client must fetch."""
        self.assertIn(f'src="cid:{LOGO_CID}"', self._html(self._send()))

    def test_html_does_not_link_the_logo_remotely(self):
        """A remote src is exactly what mail clients block."""
        self.assertNotIn(f'src="http://10.0.2.11/static/{LOGO_STATIC_PATH}"', self._html(self._send()))

    def test_html_is_wrapped_in_multipart_related(self):
        self.assertIn("multipart/related", content_types(self._send()))

    @override_settings(STATICFILES_DIRS=[])
    def test_falls_back_to_a_linked_logo_when_embedding_fails(self):
        """Degrade to the old behaviour rather than dropping the header."""
        html = self._html(self._send())
        self.assertIn("<img", html)
        self.assertNotIn("cid:", html)

    @override_settings(STATICFILES_DIRS=[])
    def test_email_still_sends_when_the_logo_is_missing(self):
        self._send()
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(STATICFILES_DIRS=[])
    def test_no_image_part_when_the_logo_is_missing(self):
        self.assertEqual(image_parts(self._send()), [])


class InvitationLogoTests(TestCase):
    """The invitation email uses a separate send path, so it needs its own proof."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)
        self.workspace = WorkspaceFactory(name="Acme", slug="acme")
        self.admin = UserFactory(email="admin@siresoft.com")

    def _send(self):
        invitation = Invitation.objects.create(
            workspace=self.workspace, email="newcomer@siresoft.com", invited_by=self.admin
        )
        with self.captureOnCommitCallbacks(execute=True):
            send_invitation(invitation)
        return mail.outbox[0]

    def _html(self, message):
        for content, mimetype in message.alternatives:
            if mimetype == "text/html":
                return content
        self.fail("no text/html alternative")

    def test_logo_travels_with_the_invitation(self):
        self.assertEqual(len(image_parts(self._send())), 1)

    def test_invitation_html_references_the_embedded_image(self):
        self.assertIn(f'src="cid:{LOGO_CID}"', self._html(self._send()))

    def test_invitation_is_multipart_related(self):
        self.assertIn("multipart/related", content_types(self._send()))
