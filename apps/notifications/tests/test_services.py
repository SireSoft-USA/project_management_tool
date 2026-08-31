"""Tests for the notification decision rules (Step 2).

These guards decide whether an event becomes an email. A bug here either spams
people or silently loses notifications, so every skip condition is pinned down.
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings

from apps.issues.factories import StoryFactory
from apps.notifications.services import notify_assignment, notify_bulk_assignment, notify_member_added
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership


class OnCommitMixin:
    """Run on_commit callbacks that TestCase would otherwise never fire.

    TestCase wraps each test in a transaction that is rolled back, so the
    on_commit hooks the service layer relies on never execute. captureOnCommitCallbacks
    executes them explicitly, which is what happens in production on COMMIT.
    """

    def run_committed(self, func, *args, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return func(*args, **kwargs)


class AssignmentNotificationTests(OnCommitMixin, TestCase):
    """notify_assignment() — the rules for a single assignment."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme")
        self.project = ProjectFactory(workspace=self.workspace, key="DP")
        self.issue = StoryFactory(project=self.project, title="Fix login")

        self.assignee = self._member("assignee@siresoft.com")
        self.actor = self._member("manager@siresoft.com")

    def _member(self, email):
        user = UserFactory(email=email)
        Membership.objects.create(workspace=self.workspace, user=user, role=roles.ROLE_MEMBER)
        return user

    def test_notifies_on_a_real_assignment(self):
        self.assertTrue(self.run_committed(notify_assignment, self.issue, new_assignee=self.assignee, actor=self.actor))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["assignee@siresoft.com"])

    def test_skips_self_assignment(self):
        """You do not need an email about something you just did yourself."""
        self.assertFalse(self.run_committed(notify_assignment, self.issue, new_assignee=self.actor, actor=self.actor))
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_unassignment(self):
        self.assertFalse(self.run_committed(notify_assignment, self.issue, new_assignee=None, actor=self.actor))
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_when_assignee_did_not_change(self):
        """Editing only the title must not re-notify the existing assignee."""
        sent = self.run_committed(
            notify_assignment,
            self.issue,
            new_assignee=self.assignee,
            actor=self.actor,
            old_assignee=self.assignee,
        )
        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    def test_notifies_when_reassigned_to_someone_else(self):
        other = self._member("other@siresoft.com")
        sent = self.run_committed(
            notify_assignment,
            self.issue,
            new_assignee=other,
            actor=self.actor,
            old_assignee=self.assignee,
        )
        self.assertTrue(sent)
        self.assertEqual(mail.outbox[0].to, ["other@siresoft.com"])

    def test_skips_user_who_is_not_a_workspace_member(self):
        """They would only receive a link they cannot open."""
        outsider = UserFactory(email="outsider@example.com")
        self.assertFalse(self.run_committed(notify_assignment, self.issue, new_assignee=outsider, actor=self.actor))
        self.assertEqual(len(mail.outbox), 0)

    def test_notifies_when_there_is_no_actor(self):
        """System-driven assignment still tells the assignee."""
        self.assertTrue(self.run_committed(notify_assignment, self.issue, new_assignee=self.assignee, actor=None))
        self.assertEqual(len(mail.outbox), 1)

    def test_email_links_to_the_issue(self):
        self.run_committed(notify_assignment, self.issue, new_assignee=self.assignee, actor=self.actor)
        self.assertIn(self.issue.get_absolute_url(), mail.outbox[0].body)


class BulkAssignmentNotificationTests(OnCommitMixin, TestCase):
    """notify_bulk_assignment() — one email each, or one digest."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme")
        self.project = ProjectFactory(workspace=self.workspace, key="DP")
        self.assignee = UserFactory(email="assignee@siresoft.com")
        Membership.objects.create(workspace=self.workspace, user=self.assignee, role=roles.ROLE_MEMBER)
        self.actor = UserFactory(email="manager@siresoft.com")
        Membership.objects.create(workspace=self.workspace, user=self.actor, role=roles.ROLE_ADMIN)

    def _issues(self, count):
        return [StoryFactory(project=self.project, title=f"Task {i}") for i in range(count)]

    @override_settings(NOTIFICATION_BULK_THRESHOLD=10)
    def test_sends_one_email_per_issue_below_the_threshold(self):
        self.run_committed(notify_bulk_assignment, self._issues(3), new_assignee=self.assignee, actor=self.actor)
        self.assertEqual(len(mail.outbox), 3)

    @override_settings(NOTIFICATION_BULK_THRESHOLD=3)
    def test_sends_a_single_digest_above_the_threshold(self):
        """The inbox-flood guard: 5 issues, 1 email."""
        self.run_committed(notify_bulk_assignment, self._issues(5), new_assignee=self.assignee, actor=self.actor)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("5", mail.outbox[0].subject)

    @override_settings(NOTIFICATION_BULK_THRESHOLD=3)
    def test_threshold_boundary_still_sends_individually(self):
        """Exactly at the threshold is not "above" it."""
        self.run_committed(notify_bulk_assignment, self._issues(3), new_assignee=self.assignee, actor=self.actor)
        self.assertEqual(len(mail.outbox), 3)

    def test_digest_lists_every_issue(self):
        with override_settings(NOTIFICATION_BULK_THRESHOLD=2):
            issues = self._issues(4)
            self.run_committed(notify_bulk_assignment, issues, new_assignee=self.assignee, actor=self.actor)

        body = mail.outbox[0].body
        for issue in issues:
            self.assertIn(issue.key, body)

    def test_skips_self_assignment(self):
        self.run_committed(notify_bulk_assignment, self._issues(3), new_assignee=self.actor, actor=self.actor)
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_unassignment(self):
        self.run_committed(notify_bulk_assignment, self._issues(3), new_assignee=None, actor=self.actor)
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_non_member(self):
        outsider = UserFactory(email="outsider@example.com")
        self.run_committed(notify_bulk_assignment, self._issues(3), new_assignee=outsider, actor=self.actor)
        self.assertEqual(len(mail.outbox), 0)

    def test_empty_list_is_a_no_op(self):
        self.assertFalse(self.run_committed(notify_bulk_assignment, [], new_assignee=self.assignee, actor=self.actor))
        self.assertEqual(len(mail.outbox), 0)


class MembershipNotificationTests(OnCommitMixin, TestCase):
    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme", slug="acme")
        self.member = UserFactory(email="member@siresoft.com")
        self.actor = UserFactory(email="admin@siresoft.com")

    def test_notifies_the_new_member(self):
        self.assertTrue(self.run_committed(notify_member_added, self.workspace, member=self.member, actor=self.actor))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["member@siresoft.com"])

    def test_skips_self_add(self):
        """Whoever creates a workspace adds themselves; no email needed."""
        self.assertFalse(self.run_committed(notify_member_added, self.workspace, member=self.actor, actor=self.actor))
        self.assertEqual(len(mail.outbox), 0)

    def test_notifies_when_there_is_no_actor(self):
        self.assertTrue(self.run_committed(notify_member_added, self.workspace, member=self.member, actor=None))
        self.assertEqual(len(mail.outbox), 1)

    def test_none_member_is_a_no_op(self):
        self.assertFalse(self.run_committed(notify_member_added, self.workspace, member=None, actor=self.actor))
        self.assertEqual(len(mail.outbox), 0)


class TransactionSafetyTests(TestCase):
    """Emails must never describe a change that was rolled back.

    These deliberately do NOT use the OnCommitMixin helper: the point is to
    observe the raw on_commit behaviour — that nothing is sent until (and unless)
    the surrounding transaction commits.
    """

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme")
        self.project = ProjectFactory(workspace=self.workspace, key="DP")
        self.issue = StoryFactory(project=self.project, title="Fix login")
        self.assignee = UserFactory(email="assignee@siresoft.com")
        Membership.objects.create(workspace=self.workspace, user=self.assignee, role=roles.ROLE_MEMBER)
        self.actor = UserFactory(email="manager@siresoft.com")

    def test_no_email_when_the_inner_transaction_rolls_back(self):
        """The reason for on_commit: a failed request must send nothing.

        captureOnCommitCallbacks only collects callbacks from transactions that
        committed, so a rolled-back atomic block leaves the outbox empty even
        though notify_assignment() was called inside it.
        """

        class Boom(Exception):
            pass

        with self.captureOnCommitCallbacks(execute=True), self.assertRaises(Boom), transaction.atomic():
            notify_assignment(self.issue, new_assignee=self.assignee, actor=self.actor)
            raise Boom

        self.assertEqual(len(mail.outbox), 0)

    def test_nothing_is_sent_before_commit(self):
        """Queued, not sent, while the transaction is still open."""
        with self.captureOnCommitCallbacks(execute=True):
            notify_assignment(self.issue, new_assignee=self.assignee, actor=self.actor)
            self.assertEqual(len(mail.outbox), 0)

        self.assertEqual(len(mail.outbox), 1)
