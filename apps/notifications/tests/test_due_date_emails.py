"""End-to-end: editing a due date puts the right words in a real email.

The unit tests in apps.issues.tests.test_changes prove the diff is correct. These
prove the whole path — form, view, notification service, template — turns that
diff into an email a person can act on without opening the app.

Every work item type is exercised separately. Story, Bug and Chore reach the
notification through the same shared edit view, but they are distinct models with
distinct forms (a Bug has severity, a Chore does not), so "it works for Story" is
not evidence it works for the other two.
"""

import datetime

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.issues.models import Bug, Chore, Epic, IssueStatus, Story
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership

ADMIN = "admin@siresoft.com"


@override_settings(
    EPIC_ACTIVITY_ADMIN_CC=[ADMIN],
    NOTIFICATION_COPY_TO=[],
    LANGUAGE_CODE="en-us",
    USE_L10N=False,
)
class DueDateEmailTestCase(TestCase):
    """A workspace whose epic is owned by somebody other than the editor.

    The editor never receives mail about their own edit, so the epic owner is a
    separate user; that is what makes an outbox entry possible at all.
    """

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
        mail.outbox = []

    # --- helpers ---------------------------------------------------------

    def _child(self, model, **kwargs):
        kwargs.setdefault("title", f"{model.__name__} work")
        kwargs.setdefault("status", IssueStatus.IN_PROGRESS)
        issue = self.epic.add_child(instance=model(project=self.project, **kwargs))
        issue.refresh_from_db()
        return issue

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
        data = {
            "project": self.project.pk,
            "title": issue.title,
            "description": issue.description,
            "status": issue.status,
            "priority": issue.priority,
            "assignee": issue.assignee_id or "",
            "estimated_points": issue.estimated_points or "",
            "due_date": issue.due_date.isoformat() if issue.due_date else "",
            "parent": self.epic.pk,
        }
        if hasattr(issue, "severity"):
            data["severity"] = issue.severity
        data.update(overrides)
        # _dispatch queues through transaction.on_commit, and TestCase wraps each
        # test in a transaction that is rolled back, so without this the callback
        # never runs and the outbox stays empty regardless of the code under test.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self._edit_url(issue), data)

    def _body(self, message):
        """Return the plain-text body plus any HTML alternative, as one string."""
        parts = [message.body]
        parts.extend(content for content, _mime in getattr(message, "alternatives", []))
        return "\n".join(parts)

    # --- adding a due date that was missing ------------------------------

    def test_adding_a_first_due_date_emails_the_epic_owner(self):
        """The "added if first missing" case: Not set -> a real date."""
        story = self._child(Story)
        self.assertIsNone(story.due_date)

        self._post_edit(story, due_date="2026-09-20")

        self.assertEqual(len(mail.outbox), 1)
        body = self._body(mail.outbox[0])
        self.assertIn("Not set", body)
        self.assertIn("September 20, 2026", body)

    def test_first_due_date_reaches_the_epic_owner_and_the_audit_copy(self):
        story = self._child(Story)

        self._post_edit(story, due_date="2026-09-20")

        message = mail.outbox[0]
        self.assertEqual(message.to, [self.owner.email])
        self.assertIn(ADMIN, message.cc + message.bcc)

    def test_the_editor_is_never_mailed_about_their_own_edit(self):
        story = self._child(Story)

        self._post_edit(story, due_date="2026-09-20")

        for message in mail.outbox:
            self.assertNotIn(self.editor.email, message.to)

    # --- changing an existing due date, per work item type ----------------

    def test_story_due_date_change_shows_both_dates(self):
        story = self._child(Story, due_date=datetime.date(2026, 9, 15))

        self._post_edit(story, due_date="2026-09-20")

        self.assertEqual(len(mail.outbox), 1)
        body = self._body(mail.outbox[0])
        self.assertIn("September 15, 2026", body)
        self.assertIn("September 20, 2026", body)

    def test_bug_due_date_change_shows_both_dates(self):
        bug = self._child(Bug, due_date=datetime.date(2026, 9, 15))

        self._post_edit(bug, due_date="2026-09-20")

        self.assertEqual(len(mail.outbox), 1)
        body = self._body(mail.outbox[0])
        self.assertIn("September 15, 2026", body)
        self.assertIn("September 20, 2026", body)

    def test_chore_due_date_change_shows_both_dates(self):
        chore = self._child(Chore, due_date=datetime.date(2026, 9, 15))

        self._post_edit(chore, due_date="2026-09-20")

        self.assertEqual(len(mail.outbox), 1)
        body = self._body(mail.outbox[0])
        self.assertIn("September 15, 2026", body)
        self.assertIn("September 20, 2026", body)

    def test_no_iso_date_leaks_into_the_email(self):
        """The whole point of the change: a person reads prose, not a timestamp."""
        story = self._child(Story, due_date=datetime.date(2026, 9, 15))

        self._post_edit(story, due_date="2026-09-20")

        body = self._body(mail.outbox[0])
        self.assertNotIn("2026-09-15", body)
        self.assertNotIn("2026-09-20", body)

    def test_the_email_names_the_field_that_changed(self):
        story = self._child(Story, due_date=datetime.date(2026, 9, 15))

        self._post_edit(story, due_date="2026-09-20")

        self.assertIn("Due date", self._body(mail.outbox[0]))

    # --- clearing a due date ---------------------------------------------

    def test_clearing_a_due_date_is_reported_as_not_set(self):
        """Removing a deadline is news, so it must not be silent."""
        story = self._child(Story, due_date=datetime.date(2026, 9, 15))

        self._post_edit(story, due_date="")

        self.assertEqual(len(mail.outbox), 1)
        body = self._body(mail.outbox[0])
        self.assertIn("September 15, 2026", body)
        self.assertIn("Not set", body)

    # --- the silence cases ------------------------------------------------

    def test_resaving_the_same_due_date_sends_nothing(self):
        """The guard against spam: an edit that changed nothing must be silent."""
        story = self._child(Story, due_date=datetime.date(2026, 9, 15))

        self._post_edit(story, due_date="2026-09-15")

        self.assertEqual(mail.outbox, [])

    def test_editing_only_the_title_reports_no_due_date(self):
        story = self._child(Story, due_date=datetime.date(2026, 9, 15))

        self._post_edit(story, title="Renamed")

        self.assertEqual(len(mail.outbox), 1)
        body = self._body(mail.outbox[0])
        self.assertNotIn("Due date", body)
        self.assertNotIn("September 15, 2026", body)

    def test_a_title_that_looks_like_a_date_is_not_reformatted(self):
        """Coercion is scoped to date columns, so typed text survives intact."""
        story = self._child(Story)

        self._post_edit(story, title="2026-09-15")

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2026-09-15", self._body(mail.outbox[0]))


@override_settings(
    EPIC_ACTIVITY_ADMIN_CC=[ADMIN],
    NOTIFICATION_COPY_TO=[],
    LANGUAGE_CODE="en-us",
    USE_L10N=False,
)
class EpicOwnDueDateTestCase(TestCase):
    """Editing an Epic's *own* due date.

    Documents current behaviour rather than asserting a desired one: the epic
    inline-edit view saves the change but calls no notification service, so no
    mail is produced. The work-item tests above cover the path that does notify.
    """

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory(email="olive@example.com", first_name="Olive", last_name="Owner")
        cls.editor = UserFactory(email="eddie@example.com", first_name="Eddie", last_name="Editor")
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role=roles.ROLE_ADMIN)
        Membership.objects.create(workspace=cls.workspace, user=cls.editor, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = Epic.add_root(
            project=self.project,
            title="Website Redesign",
            assignee=self.owner,
            due_date=datetime.date(2026, 9, 15),
        )
        self.client.force_login(self.editor)
        mail.outbox = []

    def _post(self, due_date):
        url = reverse(
            "issues:epic_detail_inline_edit",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "project_key": self.project.key,
                "key": self.epic.key,
            },
        )
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                url,
                {
                    "title": self.epic.title,
                    "description": "",
                    "status": self.epic.status,
                    "priority": self.epic.priority or "",
                    "assignee": self.owner.pk,
                    "due_date": due_date,
                },
            )

    def _body(self, message):
        parts = [message.body]
        parts.extend(content for content, _mime in getattr(message, "alternatives", []))
        return "\n".join(parts)

    def test_epic_due_date_change_is_saved(self):
        self._post("2026-09-20")

        self.epic.refresh_from_db()
        self.assertEqual(self.epic.due_date, datetime.date(2026, 9, 20))

    def test_epic_due_date_change_emails_the_epic_assignee(self):
        """An epic deadline is usually the biggest one, so it must not be silent."""
        self._post("2026-09-20")

        self.assertEqual(len(mail.outbox), 1)
        body = self._body(mail.outbox[0])
        self.assertIn("September 15, 2026", body)
        self.assertIn("September 20, 2026", body)

    def test_the_email_goes_to_the_assignee_with_the_audit_copy(self):
        self._post("2026-09-20")

        message = mail.outbox[0]
        self.assertEqual(message.to, [self.owner.email])
        self.assertIn(ADMIN, message.cc + message.bcc)

    def test_the_subject_names_the_epic_not_a_child_item(self):
        """Passing the epic as its own subject must not read as a work item."""
        self._post("2026-09-20")

        subject = mail.outbox[0].subject
        self.assertIn(self.epic.key, subject)
        self.assertIn("Epic", subject)

    def test_the_body_omits_the_redundant_epic_context_line(self):
        """The epic is its own context, so the "Epic: KEY - Title" line is dropped.

        The title still appears twice by design — once in the opening sentence and
        once as the item the card describes — which is how the work-item mails read
        too. What must not appear is the separate context line pointing the epic at
        itself.
        """
        self._post("2026-09-20")

        body = mail.outbox[0].body
        self.assertNotIn(f"Epic: {self.epic.key}", body)

    def test_the_body_does_not_call_an_epic_a_work_item(self):
        self._post("2026-09-20")

        self.assertNotIn("updated a epic", self._body(mail.outbox[0]))

    def test_resaving_the_same_epic_due_date_sends_nothing(self):
        """The no-op guard applies to epics exactly as it does to work items."""
        self._post("2026-09-15")

        self.assertEqual(mail.outbox, [])

    def test_clearing_an_epic_due_date_is_reported(self):
        self._post("")

        self.assertEqual(len(mail.outbox), 1)
        body = self._body(mail.outbox[0])
        self.assertIn("September 15, 2026", body)
        self.assertIn("Not set", body)

    def test_an_epic_assignee_editing_their_own_epic_is_not_mailed(self):
        """Self-action stays silent here, the same as on the work-item path."""
        self.client.force_login(self.owner)

        self._post("2026-09-20")

        self.assertEqual([m for m in mail.outbox if self.owner.email in m.to], [])
