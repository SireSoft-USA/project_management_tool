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
from apps.issues.factories import (
    BugFactory,
    ChoreFactory,
    EpicFactory,
    MilestoneFactory,
    StoryFactory,
    SubtaskFactory,
)
from apps.issues.models import Bug, Chore, Epic, IssueStatus, Story
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


class EveryWorkItemTypeResetsTheClockTest(TestCase):
    """Bugs and Chores are work on an Epic, exactly as Stories are.

    BaseIssue is polymorphic, so post_save fires for the concrete subclass and
    never for the base. An earlier version registered only Story, which meant a
    team could spend a week fixing bugs under an Epic and have every one of those
    saves recorded as nothing - the Epic was then reported inactive while people
    were actively working on it.

    Parameterised over the three types rather than written once per type, so
    adding a fourth work item to the app fails here until it is registered
    instead of silently going unrecorded.
    """

    WORK_ITEM_FACTORIES = (
        ("story", StoryFactory),
        ("bug", BugFactory),
        ("chore", ChoreFactory),
    )

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def _stale_epic(self, **kwargs):
        epic = EpicFactory(project=self.project, **kwargs)
        long_ago = timezone.now() - timedelta(days=30)
        Epic.objects.filter(pk=epic.pk).update(last_activity_at=long_ago, inactivity_alert_due_at=long_ago)
        epic.refresh_from_db()
        return epic

    def test_creating_any_work_item_resets_the_parent_epics_clock(self):
        """The regression: before Bug and Chore were registered, only the story
        subtest passed."""
        for label, work_item_factory in self.WORK_ITEM_FACTORIES:
            with self.subTest(work_item=label):
                epic = self._stale_epic()

                work_item_factory(project=self.project, parent=epic)

                epic.refresh_from_db()
                self.assertGreater(
                    epic.inactivity_alert_due_at,
                    timezone.now(),
                    f"creating a {label} under an Epic must reset its inactivity clock",
                )

    def test_creating_any_work_item_sets_a_full_grace_period(self):
        """Not merely "some time in the future" but the full window, so the alert
        lands seven days after the work rather than at an arbitrary point."""
        for label, work_item_factory in self.WORK_ITEM_FACTORIES:
            with self.subTest(work_item=label):
                epic = self._stale_epic()
                before = timezone.now()

                work_item_factory(project=self.project, parent=epic)

                epic.refresh_from_db()
                self.assertGreaterEqual(epic.inactivity_alert_due_at, before + EPIC_INACTIVITY_ALERT_AFTER)

    def test_editing_any_work_item_resets_the_parent_epics_clock(self):
        """The second half of the requirement: work on an *existing* item counts,
        not only adding a new one. A team grinding through open bugs without
        filing new ones is still working."""
        for label, work_item_factory in self.WORK_ITEM_FACTORIES:
            with self.subTest(work_item=label):
                epic = self._stale_epic()
                item = work_item_factory(project=self.project, parent=epic)
                # Creating it already reset the clock; wind it back so the edit is
                # what is being measured rather than the creation.
                long_ago = timezone.now() - timedelta(days=30)
                Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=long_ago)

                item.title = f"Renamed {label}"
                item.save(update_fields=["title", "updated_at"])

                epic.refresh_from_db()
                self.assertGreater(epic.inactivity_alert_due_at, timezone.now())

    def test_completing_any_work_item_counts_as_activity(self):
        """Marking work Done is the most common real edit, and the exact action a
        user performs before wondering why they were told the Epic was
        abandoned."""
        for label, work_item_factory in self.WORK_ITEM_FACTORIES:
            with self.subTest(work_item=label):
                epic = self._stale_epic()
                item = work_item_factory(project=self.project, parent=epic, status=IssueStatus.IN_PROGRESS)
                long_ago = timezone.now() - timedelta(days=30)
                Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=long_ago)

                item.status = IssueStatus.DONE
                item.save(update_fields=["status", "updated_at"])

                epic.refresh_from_db()
                self.assertGreater(epic.inactivity_alert_due_at, timezone.now())

    def test_a_root_level_work_item_records_nothing(self):
        """No parent Epic, so nothing to record - and crucially no crash. The
        guard must hold for every newly registered type, not just Story."""
        for label, work_item_factory in self.WORK_ITEM_FACTORIES:
            with self.subTest(work_item=label):
                bystander = self._stale_epic()
                due_at_before = bystander.inactivity_alert_due_at

                work_item_factory(project=self.project)  # root level

                bystander.refresh_from_db()
                self.assertEqual(due_at_before, bystander.inactivity_alert_due_at)

    def test_a_work_item_under_a_milestone_records_nothing(self):
        """A Milestone is not an Epic. Its path matches no Epic row, so the
        update finds nothing - the intended behaviour for every type."""
        for label, work_item_factory in self.WORK_ITEM_FACTORIES:
            with self.subTest(work_item=label):
                milestone = MilestoneFactory(project=self.project)
                bystander = self._stale_epic()
                due_at_before = bystander.inactivity_alert_due_at

                work_item_factory(project=self.project, parent=milestone)

                bystander.refresh_from_db()
                self.assertEqual(due_at_before, bystander.inactivity_alert_due_at)

    def test_work_on_one_epic_does_not_touch_another(self):
        """Scoping holds for the newly registered types too: a bug filed under
        one Epic must not keep an unrelated Epic looking fresh."""
        for label, work_item_factory in self.WORK_ITEM_FACTORIES:
            with self.subTest(work_item=label):
                target = self._stale_epic()
                bystander = self._stale_epic()
                due_at_before = bystander.inactivity_alert_due_at

                work_item_factory(project=self.project, parent=target)

                target.refresh_from_db()
                bystander.refresh_from_db()
                self.assertGreater(target.inactivity_alert_due_at, timezone.now())
                self.assertEqual(due_at_before, bystander.inactivity_alert_due_at)

    def test_no_work_item_type_revives_a_done_epic(self):
        """A finished Epic stays finished. Widening what counts as activity must
        not accidentally let a Bug resurrect an alert for completed work."""
        for label, work_item_factory in self.WORK_ITEM_FACTORIES:
            with self.subTest(work_item=label):
                epic = self._stale_epic(status=IssueStatus.DONE)
                due_at_before = epic.inactivity_alert_due_at

                work_item_factory(project=self.project, parent=epic)

                epic.refresh_from_db()
                self.assertEqual(due_at_before, epic.inactivity_alert_due_at)

    def test_a_subtask_does_not_reset_the_clock(self):
        """The line is drawn at direct children of the Epic. A Subtask hangs off a
        work item, not off the Epic, and apps.notifications.signals excludes it
        for the same reason. Its parent path is the work item's, which matches no
        Epic row.
        """
        epic = self._stale_epic()
        story = StoryFactory(project=self.project, parent=epic)
        long_ago = timezone.now() - timedelta(days=30)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=long_ago)

        SubtaskFactory(project=self.project, parent=story)

        epic.refresh_from_db()
        self.assertEqual(long_ago, epic.inactivity_alert_due_at)

    def test_each_work_item_type_is_registered_exactly_once(self):
        """Duplicate registration would fire the clock UPDATE twice per save.

        Each type gets its own dispatch_uid, which is what makes register()
        idempotent across repeated app loading. Re-running it here would double
        every subsequent save if the uids collided.
        """
        signals.register()  # deliberately re-run; must not double-connect

        for model in (Story, Bug, Chore):
            with self.subTest(model=model.__name__):
                epic = self._stale_epic()
                item = model(project=self.project, title=f"{model.__name__} probe")

                with CaptureQueriesContext(connection) as captured:
                    epic.add_child(instance=item)

                clock_updates = [
                    q["sql"]
                    for q in captured.captured_queries
                    if q["sql"].lstrip().upper().startswith("UPDATE") and "INACTIVITY_ALERT_DUE_AT" in q["sql"].upper()
                ]
                self.assertEqual(
                    1,
                    len(clock_updates),
                    f"{model.__name__}: expected exactly one clock UPDATE, got {clock_updates}",
                )
