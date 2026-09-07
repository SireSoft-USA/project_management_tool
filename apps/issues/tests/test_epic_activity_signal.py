"""Tests for the post_save hook that keeps an Epic's inactivity clock current.

The clock only means anything if it is reset by real work. These tests cover
both halves of the requirement — "a Story was added" and "work was done on an
existing Story" — plus the cases that must NOT reset it, and the query cost on
the Story save path.
"""

from datetime import timedelta

from django.db import connection
from django.db.models.signals import post_save
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.issues import signals
from apps.issues.activity import EPIC_INACTIVITY_ALERT_AFTER, story_parent_path
from apps.issues.factories import EpicFactory, MilestoneFactory, StoryFactory
from apps.issues.models import Epic, IssueStatus, Story
from apps.projects.factories import ProjectFactory


class StoryParentPathTest(TestCase):
    """The pure-Python parent lookup the signal relies on to avoid a query."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_returns_parent_path_for_a_child_story(self):
        epic = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=epic)

        self.assertEqual(epic.path, story_parent_path(story))

    def test_returns_none_for_a_root_story(self):
        """A Story with no parent belongs to no Epic."""
        story = StoryFactory(project=self.project)

        self.assertIsNone(story_parent_path(story))

    def test_costs_no_queries(self):
        """The whole point: deriving the parent must not hit the database."""
        epic = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=epic)

        with self.assertNumQueries(0):
            story_parent_path(story)


class EpicActivitySignalTest(TestCase):
    """post_save on Story → parent Epic's clock moves forward."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def _stale_epic(self, **kwargs):
        """An Epic whose clock has already expired, so a reset is observable."""
        epic = EpicFactory(project=self.project, **kwargs)
        long_ago = timezone.now() - timedelta(days=30)
        Epic.objects.filter(pk=epic.pk).update(last_activity_at=long_ago, inactivity_alert_due_at=long_ago)
        epic.refresh_from_db()
        return epic

    def test_creating_a_story_resets_the_parent_epics_clock(self):
        epic = self._stale_epic()

        StoryFactory(project=self.project, parent=epic)

        epic.refresh_from_db()
        self.assertGreater(epic.inactivity_alert_due_at, timezone.now())

    def test_creating_a_story_sets_a_full_grace_period(self):
        epic = self._stale_epic()
        before = timezone.now()

        StoryFactory(project=self.project, parent=epic)

        epic.refresh_from_db()
        self.assertGreaterEqual(epic.inactivity_alert_due_at, before + EPIC_INACTIVITY_ALERT_AFTER)

    def test_editing_an_existing_story_resets_the_clock(self):
        """ "Work performed on an existing Story" is activity too, not just adds."""
        epic = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=epic)
        long_ago = timezone.now() - timedelta(days=30)
        Epic.objects.filter(pk=epic.pk).update(last_activity_at=long_ago, inactivity_alert_due_at=long_ago)

        story.status = IssueStatus.IN_PROGRESS
        story.save()

        epic.refresh_from_db()
        self.assertGreater(epic.inactivity_alert_due_at, timezone.now())

    def test_root_level_story_records_nothing(self):
        """A Story with no Epic parent has no clock to reset — and must not crash."""
        story = StoryFactory(project=self.project)

        self.assertIsNone(story_parent_path(story))
        story.title = "Renamed"
        story.save()  # must not raise

    def test_story_under_a_milestone_records_nothing(self):
        """Milestones are not Epics; a Story parented to one resets no Epic."""
        milestone = MilestoneFactory(project=self.project)
        stale = self._stale_epic()
        stale_due_at = stale.inactivity_alert_due_at

        StoryFactory(project=self.project, parent=milestone)

        stale.refresh_from_db()
        self.assertEqual(stale_due_at, stale.inactivity_alert_due_at)

    def test_only_the_parent_epic_is_affected(self):
        """Activity must not bleed into sibling Epics."""
        target = self._stale_epic()
        bystander = self._stale_epic()
        bystander_due_at = bystander.inactivity_alert_due_at

        StoryFactory(project=self.project, parent=target)

        bystander.refresh_from_db()
        self.assertEqual(bystander_due_at, bystander.inactivity_alert_due_at)

    def test_story_activity_does_not_revive_a_done_epic(self):
        """Finished Epics stay finished even if their Stories are still edited."""
        epic = self._stale_epic(status=IssueStatus.DONE)
        due_at_before = epic.inactivity_alert_due_at

        StoryFactory(project=self.project, parent=epic)

        epic.refresh_from_db()
        self.assertEqual(due_at_before, epic.inactivity_alert_due_at)

    def test_editing_the_epic_itself_does_not_reset_its_clock(self):
        """Per the requirement, only Story work counts — renaming or reassigning
        the Epic is bookkeeping, not progress, and must leave the clock alone."""
        epic = self._stale_epic()
        due_at_before = epic.inactivity_alert_due_at

        epic.title = "Renamed Epic"
        epic.save()

        epic.refresh_from_db()
        self.assertEqual(due_at_before, epic.inactivity_alert_due_at)

    def test_signal_adds_exactly_one_query_to_a_story_save(self):
        """The hook must stay cheap: one UPDATE, and crucially no SELECT to find
        the parent Epic. Measured as a delta against the same save with the hook
        disconnected, so unrelated per-save cost (auditlog's own history write)
        does not make this assertion brittle.
        """
        epic = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=epic)

        post_save.disconnect(sender=Story, dispatch_uid="issues.epic_activity.story")
        try:
            with CaptureQueriesContext(connection) as without_hook:
                story.title = "Renamed once"
                story.save(update_fields=["title", "updated_at"])
        finally:
            signals.register()

        with CaptureQueriesContext(connection) as with_hook:
            story.title = "Renamed twice"
            story.save(update_fields=["title", "updated_at"])

        self.assertEqual(1, len(with_hook) - len(without_hook))

    def test_signal_does_not_select_the_parent_epic(self):
        """Regression guard for the design decision to derive the parent path in
        Python rather than call treebeard's get_parent(), which costs a query."""
        epic = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=epic)

        with CaptureQueriesContext(connection) as captured:
            story.title = "Renamed"
            story.save(update_fields=["title", "updated_at"])

        epic_selects = [
            q["sql"] for q in captured.captured_queries if "issues_epic" in q["sql"] and q["sql"].startswith("SELECT")
        ]
        self.assertEqual([], epic_selects)


class EpicActivityThroughViewsTest(TestCase):
    """The signal exists so no view has to remember to call the recorder.
    These exercise real creation paths rather than the factory."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_add_child_from_an_epic_records_activity(self):
        """The epic-scoped "new issue under this epic" path."""
        epic = EpicFactory(project=self.project)
        long_ago = timezone.now() - timedelta(days=30)
        Epic.objects.filter(pk=epic.pk).update(last_activity_at=long_ago, inactivity_alert_due_at=long_ago)

        epic.add_child(instance=Story(project=self.project, title="From epic page"))

        epic.refresh_from_db()
        self.assertGreater(epic.inactivity_alert_due_at, timezone.now())

    def test_moving_a_story_under_an_epic_records_activity_for_the_new_parent(self):
        """A Story moved into an Epic counts as that Epic gaining work."""
        source = EpicFactory(project=self.project)
        destination = self._stale(EpicFactory(project=self.project))
        story = StoryFactory(project=self.project, parent=source)

        story.move(destination, pos="last-child")
        # move() rewrites paths directly; the owning view saves afterwards, which
        # is what fires the hook for the new parent.
        story.refresh_from_db()
        story.save()

        destination.refresh_from_db()
        self.assertGreater(destination.inactivity_alert_due_at, timezone.now())

    def _stale(self, epic):
        long_ago = timezone.now() - timedelta(days=30)
        Epic.objects.filter(pk=epic.pk).update(last_activity_at=long_ago, inactivity_alert_due_at=long_ago)
        epic.refresh_from_db()
        return epic
