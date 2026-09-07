"""Tests for the query that finds Epics due for an inactivity alert.

This is the one query the hourly scheduler runs forever, so it has to stay a
single indexed lookup on inactivity_alert_due_at — never a scan that touches
Story rows. These tests cover both what it selects and how it selects it.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.issues.factories import EpicFactory, StoryFactory
from apps.issues.models import BaseIssue, Epic, IssueStatus, Story
from apps.projects.factories import ProjectFactory
from apps.workspaces.factories import WorkspaceFactory


class DueForInactivityAlertTest(TestCase):
    """Which Epics come back."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def _epic_due_at(self, due_at, **kwargs):
        epic = EpicFactory(project=self.project, **kwargs)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=due_at)
        epic.refresh_from_db()
        return epic

    def test_returns_an_epic_whose_due_date_has_passed(self):
        epic = self._epic_due_at(timezone.now() - timedelta(hours=1))

        self.assertIn(epic, Epic.objects.due_for_inactivity_alert())

    def test_excludes_an_epic_whose_due_date_is_in_the_future(self):
        """The common case: activity happened recently, so nothing is due."""
        epic = self._epic_due_at(timezone.now() + timedelta(days=3))

        self.assertNotIn(epic, Epic.objects.due_for_inactivity_alert())

    def test_excludes_an_epic_with_no_pending_alert(self):
        """NULL means "no alert pending" — already sent, or the Epic is finished."""
        epic = self._epic_due_at(None)

        self.assertNotIn(epic, Epic.objects.due_for_inactivity_alert())

    def test_excludes_a_done_epic_even_with_a_stale_due_date(self):
        """Belt and braces: Step 7 clears the date on the way into Done, but a row
        that kept one (old data, direct SQL) must still never be alerted about."""
        epic = self._epic_due_at(timezone.now() - timedelta(days=30), status=IssueStatus.DONE)

        self.assertNotIn(epic, Epic.objects.due_for_inactivity_alert())

    def test_excludes_a_wont_do_epic_with_a_stale_due_date(self):
        epic = self._epic_due_at(timezone.now() - timedelta(days=30), status=IssueStatus.WONT_DO)

        self.assertNotIn(epic, Epic.objects.due_for_inactivity_alert())

    def test_excludes_an_archived_epic_with_a_stale_due_date(self):
        epic = self._epic_due_at(timezone.now() - timedelta(days=30), status=IssueStatus.ARCHIVED)

        self.assertNotIn(epic, Epic.objects.due_for_inactivity_alert())

    def test_includes_epics_in_any_active_status(self):
        """Draft, planning, in progress, blocked, in review are all alertable."""
        active_statuses = [
            IssueStatus.DRAFT,
            IssueStatus.PLANNING,
            IssueStatus.READY,
            IssueStatus.IN_PROGRESS,
            IssueStatus.BLOCKED,
            IssueStatus.IN_REVIEW,
        ]
        epics = [self._epic_due_at(timezone.now() - timedelta(hours=1), status=status) for status in active_statuses]

        due = list(Epic.objects.due_for_inactivity_alert())
        for epic in epics:
            self.assertIn(epic, due)

    def test_boundary_exactly_now_is_due(self):
        """<= not <, so an Epic due this very instant is picked up rather than
        being skipped until the next run an hour later."""
        instant = timezone.now()
        epic = self._epic_due_at(instant)

        self.assertIn(epic, Epic.objects.due_for_inactivity_alert(now=instant))

    def test_now_override_lets_callers_look_into_the_future(self):
        """The scheduler passes a single 'now' so every Epic in one run is judged
        against the same instant rather than a drifting clock."""
        epic = self._epic_due_at(timezone.now() + timedelta(days=3))

        self.assertNotIn(epic, Epic.objects.due_for_inactivity_alert())
        self.assertIn(epic, Epic.objects.due_for_inactivity_alert(now=timezone.now() + timedelta(days=4)))

    def test_only_epics_are_returned(self):
        """Stories have no inactivity clock; the Epic table is the only source.

        The Story is created first: adding one resets the parent's clock (that is
        the Step 3/4 signal doing its job), so the due date has to be forced back
        afterwards for the Epic to be due at all.
        """
        epic = EpicFactory(project=self.project)
        story = StoryFactory(project=self.project, parent=epic)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))
        epic.refresh_from_db()

        due = list(Epic.objects.due_for_inactivity_alert())

        self.assertIn(epic, due)
        self.assertNotIn(story, due)

    def test_spans_workspaces(self):
        """The scheduler is a system-wide sweep with no request or workspace
        context, so the query must not be implicitly scoped to one workspace."""
        other_project = ProjectFactory(workspace=WorkspaceFactory())
        mine = self._epic_due_at(timezone.now() - timedelta(hours=1))
        theirs = EpicFactory(project=other_project)
        Epic.objects.filter(pk=theirs.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

        due = list(Epic.objects.due_for_inactivity_alert())

        self.assertIn(mine, due)
        self.assertIn(theirs, due)


class DueForInactivityAlertCostTest(TestCase):
    """How it selects them — the part that has to stay cheap forever."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def test_is_a_single_query(self):
        for _ in range(5):
            epic = EpicFactory(project=self.project)
            Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

        with self.assertNumQueries(1):
            list(Epic.objects.due_for_inactivity_alert())

    def test_does_not_touch_story_rows(self):
        """The whole design exists so this never aggregates over children."""
        epic = EpicFactory(project=self.project)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))
        for _ in range(3):
            StoryFactory(project=self.project, parent=epic)

        with CaptureQueriesContext(connection) as captured:
            list(Epic.objects.due_for_inactivity_alert())

        sql = " ".join(q["sql"] for q in captured.captured_queries)
        self.assertNotIn("issues_story", sql)

    def test_query_cost_does_not_grow_with_the_number_of_stories(self):
        """One Epic with many Stories must cost the same as one with none."""
        epic = EpicFactory(project=self.project)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

        with self.assertNumQueries(1):
            list(Epic.objects.due_for_inactivity_alert())

        for _ in range(10):
            StoryFactory(project=self.project, parent=epic)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

        with self.assertNumQueries(1):
            list(Epic.objects.due_for_inactivity_alert())

    def test_an_index_exists_on_inactivity_alert_due_at(self):
        """Guards the db_index=True on that column.

        Asserted against pg_indexes rather than an EXPLAIN plan: on a small test
        table PostgreSQL correctly prefers a sequential scan, so a plan-based
        assertion would pass whether or not the index existed. Checking the
        catalogue verifies the thing that actually has to be true — the index is
        there for when the table is large enough to matter.
        """
        with connection.cursor() as cursor:
            cursor.execute("SELECT indexdef FROM pg_indexes WHERE tablename = 'issues_epic';")
            index_definitions = " ".join(row[0] for row in cursor.fetchall())

        self.assertIn("inactivity_alert_due_at", index_definitions)

    def test_the_planner_uses_that_index_when_a_scan_is_not_cheaper(self):
        """Proves the index is actually usable for this query's predicate, not
        merely present. enable_seqscan=off removes the small-table shortcut so the
        planner has to show which access path it would take on a big table."""
        epic = EpicFactory(project=self.project)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off;")
            cursor.execute("EXPLAIN SELECT baseissue_ptr_id FROM issues_epic WHERE inactivity_alert_due_at <= now();")
            plan = " ".join(row[0] for row in cursor.fetchall())

        self.assertIn("Index Scan", plan)
        self.assertIn("inactivity_alert_due_at", plan)


class DueForInactivityAlertMisuseTest(TestCase):
    """The clock lives on the Epic table, so the query is Epic-only."""

    def test_calling_it_on_the_base_manager_raises_a_clear_error(self):
        """BaseIssue has no inactivity_alert_due_at column. Without this guard the
        mistake surfaces as an opaque Django FieldError listing every field."""
        with self.assertRaises(TypeError) as ctx:
            BaseIssue.objects.due_for_inactivity_alert()

        self.assertIn("Epic.objects.due_for_inactivity_alert()", str(ctx.exception))

    def test_calling_it_on_story_raises_a_clear_error(self):
        with self.assertRaises(TypeError):
            Story.objects.due_for_inactivity_alert()
