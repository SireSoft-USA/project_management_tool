"""Tests for claiming an Epic's pending inactivity alert.

The claim is what makes "alert once per period of silence" true rather than
hopeful. Everything here is about the guarantee that two callers can never both
be told to send the same alert, and that work landing at the same instant wins
over a stale alert.
"""

import threading
from datetime import timedelta

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.issues.factories import EpicFactory, StoryFactory
from apps.issues.models import Epic, IssueStatus
from apps.projects.factories import ProjectFactory


class ClaimInactivityAlertTest(TestCase):
    """Single-caller behaviour."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def _due_epic(self, **kwargs):
        epic = EpicFactory(project=self.project, **kwargs)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))
        epic.refresh_from_db()
        return epic

    def test_claiming_a_due_epic_succeeds(self):
        epic = self._due_epic()

        self.assertTrue(Epic.objects.claim_inactivity_alert(epic.pk))

    def test_claiming_clears_the_due_date(self):
        """Clearing it is what stops the next hourly run re-alerting the same Epic."""
        epic = self._due_epic()

        Epic.objects.claim_inactivity_alert(epic.pk)

        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)

    def test_a_second_claim_fails(self):
        """The core guarantee: one alert per period of silence."""
        epic = self._due_epic()

        self.assertTrue(Epic.objects.claim_inactivity_alert(epic.pk))
        self.assertFalse(Epic.objects.claim_inactivity_alert(epic.pk))

    def test_claiming_an_epic_with_no_pending_alert_fails(self):
        epic = EpicFactory(project=self.project)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=None)

        self.assertFalse(Epic.objects.claim_inactivity_alert(epic.pk))

    def test_claiming_an_epic_whose_date_is_still_in_the_future_fails(self):
        """Not yet due — the scheduler should never have offered it, but the claim
        refuses independently rather than trusting its caller."""
        epic = EpicFactory(project=self.project)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() + timedelta(days=3))

        self.assertFalse(Epic.objects.claim_inactivity_alert(epic.pk))

    def test_claiming_a_deleted_epic_fails_without_raising(self):
        """An Epic can be deleted between the sweep and the claim. That is an
        expected race, not an error — it must return False, not explode."""
        epic = self._due_epic()
        epic_id = epic.pk
        Epic.objects.filter(pk=epic_id).delete()

        self.assertFalse(Epic.objects.claim_inactivity_alert(epic_id))

    def test_story_activity_just_before_the_claim_cancels_it(self):
        """The race the design exists to handle: work lands microseconds before
        the claim. Recording it pushes the due date into the future, so the claim
        finds nothing to take and no stale alert is sent."""
        epic = self._due_epic()

        StoryFactory(project=self.project, parent=epic)  # pushes the clock forward

        self.assertFalse(Epic.objects.claim_inactivity_alert(epic.pk))

    def test_claim_is_a_single_query(self):
        epic = self._due_epic()

        with self.assertNumQueries(1):
            Epic.objects.claim_inactivity_alert(epic.pk)

    def test_claim_does_not_read_the_row_first(self):
        """Read-then-write would reopen the race the conditional update closes."""
        epic = self._due_epic()

        with CaptureQueriesContext(connection) as captured:
            Epic.objects.claim_inactivity_alert(epic.pk)

        selects = [q["sql"] for q in captured.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertEqual([], selects)

    def test_now_override_is_honoured(self):
        """The scheduler judges a whole batch against one instant."""
        epic = EpicFactory(project=self.project)
        due_at = timezone.now() + timedelta(days=3)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=due_at)

        self.assertFalse(Epic.objects.claim_inactivity_alert(epic.pk))
        self.assertTrue(Epic.objects.claim_inactivity_alert(epic.pk, now=due_at + timedelta(seconds=1)))

    def test_a_new_period_of_silence_can_be_claimed_again(self):
        """After an alert is sent, fresh work then fresh silence must be alertable
        again — the claim blocks repeats, not all future alerts."""
        epic = self._due_epic()
        self.assertTrue(Epic.objects.claim_inactivity_alert(epic.pk))

        StoryFactory(project=self.project, parent=epic)  # new activity
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

        self.assertTrue(Epic.objects.claim_inactivity_alert(epic.pk))

    def test_claiming_a_done_epic_still_clears_a_stale_date(self):
        """A terminal Epic should never be offered by the sweep, which filters
        them out. If one is claimed directly the claim still succeeds and clears
        the stale date — the caller is responsible for not emailing about
        finished work, and the sweep is what enforces that."""
        epic = self._due_epic(status=IssueStatus.DONE)

        self.assertTrue(Epic.objects.claim_inactivity_alert(epic.pk))
        epic.refresh_from_db()
        self.assertIsNone(epic.inactivity_alert_due_at)


class ClaimInactivityAlertAtomicityTest(TestCase):
    """Proof that the claim is atomic at the database level.

    ClaimInactivityAlertConcurrencyTest below races real threads. These tests
    pin the mechanism that makes that safe, so a refactor to read-then-write
    fails here immediately rather than only under a timing-dependent race that
    may not reproduce on a fast local database.
    """

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()

    def _due_epic(self):
        epic = EpicFactory(project=self.project)
        Epic.objects.filter(pk=epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))
        epic.refresh_from_db()
        return epic

    def test_claim_is_one_conditional_update_not_a_read_then_write(self):
        """Atomicity comes entirely from the WHERE clause: the row is only
        cleared if it is still claimable at the moment the UPDATE runs, so the
        database — not application code — arbitrates between racing callers."""
        epic = self._due_epic()

        with CaptureQueriesContext(connection) as captured:
            Epic.objects.claim_inactivity_alert(epic.pk)

        statements = [q["sql"] for q in captured.captured_queries]
        self.assertEqual(1, len(statements))

        sql = statements[0].upper()
        self.assertTrue(sql.lstrip().startswith("UPDATE"))
        # The condition that makes it safe: only an Epic that is still due.
        self.assertIn("INACTIVITY_ALERT_DUE_AT", sql)
        self.assertIn("WHERE", sql)

    def test_second_claim_matches_zero_rows(self):
        """The losing caller is distinguished purely by rows-affected being 0,
        which is what a racing transaction would also see."""
        epic = self._due_epic()

        first = Epic.objects.filter(pk=epic.pk, inactivity_alert_due_at__lte=timezone.now()).update(
            inactivity_alert_due_at=None
        )
        second = Epic.objects.filter(pk=epic.pk, inactivity_alert_due_at__lte=timezone.now()).update(
            inactivity_alert_due_at=None
        )

        self.assertEqual(1, first)
        self.assertEqual(0, second)


class ClaimInactivityAlertConcurrencyTest(TransactionTestCase):
    """Two real connections racing for the same Epic.

    TransactionTestCase (not TestCase) because the threads must see each other's
    committed writes, which the usual test-wrapping transaction would hide.
    available_apps limits the flush to apps this test touches, avoiding the
    circular-FK truncate failure that a full flush hits on this schema.
    """

    available_apps = [
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "django.contrib.sites",
        "auditlog",  # Epic is @auditlog.register()ed; saving one needs its model
        "apps.users",
        "apps.workspaces",
        "apps.projects",
        "apps.issues",
        "apps.notifications",  # the assignment signal fires on issue creation
    ]

    def setUp(self):
        self.project = ProjectFactory()
        self.epic = EpicFactory(project=self.project)
        Epic.objects.filter(pk=self.epic.pk).update(inactivity_alert_due_at=timezone.now() - timedelta(hours=1))

    def _race(self, worker_count):
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(worker_count)

        def claim():
            try:
                barrier.wait(timeout=5)  # line every thread up on the same instant
                won = Epic.objects.claim_inactivity_alert(self.epic.pk)
                with lock:
                    results.append(won)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=claim) for _ in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        return results

    def test_only_one_of_two_concurrent_claims_wins(self):
        results = self._race(2)

        self.assertEqual(2, len(results), "both threads should have completed")
        self.assertEqual(1, results.count(True), f"exactly one claim must win, got {results}")

    def test_eight_concurrent_claims_produce_exactly_one_winner(self):
        """Scaled up: a backlog of workers all reaching the same Epic at once
        must still yield a single alert."""
        results = self._race(8)

        self.assertEqual(8, len(results))
        self.assertEqual(1, results.count(True), f"exactly one claim must win, got {results}")
