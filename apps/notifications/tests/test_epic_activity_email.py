"""Tests for the epic-activity email: subject wording and both rendered bodies.

Rendering is exercised directly rather than through Celery, so a wording or
escaping regression fails here rather than in an integration test that is slower
and harder to read.
"""

from django.template.loader import render_to_string
from django.test import TestCase

from apps.issues.changes import describe
from apps.issues.models import Epic, IssueStatus, Story
from apps.notifications.dispatch import build_epic_activity_message
from apps.notifications.models import NotificationKind
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership

CHANGES = [
    {"field": "status", "label": "Status", "old_value": "In Progress", "new_value": "Done"},
    {"field": "priority", "label": "Priority", "old_value": "Medium", "new_value": "High"},
]


class EpicActivityEmailTestCase(TestCase):
    """Shared fixtures: one epic with a story under it."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory(name="Acme")
        cls.project = ProjectFactory(workspace=cls.workspace, name="Demo Project")
        cls.owner = UserFactory(first_name="Olive", last_name="Owner")
        cls.actor = UserFactory(first_name="John", last_name="Smith")
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = Epic.add_root(project=self.project, title="Website Redesign", assignee=self.owner)
        self.story = self.epic.add_child(
            instance=Story(project=self.project, title="Build login", status=IssueStatus.DONE)
        )
        self.story.refresh_from_db()

    def _message(self, **kwargs):
        kwargs.setdefault("issue", self.story)
        kwargs.setdefault("epic", self.epic)
        kwargs.setdefault("changes", CHANGES)
        kwargs.setdefault("recipient", self.owner)
        kwargs.setdefault("actor", self.actor)
        return build_epic_activity_message(**kwargs)

    def _render(self, message, suffix):
        """Render one part with the context send_notification would have added."""
        context = {
            **message["context"],
            "server_url": "http://testserver",
            "unsubscribe_url": "http://testserver/notifications/unsubscribe/token/",
            "logo_cid": "",
            "logo_url": "",
        }
        return render_to_string(f"{message['template']}{suffix}", context)


class EpicActivityMessageTest(EpicActivityEmailTestCase):
    """Subject line and payload wiring."""

    def test_uses_the_epic_activity_kind(self):
        """Wrong kind would consult the wrong opt-out flag."""
        self.assertEqual(self._message()["kind"], NotificationKind.EPIC_ACTIVITY)

    def test_update_subject_names_item_and_epic(self):
        subject = self._message()["subject"]

        self.assertIn("DP-2", subject)
        self.assertIn("Build login", subject)
        self.assertIn("updated", subject)
        self.assertIn("Website Redesign", subject)

    def test_creation_subject_says_created(self):
        subject = self._message(created=True)["subject"]

        self.assertIn("created", subject)
        self.assertNotIn("updated", subject)

    def test_context_carries_both_links_and_workspace(self):
        context = self._message()["context"]

        self.assertIn(self.story.get_absolute_url(), context["issue_url"])
        self.assertIn(self.epic.get_absolute_url(), context["epic_url"])
        self.assertEqual(context["workspace"], self.workspace)
        self.assertEqual(context["project"], self.project)


class EpicActivityTextBodyTest(EpicActivityEmailTestCase):
    """The plain-text part, which is what many clients actually display."""

    def test_update_shows_old_and_new_values(self):
        body = self._render(self._message(), ".txt")

        self.assertIn("Status: In Progress -> Done", body)
        self.assertIn("Priority: Medium -> High", body)

    def test_update_names_the_actor_and_epic(self):
        body = self._render(self._message(), ".txt")

        self.assertIn("John Smith updated a story in Website Redesign", body)
        self.assertIn("Website Redesign", body)
        self.assertIn("Changed by: John Smith", body)

    def test_creation_lists_values_without_arrows(self):
        """A new item has no previous state, so "Not set -> X" would be a lie."""
        message = self._message(changes=describe(self.story), created=True)

        body = self._render(message, ".txt")

        self.assertIn("Details:", body)
        self.assertNotIn("->", body.split("View item")[0])

    def test_system_change_omits_the_actor_line(self):
        body = self._render(self._message(actor=None), ".txt")

        self.assertIn("A story in Website Redesign was updated", body)
        self.assertNotIn("Changed by:", body)

    def test_includes_both_links_and_unsubscribe(self):
        body = self._render(self._message(), ".txt")

        self.assertIn("View item:", body)
        self.assertIn("View epic:", body)
        self.assertIn("Unsubscribe:", body)


class EpicActivityHtmlBodyTest(EpicActivityEmailTestCase):
    """The HTML part, including the escaping guarantee."""

    def test_update_renders_both_values(self):
        body = self._render(self._message(), ".html")

        self.assertIn("In Progress", body)
        self.assertIn("Done", body)
        self.assertIn("Status", body)

    def test_creation_shows_details_heading(self):
        message = self._message(changes=describe(self.story), created=True)

        body = self._render(message, ".html")

        self.assertIn("Details", body)

    def test_markup_in_a_title_is_escaped(self):
        """A work item title is user input and must never become live HTML.

        Django autoescapes by default; this pins that behaviour so a future
        template edit adding |safe to these values fails the build.
        """
        payload = '<img src=x onerror="alert(1)">'
        self.story.title = payload
        message = self._message(
            changes=[{"field": "title", "label": "Title", "old_value": "Old", "new_value": payload}],
        )

        body = self._render(message, ".html")

        self.assertNotIn(payload, body)
        self.assertNotIn('onerror="alert(1)"', body)
        self.assertIn("&lt;img", body)

    def test_markup_in_an_epic_title_is_escaped(self):
        payload = "<script>alert(1)</script>"
        self.epic.title = payload

        body = self._render(self._message(), ".html")

        self.assertNotIn(payload, body)
        self.assertIn("&lt;script&gt;", body)

    def test_markup_in_an_actor_name_is_escaped(self):
        self.actor.first_name = "<b>Bold</b>"
        self.actor.last_name = ""

        body = self._render(self._message(), ".html")

        self.assertNotIn("<b>Bold</b>", body)
        self.assertIn("&lt;b&gt;", body)
