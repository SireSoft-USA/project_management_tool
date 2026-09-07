"""Tests for the hourly sweep that alerts on inactive epics.

This is where the pieces meet: the indexed sweep (Step 8), the atomic claim
(Step 9) and the notification rules (Step 10). The tests here are mostly about
the interactions between them — duplicate suppression, the race with real work,
and the cost of a run.
"""

from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.issues.factories import EpicFactory, StoryFactory
from apps.issues.models import Epic, IssueStatus
from apps.issues.tasks import send_epic_inactivity_alerts
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces.factories import MembershipFactory, WorkspaceFactory


class EpicInactivitySweepTest(TestCase):
    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.project = ProjectFactory(workspace=self.workspace)
        self.assignee = UserFactory(email="owner@siresoft.com")
        MembershipFactory(workspace=self.workspace, user=self.assignee)

    def _due_epic(self, **kwargs):
        kwargs.setdefault("assignee", self.assignee)
        epic = EpicFactory(project=self.project, **kwargs)
        Epic.objects.filter(pk=epic.pk).update(
            last_activity_at=timezone.now() - timedelta(days=8),
            inactivity_alert_due_at=timezone.now() - timedelta(days=1),
        )
        epic.refresh_from_db()
        return epic

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_alerts_a_due_epic(self, mock_notify):
        epic = self._due_epic()

        self.assertEqual(1, send_epic_inactivity_alerts())
        mock_notify.assert_called_once()
        self.assertEqual(epic.pk, mock_notify.call_args.args[0].pk)

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_does_not_alert_an_epic_that_is_not_due(self, mock_notify):
        EpicFactory(project=self.project, assignee=self.assignee)

        self.assertEqual(0, send_epic_inactivity_alerts())
        mock_notify.assert_not_called()

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_clears_the_due_date_so_the_next_run_stays_quiet(self, mock_notify):
        """The core anti-nagging guarantee, exercised end to end."""
        epic = self._due_epic()

        self.assertEqual(1, send_epic_inactivity_alerts())
        self.assertEqual(0, send_epic_inactivity_alerts())

        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)
        self.assertEqual(1, mock_notify.call_count)

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_alerts_again_after_new_work_and_new_silence(self, mock_notify):
        """Suppression is per period of silence, not permanent."""
        epic = self._due_epic()
        send_epic_inactivity_alerts()

        StoryFactory(project=self.project, parent=epic)  # activity restarts the cycle
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

        self.assertEqual(1, send_epic_inactivity_alerts())
        self.assertEqual(2, mock_notify.call_count)

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_skips_terminal_epics(self, mock_notify):
        self._due_epic(status=IssueStatus.DONE)

        self.assertEqual(0, send_epic_inactivity_alerts())
        mock_notify.assert_not_called()

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=False)
    def test_an_epic_that_cannot_be_notified_is_still_claimed(self, mock_notify):
        """No assignee (or they left the workspace): the notification declines,
        but the claim stands so the sweep does not retry it every hour forever."""
        epic = self._due_epic(assignee=None)

        self.assertEqual(0, send_epic_inactivity_alerts())
        mock_notify.assert_called_once()

        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_alerts_several_epics_in_one_run(self, mock_notify):
        first, second, third = self._due_epic(), self._due_epic(), self._due_epic()

        self.assertEqual(3, send_epic_inactivity_alerts())

        alerted = {call.args[0].pk for call in mock_notify.call_args_list}
        self.assertEqual({first.pk, second.pk, third.pk}, alerted)

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_spans_workspaces(self, mock_notify):
        """A system sweep has no workspace context and must cover them all."""
        other_workspace = WorkspaceFactory()
        other_project = ProjectFactory(workspace=other_workspace)
        other_user = UserFactory()
        MembershipFactory(workspace=other_workspace, user=other_user)

        mine = self._due_epic()
        theirs = EpicFactory(project=other_project, assignee=other_user)
        Epic.objects.filter(pk=theirs.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

        send_epic_inactivity_alerts()

        alerted = {call.args[0].pk for call in mock_notify.call_args_list}
        self.assertEqual({mine.pk, theirs.pk}, alerted)

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_reports_the_real_inactive_day_count(self, mock_notify):
        """Derived from last_activity_at, not assumed to be exactly the grace
        period — a backed-up worker should not claim seven days when it was ten."""
        epic = EpicFactory(project=self.project, assignee=self.assignee)
        Epic.objects.filter(pk=epic.pk).update(
            last_activity_at=timezone.now() - timedelta(days=10),
            inactivity_alert_due_at=timezone.now() - timedelta(days=3),
        )

        send_epic_inactivity_alerts()

        self.assertEqual(10, mock_notify.call_args.kwargs["inactive_days"])

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_story_activity_between_sweep_and_claim_cancels_the_alert(self, mock_notify):
        """The race the claim exists for: work lands after the epic is selected
        but before it is claimed, so the alert must be abandoned."""
        self._due_epic()

        real_claim = Epic.objects.claim_inactivity_alert

        def claim_after_activity(epic_id, now=None):
            # Simulate a Story being saved in the instant before the claim runs.
            Epic.objects.filter(pk=epic_id).update(inactivity_alert_due_at=timezone.now() + timedelta(days=7))
            return real_claim(epic_id, now=now)

        with patch.object(Epic.objects, "claim_inactivity_alert", side_effect=claim_after_activity):
            self.assertEqual(0, send_epic_inactivity_alerts())

        mock_notify.assert_not_called()

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_deleted_epic_between_sweep_and_claim_is_handled(self, mock_notify):
        """An epic can vanish mid-run; that is an expected race, not an error."""
        epic = self._due_epic()

        real_claim = Epic.objects.claim_inactivity_alert

        def claim_after_delete(epic_id, now=None):
            Epic.objects.filter(pk=epic_id).delete()
            return real_claim(epic_id, now=now)

        with patch.object(Epic.objects, "claim_inactivity_alert", side_effect=claim_after_delete):
            self.assertEqual(0, send_epic_inactivity_alerts())  # must not raise

        mock_notify.assert_not_called()
        self.assertFalse(Epic.objects.filter(pk=epic.pk).exists())


class EpicInactivitySweepCostTest(TestCase):
    """A run must not get more expensive as the project grows."""

    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.project = ProjectFactory(workspace=self.workspace)
        self.assignee = UserFactory()
        MembershipFactory(workspace=self.workspace, user=self.assignee)

    def _due_epic(self):
        epic = EpicFactory(project=self.project, assignee=self.assignee)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))
        return epic

    def test_a_run_with_nothing_due_is_a_single_query(self):
        """The common case, every hour, forever."""
        EpicFactory(project=self.project, assignee=self.assignee)  # not due

        with self.assertNumQueries(1):
            send_epic_inactivity_alerts()

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_does_not_read_story_rows(self, _mock_notify):
        epic = self._due_epic()
        for _ in range(5):
            StoryFactory(project=self.project, parent=epic)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

        with CaptureQueriesContext(connection) as captured:
            send_epic_inactivity_alerts()

        selects = [q["sql"] for q in captured.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertFalse(
            any("issues_story" in sql for sql in selects),
            f"sweep should never read Story rows, got: {selects}",
        )

    @patch("apps.issues.tasks.notify_epic_inactivity", return_value=True)
    def test_per_epic_cost_is_one_claim_query(self, _mock_notify):
        """Two due epics must cost the sweep plus one claim each — no per-epic
        SELECT to re-fetch the row or look up its assignee."""
        self._due_epic()
        self._due_epic()

        with CaptureQueriesContext(connection) as captured:
            send_epic_inactivity_alerts()

        # 1 sweep SELECT + 2 claim UPDATEs.
        self.assertEqual(3, len(captured.captured_queries), [q["sql"] for q in captured.captured_queries])
