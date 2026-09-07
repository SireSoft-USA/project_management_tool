"""Tests for render_invitation_email() (Phase 4: logo branding).

This function builds its own email context independently of both
send_notification() and allauth's send_mail(), so logo_url has to be wired in
here separately — these tests are the guard against a future refactor
silently dropping it from this third path.
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.test import TestCase

from apps.users.factories import UserFactory
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.invitations import render_invitation_email
from apps.workspaces.models import Invitation


class RenderInvitationEmailLogoTests(TestCase):
    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme")
        self.inviter = UserFactory()
        self.invitation = Invitation.objects.create(
            workspace=self.workspace,
            email="newcomer@siresoft.com",
            invited_by=self.inviter,
        )

    def test_html_body_contains_an_absolute_logo_url(self):
        message = render_invitation_email(self.invitation)
        self.assertIn("http://10.0.2.11:8000/static/images/header-logo2.png", message["html_message"])

    def test_logo_url_is_not_relative(self):
        message = render_invitation_email(self.invitation)
        self.assertNotIn('src="/static/', message["html_message"])
