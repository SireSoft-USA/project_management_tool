"""End-to-end tests for the five assignment paths (Step 2).

Unit tests prove the guards work; these prove the guards are actually *reached*
from the real views. Assignment happens through five different code paths, and
one of them (bulk) uses queryset.update(), which bypasses signals entirely — so
each path needs its own proof.
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from apps.issues.factories import StoryFactory
from apps.issues.models import IssuePriority, IssueStatus
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership


class AssignmentPathTestCase(TestCase):
    """Shared workspace/project/user fixture for the five paths."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.workspace = WorkspaceFactory(name="Acme", slug="acme")
        self.project = ProjectFactory(workspace=self.workspace, key="DP", name="Demo")

        self.manager = self._member("manager@siresoft.com", roles.ROLE_ADMIN)
        self.assignee = self._member("assignee@siresoft.com", roles.ROLE_MEMBER)

        self.client.force_login(self.manager)

    def _member(self, email, role):
        user = UserFactory(email=email)
        Membership.objects.create(workspace=self.workspace, user=user, role=role)
        return user

    def _post(self, url, data):
        """POST inside captureOnCommitCallbacks so on_commit hooks actually run."""
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data)


class CreateWithAssigneePathTests(AssignmentPathTestCase):
    """Path 1 — creating an issue with an assignee already set."""

    def _url(self):
        return reverse(
            "issues:issue_create_typed",
            kwargs={"workspace_slug": self.workspace.slug, "project_key": self.project.key, "issue_type": "story"},
        )

    def _payload(self, **overrides):
        data = {
            "project": self.project.pk,
            "title": "New story",
            "description": "",
            "status": IssueStatus.DRAFT,
            "priority": IssuePriority.MEDIUM,
            "assignee": self.assignee.pk,
            "estimated_points": "",
            "due_date": "",
            "parent": "",
        }
        data.update(overrides)
        return data

    def test_emails_the_assignee(self):
        response = self._post(self._url(), self._payload())
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["assignee@siresoft.com"])

    def test_no_email_when_created_unassigned(self):
        self._post(self._url(), self._payload(assignee=""))
        self.assertEqual(len(mail.outbox), 0)

    def test_no_email_when_assigned_to_self(self):
        self._post(self._url(), self._payload(assignee=self.manager.pk))
        self.assertEqual(len(mail.outbox), 0)


class UpdateFormPathTests(AssignmentPathTestCase):
    """Path 2 — the full edit form."""

    def setUp(self):
        super().setUp()
        self.issue = StoryFactory(project=self.project, title="Existing story")

    def _url(self):
        return reverse(
            "issues:issue_update",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "project_key": self.project.key,
                "key": self.issue.key,
            },
        )

    def _payload(self, **overrides):
        data = {
            "project": self.project.pk,
            "title": self.issue.title,
            "description": "",
            "status": self.issue.status,
            "priority": IssuePriority.MEDIUM,
            "assignee": self.assignee.pk,
            "estimated_points": "",
            "due_date": "",
            "parent": "",
        }
        data.update(overrides)
        return data

    def test_emails_the_new_assignee(self):
        self._post(self._url(), self._payload())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["assignee@siresoft.com"])

    def test_email_links_to_the_issue(self):
        self._post(self._url(), self._payload())
        self.assertIn(self.issue.get_absolute_url(), mail.outbox[0].body)

    def test_no_second_email_when_assignee_unchanged(self):
        """The regression this guards: editing a title must not re-notify."""
        self._post(self._url(), self._payload())
        mail.outbox.clear()

        self._post(self._url(), self._payload(title="Renamed"))
        self.assertEqual(len(mail.outbox), 0)

    def test_no_email_when_unassigned(self):
        self.issue.assignee = self.assignee
        self.issue.save()

        self._post(self._url(), self._payload(assignee=""))
        self.assertEqual(len(mail.outbox), 0)


class RowInlineEditPathTests(AssignmentPathTestCase):
    """Path 4 — the inline editor on list rows."""

    def setUp(self):
        super().setUp()
        self.issue = StoryFactory(project=self.project, title="Row story")

    def _url(self):
        return reverse(
            "issues:issue_inline_edit",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "project_key": self.project.key,
                "key": self.issue.key,
            },
        )

    def _payload(self, **overrides):
        data = {
            "title": self.issue.title,
            "status": self.issue.status,
            "priority": IssuePriority.MEDIUM,
            "assignee": self.assignee.pk,
            "estimated_points": "",
        }
        data.update(overrides)
        return data

    def test_emails_the_new_assignee(self):
        self._post(self._url(), self._payload())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["assignee@siresoft.com"])

    def test_no_email_when_assignee_unchanged(self):
        self.issue.assignee = self.assignee
        self.issue.save()

        self._post(self._url(), self._payload())
        self.assertEqual(len(mail.outbox), 0)

    def test_no_email_on_self_assignment(self):
        self._post(self._url(), self._payload(assignee=self.manager.pk))
        self.assertEqual(len(mail.outbox), 0)


class DetailInlineEditPathTests(AssignmentPathTestCase):
    """Path 5 — the inline editor on the issue detail page."""

    def setUp(self):
        super().setUp()
        self.issue = StoryFactory(project=self.project, title="Detail story")

    def _url(self):
        return reverse(
            "issues:issue_detail_inline_edit",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "project_key": self.project.key,
                "key": self.issue.key,
            },
        )

    def _payload(self, **overrides):
        data = {
            "title": self.issue.title,
            "description": "",
            "status": self.issue.status,
            "priority": IssuePriority.MEDIUM,
            "assignee": self.assignee.pk,
            "estimated_points": "",
            "due_date": "",
        }
        data.update(overrides)
        return data

    def test_emails_the_new_assignee(self):
        self._post(self._url(), self._payload())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["assignee@siresoft.com"])

    def test_no_email_when_assignee_unchanged(self):
        self.issue.assignee = self.assignee
        self.issue.save()

        self._post(self._url(), self._payload())
        self.assertEqual(len(mail.outbox), 0)


class BulkAssignPathTests(AssignmentPathTestCase):
    """Path 3 — bulk assign. Uses queryset.update(), so signals never fire."""

    def setUp(self):
        super().setUp()
        self.issues = [StoryFactory(project=self.project, title=f"Bulk {i}") for i in range(3)]

    def _url(self):
        # Workspace-scoped issue URLs are mounted without a namespace.
        return reverse("workspace_issues_bulk_assignee", kwargs={"workspace_slug": self.workspace.slug})

    def _payload(self, assignee=None, issues=None):
        return {
            # The form validates issues by key (to_field_name="key"), not pk.
            "issues": [i.key for i in (issues if issues is not None else self.issues)],
            "assignee": str(assignee.pk) if assignee else "",
        }

    def test_emails_the_assignee_for_each_issue(self):
        """The path that would silently do nothing under a signal-based design."""
        self._post(self._url(), self._payload(assignee=self.assignee))
        self.assertEqual(len(mail.outbox), 3)
        for message in mail.outbox:
            self.assertEqual(message.to, ["assignee@siresoft.com"])

    def test_no_email_on_bulk_self_assignment(self):
        self._post(self._url(), self._payload(assignee=self.manager))
        self.assertEqual(len(mail.outbox), 0)

    def test_no_email_on_bulk_unassign(self):
        self._post(self._url(), self._payload(assignee=None))
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_issues_already_assigned_to_that_person(self):
        """Only genuinely-changed rows are notified."""
        self.issues[0].assignee = self.assignee
        self.issues[0].save()

        self._post(self._url(), self._payload(assignee=self.assignee))
        self.assertEqual(len(mail.outbox), 2)


class CreationViewCoverageTests(AssignmentPathTestCase):
    """Issues are created by eight different views; a signal covers them all.

    These pin the contract down so that adding a ninth creation view cannot
    silently skip notifications.
    """

    def test_project_scoped_creation_notifies(self):
        url = reverse(
            "projects:project_issue_create_typed",
            kwargs={"workspace_slug": self.workspace.slug, "key": self.project.key, "issue_type": "story"},
        )
        self._post(
            url,
            {
                "title": "Project scoped",
                "description": "",
                "status": IssueStatus.DRAFT,
                "priority": IssuePriority.MEDIUM,
                "assignee": self.assignee.pk,
                "estimated_points": "",
                "due_date": "",
            },
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["assignee@siresoft.com"])

    def test_creating_unassigned_sends_nothing(self):
        url = reverse(
            "projects:project_issue_create_typed",
            kwargs={"workspace_slug": self.workspace.slug, "key": self.project.key, "issue_type": "story"},
        )
        self._post(
            url,
            {
                "title": "No assignee",
                "description": "",
                "status": IssueStatus.DRAFT,
                "priority": IssuePriority.MEDIUM,
                "assignee": "",
                "estimated_points": "",
                "due_date": "",
            },
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_factory_created_issue_with_assignee_notifies(self):
        """Covers every creation path that goes through save(), including clones."""
        with self.captureOnCommitCallbacks(execute=True):
            StoryFactory(project=self.project, title="Direct", assignee=self.assignee, created_by=self.manager)
        self.assertEqual(len(mail.outbox), 1)

    def test_self_assignment_on_creation_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            StoryFactory(project=self.project, title="Mine", assignee=self.manager, created_by=self.manager)
        self.assertEqual(len(mail.outbox), 0)
