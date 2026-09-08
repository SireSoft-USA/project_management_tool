"""Tests for epic-activity delivery: the Celery task and the audit copy.

These run the task directly against Django's locmem mail outbox, so they assert
on the message that would actually leave the system — To, Cc, subject and body —
rather than on a mock of the send.
"""

from django.core import mail
from django.test import TestCase, override_settings

from apps.issues.models import Epic, IssueStatus, Story
from apps.notifications.models import NotificationPreference
from apps.notifications.tasks import send_epic_activity_email
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership

CHANGES = [{"field": "status", "label": "Status", "old_value": "In Progress", "new_value": "Done"}]
ADMIN = "admin@siresoft.com"


@override_settings(EPIC_ACTIVITY_ADMIN_CC=[ADMIN], NOTIFICATION_COPY_TO=[])
class EpicActivityDeliveryTest(TestCase):
    """The task end to end, asserting on the outbox."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory(email="olive@example.com", first_name="Olive", last_name="Owner")
        cls.actor = UserFactory(email="john@example.com", first_name="John", last_name="Smith")
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role=roles.ROLE_MEMBER)
        Membership.objects.create(workspace=cls.workspace, user=cls.actor, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = Epic.add_root(project=self.project, title="Website Redesign", assignee=self.owner)
        self.story = self.epic.add_child(
            instance=Story(project=self.project, title="Build login", status=IssueStatus.DONE)
        )
        self.story.refresh_from_db()

    def _run(self, **kwargs):
        return send_epic_activity_email(
            kwargs.get("issue_id", self.story.pk),
            kwargs.get("epic_id", self.epic.pk),
            kwargs.get("changes", CHANGES),
            kwargs.get("actor_id", self.actor.pk),
            kwargs.get("created", False),
        )

    def test_sends_to_assignee_with_admin_copied(self):
        sent = self._run()

        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["olive@example.com"])
        self.assertIn(ADMIN, message.cc)

    def test_body_carries_the_change(self):
        self._run()

        body = mail.outbox[0].body
        self.assertIn("Status: In Progress -> Done", body)
        self.assertIn("Website Redesign", body)

    def test_sends_both_text_and_html_parts(self):
        self._run()

        alternatives = mail.outbox[0].alternatives
        self.assertEqual(len(alternatives), 1)
        self.assertEqual(alternatives[0][1], "text/html")

    def test_deleted_issue_is_not_an_error(self):
        """A task can outlive its subject; that is expected, not a failure."""
        issue_id = self.story.pk
        self.story.delete()

        sent = self._run(issue_id=issue_id)

        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    def test_deleted_epic_is_not_an_error(self):
        epic_id = self.epic.pk
        self.epic.delete()

        sent = self._run(epic_id=epic_id)

        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    def test_recipient_is_resolved_at_send_time(self):
        """Reassigning the epic after the edit redirects the email to the new owner."""
        new_owner = UserFactory(email="nina@example.com")
        Membership.objects.create(workspace=self.workspace, user=new_owner, role=roles.ROLE_MEMBER)
        self.epic.assignee = new_owner
        self.epic.save(update_fields=["assignee"])

        self._run()

        self.assertEqual(mail.outbox[0].to, ["nina@example.com"])

    def test_opted_out_assignee_still_leaves_no_message(self):
        preference = NotificationPreference.objects.for_user(self.owner)
        preference.notify_on_epic_activity = False
        preference.save(update_fields=["notify_on_epic_activity"])

        sent = self._run()

        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(NOTIFICATIONS_ENABLED=False)
    def test_global_kill_switch_stops_delivery(self):
        sent = self._run()

        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EPIC_ACTIVITY_ADMIN_CC=[ADMIN], NOTIFICATION_COPY_TO=[])
class EpicActivityAdminOnlyTest(TestCase):
    """An epic with no assignee still has an audit trail."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.actor = UserFactory(email="john@example.com")
        Membership.objects.create(workspace=cls.workspace, user=cls.actor, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = Epic.add_root(project=self.project, title="Unowned", assignee=None)
        self.story = self.epic.add_child(instance=Story(project=self.project, title="S"))
        self.story.refresh_from_db()

    def _run(self):
        return send_epic_activity_email(self.story.pk, self.epic.pk, CHANGES, self.actor.pk, False)

    def test_unassigned_epic_mails_the_admin_directly(self):
        sent = self._run()

        self.assertTrue(sent)
        self.assertEqual(mail.outbox[0].to, [ADMIN])

    def test_admin_only_message_offers_no_unsubscribe_link(self):
        """An operator-configured address has no preference row to unsubscribe from."""
        self._run()

        self.assertNotIn("Unsubscribe:", mail.outbox[0].body)

    def test_admin_only_footer_does_not_claim_the_reader_is_the_assignee(self):
        """The epic is unassigned, so the standard footer would state a falsehood."""
        self._run()

        body = mail.outbox[0].body
        self.assertIn("configured to receive epic activity notifications", body)
        self.assertNotIn("you are the assignee", body)

    @override_settings(EPIC_ACTIVITY_ADMIN_CC=[])
    def test_no_assignee_and_no_admin_sends_nothing(self):
        sent = self._run()

        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(NOTIFICATIONS_ENABLED=False)
    def test_kill_switch_also_covers_the_admin_only_path(self):
        """The global switch must not be bypassed by the branch that skips send_notification."""
        sent = self._run()

        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EPIC_ACTIVITY_ADMIN_CC=[ADMIN])
class EpicActivityCopyDeduplicationTest(TestCase):
    """Nobody should receive the same notification twice."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.actor = UserFactory(email="john@example.com")
        Membership.objects.create(workspace=cls.workspace, user=cls.actor, role=roles.ROLE_MEMBER)

    def _run_with_assignee(self, assignee):
        Membership.objects.get_or_create(workspace=self.workspace, user=assignee, defaults={"role": roles.ROLE_MEMBER})
        epic = Epic.add_root(project=self.project, title="E", assignee=assignee)
        story = epic.add_child(instance=Story(project=self.project, title="S"))
        story.refresh_from_db()
        return send_epic_activity_email(story.pk, epic.pk, CHANGES, self.actor.pk, False)

    @override_settings(NOTIFICATION_COPY_TO=[])
    def test_assignee_who_is_the_admin_is_not_copied_as_well(self):
        """To and Cc must not both be the admin — one message, one copy."""
        admin_user = UserFactory(email=ADMIN)

        self._run_with_assignee(admin_user)

        message = mail.outbox[0]
        self.assertEqual(message.to, [ADMIN])
        self.assertNotIn(ADMIN, message.cc)

    @override_settings(NOTIFICATION_COPY_TO=[ADMIN], NOTIFICATION_COPY_MODE="bcc")
    def test_address_already_in_the_site_wide_copy_is_not_added_twice(self):
        assignee = UserFactory(email="olive@example.com")

        self._run_with_assignee(assignee)

        message = mail.outbox[0]
        self.assertEqual(message.bcc.count(ADMIN), 1)
        self.assertEqual(message.cc, [])
