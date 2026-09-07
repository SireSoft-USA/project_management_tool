"""Tests that bulk actions reset the parent Epic's inactivity clock.

Bulk views mutate rows with queryset.update(), which never fires post_save, so
apps.issues.signals cannot see those edits. Each such view calls
record_activity_for_issues() explicitly instead. If that call is ever dropped, an
Epic whose Stories are being worked on in bulk would be wrongly reported
inactive — these tests are the guard against that going unnoticed.
"""

from datetime import timedelta

from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.issues.activity import EPIC_INACTIVITY_ALERT_AFTER
from apps.issues.cascade import _apply_cascade_down
from apps.issues.factories import BugFactory, EpicFactory, MilestoneFactory, StoryFactory
from apps.issues.models import Epic, IssueStatus
from apps.projects.factories import ProjectFactory
from apps.sprints.factories import SprintFactory
from apps.sprints.models import SprintStatus
from apps.users.factories import UserFactory
from apps.workspaces.factories import MembershipFactory, WorkspaceFactory
from apps.workspaces.roles import ROLE_ADMIN


class BulkActivityTestBase(TestCase):
    """Shared setup: an admin in a workspace, and helpers for stale Epics."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.user = UserFactory()
        MembershipFactory(workspace=cls.workspace, user=cls.user, role=ROLE_ADMIN)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _post(self, name, data):
        url = reverse(name, kwargs={"workspace_slug": self.workspace.slug})
        return self.client.post(url, data)

    def _stale_epic(self, **kwargs):
        """An Epic whose clock has already expired, so a reset is observable."""
        epic = EpicFactory(project=self.project, **kwargs)
        self._expire(epic)
        return epic

    def _expire(self, epic):
        long_ago = timezone.now() - timedelta(days=30)
        Epic.objects.filter(pk=epic.pk).update(last_activity_at=long_ago, inactivity_alert_due_at=long_ago)
        epic.refresh_from_db()
        return epic

    def assertClockReset(self, epic):
        epic.refresh_from_db()
        self.assertGreater(
            epic.inactivity_alert_due_at,
            timezone.now(),
            "bulk action did not reset the parent Epic's inactivity clock",
        )

    def assertClockUnchanged(self, epic, previous_due_at):
        epic.refresh_from_db()
        self.assertEqual(previous_due_at, epic.inactivity_alert_due_at)


class BulkStatusActivityTest(BulkActivityTestBase):
    def test_bulk_status_change_resets_the_clock(self):
        epic = self._stale_epic()
        story = StoryFactory(project=self.project, parent=epic)
        self._expire(epic)  # the Story's own creation already reset it

        self._post("workspace_issues_bulk_status", {"issues": [story.key], "status": IssueStatus.IN_PROGRESS})

        self.assertClockReset(epic)

    def test_bulk_status_change_spanning_two_epics_resets_both(self):
        first, second = self._stale_epic(), self._stale_epic()
        story_one = StoryFactory(project=self.project, parent=first)
        story_two = StoryFactory(project=self.project, parent=second)
        self._expire(first)
        self._expire(second)

        self._post(
            "workspace_issues_bulk_status",
            {"issues": [story_one.key, story_two.key], "status": IssueStatus.IN_PROGRESS},
        )

        self.assertClockReset(first)
        self.assertClockReset(second)

    def test_bulk_status_change_on_a_bug_does_not_reset_the_clock(self):
        """Only Story work counts toward Epic activity, per apps.issues.activity."""
        epic = self._stale_epic()
        bug = BugFactory(project=self.project, parent=epic)
        self._expire(epic)
        due_at_before = epic.inactivity_alert_due_at

        self._post("workspace_issues_bulk_status", {"issues": [bug.key], "status": IssueStatus.IN_PROGRESS})

        self.assertClockUnchanged(epic, due_at_before)

    def test_bulk_status_change_does_not_revive_a_done_epic(self):
        epic = self._stale_epic(status=IssueStatus.DONE)
        story = StoryFactory(project=self.project, parent=epic)
        self._expire(epic)
        due_at_before = epic.inactivity_alert_due_at

        self._post("workspace_issues_bulk_status", {"issues": [story.key], "status": IssueStatus.IN_PROGRESS})

        self.assertClockUnchanged(epic, due_at_before)

    def test_root_level_story_in_bulk_selection_is_harmless(self):
        """A Story with no Epic parent must not break the bulk action."""
        orphan = StoryFactory(project=self.project)

        response = self._post(
            "workspace_issues_bulk_status", {"issues": [orphan.key], "status": IssueStatus.IN_PROGRESS}
        )

        self.assertIn(response.status_code, (200, 302))
        orphan.refresh_from_db()
        self.assertEqual(IssueStatus.IN_PROGRESS, orphan.status)


class BulkPriorityActivityTest(BulkActivityTestBase):
    def test_bulk_priority_change_resets_the_clock(self):
        epic = self._stale_epic()
        story = StoryFactory(project=self.project, parent=epic)
        self._expire(epic)

        self._post("workspace_issues_bulk_priority", {"issues": [story.key], "priority": "high"})

        self.assertClockReset(epic)


class BulkPointsActivityTest(BulkActivityTestBase):
    def test_bulk_points_change_resets_the_clock(self):
        epic = self._stale_epic()
        story = StoryFactory(project=self.project, parent=epic)
        self._expire(epic)

        self._post("workspace_issues_bulk_points", {"issues": [story.key], "points": 5})

        self.assertClockReset(epic)


class BulkAssigneeActivityTest(BulkActivityTestBase):
    def test_bulk_assignee_change_resets_the_clock(self):
        epic = self._stale_epic()
        story = StoryFactory(project=self.project, parent=epic)
        self._expire(epic)

        self._post("workspace_issues_bulk_assignee", {"issues": [story.key], "assignee": self.user.pk})

        self.assertClockReset(epic)


class BulkSprintActivityTest(BulkActivityTestBase):
    def test_bulk_add_to_sprint_resets_the_clock(self):
        sprint = SprintFactory(workspace=self.workspace, status=SprintStatus.PLANNING)
        epic = self._stale_epic()
        story = StoryFactory(project=self.project, parent=epic)
        self._expire(epic)

        self._post("workspace_issues_bulk_add_to_sprint", {"issues": [story.key], "sprint": sprint.key})

        self.assertClockReset(epic)

    def test_bulk_remove_from_sprint_resets_the_clock(self):
        sprint = SprintFactory(workspace=self.workspace, status=SprintStatus.PLANNING)
        epic = self._stale_epic()
        story = StoryFactory(project=self.project, parent=epic, sprint=sprint)
        self._expire(epic)

        self._post("workspace_issues_bulk_remove_from_sprint", {"issues": [story.key]})

        self.assertClockReset(epic)


class BulkActivityQueryCostTest(BulkActivityTestBase):
    """Recording activity must cost one UPDATE for the whole batch, not one per row."""

    def test_many_stories_across_one_epic_cost_a_single_activity_update(self):
        epic = self._stale_epic()
        stories = [StoryFactory(project=self.project, parent=epic) for _ in range(5)]
        self._expire(epic)

        with CaptureQueriesContext(connection) as captured:
            self._post(
                "workspace_issues_bulk_status",
                {"issues": [s.key for s in stories], "status": IssueStatus.IN_PROGRESS},
            )

        activity_updates = [
            q["sql"]
            for q in captured.captured_queries
            if q["sql"].startswith("UPDATE") and "inactivity_alert_due_at" in q["sql"]
        ]
        self.assertEqual(1, len(activity_updates))


class BulkStatusInactivityClockTest(BulkActivityTestBase):
    """Epic status changed via queryset.update() must still realign the clock.

    Epic.save() covers every path that saves an instance, but the bulk status
    view and the cascade helper both use queryset.update(), which never calls
    save(). Without the explicit sync, a bulk-completed Epic would keep a pending
    alert and nag about finished work.
    """

    def test_bulk_marking_an_epic_done_clears_its_pending_alert(self):
        epic = EpicFactory(project=self.project)
        self.assertIsNotNone(epic.inactivity_alert_due_at)

        self._post("workspace_issues_bulk_status", {"issues": [epic.key], "status": IssueStatus.DONE})

        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)

    def test_bulk_reopening_an_epic_starts_a_fresh_grace_period(self):
        epic = EpicFactory(project=self.project, status=IssueStatus.DONE)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=None)
        before = timezone.now()

        self._post("workspace_issues_bulk_status", {"issues": [epic.key], "status": IssueStatus.IN_PROGRESS})

        epic.refresh_from_db()
        self.assertIsNotNone(epic.inactivity_alert_due_at)
        self.assertGreaterEqual(epic.inactivity_alert_due_at, before + EPIC_INACTIVITY_ALERT_AFTER)

    def test_bulk_status_change_between_active_states_leaves_the_clock_alone(self):
        """Moving Draft → In Progress is not a finish or a reopen; the countdown
        must keep running from where it was."""
        epic = self._stale_epic(status=IssueStatus.DRAFT)
        due_at_before = epic.inactivity_alert_due_at

        self._post("workspace_issues_bulk_status", {"issues": [epic.key], "status": IssueStatus.IN_PROGRESS})

        self.assertClockUnchanged(epic, due_at_before)

    def test_cascade_completing_a_milestone_clears_child_epic_alerts(self):
        """Cascade-down uses queryset.update(); Epics swept up in it must still
        have their clocks cleared."""
        milestone = MilestoneFactory(project=self.project)
        epic = EpicFactory(project=self.project, parent=milestone)
        self.assertIsNotNone(epic.inactivity_alert_due_at)

        _apply_cascade_down([epic.pk], IssueStatus.DONE, "issue", self.user)

        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)
