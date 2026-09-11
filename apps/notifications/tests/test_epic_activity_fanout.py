"""Tests for epic-activity email fan-out.

One edit to a work item under an Epic produces one email *per assignee*, not one
email addressed to all of them. These tests are about that multiplication: that
everybody gets a message, that each message is addressed only to its own
recipient, that per-person rules (opt-out, no address) are applied per person
rather than to the batch, and that the audit copy is attached exactly once.

Delivery itself is exercised through Django's locmem backend rather than mocked,
so what is asserted is the messages that would actually leave the system -
headers included.
"""

from django.core import mail
from django.test import TestCase, override_settings

from apps.issues.factories import EpicAssignmentFactory, EpicFactory, StoryFactory
from apps.notifications.models import NotificationPreference
from apps.notifications.tasks import send_epic_activity_email
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership

CHANGES = [{"field": "status", "label": "Status", "old_value": "Draft", "new_value": "Done"}]


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EPIC_ACTIVITY_ADMIN_CC=[],
    NOTIFICATION_COPY_TO=[],
)
class EpicActivityFanOutTest(TestCase):
    """One event, one message per assignee."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory(email="owner@example.com", first_name="Olive", last_name="Owner")
        cls.second = UserFactory(email="second@example.com", first_name="Sam", last_name="Second")
        cls.third = UserFactory(email="third@example.com", first_name="Tara", last_name="Third")
        cls.editor = UserFactory(email="editor@example.com", first_name="Eddie", last_name="Editor")

        for user in (cls.owner, cls.second, cls.third, cls.editor):
            Membership.objects.create(workspace=cls.workspace, user=user, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = EpicFactory(project=self.project, title="Payment System", assignee=self.owner)
        self.story = StoryFactory(project=self.project, title="Build checkout", parent=self.epic)
        mail.outbox = []

    def _send(self, actor=None):
        return send_epic_activity_email(
            self.story.pk,
            self.epic.pk,
            CHANGES,
            (actor or self.editor).pk,
            False,
        )

    def _addressed_to(self):
        return sorted(address for message in mail.outbox for address in message.to)

    def test_a_single_assignee_receives_one_email(self):
        """The pre-existing behaviour, unchanged."""
        self._send()

        self.assertEqual(1, len(mail.outbox))
        self.assertEqual(["owner@example.com"], mail.outbox[0].to)

    def test_every_assignee_receives_an_email(self):
        """The requirement: three assignees, three emails."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)
        EpicAssignmentFactory(epic=self.epic, user=self.third)

        self._send()

        self.assertEqual(3, len(mail.outbox))
        self.assertEqual(
            ["owner@example.com", "second@example.com", "third@example.com"],
            self._addressed_to(),
        )

    def test_each_message_is_addressed_to_one_person_only(self):
        """Recipients must not see each other's addresses."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        self._send()

        for message in mail.outbox:
            self.assertEqual(1, len(message.to))

    def test_each_message_carries_its_own_unsubscribe_link(self):
        """A shared message could not do this - the token is per person."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        self._send()

        tokens = set()
        for message in mail.outbox:
            recipient = NotificationPreference.objects.get(user__email=message.to[0])
            header = message.extra_headers["List-Unsubscribe"]
            self.assertIn(str(recipient.unsubscribe_token), header)
            tokens.add(header)

        self.assertEqual(2, len(tokens))

    def test_the_actor_is_not_emailed(self):
        """Even when they are one of the assignees."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        self._send(actor=self.second)

        self.assertEqual(["owner@example.com"], self._addressed_to())

    def test_an_opted_out_assignee_is_skipped_but_the_others_are_not(self):
        """Preferences are per person, so they must not suppress the batch."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)
        preference = NotificationPreference.objects.for_user(self.second)
        preference.notify_on_epic_activity = False
        preference.save(update_fields=["notify_on_epic_activity"])

        self._send()

        self.assertEqual(["owner@example.com"], self._addressed_to())

    def test_an_inactive_assignee_is_skipped_but_the_others_are_not(self):
        EpicAssignmentFactory(epic=self.epic, user=self.second)
        self.second.is_active = False
        self.second.save(update_fields=["is_active"])

        self._send()

        self.assertEqual(["owner@example.com"], self._addressed_to())

    def test_an_assignee_without_an_address_is_skipped_but_the_others_are_not(self):
        """One unusable address must not cost everybody else their email."""
        no_address = UserFactory(email="")
        Membership.objects.create(workspace=self.workspace, user=no_address, role=roles.ROLE_MEMBER)
        EpicAssignmentFactory(epic=self.epic, user=no_address)

        self._send()

        self.assertEqual(["owner@example.com"], self._addressed_to())

    def test_it_reports_success_when_at_least_one_message_was_sent(self):
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        self.assertTrue(self._send())

    def test_it_reports_failure_when_every_recipient_was_declined(self):
        """False is a normal outcome here, not an error."""
        preference = NotificationPreference.objects.for_user(self.owner)
        preference.notify_on_epic_activity = False
        preference.save(update_fields=["notify_on_epic_activity"])

        self.assertFalse(self._send())
        self.assertEqual(0, len(mail.outbox))

    def test_an_unassigned_epic_sends_nothing(self):
        epic = EpicFactory(project=self.project, assignee=None)
        story = StoryFactory(project=self.project, parent=epic)

        result = send_epic_activity_email(story.pk, epic.pk, CHANGES, self.editor.pk, False)

        self.assertFalse(result)
        self.assertEqual(0, len(mail.outbox))

    def test_a_deleted_epic_sends_nothing(self):
        """The task can run after its subject was removed."""
        epic_id = self.epic.pk
        story_id = self.story.pk
        self.epic.delete()

        result = send_epic_activity_email(story_id, epic_id, CHANGES, self.editor.pk, False)

        self.assertFalse(result)
        self.assertEqual(0, len(mail.outbox))

    def test_every_message_describes_the_same_change(self):
        """One event, one story - the wording must not vary per recipient."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        self._send()

        subjects = {message.subject for message in mail.outbox}
        self.assertEqual(1, len(subjects))
        for message in mail.outbox:
            self.assertIn("Draft", message.body)
            self.assertIn("Done", message.body)

    def test_recipients_are_resolved_at_send_time_not_queue_time(self):
        """An assignee added after the edit still receives the email.

        The task takes ids, not addresses, precisely so that the answer reflects
        the epic as it stands when the worker runs.
        """
        EpicAssignmentFactory(epic=self.epic, user=self.third)

        self._send()

        self.assertIn("third@example.com", self._addressed_to())


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    NOTIFICATION_COPY_TO=[],
    EPIC_ACTIVITY_ADMIN_CC=["audit@example.com"],
    NOTIFICATION_COPY_MODE="cc",
)
class EpicActivityFanOutAdminCopyTest(TestCase):
    """The audit copy must be attached once, not once per recipient."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory(email="owner@example.com")
        cls.second = UserFactory(email="second@example.com")
        cls.editor = UserFactory(email="editor@example.com")
        for user in (cls.owner, cls.second, cls.editor):
            Membership.objects.create(workspace=cls.workspace, user=user, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = EpicFactory(project=self.project, assignee=self.owner)
        self.story = StoryFactory(project=self.project, parent=self.epic)
        mail.outbox = []

    def _send(self):
        return send_epic_activity_email(self.story.pk, self.epic.pk, CHANGES, self.editor.pk, False)

    def _copies(self):
        return [address for message in mail.outbox for address in (message.cc or []) + (message.bcc or [])]

    def test_the_audit_address_is_copied_once_for_several_recipients(self):
        """Copying it per message would mail the auditor once per assignee."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        self._send()

        self.assertEqual(2, len(mail.outbox))
        self.assertEqual(["audit@example.com"], self._copies())

    def test_the_audit_address_is_still_copied_for_a_single_recipient(self):
        self._send()

        self.assertEqual(["audit@example.com"], self._copies())

    @override_settings(EPIC_ACTIVITY_ADMIN_CC=["second@example.com"])
    def test_an_auditor_who_is_also_an_assignee_is_not_copied_as_well(self):
        """They are already receiving their own message; a copy would duplicate it."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        self._send()

        self.assertEqual(2, len(mail.outbox))
        self.assertEqual([], self._copies())
        self.assertIn("second@example.com", [address for m in mail.outbox for address in m.to])

    @override_settings(EPIC_ACTIVITY_ADMIN_CC=[])
    def test_no_audit_address_means_no_copies(self):
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        self._send()

        self.assertEqual([], self._copies())

    def test_an_unassigned_epic_still_reaches_the_audit_address(self):
        """The admin-only path is unchanged by fan-out."""
        epic = EpicFactory(project=self.project, assignee=None)
        story = StoryFactory(project=self.project, parent=epic)

        result = send_epic_activity_email(story.pk, epic.pk, CHANGES, self.editor.pk, False)

        self.assertTrue(result)
        self.assertEqual(1, len(mail.outbox))
        self.assertEqual(["audit@example.com"], mail.outbox[0].to)
