"""Tests for the Celery notification tasks (Step 1.4).

CELERY_TASK_ALWAYS_EAGER is on during tests, so calling a task runs it inline.
The behaviour that matters here is resilience: rows deleted between queueing and
delivery must not cause retries or crashes.
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.test import TestCase, override_settings

from apps.issues.factories import StoryFactory
from apps.notifications.models import NotificationPreference
from apps.notifications.tasks import (
    send_assignment_digest_email,
    send_assignment_email,
    send_membership_email,
)
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces.factories import WorkspaceFactory


class AssignmentTaskTests(TestCase):
    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme")
        self.project = ProjectFactory(workspace=self.workspace, name="Demo", key="DP")
        self.assignee = UserFactory(email="assignee@siresoft.com")
        self.actor = UserFactory(email="manager@siresoft.com", first_name="Mo", last_name="Manager")
        self.issue = StoryFactory(project=self.project, title="Fix the login bug")

    def test_sends_one_email_to_the_assignee(self):
        send_assignment_email(self.issue.pk, self.assignee.pk, self.actor.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["assignee@siresoft.com"])

    def test_subject_contains_key_and_title(self):
        """Inbox scanning and threading both rely on the key being visible."""
        send_assignment_email(self.issue.pk, self.assignee.pk, self.actor.pk)
        subject = mail.outbox[0].subject
        self.assertIn(self.issue.key, subject)
        self.assertIn("Fix the login bug", subject)

    def test_body_contains_a_clickable_absolute_link(self):
        send_assignment_email(self.issue.pk, self.assignee.pk, self.actor.pk)
        self.assertIn(f"http://10.0.2.11:8000{self.issue.get_absolute_url()}", mail.outbox[0].body)

    def test_names_the_person_who_assigned_it(self):
        send_assignment_email(self.issue.pk, self.assignee.pk, self.actor.pk)
        self.assertIn("Mo Manager", mail.outbox[0].body)

    def test_works_without_an_actor(self):
        """System-driven assignment has no actor; the email must still send."""
        send_assignment_email(self.issue.pk, self.assignee.pk, None)
        self.assertEqual(len(mail.outbox), 1)

    def test_deleted_issue_is_a_no_op(self):
        """The issue can be deleted between queueing and delivery."""
        issue_pk = self.issue.pk
        self.issue.delete()

        self.assertFalse(send_assignment_email(issue_pk, self.assignee.pk, self.actor.pk))
        self.assertEqual(len(mail.outbox), 0)

    def test_deleted_recipient_is_a_no_op(self):
        recipient_pk = self.assignee.pk
        self.assignee.delete()

        self.assertFalse(send_assignment_email(self.issue.pk, recipient_pk, self.actor.pk))
        self.assertEqual(len(mail.outbox), 0)

    def test_respects_opt_out(self):
        preference = NotificationPreference.objects.for_user(self.assignee)
        preference.notify_on_assignment = False
        preference.save()

        self.assertFalse(send_assignment_email(self.issue.pk, self.assignee.pk, self.actor.pk))
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(NOTIFICATIONS_ENABLED=False)
    def test_respects_global_kill_switch(self):
        self.assertFalse(send_assignment_email(self.issue.pk, self.assignee.pk, self.actor.pk))
        self.assertEqual(len(mail.outbox), 0)


class AssignmentDigestTaskTests(TestCase):
    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme")
        self.project = ProjectFactory(workspace=self.workspace, key="DP")
        self.assignee = UserFactory(email="assignee@siresoft.com")
        self.issues = [StoryFactory(project=self.project, title=f"Task {i}") for i in range(3)]

    def test_sends_a_single_email_for_many_issues(self):
        """The whole point: one message, not one per issue."""
        send_assignment_digest_email([i.pk for i in self.issues], self.assignee.pk, None)
        self.assertEqual(len(mail.outbox), 1)

    def test_subject_states_the_count(self):
        send_assignment_digest_email([i.pk for i in self.issues], self.assignee.pk, None)
        self.assertIn("3", mail.outbox[0].subject)

    def test_lists_every_issue_with_a_link(self):
        send_assignment_digest_email([i.pk for i in self.issues], self.assignee.pk, None)
        body = mail.outbox[0].body
        for issue in self.issues:
            self.assertIn(issue.key, body)
            self.assertIn(issue.get_absolute_url(), body)

    def test_skips_when_every_issue_was_deleted(self):
        pks = [i.pk for i in self.issues]
        for issue in self.issues:
            issue.delete()

        self.assertFalse(send_assignment_digest_email(pks, self.assignee.pk, None))
        self.assertEqual(len(mail.outbox), 0)

    def test_sends_for_the_issues_that_remain(self):
        """A partial deletion must still notify about the survivors."""
        pks = [i.pk for i in self.issues]
        self.issues[0].delete()

        self.assertTrue(send_assignment_digest_email(pks, self.assignee.pk, None))
        self.assertEqual(len(mail.outbox), 1)

    def test_empty_id_list_is_a_no_op(self):
        self.assertFalse(send_assignment_digest_email([], self.assignee.pk, None))
        self.assertEqual(len(mail.outbox), 0)


class MembershipTaskTests(TestCase):
    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme", slug="acme")
        self.member = UserFactory(email="member@siresoft.com")
        self.actor = UserFactory(email="admin@siresoft.com", first_name="Ada", last_name="Admin")

    def test_sends_to_the_new_member(self):
        send_membership_email(self.workspace.pk, self.member.pk, self.actor.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["member@siresoft.com"])

    def test_subject_names_the_workspace(self):
        send_membership_email(self.workspace.pk, self.member.pk, self.actor.pk)
        self.assertIn("Acme", mail.outbox[0].subject)

    def test_body_links_to_the_workspace(self):
        send_membership_email(self.workspace.pk, self.member.pk, self.actor.pk)
        self.assertIn(f"http://10.0.2.11:8000{self.workspace.get_absolute_url()}", mail.outbox[0].body)

    def test_deleted_workspace_is_a_no_op(self):
        workspace_pk = self.workspace.pk
        self.workspace.delete()

        self.assertFalse(send_membership_email(workspace_pk, self.member.pk, self.actor.pk))
        self.assertEqual(len(mail.outbox), 0)

    def test_respects_membership_opt_out(self):
        preference = NotificationPreference.objects.for_user(self.member)
        preference.notify_on_membership = False
        preference.save()

        self.assertFalse(send_membership_email(self.workspace.pk, self.member.pk, self.actor.pk))
        self.assertEqual(len(mail.outbox), 0)


class TaskConfigurationTests(TestCase):
    """Retry/limit settings are the difference between resilient and lossy."""

    def test_tasks_retry_on_transport_errors(self):
        for task in (send_assignment_email, send_assignment_digest_email, send_membership_email):
            with self.subTest(task=task.name):
                self.assertTrue(task.autoretry_for)
                self.assertEqual(task.max_retries, 5)

    def test_tasks_use_backoff_with_jitter(self):
        """Without jitter, retries from many workers stampede together."""
        self.assertTrue(send_assignment_email.retry_backoff)
        self.assertTrue(send_assignment_email.retry_jitter)

    def test_tasks_have_time_limits(self):
        """A hung SMTP socket must not pin a worker forever."""
        self.assertIsNotNone(send_assignment_email.soft_time_limit)
        self.assertIsNotNone(send_assignment_email.time_limit)

    def test_tasks_do_not_store_results(self):
        """Fire-and-forget; storing results would fill the backend for nothing."""
        self.assertTrue(send_assignment_email.ignore_result)
