"""End-to-end lifecycle and remaining edge cases for epic inactivity alerts.

The per-step test modules each cover one mechanism in isolation. This one walks
an epic through the whole cycle the way it happens in production, and pins the
edge cases the design made explicit decisions about — chiefly that removing work
must not count as progress.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.issues.activity import EPIC_INACTIVITY_ALERT_AFTER
from apps.issues.factories import EpicFactory, MilestoneFactory, StoryFactory
from apps.issues.models import BaseIssue, Epic, IssueStatus
from apps.issues.tasks import send_epic_inactivity_alerts
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces.factories import MembershipFactory, WorkspaceFactory


class EpicInactivityLifecycleTest(TestCase):
    """One epic, walked through the full cycle."""

    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.project = ProjectFactory(workspace=self.workspace)
        self.assignee = UserFactory(email="owner@siresoft.com")
        MembershipFactory(workspace=self.workspace, user=self.assignee)

    def _travel(self, epic, days):
        """Age an epic by rewinding its clock, rather than waiting seven days."""
        Epic.objects.filter(pk=epic.pk).update(
            last_activity_at=timezone.now() - timedelta(days=days),
            inactivity_alert_due_at=timezone.now() - timedelta(days=days) + EPIC_INACTIVITY_ALERT_AFTER,
        )
        epic.refresh_from_db()
        return epic

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_new_epic_with_no_stories_alerts_after_the_grace_period(self, mock_notify):
        """The clock starts at creation, so an epic nobody ever touches is the
        simplest thing that should be caught."""
        epic = EpicFactory(project=self.project, assignee=self.assignee)

        self._travel(epic, days=6)
        self.assertEqual(0, send_epic_inactivity_alerts(), "not due yet at six days")

        self._travel(epic, days=8)
        self.assertEqual(1, send_epic_inactivity_alerts())
        self.assertEqual(epic.pk, mock_notify.call_args.args[0].pk)

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_a_story_added_on_day_six_defers_the_alert(self, mock_notify):
        """Work resets the countdown; the alert should land a week after the
        work, not a week after the epic was created."""
        epic = EpicFactory(project=self.project, assignee=self.assignee)
        self._travel(epic, days=6)

        StoryFactory(project=self.project, parent=epic)  # activity on day six

        self.assertEqual(0, send_epic_inactivity_alerts(), "the story reset the clock")
        mock_notify.assert_not_called()

        self._travel(epic, days=8)  # now quiet again for eight days
        self.assertEqual(1, send_epic_inactivity_alerts())

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_full_cycle_alert_then_work_then_alert_again(self, mock_notify):
        """The complete loop: quiet → alert → work → quiet → alert."""
        epic = EpicFactory(project=self.project, assignee=self.assignee)

        self._travel(epic, days=8)
        self.assertEqual(1, send_epic_inactivity_alerts())

        # Silent while nothing changes, however many times the sweep runs.
        self.assertEqual(0, send_epic_inactivity_alerts())
        self.assertEqual(0, send_epic_inactivity_alerts())

        StoryFactory(project=self.project, parent=epic)
        epic.refresh_from_db()
        self.assertIsNotNone(epic.inactivity_alert_due_at, "work restarts the cycle")
        self.assertEqual(0, send_epic_inactivity_alerts(), "fresh grace period")

        self._travel(epic, days=8)
        self.assertEqual(1, send_epic_inactivity_alerts())
        self.assertEqual(2, mock_notify.call_count)

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_completing_an_epic_stops_alerts_and_reopening_grants_a_fresh_period(self, mock_notify):
        epic = EpicFactory(project=self.project, assignee=self.assignee)
        self._travel(epic, days=8)

        epic.status = IssueStatus.DONE
        epic.save()

        self.assertEqual(0, send_epic_inactivity_alerts(), "finished work is never nagged about")

        epic.status = IssueStatus.IN_PROGRESS
        epic.save()
        epic.refresh_from_db()

        self.assertEqual(0, send_epic_inactivity_alerts(), "reopening grants a full new week")
        self.assertGreater(epic.inactivity_alert_due_at, timezone.now())
        mock_notify.assert_not_called()


class RemovingWorkIsNotProgressTest(TestCase):
    """Deleting or moving a story away must not keep an epic looking alive.

    Documented in apps.issues.activity: treating removal as activity would let an
    epic be held "fresh" indefinitely without anything being delivered.
    """

    def setUp(self):
        self.project = ProjectFactory()

    def _stale(self, epic):
        long_ago = timezone.now() - timedelta(days=30)
        Epic.objects.filter(pk=epic.pk).update(last_activity_at=long_ago, inactivity_alert_due_at=long_ago)
        epic.refresh_from_db()
        return epic

    def test_deleting_a_story_does_not_reset_the_clock(self):
        epic = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=epic)
        self._stale(epic)
        due_at_before = epic.inactivity_alert_due_at

        story.delete()

        epic.refresh_from_db()
        self.assertEqual(due_at_before, epic.inactivity_alert_due_at)

    def test_a_deleted_story_leaves_the_epic_still_alertable(self):
        """Deleting the last story must not accidentally take the epic out of the
        sweep — an emptied epic is exactly the kind that has gone quiet."""
        epic = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=epic)
        self._stale(epic)

        story.delete()

        self.assertIn(epic, Epic.objects.due_for_inactivity_alert())

    def test_moving_a_story_away_does_not_reset_the_source_epic(self):
        """The epic losing the work has not made progress."""
        source = EpicFactory(project=self.project)
        destination = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=source)
        self._stale(source)
        source_due_at = source.inactivity_alert_due_at

        story.move(destination, pos="last-child")

        source.refresh_from_db()
        self.assertEqual(source_due_at, source.inactivity_alert_due_at)

    def test_moving_a_story_in_credits_the_destination_once_it_is_saved(self):
        """The epic gaining work has: the owning view saves the story after
        move(), and that save is what records the activity."""
        source = EpicFactory(project=self.project)
        destination = self._stale(EpicFactory(project=self.project))
        story = StoryFactory(project=self.project, parent=source)

        story.move(destination, pos="last-child")
        story.refresh_from_db()
        story.save()

        destination.refresh_from_db()
        self.assertGreater(destination.inactivity_alert_due_at, timezone.now())


class EpicInactivityIsolationTest(TestCase):
    """Activity must not leak between epics, projects or workspaces."""

    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.project = ProjectFactory(workspace=self.workspace)

    def _stale(self, epic):
        long_ago = timezone.now() - timedelta(days=30)
        Epic.objects.filter(pk=epic.pk).update(last_activity_at=long_ago, inactivity_alert_due_at=long_ago)
        epic.refresh_from_db()
        return epic

    def test_work_in_one_workspace_does_not_touch_another(self):
        other_project = ProjectFactory(workspace=WorkspaceFactory())
        mine = self._stale(EpicFactory(project=self.project))
        theirs = self._stale(EpicFactory(project=other_project))
        theirs_due_at = theirs.inactivity_alert_due_at

        StoryFactory(project=self.project, parent=mine)

        theirs.refresh_from_db()
        self.assertEqual(theirs_due_at, theirs.inactivity_alert_due_at)

    def test_a_story_under_a_milestone_never_credits_an_epic(self):
        """Milestones sit beside epics in the tree; a story parented to one has
        no epic to credit."""
        milestone = MilestoneFactory(project=self.project)
        epic = self._stale(EpicFactory(project=self.project))
        due_at_before = epic.inactivity_alert_due_at

        StoryFactory(project=self.project, parent=milestone)

        epic.refresh_from_db()
        self.assertEqual(due_at_before, epic.inactivity_alert_due_at)

    def test_a_subtask_under_a_story_does_not_credit_the_epic(self):
        """Only stories count. A subtask is a child of a story, one level deeper,
        so its parent path is the story's — not the epic's."""
        epic = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=epic)
        self._stale(epic)
        due_at_before = epic.inactivity_alert_due_at

        BaseIssue.objects.record_story_activity_by_parent_path(story.path)

        epic.refresh_from_db()
        self.assertEqual(due_at_before, epic.inactivity_alert_due_at)
