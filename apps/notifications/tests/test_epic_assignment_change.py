"""Tests for notifying people when an Epic's assignees change.

Three groups need different treatment, and getting them confused is the failure
this covers:

* somebody newly added has never seen the epic, so they are told about the epic
  itself rather than handed a diff naming strangers;
* somebody already assigned wants the change as context, reported alongside any
  other edit in the same save;
* somebody removed is told nothing - the link would lead to work that is no
  longer theirs.
"""

from django.core import mail
from django.test import TestCase, override_settings

from apps.issues.assignments import AssignmentDiff, set_epic_assignees
from apps.issues.changes import describe_assignment_change
from apps.issues.factories import EpicAssignmentFactory, EpicFactory
from apps.notifications.models import NotificationPreference
from apps.notifications.services import notify_epic_assignment_change
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import MembershipFactory, WorkspaceFactory


class DescribeAssignmentChangeTest(TestCase):
    """Rendering the delta as a change entry the email template understands."""

    @classmethod
    def setUpTestData(cls):
        cls.added = UserFactory(first_name="Khadija", last_name="Noor")
        cls.removed = UserFactory(first_name="Awais", last_name="Raza")

    def test_an_addition_is_described(self):
        entry = describe_assignment_change(AssignmentDiff(added=[self.added]))[0]

        self.assertIn("Khadija Noor", entry["new_value"])
        self.assertIn("Added", entry["new_value"])

    def test_a_removal_is_described(self):
        entry = describe_assignment_change(AssignmentDiff(removed=[self.removed]))[0]

        self.assertIn("Awais Raza", entry["new_value"])
        self.assertIn("Removed", entry["new_value"])

    def test_both_halves_appear_together(self):
        diff = AssignmentDiff(added=[self.added], removed=[self.removed])

        entry = describe_assignment_change(diff)[0]

        self.assertIn("Khadija Noor", entry["new_value"])
        self.assertIn("Awais Raza", entry["new_value"])

    def test_several_names_are_listed(self):
        other = UserFactory(first_name="Yasir", last_name="Kashif")

        entry = describe_assignment_change(AssignmentDiff(added=[self.added, other]))[0]

        self.assertIn("Khadija Noor", entry["new_value"])
        self.assertIn("Yasir Kashif", entry["new_value"])

    def test_no_change_describes_nothing(self):
        """A save that re-submits the same people must not report an edit."""
        self.assertEqual([], describe_assignment_change(AssignmentDiff()))

    def test_people_who_merely_stayed_are_not_reported(self):
        """Remaining assignees are not news; only the delta is."""
        stayed = UserFactory(first_name="Sam", last_name="Stayed")

        self.assertEqual([], describe_assignment_change(AssignmentDiff(remaining=[stayed])))

    def test_it_matches_the_shape_the_template_expects(self):
        """Same keys as changes.diff(), so one template renders both."""
        entry = describe_assignment_change(AssignmentDiff(added=[self.added]))[0]

        self.assertEqual({"field", "label", "old_value", "new_value"}, set(entry))
        self.assertEqual("assignees", entry["field"])
        self.assertIsNone(entry["old_value"])


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EPIC_ACTIVITY_ADMIN_CC=[],
    NOTIFICATION_COPY_TO=[],
)
class NotifyEpicAssignmentChangeTest(TestCase):
    """Who is emailed, and what they are told."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory(email="owner@example.com", first_name="Olive", last_name="Owner")
        cls.newcomer = UserFactory(email="newcomer@example.com", first_name="Khadija", last_name="Noor")
        cls.leaver = UserFactory(email="leaver@example.com", first_name="Awais", last_name="Raza")
        cls.editor = UserFactory(email="editor@example.com", first_name="Eddie", last_name="Editor")
        for user in (cls.owner, cls.newcomer, cls.leaver, cls.editor):
            MembershipFactory(workspace=cls.workspace, user=user, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = EpicFactory(project=self.project, title="Payment System", assignee=self.owner)
        mail.outbox = []

    def _change(self, users, actor=None):
        """Apply an assignee set and run the resulting notifications."""
        with self.captureOnCommitCallbacks(execute=True):
            diff = set_epic_assignees(self.epic, users, actor=actor or self.editor)
            notify_epic_assignment_change(self.epic, diff, actor=actor or self.editor)
        return diff

    def _addressed(self):
        return sorted(address for message in mail.outbox for address in message.to)

    def test_a_newly_added_person_is_emailed(self):
        self._change([self.newcomer])

        self.assertIn("newcomer@example.com", self._addressed())

    def test_a_newly_added_person_is_told_about_the_epic(self):
        """They have never seen it, so a diff naming other people is useless."""
        self._change([self.newcomer])

        message = next(m for m in mail.outbox if m.to == ["newcomer@example.com"])
        self.assertIn("Payment System", message.subject + message.body)

    def test_an_existing_assignee_is_told_what_changed(self):
        EpicAssignmentFactory(epic=self.epic, user=self.leaver)
        mail.outbox = []

        self._change([self.newcomer])

        message = next(m for m in mail.outbox if m.to == ["owner@example.com"])
        self.assertIn("Khadija Noor", message.body)
        self.assertIn("Awais Raza", message.body)

    def test_a_removed_person_is_not_emailed(self):
        """The link would lead to work that is no longer theirs."""
        EpicAssignmentFactory(epic=self.epic, user=self.leaver)
        mail.outbox = []

        self._change([])

        self.assertNotIn("leaver@example.com", self._addressed())

    def test_the_actor_is_not_emailed_for_their_own_change(self):
        self._change([self.editor], actor=self.editor)

        self.assertNotIn("editor@example.com", self._addressed())

    def test_adding_yourself_sends_you_nothing(self):
        """You know you just did it."""
        self._change([self.editor], actor=self.editor)

        self.assertEqual(["owner@example.com"], self._addressed())

    def test_a_no_op_change_emails_nobody(self):
        """Re-submitting the same people is not an edit."""
        self._change([self.newcomer])
        mail.outbox = []

        self._change([self.newcomer])

        self.assertEqual(0, len(mail.outbox))

    def test_an_opted_out_newcomer_is_skipped(self):
        preference = NotificationPreference.objects.for_user(self.newcomer)
        preference.notify_on_assignment = False
        preference.save(update_fields=["notify_on_assignment"])

        self._change([self.newcomer])

        self.assertNotIn("newcomer@example.com", self._addressed())

    def test_a_newcomer_who_left_the_workspace_is_skipped(self):
        outsider = UserFactory(email="outsider@example.com")

        self._change([outsider])

        self.assertNotIn("outsider@example.com", self._addressed())

    def test_it_reports_how_many_notifications_were_queued(self):
        count = notify_epic_assignment_change(
            self.epic,
            AssignmentDiff(added=[self.newcomer]),
            actor=self.editor,
        )

        self.assertGreaterEqual(count, 1)

    def test_an_empty_diff_queues_nothing(self):
        count = notify_epic_assignment_change(self.epic, AssignmentDiff(), actor=self.editor)

        self.assertEqual(0, count)
        self.assertEqual(0, len(mail.outbox))

    def test_adding_and_removing_in_one_edit_reaches_both_groups(self):
        """The full three-way split, end to end."""
        EpicAssignmentFactory(epic=self.epic, user=self.leaver)
        mail.outbox = []

        self._change([self.newcomer])

        addressed = self._addressed()
        self.assertIn("newcomer@example.com", addressed)  # added -> told about the epic
        self.assertIn("owner@example.com", addressed)  # stayed -> told what changed
        self.assertNotIn("leaver@example.com", addressed)  # removed -> told nothing

    def test_each_recipient_gets_one_email(self):
        EpicAssignmentFactory(epic=self.epic, user=self.leaver)
        mail.outbox = []

        self._change([self.newcomer])

        addressed = self._addressed()
        self.assertEqual(len(addressed), len(set(addressed)))
