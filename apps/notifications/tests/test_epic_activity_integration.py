"""End-to-end tests: a real request through a real view produces the right email.

The unit tests cover each layer in isolation. These prove the layers are actually
wired together — that editing a story through the UI reaches the outbox, and that
the paths which bypass save() are covered too.
"""

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.issues.models import Bug, Chore, Epic, IssuePriority, IssueStatus, Story
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership

ADMIN = "admin@siresoft.com"


@override_settings(EPIC_ACTIVITY_ADMIN_CC=[ADMIN], NOTIFICATION_COPY_TO=[])
class EpicActivityIntegrationTestCase(TestCase):
    """A workspace with an epic owned by someone other than the editor."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory(email="olive@example.com", first_name="Olive", last_name="Owner")
        cls.editor = UserFactory(email="eddie@example.com", first_name="Eddie", last_name="Editor")
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role=roles.ROLE_ADMIN)
        Membership.objects.create(workspace=cls.workspace, user=cls.editor, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = Epic.add_root(project=self.project, title="Website Redesign", assignee=self.owner)
        self.client.force_login(self.editor)

    def _story(self, **kwargs):
        kwargs.setdefault("title", "Build login")
        kwargs.setdefault("status", IssueStatus.IN_PROGRESS)
        story = self.epic.add_child(instance=Story(project=self.project, **kwargs))
        story.refresh_from_db()
        return story

    def _edit_url(self, issue):
        return reverse(
            "issues:issue_update",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "project_key": self.project.key,
                "key": issue.key,
            },
        )

    def _post_edit(self, issue, **overrides):
        """Submit the full edit form, changing only what the caller overrides."""
        data = {
            "project": self.project.pk,
            "title": issue.title,
            "description": issue.description,
            "status": issue.status,
            "priority": issue.priority,
            "assignee": issue.assignee_id or "",
            "estimated_points": issue.estimated_points or "",
            "due_date": "",
            "parent": self.epic.pk,
        }
        data.update(overrides)
        return self.client.post(self._edit_url(issue), data)


class EpicActivityCreationIntegrationTest(EpicActivityIntegrationTestCase):
    """Creating a work item under an epic notifies its owner."""

    def _create_url(self, issue_type):
        return reverse(
            "projects:project_issue_create_typed",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "key": self.project.key,
                "issue_type": issue_type,
            },
        )

    def test_creating_a_story_notifies_the_epic_owner(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                self._create_url("story"),
                {
                    "title": "Implement authentication",
                    "description": "",
                    "status": IssueStatus.DRAFT,
                    "priority": IssuePriority.HIGH,
                    "assignee": "",
                    "estimated_points": "",
                    "due_date": "",
                    "parent": self.epic.pk,
                },
            )

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["olive@example.com"])
        self.assertIn("created", message.subject)
        self.assertIn("Implement authentication", message.body)

    def test_creation_email_lists_values_without_arrows(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                self._create_url("story"),
                {
                    "title": "New work",
                    "description": "",
                    "status": IssueStatus.DRAFT,
                    "priority": IssuePriority.HIGH,
                    "assignee": "",
                    "estimated_points": "",
                    "due_date": "",
                    "parent": self.epic.pk,
                },
            )

        body = mail.outbox[0].body
        self.assertIn("Details:", body)
        self.assertNotIn("->", body.split("View item")[0])

    def test_creating_a_root_item_notifies_nobody(self):
        """No parent epic means no owner to tell."""
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                self._create_url("story"),
                {
                    "title": "Orphan",
                    "description": "",
                    "status": IssueStatus.DRAFT,
                    "priority": IssuePriority.MEDIUM,
                    "assignee": "",
                    "estimated_points": "",
                    "due_date": "",
                    "parent": "",
                },
            )

        self.assertEqual(len(mail.outbox), 0)

    def test_bug_and_chore_creation_also_notify(self):
        """All three work item types count as epic activity."""
        for model, label in ((Bug, "bug"), (Chore, "chore")):
            mail.outbox.clear()
            with self.captureOnCommitCallbacks(execute=True):
                self.epic.add_child(instance=model(project=self.project, title=f"A {label}", created_by=self.editor))

            self.assertEqual(len(mail.outbox), 1, f"{label} did not notify")

    def test_creating_an_epic_is_not_epic_activity(self):
        """An epic is a container, not work done inside one."""
        with self.captureOnCommitCallbacks(execute=True):
            Epic.add_root(project=self.project, title="Another epic", created_by=self.editor)

        self.assertEqual(len(mail.outbox), 0)


class EpicActivityUpdateIntegrationTest(EpicActivityIntegrationTestCase):
    """Editing a work item reports the exact change."""

    def test_status_change_reports_old_and_new(self):
        story = self._story()

        with self.captureOnCommitCallbacks(execute=True):
            self._post_edit(story, status=IssueStatus.DONE)

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["olive@example.com"])
        self.assertIn(ADMIN, message.cc)
        self.assertIn("Status: In Progress -> Done", message.body)

    def test_saving_without_changing_anything_sends_nothing(self):
        """The guard that matters most: an idle save must not email anybody."""
        story = self._story()

        with self.captureOnCommitCallbacks(execute=True):
            self._post_edit(story)

        self.assertEqual(len(mail.outbox), 0)

    def test_several_fields_changed_at_once_send_one_email(self):
        story = self._story()

        with self.captureOnCommitCallbacks(execute=True):
            self._post_edit(
                story,
                title="Renamed",
                status=IssueStatus.DONE,
                priority=IssuePriority.CRITICAL,
            )

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("Title:", body)
        self.assertIn("Status:", body)
        self.assertIn("Priority:", body)

    def test_setting_a_previously_empty_field_reads_as_not_set(self):
        story = self._story()

        with self.captureOnCommitCallbacks(execute=True):
            self._post_edit(story, estimated_points=8)

        self.assertIn("Points: Not set -> 8", mail.outbox[0].body)

    def test_editor_who_owns_the_epic_gets_no_mail_but_admin_does(self):
        story = self._story()
        self.client.force_login(self.owner)

        with self.captureOnCommitCallbacks(execute=True):
            self._post_edit(story, status=IssueStatus.DONE)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [ADMIN])


class EpicActivityBulkIntegrationTest(EpicActivityIntegrationTestCase):
    """Bulk edits bypass save() entirely, so they need their own coverage."""

    def _bulk_url(self, name):
        return reverse(name, kwargs={"workspace_slug": self.workspace.slug})

    def test_bulk_status_change_notifies_per_item(self):
        first = self._story(title="One")
        second = self._story(title="Two")

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                self._bulk_url("workspace_issues_bulk_status"),
                {"issues": [first.key, second.key], "status": IssueStatus.DONE, "page": 1},
            )

        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Status: In Progress -> Done", mail.outbox[0].body)

    def test_bulk_status_change_to_the_same_value_sends_nothing(self):
        story = self._story(status=IssueStatus.DONE)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                self._bulk_url("workspace_issues_bulk_status"),
                {"issues": [story.key], "status": IssueStatus.DONE, "page": 1},
            )

        self.assertEqual(len(mail.outbox), 0)

    def test_bulk_priority_change_notifies(self):
        story = self._story(priority=IssuePriority.LOW)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                self._bulk_url("workspace_issues_bulk_priority"),
                {"issues": [story.key], "priority": IssuePriority.CRITICAL, "page": 1},
            )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Priority: Low -> Critical", mail.outbox[0].body)


@override_settings(EPIC_ACTIVITY_ADMIN_CC=[], NOTIFICATION_COPY_TO=[])
class EpicActivityWorkspaceIsolationTest(TestCase):
    """A notification must never cross a workspace boundary."""

    def test_edit_in_one_workspace_never_reaches_another(self):
        ws_a, ws_b = WorkspaceFactory(), WorkspaceFactory()
        project_a = ProjectFactory(workspace=ws_a)
        project_b = ProjectFactory(workspace=ws_b)

        owner_a = UserFactory(email="a@example.com")
        owner_b = UserFactory(email="b@example.com")
        editor = UserFactory(email="editor@example.com")
        Membership.objects.create(workspace=ws_a, user=owner_a, role=roles.ROLE_ADMIN)
        Membership.objects.create(workspace=ws_a, user=editor, role=roles.ROLE_MEMBER)
        Membership.objects.create(workspace=ws_b, user=owner_b, role=roles.ROLE_ADMIN)

        epic_a = Epic.add_root(project=project_a, title="A", assignee=owner_a)
        Epic.add_root(project=project_b, title="B", assignee=owner_b)
        story = epic_a.add_child(instance=Story(project=project_a, title="S", status=IssueStatus.IN_PROGRESS))
        story.refresh_from_db()

        self.client.force_login(editor)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse(
                    "issues:issue_update",
                    kwargs={
                        "workspace_slug": ws_a.slug,
                        "project_key": project_a.key,
                        "key": story.key,
                    },
                ),
                {
                    "project": project_a.pk,
                    "title": story.title,
                    "description": "",
                    "status": IssueStatus.DONE,
                    "priority": story.priority,
                    "assignee": "",
                    "estimated_points": "",
                    "due_date": "",
                    "parent": epic_a.pk,
                },
            )

        recipients = [address for message in mail.outbox for address in message.to]
        self.assertIn("a@example.com", recipients)
        self.assertNotIn("b@example.com", recipients)
