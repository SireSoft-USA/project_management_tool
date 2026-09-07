from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.issues.factories import BugFactory, ChoreFactory, EpicFactory, MilestoneFactory, StoryFactory
from apps.issues.activity import EPIC_INACTIVITY_ALERT_AFTER
from apps.issues.models import BaseIssue, Bug, Chore, Epic, IssueStatus, Milestone, Story
from apps.projects.factories import ProjectFactory


class MilestoneKeyAutoGenerationTest(TestCase):
    """Tests for automatic key generation for milestones (shares project key sequence)."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory(key="PROJ-1")

    def test_key_auto_generated_when_blank(self):
        """Key is auto-generated using the project key prefix."""
        milestone = MilestoneFactory(project=self.project, title="Test Milestone")

        self.assertNotEqual("", milestone.key)
        self.assertTrue(milestone.key.startswith("PROJ-1-"))

    def test_key_increments_correctly(self):
        """Keys increment sequentially within the project."""
        m1 = MilestoneFactory(project=self.project, title="Milestone 1")
        m2 = MilestoneFactory(project=self.project, title="Milestone 2")

        self.assertEqual("PROJ-1-1", m1.key)
        self.assertEqual("PROJ-1-2", m2.key)

    def test_key_handles_gaps(self):
        """Key generation finds max existing key and increments."""
        MilestoneFactory(project=self.project, title="Milestone 1", key="PROJ-1-1")
        MilestoneFactory(project=self.project, title="Milestone 3", key="PROJ-1-5")
        m3 = MilestoneFactory(project=self.project, title="Milestone 4")

        self.assertEqual("PROJ-1-6", m3.key)

    def test_key_normalized_to_uppercase(self):
        """Lowercase keys are converted to uppercase."""
        milestone = MilestoneFactory(project=self.project, title="Test", key="proj-1-custom")

        self.assertEqual("PROJ-1-CUSTOM", milestone.key)

    def test_key_whitespace_stripped(self):
        """Whitespace is stripped from keys."""
        milestone = MilestoneFactory(project=self.project, title="Test", key=" PROJ-1-X ")

        self.assertEqual("PROJ-1-X", milestone.key)


class IssueKeyAutoGenerationTest(TestCase):
    """Tests for automatic key generation from project keys."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory(key="PROJ-1")

    def test_key_auto_generated_when_blank(self):
        """Key is auto-generated if not provided."""
        epic = EpicFactory(project=self.project, title="Test Epic")

        self.assertNotEqual("", epic.key)
        self.assertTrue(epic.key.startswith("PROJ-1-"))

    def test_key_increments_correctly(self):
        """Keys increment sequentially."""
        e1 = EpicFactory(project=self.project, title="Epic 1")
        e2 = EpicFactory(project=self.project, title="Epic 2")

        self.assertEqual("PROJ-1-1", e1.key)
        self.assertEqual("PROJ-1-2", e2.key)

    def test_key_handles_gaps(self):
        """Key generation finds max existing key and increments."""
        EpicFactory(project=self.project, title="Epic 1", key="PROJ-1-1")
        EpicFactory(project=self.project, title="Epic 3", key="PROJ-1-5")
        e3 = EpicFactory(project=self.project, title="Epic 4")

        self.assertEqual("PROJ-1-6", e3.key)


class IssueKeyNormalizationTest(TestCase):
    """Tests for key normalization behavior."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory(key="PROJ-1")

    def test_key_normalized_to_uppercase(self):
        """Lowercase keys are converted to uppercase."""
        epic = EpicFactory(project=self.project, title="Test", key="proj-1-custom")

        self.assertEqual("PROJ-1-CUSTOM", epic.key)

    def test_key_whitespace_stripped(self):
        """Whitespace is stripped from keys."""
        epic = EpicFactory(project=self.project, title="Test", key=" PROJ-1-X ")

        self.assertEqual("PROJ-1-X", epic.key)


class MilestoneCreationTest(TestCase):
    """Tests for creating project-scoped milestones."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_create_milestone(self):
        """Milestones can be created at project level."""
        milestone = MilestoneFactory(project=self.project, title="Release 1.0")

        self.assertIsInstance(milestone, Milestone)
        self.assertEqual(self.project, milestone.project)
        self.assertTrue(milestone.key.startswith(self.project.key + "-"))

    def test_milestone_default_status(self):
        """Milestones are created with draft status by default."""
        milestone = MilestoneFactory(project=self.project)

        self.assertEqual(IssueStatus.DRAFT, milestone.status)


class IssueTypeCreationTest(TestCase):
    """Tests for creating different issue types."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()
        cls.workspace = cls.project.workspace

    def test_create_epic_as_root(self):
        """Epics can be created as root nodes."""
        epic = EpicFactory(project=self.project, title="Feature Epic")

        self.assertIsInstance(epic, Epic)
        self.assertEqual(1, epic.depth)
        self.assertIsNone(epic.get_parent())

    def test_create_epic_with_milestone(self):
        """Epics can be created as children of a milestone."""
        milestone = MilestoneFactory(project=self.project, title="Release 1.0")
        epic = EpicFactory(project=self.project, title="Feature Epic", parent=milestone)

        self.assertIsInstance(epic, Epic)
        self.assertEqual(2, epic.depth)  # Child of milestone
        self.assertEqual(milestone, epic.get_parent())

    def test_create_story_under_epic(self):
        """Stories must be created under epics."""
        epic = EpicFactory(project=self.project, title="Feature Epic")
        story = StoryFactory(project=self.project, title="User Story", parent=epic)

        self.assertIsInstance(story, Story)
        self.assertEqual(2, story.depth)
        self.assertEqual(epic, story.get_parent())

    def test_create_bug_under_epic(self):
        """Bugs must be created under epics."""
        epic = EpicFactory(project=self.project, title="Feature Epic")
        bug = BugFactory(project=self.project, title="Bug Fix", parent=epic)

        self.assertIsInstance(bug, Bug)
        self.assertEqual(epic, bug.get_parent())

    def test_create_chore_under_epic(self):
        """Chores must be created under epics."""
        epic = EpicFactory(project=self.project, title="Feature Epic")
        chore = ChoreFactory(project=self.project, title="Tech Debt", parent=epic)

        self.assertIsInstance(chore, Chore)
        self.assertEqual(epic, chore.get_parent())


class EpicMilestoneRelationshipTest(TestCase):
    """Tests for Epic-Milestone tree relationship."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_epic_can_access_parent_milestone(self):
        """Epics can access their parent milestone via get_parent()."""
        milestone = MilestoneFactory(project=self.project, title="Release 1.0")
        epic = EpicFactory(project=self.project, title="Feature Epic", parent=milestone)

        self.assertEqual(milestone, epic.get_parent())

    def test_milestone_can_access_child_epics(self):
        """Milestones can access their child epics via get_children()."""
        milestone = MilestoneFactory(project=self.project, title="Release 1.0")
        epic1 = EpicFactory(project=self.project, title="Epic 1", parent=milestone)
        epic2 = EpicFactory(project=self.project, title="Epic 2", parent=milestone)
        EpicFactory(project=self.project, title="Epic 3")  # Orphan, no milestone

        child_epics = list(milestone.get_children().instance_of(Epic))

        self.assertEqual(2, len(child_epics))
        self.assertIn(epic1, child_epics)
        self.assertIn(epic2, child_epics)

    def test_story_can_access_milestone_through_tree(self):
        """Stories can access milestone by traversing up the tree."""
        milestone = MilestoneFactory(project=self.project, title="Release 1.0")
        epic = EpicFactory(project=self.project, title="Feature Epic", parent=milestone)
        story = StoryFactory(project=self.project, title="Story", parent=epic)

        parent_epic = story.get_parent()
        self.assertEqual(milestone, parent_epic.get_parent())

    def test_epic_can_only_have_milestone_as_parent(self):
        """Epic validates that its tree parent is a Milestone (not a work item)."""
        story = StoryFactory(project=self.project, title="A Story")
        epic = Epic(project=self.project, title="Feature Epic")

        with self.assertRaises(ValidationError):
            story.add_child(instance=epic)


class EpicInactivityTrackingDefaultsTest(TestCase):
    """Tests for Epic.last_activity_at / inactivity_alert_due_at defaults on creation.

    These fields are the "sticker" the inactivity scheduler reads later (Step 8+):
    a freshly created Epic must start with a real timestamp (never NULL, since the
    field is non-nullable) and, for the common case of an active Epic, a due date
    exactly one grace period in the future — not immediately eligible for an alert.
    """

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_new_active_epic_gets_last_activity_at_set(self):
        epic = EpicFactory(project=self.project)
        self.assertIsNotNone(epic.last_activity_at)

    def test_new_active_epic_gets_a_future_due_date(self):
        """A brand-new Epic isn't immediately stale — it gets the full grace period."""
        before = timezone.now()
        epic = EpicFactory(project=self.project)
        after = timezone.now()

        self.assertIsNotNone(epic.inactivity_alert_due_at)
        self.assertGreaterEqual(epic.inactivity_alert_due_at, before + EPIC_INACTIVITY_ALERT_AFTER)
        self.assertLessEqual(epic.inactivity_alert_due_at, after + EPIC_INACTIVITY_ALERT_AFTER)

    def test_epic_created_already_done_has_no_pending_alert(self):
        """An Epic created directly into a terminal status (e.g. via type
        promotion) must never carry a due date — there's nothing to alert about
        on work that's already finished."""
        epic = EpicFactory(project=self.project, status=IssueStatus.DONE)
        self.assertIsNone(epic.inactivity_alert_due_at)

    def test_epic_created_already_wont_do_has_no_pending_alert(self):
        epic = EpicFactory(project=self.project, status=IssueStatus.WONT_DO)
        self.assertIsNone(epic.inactivity_alert_due_at)

    def test_epic_created_already_archived_has_no_pending_alert(self):
        epic = EpicFactory(project=self.project, status=IssueStatus.ARCHIVED)
        self.assertIsNone(epic.inactivity_alert_due_at)

    def test_epic_created_in_progress_still_gets_a_pending_alert(self):
        """Only terminal statuses are exempt — an active-but-not-draft Epic still
        gets the normal grace period, not treated as already finished."""
        epic = EpicFactory(project=self.project, status=IssueStatus.IN_PROGRESS)
        self.assertIsNotNone(epic.inactivity_alert_due_at)

    def test_saving_an_existing_epic_does_not_reset_last_activity_at(self):
        """The terminal-status-at-creation guard in Epic.save() must only ever
        apply to brand-new rows (pk is None) — editing an existing active Epic's
        unrelated fields must not silently wipe out its already-tracked activity."""
        epic = EpicFactory(project=self.project)
        original_activity = epic.last_activity_at
        original_due_at = epic.inactivity_alert_due_at

        epic.title = "Renamed"
        epic.save()

        self.assertEqual(original_activity, epic.last_activity_at)
        self.assertEqual(original_due_at, epic.inactivity_alert_due_at)

    def test_marking_an_epic_done_clears_the_pending_alert(self):
        """Finished work is never nagged about."""
        epic = EpicFactory(project=self.project)
        self.assertIsNotNone(epic.inactivity_alert_due_at)

        epic.status = IssueStatus.DONE
        epic.save()

        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)

    def test_marking_an_epic_wont_do_clears_the_pending_alert(self):
        epic = EpicFactory(project=self.project)

        epic.status = IssueStatus.WONT_DO
        epic.save()

        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)

    def test_archiving_an_epic_clears_the_pending_alert(self):
        epic = EpicFactory(project=self.project)

        epic.status = IssueStatus.ARCHIVED
        epic.save()

        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)

    def test_clearing_works_when_saving_with_update_fields(self):
        """Cascade and the status helper save with update_fields=['status', ...];
        the clock adjustment must not be silently dropped by that."""
        epic = EpicFactory(project=self.project)

        epic.status = IssueStatus.DONE
        epic.save(update_fields=["status", "updated_at"])

        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)

    def test_reopening_a_done_epic_starts_a_fresh_grace_period(self):
        """An Epic that sat Done for months must not fire an alert the instant it
        is picked back up — it gets a full new countdown."""
        epic = EpicFactory(project=self.project)
        epic.status = IssueStatus.DONE
        epic.save()
        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)

        before = timezone.now()
        epic.status = IssueStatus.IN_PROGRESS
        epic.save()

        epic.refresh_from_db()
        self.assertIsNotNone(epic.inactivity_alert_due_at)
        self.assertGreaterEqual(epic.inactivity_alert_due_at, before + EPIC_INACTIVITY_ALERT_AFTER)

    def test_saving_an_active_epic_does_not_extend_an_existing_clock(self):
        """Only a status transition adjusts the clock. Editing an active Epic's
        other fields must leave its countdown exactly where it was, or renaming
        an Epic daily would keep it alive forever."""
        epic = EpicFactory(project=self.project)
        due_at_before = epic.inactivity_alert_due_at

        epic.title = "Renamed"
        epic.save()

        epic.refresh_from_db()
        self.assertEqual(due_at_before, epic.inactivity_alert_due_at)


class IssueHierarchyValidationTest(TestCase):
    """Tests for parent type validation."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()
        cls.epic = EpicFactory(project=cls.project, title="Epic")
        cls.story = StoryFactory(project=cls.project, title="Story", parent=cls.epic)

    def test_epic_with_no_parent_passes_validation(self):
        """Epics with no tree parent pass validation."""
        epic2 = Epic(project=self.project, title="Another Epic")
        epic2._validate_parent_type()  # Should not raise when no parent


class IssueTreeOperationsTest(TestCase):
    """Tests for tree operations."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_get_children(self):
        """Can retrieve direct children of an issue."""
        epic = EpicFactory(project=self.project, title="Epic")
        story1 = StoryFactory(project=self.project, title="Story 1", parent=epic)
        story2 = StoryFactory(project=self.project, title="Story 2", parent=epic)

        children = list(epic.get_children())

        self.assertEqual(2, len(children))
        self.assertIn(story1, children)
        self.assertIn(story2, children)

    def test_get_descendants(self):
        """Can retrieve all descendants of an issue."""
        epic = EpicFactory(project=self.project, title="Epic")
        story = StoryFactory(project=self.project, title="Story", parent=epic)

        descendants = list(epic.get_descendants())

        self.assertEqual(1, len(descendants))
        self.assertIn(story, descendants)

    def test_get_ancestors(self):
        """Can retrieve all ancestors of an issue."""
        epic = EpicFactory(project=self.project, title="Epic")
        story = StoryFactory(project=self.project, title="Story", parent=epic)

        ancestors = list(story.get_ancestors())

        self.assertEqual(1, len(ancestors))
        self.assertIn(epic, ancestors)

    def test_get_descendant_count(self):
        """Can count descendants."""
        epic = EpicFactory(project=self.project, title="Epic")
        StoryFactory(project=self.project, title="Story", parent=epic)

        self.assertEqual(1, epic.get_descendant_count())


class IssueTypeDisplayTest(TestCase):
    """Tests for issue type display methods."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_get_issue_type(self):
        """get_issue_type returns lowercase class name."""
        epic = EpicFactory(project=self.project)

        self.assertEqual("epic", epic.get_issue_type())

    def test_get_issue_type_display(self):
        """get_issue_type_display returns human-readable name."""
        epic = EpicFactory(project=self.project)

        self.assertEqual("Epic", epic.get_issue_type_display())


class IssueStatusTest(TestCase):
    """Tests for issue status."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_default_status_is_draft(self):
        """Issues are created with draft status by default."""
        epic = EpicFactory(project=self.project)

        self.assertEqual(IssueStatus.DRAFT, epic.status)

    def test_can_set_status(self):
        """Issue status can be changed."""
        epic = EpicFactory(project=self.project, status=IssueStatus.IN_PROGRESS)

        self.assertEqual(IssueStatus.IN_PROGRESS, epic.status)


class IssueCascadeDeleteTest(TestCase):
    """Tests for tree-aware cascade deletion via queryset.delete()."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_queryset_delete_cascades_to_children(self):
        """Deleting an epic via queryset.delete() also deletes its children."""
        epic = EpicFactory(project=self.project, title="Epic to delete")
        story = StoryFactory(project=self.project, title="Child story", parent=epic)
        bug = BugFactory(project=self.project, title="Child bug", parent=epic)

        # Store IDs to verify deletion
        epic_id = epic.pk
        story_id = story.pk
        bug_id = bug.pk

        # Delete via queryset (simulates bulk delete)
        Epic.objects.filter(pk=epic_id).delete()

        # Verify all are deleted
        self.assertFalse(BaseIssue.objects.filter(pk=epic_id).exists())
        self.assertFalse(BaseIssue.objects.filter(pk=story_id).exists())
        self.assertFalse(BaseIssue.objects.filter(pk=bug_id).exists())

    def test_bulk_delete_cascades_to_children(self):
        """Bulk deleting multiple epics also deletes all their children."""
        epic1 = EpicFactory(project=self.project, title="Epic 1")
        story1 = StoryFactory(project=self.project, title="Story under Epic 1", parent=epic1)
        epic2 = EpicFactory(project=self.project, title="Epic 2")
        story2 = StoryFactory(project=self.project, title="Story under Epic 2", parent=epic2)

        # Store IDs
        epic1_id, story1_id = epic1.pk, story1.pk
        epic2_id, story2_id = epic2.pk, story2.pk

        # Bulk delete both epics
        Epic.objects.filter(pk__in=[epic1_id, epic2_id]).delete()

        # Verify all are deleted
        self.assertEqual(
            0,
            BaseIssue.objects.filter(pk__in=[epic1_id, story1_id, epic2_id, story2_id]).count(),
        )

    def test_instance_delete_cascades_to_children(self):
        """Deleting an epic via instance.delete() also deletes its children.

        This tests the code path used by IssueDeleteView (detail page deletion).
        """
        epic = EpicFactory(project=self.project, title="Epic to delete")
        story = StoryFactory(project=self.project, title="Child story", parent=epic)
        bug = BugFactory(project=self.project, title="Child bug", parent=epic)

        # Store IDs to verify deletion
        epic_id = epic.pk
        story_id = story.pk
        bug_id = bug.pk

        # Delete via instance.delete() - same as IssueDeleteView does
        epic.delete()

        # Verify all are deleted
        self.assertFalse(BaseIssue.objects.filter(pk=epic_id).exists())
        self.assertFalse(BaseIssue.objects.filter(pk=story_id).exists())
        self.assertFalse(BaseIssue.objects.filter(pk=bug_id).exists())

    def test_fetched_instance_delete_cascades_to_children(self):
        """Deleting an epic fetched from a queryset also deletes its children.

        This more closely mimics IssueDeleteView which fetches via get_object().
        """
        epic = EpicFactory(project=self.project, title="Epic to delete")
        story = StoryFactory(project=self.project, title="Child story", parent=epic)
        bug = BugFactory(project=self.project, title="Child bug", parent=epic)

        # Store values to verify deletion
        epic_id = epic.pk
        epic_key = epic.key
        story_id = story.pk
        bug_id = bug.pk

        # Fetch via queryset (simulates how DeleteView gets the object)
        fetched_epic = BaseIssue.objects.for_project(self.project).get(key=epic_key)

        # Delete the fetched object
        fetched_epic.delete()

        # Verify all are deleted
        self.assertFalse(BaseIssue.objects.filter(pk=epic_id).exists())
        self.assertFalse(BaseIssue.objects.filter(pk=story_id).exists())
        self.assertFalse(BaseIssue.objects.filter(pk=bug_id).exists())
