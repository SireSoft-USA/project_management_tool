"""Tests for the EpicAssignment model.

EpicAssignment is what makes an Epic notifiable to several people. Nothing reads
it yet - recipient resolution arrives in a later step - so what matters here is
that the table itself cannot get into a state the notification code would later
have to defend against: duplicate rows (a duplicate email), orphaned rows
(pointing at a deleted epic or user), or a missing row for somebody who was
already an assignee before the migration ran.
"""

from importlib import import_module

from django.apps import apps as live_registry
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from apps.issues.factories import EpicAssignmentFactory, EpicFactory
from apps.issues.models import Epic, EpicAssignment

from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory

# The migration module name starts with a digit, so it cannot be imported with
# normal import syntax. Loading it by path is what lets these tests exercise the
# shipped migration code rather than a copy of it.
_migration = import_module("apps.issues.migrations.0012_backfill_epic_assignments")
backfill_assignments = _migration.backfill_assignments
remove_backfilled_assignments = _migration.remove_backfilled_assignments


class EpicAssignmentModelTest(TestCase):
    """Field behaviour and the relationships the notification code will rely on."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()
        cls.epic = EpicFactory(project=cls.project)
        cls.user = UserFactory()
        cls.other_user = UserFactory()

    def test_an_epic_can_have_several_assignees(self):
        """The whole point of the model."""
        EpicAssignmentFactory(epic=self.epic, user=self.user)
        EpicAssignmentFactory(epic=self.epic, user=self.other_user)

        self.assertEqual(2, self.epic.assignments.count())

    def test_assignments_are_reachable_from_the_epic(self):
        """related_name is what the recipient resolver will query."""
        assignment = EpicAssignmentFactory(epic=self.epic, user=self.user)

        self.assertIn(assignment, self.epic.assignments.all())

    def test_assignments_are_reachable_from_the_user(self):
        """The reverse question: which epics is this person on?"""
        assignment = EpicAssignmentFactory(epic=self.epic, user=self.user)

        self.assertIn(assignment, self.user.epic_assignments.all())

    def test_a_user_can_be_assigned_to_several_epics(self):
        other_epic = EpicFactory(project=self.project)
        EpicAssignmentFactory(epic=self.epic, user=self.user)
        EpicAssignmentFactory(epic=other_epic, user=self.user)

        self.assertEqual(2, self.user.epic_assignments.count())

    def test_added_by_records_who_made_the_assignment(self):
        actor = UserFactory()

        assignment = EpicAssignmentFactory(epic=self.epic, user=self.user, added_by=actor)

        self.assertEqual(actor, assignment.added_by)

    def test_added_by_is_optional(self):
        """The backfill migration leaves it NULL: nobody performed those."""
        assignment = EpicAssignmentFactory(epic=self.epic, user=self.user, added_by=None)

        self.assertIsNone(assignment.added_by)

    def test_timestamps_are_populated(self):
        """Inherited from BaseModel, as with every other model in the project."""
        assignment = EpicAssignmentFactory(epic=self.epic, user=self.user)

        self.assertIsNotNone(assignment.created_at)
        self.assertIsNotNone(assignment.updated_at)

    def test_str_names_both_sides(self):
        assignment = EpicAssignmentFactory(epic=self.epic, user=self.user)

        self.assertIn(str(self.user), str(assignment))
        self.assertIn(str(self.epic), str(assignment))

    def test_default_ordering_is_oldest_first(self):
        """Stable ordering keeps a rendered recipient list from reshuffling."""
        first = EpicAssignmentFactory(epic=self.epic, user=self.user)
        second = EpicAssignmentFactory(epic=self.epic, user=self.other_user)

        self.assertEqual([first, second], list(self.epic.assignments.all()))


class EpicAssignmentConstraintTest(TestCase):
    """The database must be what prevents a duplicate, not the calling code."""

    @classmethod
    def setUpTestData(cls):
        cls.epic = EpicFactory()
        cls.user = UserFactory()

    def test_the_same_user_cannot_be_assigned_twice(self):
        """A duplicate row would mean the same person emailed twice."""
        EpicAssignmentFactory(epic=self.epic, user=self.user)

        with self.assertRaises(IntegrityError), transaction.atomic():
            EpicAssignment.objects.create(epic=self.epic, user=self.user)

    def test_the_constraint_is_scoped_to_one_epic(self):
        """Being on epic A must not block being on epic B."""
        other_epic = EpicFactory(project=self.epic.project)

        EpicAssignmentFactory(epic=self.epic, user=self.user)
        EpicAssignmentFactory(epic=other_epic, user=self.user)

        self.assertEqual(2, EpicAssignment.objects.filter(user=self.user).count())

    def test_the_constraint_is_scoped_to_one_user(self):
        """Two different people on the same epic is the normal case."""
        other_user = UserFactory()

        EpicAssignmentFactory(epic=self.epic, user=self.user)
        EpicAssignmentFactory(epic=self.epic, user=other_user)

        self.assertEqual(2, EpicAssignment.objects.filter(epic=self.epic).count())

    def test_the_constraint_is_named_for_the_migration(self):
        """The name is part of the schema; renaming it needs a migration."""
        names = {c.name for c in EpicAssignment._meta.constraints}

        self.assertIn("unique_epic_assignee", names)


class EpicAssignmentCascadeTest(TestCase):
    """Deleting either side must not leave a row pointing at nothing."""

    def test_deleting_the_epic_removes_its_assignments(self):
        epic = EpicFactory()
        EpicAssignmentFactory(epic=epic, user=UserFactory())

        epic.delete()

        self.assertEqual(0, EpicAssignment.objects.count())

    def test_deleting_the_user_removes_their_assignments(self):
        """Otherwise the resolver would later load a user that no longer exists."""
        user = UserFactory()
        EpicAssignmentFactory(epic=EpicFactory(), user=user)

        user.delete()

        self.assertEqual(0, EpicAssignment.objects.count())

    def test_deleting_the_assigning_user_keeps_the_assignment(self):
        """added_by is SET_NULL: removing the actor must not un-assign anybody.

        A CASCADE here would mean deleting one account silently stops a
        *different* person being notified.
        """
        actor = UserFactory()
        assignment = EpicAssignmentFactory(epic=EpicFactory(), user=UserFactory(), added_by=actor)

        actor.delete()
        assignment.refresh_from_db()

        self.assertIsNone(assignment.added_by)
        self.assertTrue(EpicAssignment.objects.filter(pk=assignment.pk).exists())

    def test_deleting_the_project_removes_assignments_through_the_epic(self):
        """Project -> Epic -> EpicAssignment must cascade the whole way down."""
        project = ProjectFactory()
        EpicAssignmentFactory(epic=EpicFactory(project=project), user=UserFactory())

        project.delete()

        self.assertEqual(0, EpicAssignment.objects.count())

    def test_an_assignment_does_not_block_deleting_an_epic(self):
        """Guards against someone changing on_delete to PROTECT by accident."""
        epic = EpicFactory()
        EpicAssignmentFactory(epic=epic, user=UserFactory())

        try:
            epic.delete()
        except ProtectedError:  # pragma: no cover - only reachable if on_delete regresses
            self.fail("an assignment must not prevent its epic from being deleted")


class EpicAssignmentBackfillTest(TestCase):
    """The backfill logic, exercised against real rows.

    Applied as a data migration in 0012; re-implemented here against the live
    models so the *rule* is tested (every Epic with a primary assignee ends up
    with a matching assignment, and nothing else does) without re-running the
    migration framework.
    """

    def _backfill(self):
        """Mirror of migration 0012's forward function."""
        EpicAssignment.objects.bulk_create(
            [
                EpicAssignment(epic_id=epic_id, user_id=assignee_id, added_by=None)
                for epic_id, assignee_id in Epic.objects.exclude(assignee__isnull=True).values_list("pk", "assignee_id")
            ],
            batch_size=500,
            ignore_conflicts=True,
        )

    def test_an_epic_with_an_assignee_gets_one_assignment(self):
        user = UserFactory()
        epic = EpicFactory(assignee=user)

        self._backfill()

        self.assertTrue(EpicAssignment.objects.filter(epic=epic, user=user).exists())

    def test_an_unassigned_epic_gets_nothing(self):
        """Inventing a recipient for an unowned epic would email the wrong person."""
        EpicFactory(assignee=None)

        self._backfill()

        self.assertEqual(0, EpicAssignment.objects.count())

    def test_the_backfill_is_idempotent(self):
        """Re-running must not fail on the unique constraint or duplicate rows."""
        user = UserFactory()
        EpicFactory(assignee=user)

        self._backfill()
        self._backfill()

        self.assertEqual(1, EpicAssignment.objects.count())

    def test_the_backfill_leaves_added_by_null(self):
        """Nobody performed these; attributing them to a user would be a lie."""
        EpicFactory(assignee=UserFactory())

        self._backfill()

        self.assertIsNone(EpicAssignment.objects.get().added_by)

    def test_the_backfill_does_not_disturb_existing_assignments(self):
        """A row added through the UI must survive a re-run."""
        user = UserFactory()
        epic = EpicFactory(assignee=user)
        extra = UserFactory()
        EpicAssignmentFactory(epic=epic, user=extra)

        self._backfill()

        self.assertEqual({user, extra}, {a.user for a in epic.assignments.all()})

    def test_every_assigned_epic_is_covered(self):
        """The property that matters: nobody currently notified is dropped."""
        assigned = [EpicFactory(assignee=UserFactory()) for _ in range(3)]
        EpicFactory(assignee=None)

        self._backfill()

        self.assertEqual(len(assigned), EpicAssignment.objects.count())
        for epic in assigned:
            self.assertTrue(EpicAssignment.objects.filter(epic=epic, user=epic.assignee).exists())


class EpicAssignmentMigrationFunctionTest(TestCase):
    """Exercise migration 0012's own functions, not a re-implementation.

    EpicAssignmentBackfillTest covers the rule; this covers the shipped code -
    the actual callables the migration runs, so a mistake in the migration file
    itself is caught rather than only a mistake in a copy of its logic.

    The functions are called with Django's live app registry in place of the
    frozen historical one. That is sound here because neither model has changed
    shape since 0012: the migration only reads Epic.assignee and writes
    EpicAssignment rows, and both look the same in both registries. Driving the
    real MigrationExecutor instead would force a database flush, which
    PostgreSQL refuses on tables referenced by foreign keys.
    """

    def _run(self, function):
        function(live_registry, connection.schema_editor)

    def test_forward_creates_an_assignment_for_an_existing_assignee(self):
        user = UserFactory()
        epic = EpicFactory(assignee=user)
        EpicAssignment.objects.all().delete()

        self._run(backfill_assignments)

        self.assertTrue(EpicAssignment.objects.filter(epic=epic, user=user).exists())

    def test_forward_ignores_epics_with_no_assignee(self):
        EpicFactory(assignee=None)

        self._run(backfill_assignments)

        self.assertEqual(0, EpicAssignment.objects.count())

    def test_forward_can_be_applied_twice(self):
        """ignore_conflicts must absorb a re-run against existing rows."""
        EpicFactory(assignee=UserFactory())
        EpicAssignment.objects.all().delete()

        self._run(backfill_assignments)
        self._run(backfill_assignments)

        self.assertEqual(1, EpicAssignment.objects.count())

    def test_reverse_removes_the_backfilled_row(self):
        user = UserFactory()
        epic = EpicFactory(assignee=user)
        EpicAssignment.objects.all().delete()
        self._run(backfill_assignments)

        self._run(remove_backfilled_assignments)

        self.assertFalse(EpicAssignment.objects.filter(epic=epic, user=user).exists())

    def test_reverse_keeps_assignments_that_were_added_by_a_person(self):
        """Rolling back must not discard extra assignees added through the UI."""
        epic = EpicFactory(assignee=UserFactory())
        EpicAssignment.objects.all().delete()
        kept = EpicAssignmentFactory(epic=epic, user=UserFactory(), added_by=UserFactory())

        self._run(remove_backfilled_assignments)

        self.assertTrue(EpicAssignment.objects.filter(pk=kept.pk).exists())

    def test_forward_then_reverse_returns_to_an_empty_table(self):
        """The round trip must leave nothing behind."""
        for _ in range(3):
            EpicFactory(assignee=UserFactory())
        EpicAssignment.objects.all().delete()

        self._run(backfill_assignments)
        self._run(remove_backfilled_assignments)

        self.assertEqual(0, EpicAssignment.objects.count())
