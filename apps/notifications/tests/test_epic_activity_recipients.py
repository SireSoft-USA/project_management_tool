"""Tests for epic-activity recipient resolution.

``epic_activity_recipients`` is the single rule deciding who hears about a change
to an Epic. Two callers depend on it agreeing with itself - the service layer
asks at edit time whether anybody can be told, and the Celery task asks again at
send time - so the rules are tested here once, directly, rather than inferred
from the behaviour of either caller.

What is deliberately NOT tested here: whether a recipient is active, has an email
address, or has opted out. Those guards live in ``emails._check_guards`` and are
applied to every send; asserting them here as well would encourage a second
implementation of them.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.issues.factories import EpicAssignmentFactory, EpicFactory
from apps.issues.models import EpicAssignment
from apps.notifications.recipients import epic_activity_recipient, epic_activity_recipients
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership


class EpicActivityRecipientsTest(TestCase):
    """The plural resolver: who is told about an epic-level change."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory(first_name="Olive", last_name="Owner")
        cls.second = UserFactory(first_name="Sam", last_name="Second")
        cls.third = UserFactory(first_name="Tara", last_name="Third")
        cls.editor = UserFactory(first_name="Eddie", last_name="Editor")
        cls.outsider = UserFactory(first_name="Oscar", last_name="Outsider")

        for user in (cls.owner, cls.second, cls.third, cls.editor):
            Membership.objects.create(workspace=cls.workspace, user=user, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = EpicFactory(project=self.project, assignee=self.owner)

    def test_the_primary_assignee_is_a_recipient(self):
        """The behaviour that existed before multi-assignee, unchanged."""
        self.assertEqual([self.owner], epic_activity_recipients(self.epic, self.editor))

    def test_every_assignee_is_a_recipient(self):
        """The requirement: all assigned people hear about the change."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)
        EpicAssignmentFactory(epic=self.epic, user=self.third)

        recipients = epic_activity_recipients(self.epic, self.editor)

        self.assertEqual({self.owner, self.second, self.third}, set(recipients))

    def test_an_unassigned_epic_has_no_recipients(self):
        """Nobody to tell, rather than telling somebody arbitrary."""
        epic = EpicFactory(project=self.project, assignee=None)

        self.assertEqual([], epic_activity_recipients(epic, self.editor))

    def test_an_epic_with_only_secondary_assignees_still_notifies_them(self):
        """The primary field being empty must not suppress the others."""
        epic = EpicFactory(project=self.project, assignee=None)
        EpicAssignmentFactory(epic=epic, user=self.second)

        self.assertEqual([self.second], epic_activity_recipients(epic, self.editor))

    def test_the_actor_is_excluded(self):
        """You do not need an email about what you just did."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        recipients = epic_activity_recipients(self.epic, actor=self.second)

        self.assertEqual([self.owner], recipients)

    def test_the_actor_is_excluded_even_when_they_are_the_primary_assignee(self):
        recipients = epic_activity_recipients(self.epic, actor=self.owner)

        self.assertEqual([], recipients)

    def test_a_system_change_with_no_actor_notifies_everybody(self):
        """actor=None means nobody performed it, so nobody is excluded."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        recipients = epic_activity_recipients(self.epic, actor=None)

        self.assertEqual({self.owner, self.second}, set(recipients))

    def test_a_user_who_left_the_workspace_is_excluded(self):
        """They would only receive a link they can no longer open."""
        EpicAssignmentFactory(epic=self.epic, user=self.outsider)

        recipients = epic_activity_recipients(self.epic, self.editor)

        self.assertEqual([self.owner], recipients)

    def test_a_primary_assignee_who_left_the_workspace_is_excluded(self):
        epic = EpicFactory(project=self.project, assignee=self.outsider)

        self.assertEqual([], epic_activity_recipients(epic, self.editor))

    def test_removing_a_membership_removes_the_recipient(self):
        """Resolution reflects membership now, not when the row was written."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)
        Membership.objects.filter(workspace=self.workspace, user=self.second).delete()

        self.assertEqual([self.owner], epic_activity_recipients(self.epic, self.editor))

    def test_a_person_assigned_twice_over_is_one_recipient(self):
        """Reached through both the primary field and an assignment row.

        A duplicate here would mean the same person emailed twice about one edit.
        """
        EpicAssignment.objects.get_or_create(epic=self.epic, user=self.owner)

        recipients = epic_activity_recipients(self.epic, self.editor)

        self.assertEqual([self.owner], recipients)
        self.assertEqual(1, len(recipients))

    def test_assignees_of_another_epic_are_not_included(self):
        """Assignment is per epic, not per project."""
        other_epic = EpicFactory(project=self.project, assignee=None)
        EpicAssignmentFactory(epic=other_epic, user=self.second)

        self.assertEqual([self.owner], epic_activity_recipients(self.epic, self.editor))

    def test_the_primary_assignee_is_listed_first(self):
        """Stable, meaningful order: the owner leads, whatever the row dates."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)
        EpicAssignmentFactory(epic=self.epic, user=self.third)

        recipients = epic_activity_recipients(self.epic, self.editor)

        self.assertEqual(self.owner, recipients[0])

    def test_the_order_is_stable_across_calls(self):
        """Two emails about one epic must not list recipients differently."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)
        EpicAssignmentFactory(epic=self.epic, user=self.third)

        self.assertEqual(
            epic_activity_recipients(self.epic, self.editor),
            epic_activity_recipients(self.epic, self.editor),
        )

    def test_removing_an_assignment_removes_the_recipient(self):
        EpicAssignmentFactory(epic=self.epic, user=self.second)
        EpicAssignment.objects.filter(epic=self.epic, user=self.second).delete()

        self.assertEqual([self.owner], epic_activity_recipients(self.epic, self.editor))

    def test_a_primary_assignee_without_an_assignment_row_is_still_notified(self):
        """Belt and braces for a row deleted directly in the database.

        The signal keeps the two in step, so this should not arise - but the
        owner silently losing their own notifications is bad enough to guard
        against explicitly.
        """
        EpicAssignment.objects.filter(epic=self.epic, user=self.owner).delete()

        self.assertEqual([self.owner], epic_activity_recipients(self.epic, self.editor))


class EpicActivityRecipientQueryCostTest(TestCase):
    """Resolution must not cost a query per recipient."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory()
        cls.editor = UserFactory()
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role=roles.ROLE_MEMBER)
        Membership.objects.create(workspace=cls.workspace, user=cls.editor, role=roles.ROLE_MEMBER)

    def _resolve(self, epic):
        with CaptureQueriesContext(connection) as queries:
            epic_activity_recipients(epic, self.editor)
        return len(queries)

    def test_one_assignee_costs_one_query(self):
        epic = EpicFactory(project=self.project, assignee=self.owner)

        self.assertEqual(1, self._resolve(epic))

    def test_many_assignees_cost_the_same_single_query(self):
        """The property that matters: cost is flat, not per recipient."""
        epic = EpicFactory(project=self.project, assignee=self.owner)
        for _ in range(10):
            user = UserFactory()
            Membership.objects.create(workspace=self.workspace, user=user, role=roles.ROLE_MEMBER)
            EpicAssignmentFactory(epic=epic, user=user)

        self.assertEqual(11, len(epic_activity_recipients(epic, self.editor)))
        self.assertEqual(1, self._resolve(epic))

    def test_an_unassigned_epic_costs_one_query(self):
        epic = EpicFactory(project=self.project, assignee=None)

        self.assertEqual(1, self._resolve(epic))


class EpicActivityRecipientSingularTest(TestCase):
    """The singular wrapper must stay consistent with the plural rule.

    It exists so the service layer can cheaply ask "is there anybody to tell?".
    Because it delegates rather than re-implementing, these tests are about the
    delegation holding - not about re-proving the rules above.
    """

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory()
        cls.second = UserFactory()
        cls.editor = UserFactory()
        for user in (cls.owner, cls.second, cls.editor):
            Membership.objects.create(workspace=cls.workspace, user=user, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = EpicFactory(project=self.project, assignee=self.owner)

    def test_it_returns_the_first_recipient(self):
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        self.assertEqual(self.owner, epic_activity_recipient(self.epic, self.editor))

    def test_it_returns_none_when_there_are_no_recipients(self):
        epic = EpicFactory(project=self.project, assignee=None)

        self.assertIsNone(epic_activity_recipient(epic, self.editor))

    def test_it_returns_none_when_the_only_recipient_is_the_actor(self):
        self.assertIsNone(epic_activity_recipient(self.epic, self.owner))

    def test_it_agrees_with_the_plural_resolver(self):
        """The invariant that stops the two drifting apart."""
        EpicAssignmentFactory(epic=self.epic, user=self.second)

        for actor in (self.editor, self.owner, self.second, None):
            with self.subTest(actor=actor):
                plural = epic_activity_recipients(self.epic, actor)
                expected = plural[0] if plural else None

                self.assertEqual(expected, epic_activity_recipient(self.epic, actor))


class EpicPrimaryAssigneeMirrorTest(TestCase):
    """Epic.assignee must always appear in the Epic's own assignment rows.

    Resolution reads the assignment rows, so an owner missing from them would
    stop being notified. The backfill migration established this for existing
    Epics; a post_save handler keeps it true for new ones, whichever view or
    factory created them.
    """

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory()
        cls.replacement = UserFactory()
        for user in (cls.owner, cls.replacement):
            Membership.objects.create(workspace=cls.workspace, user=user, role=roles.ROLE_MEMBER)

    def test_creating_an_assigned_epic_creates_the_assignment_row(self):
        epic = EpicFactory(project=self.project, assignee=self.owner)

        self.assertTrue(epic.assignments.filter(user=self.owner).exists())

    def test_creating_an_unassigned_epic_creates_nothing(self):
        epic = EpicFactory(project=self.project, assignee=None)

        self.assertEqual(0, epic.assignments.count())

    def test_reassigning_adds_the_new_owner(self):
        epic = EpicFactory(project=self.project, assignee=self.owner)

        epic.assignee = self.replacement
        epic.save()

        self.assertTrue(epic.assignments.filter(user=self.replacement).exists())

    def test_reassigning_keeps_the_previous_owner_as_an_assignee(self):
        """Only ever adds.

        The previous owner may have been deliberately kept on as an additional
        assignee. Dropping them because the *primary* field changed would
        silently remove a recipient; removal is an explicit action.
        """
        epic = EpicFactory(project=self.project, assignee=self.owner)

        epic.assignee = self.replacement
        epic.save()

        self.assertTrue(epic.assignments.filter(user=self.owner).exists())

    def test_saving_an_unchanged_epic_does_not_duplicate_the_row(self):
        """get_or_create, not create: the unique constraint would otherwise raise."""
        epic = EpicFactory(project=self.project, assignee=self.owner)

        epic.save()
        epic.save()

        self.assertEqual(1, epic.assignments.filter(user=self.owner).count())
