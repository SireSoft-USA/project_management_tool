"""Tests for workspace membership email (Step 4).

Two related things are covered here:
  * accepting an invitation emails the new member a welcome + workspace link
  * the invitation email itself is queued, so a broken mail server cannot turn
    "invite a member" into a 500 and lose the invitation
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.notifications.models import NotificationPreference
from apps.users.factories import UserFactory
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.invitations import process_invitation, send_invitation
from apps.workspaces.models import Invitation, Membership
from apps.workspaces.tasks import send_invitation_email


class MembershipEmailTestCase(TestCase):
    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme", slug="acme")
        self.admin = UserFactory(email="admin@siresoft.com", first_name="Ada", last_name="Admin")
        self.newcomer = UserFactory(email="newcomer@siresoft.com")

    def _invitation(self, email=None, invited_by=None):
        return Invitation.objects.create(
            workspace=self.workspace,
            email=email or self.newcomer.email,
            invited_by=invited_by or self.admin,
        )

    def _accept(self, invitation, user):
        with self.captureOnCommitCallbacks(execute=True):
            process_invitation(invitation, user)


class AcceptInvitationNotifiesTests(MembershipEmailTestCase):
    """Accepting an invitation should confirm access and link into the workspace."""

    def test_emails_the_new_member(self):
        self._accept(self._invitation(), self.newcomer)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["newcomer@siresoft.com"])

    def test_subject_names_the_workspace(self):
        self._accept(self._invitation(), self.newcomer)
        self.assertIn("Acme", mail.outbox[0].subject)

    def test_body_links_to_the_workspace(self):
        self._accept(self._invitation(), self.newcomer)
        self.assertIn(f"http://10.0.2.11:8000{self.workspace.get_absolute_url()}", mail.outbox[0].body)

    def test_names_the_person_who_invited_them(self):
        self._accept(self._invitation(), self.newcomer)
        self.assertIn("Ada Admin", mail.outbox[0].body)

    def test_membership_is_created(self):
        """The email is a side effect; the actual membership must still happen."""
        self._accept(self._invitation(), self.newcomer)
        self.assertTrue(Membership.objects.filter(workspace=self.workspace, user=self.newcomer).exists())

    def test_invitation_is_marked_accepted(self):
        invitation = self._invitation()
        self._accept(invitation, self.newcomer)
        invitation.refresh_from_db()
        self.assertTrue(invitation.is_accepted)
        self.assertEqual(invitation.accepted_by, self.newcomer)

    def test_no_email_when_accepting_your_own_invitation(self):
        """Inviting your own address should not send you a welcome mail."""
        self._accept(self._invitation(invited_by=self.admin), self.admin)
        self.assertEqual(len(mail.outbox), 0)

    def test_respects_membership_opt_out(self):
        preference = NotificationPreference.objects.for_user(self.newcomer)
        preference.notify_on_membership = False
        preference.save()

        self._accept(self._invitation(), self.newcomer)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(NOTIFICATIONS_ENABLED=False)
    def test_respects_global_kill_switch(self):
        self._accept(self._invitation(), self.newcomer)
        self.assertEqual(len(mail.outbox), 0)

    def test_membership_still_created_when_notifications_are_off(self):
        """A silenced notification must never block the actual access grant."""
        with override_settings(NOTIFICATIONS_ENABLED=False):
            self._accept(self._invitation(), self.newcomer)

        self.assertTrue(Membership.objects.filter(workspace=self.workspace, user=self.newcomer).exists())


class InvitationEmailTaskTests(MembershipEmailTestCase):
    """The invitation email now goes through Celery."""

    def test_send_invitation_delivers_the_email(self):
        invitation = self._invitation()
        with self.captureOnCommitCallbacks(execute=True):
            send_invitation(invitation)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [invitation.email])

    def test_nothing_is_sent_before_commit(self):
        """Queued on commit, so a rolled-back request sends no invitation."""
        invitation = self._invitation()
        with self.captureOnCommitCallbacks(execute=True):
            send_invitation(invitation)
            self.assertEqual(len(mail.outbox), 0)

        self.assertEqual(len(mail.outbox), 1)

    def test_email_contains_the_accept_link(self):
        invitation = self._invitation()
        with self.captureOnCommitCallbacks(execute=True):
            send_invitation(invitation)

        self.assertIn(str(invitation.pk), mail.outbox[0].body)
        self.assertIn("10.0.2.11:8000", mail.outbox[0].body)

    def test_email_has_both_text_and_html_parts(self):
        invitation = self._invitation()
        with self.captureOnCommitCallbacks(execute=True):
            send_invitation(invitation)

        message = mail.outbox[0]
        self.assertTrue(message.body.strip())
        self.assertTrue(any(mimetype == "text/html" for _content, mimetype in message.alternatives))

    def test_task_skips_a_cancelled_invitation(self):
        """The invite can be cancelled between queueing and delivery."""
        invitation = self._invitation()
        invitation_id = str(invitation.pk)
        invitation.delete()

        self.assertFalse(send_invitation_email(invitation_id))
        self.assertEqual(len(mail.outbox), 0)

    def test_task_retries_on_transport_errors(self):
        self.assertTrue(send_invitation_email.autoretry_for)
        self.assertEqual(send_invitation_email.max_retries, 5)

    def test_task_has_time_limits(self):
        self.assertIsNotNone(send_invitation_email.soft_time_limit)
        self.assertIsNotNone(send_invitation_email.time_limit)


class InviteMemberViewTests(MembershipEmailTestCase):
    """The real admin flow: Members → Invite."""

    def setUp(self):
        super().setUp()
        Membership.objects.create(workspace=self.workspace, user=self.admin, role="admin")
        self.client.force_login(self.admin)

    def test_inviting_sends_an_email(self):
        url = reverse("workspaces:send_invitation", kwargs={"workspace_slug": self.workspace.slug})
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"email": "invitee@siresoft.com", "role": "member"})

        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["invitee@siresoft.com"])

    def test_invitation_row_is_created(self):
        url = reverse("workspaces:send_invitation", kwargs={"workspace_slug": self.workspace.slug})
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {"email": "invitee@siresoft.com", "role": "member"})

        self.assertTrue(Invitation.objects.filter(email="invitee@siresoft.com").exists())
