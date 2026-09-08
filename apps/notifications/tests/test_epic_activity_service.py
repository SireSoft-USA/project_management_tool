"""Tests for the epic-activity decision layer.

These cover *whether* an email is queued and *who* it is addressed to. Rendering
and delivery are tested separately; here the task is patched out so each rule can
be asserted on its own.
"""

from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.issues.models import Epic, IssueStatus, Milestone, Story
from apps.notifications.services import notify_epic_activity
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership

CHANGES = [{"field": "status", "label": "Status", "old_value": "Draft", "new_value": "Done"}]


class EpicActivityServiceTest(TestCase):
    """Rules that decide whether an epic-activity email is queued."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory(first_name="Olive", last_name="Owner")
        cls.editor = UserFactory(first_name="Eddie", last_name="Editor")
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role=roles.ROLE_MEMBER)
        Membership.objects.create(workspace=cls.workspace, user=cls.editor, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = Epic.add_root(project=self.project, title="Website Redesign", assignee=self.owner)
        self.story = self.epic.add_child(instance=Story(project=self.project, title="Build login"))
        self.story.refresh_from_db()

    def _notify(self, **kwargs):
        """Call the service and run the on_commit callback, as a committed request would.

        Dispatch is deferred to transaction.on_commit, which TestCase never runs
        on its own (it rolls every test back). captureOnCommitCallbacks executes
        them, so these assertions see what a real committed edit would produce.
        """
        kwargs.setdefault("changes", CHANGES)
        kwargs.setdefault("actor", self.editor)
        with (
            patch("apps.notifications.services.send_epic_activity_email") as task,
            self.captureOnCommitCallbacks(execute=True),
        ):
            queued = notify_epic_activity(self.story, **kwargs)
        return queued, task

    def test_edit_under_epic_queues_email(self):
        queued, task = self._notify()

        self.assertTrue(queued)
        task.delay.assert_called_once()

    def test_no_changes_sends_nothing(self):
        """The central guard: a save that changed nothing must not email anybody."""
        queued, task = self._notify(changes=[])

        self.assertFalse(queued)
        task.delay.assert_not_called()

    def test_root_level_item_has_no_epic_to_notify(self):
        orphan = Story.add_root(project=self.project, title="No epic")
        orphan.refresh_from_db()

        with (
            patch("apps.notifications.services.send_epic_activity_email") as task,
            self.captureOnCommitCallbacks(execute=True),
        ):
            queued = notify_epic_activity(orphan, changes=CHANGES, actor=self.editor)

        self.assertFalse(queued)
        task.delay.assert_not_called()

    def test_item_under_a_milestone_is_not_epic_activity(self):
        """A Milestone is not an Epic; its children must not trigger epic mail."""
        milestone = Milestone.add_root(project=self.project, title="v1.0")
        child = milestone.add_child(instance=Story(project=self.project, title="Under milestone"))
        child.refresh_from_db()

        with (
            patch("apps.notifications.services.send_epic_activity_email") as task,
            self.captureOnCommitCallbacks(execute=True),
        ):
            queued = notify_epic_activity(child, changes=CHANGES, actor=self.editor)

        self.assertFalse(queued)
        task.delay.assert_not_called()

    def test_payload_identifies_issue_epic_and_actor(self):
        queued, task = self._notify(created=True)

        self.assertTrue(queued)
        args = task.delay.call_args.args
        self.assertEqual(args[0], self.story.pk)
        self.assertEqual(args[1], self.epic.pk)
        self.assertEqual(args[2], CHANGES)
        self.assertEqual(args[3], self.editor.pk)
        self.assertTrue(args[4])

    def test_system_change_without_actor_still_notifies(self):
        queued, task = self._notify(actor=None)

        self.assertTrue(queued)
        task.delay.assert_called_once()


class EpicActivityRecipientTest(TestCase):
    """Who the email is addressed to, and when nobody is left to address."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory()
        cls.outsider = UserFactory()
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role=roles.ROLE_MEMBER)

    def _story_under(self, assignee):
        epic = Epic.add_root(project=self.project, title="E", assignee=assignee)
        story = epic.add_child(instance=Story(project=self.project, title="S"))
        story.refresh_from_db()
        return story

    def test_self_action_still_reaches_the_admin_copy(self):
        """Editing your own epic tells you nothing new, but admin still wants the record."""
        story = self._story_under(self.owner)

        with (
            patch("apps.notifications.services.send_epic_activity_email") as task,
            self.captureOnCommitCallbacks(execute=True),
        ):
            queued = notify_epic_activity(story, changes=CHANGES, actor=self.owner)

        self.assertTrue(queued)
        task.delay.assert_called_once()

    @override_settings(EPIC_ACTIVITY_ADMIN_CC=[])
    def test_self_action_with_no_admin_copy_sends_nothing(self):
        """With no admin configured and the actor the only recipient, stop."""
        story = self._story_under(self.owner)

        with (
            patch("apps.notifications.services.send_epic_activity_email") as task,
            self.captureOnCommitCallbacks(execute=True),
        ):
            queued = notify_epic_activity(story, changes=CHANGES, actor=self.owner)

        self.assertFalse(queued)
        task.delay.assert_not_called()

    def test_unassigned_epic_still_notifies_admin(self):
        story = self._story_under(None)

        with (
            patch("apps.notifications.services.send_epic_activity_email"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            queued = notify_epic_activity(story, changes=CHANGES, actor=self.owner)

        self.assertTrue(queued)

    @override_settings(EPIC_ACTIVITY_ADMIN_CC=[])
    def test_unassigned_epic_with_no_admin_copy_sends_nothing(self):
        story = self._story_under(None)

        with (
            patch("apps.notifications.services.send_epic_activity_email") as task,
            self.captureOnCommitCallbacks(execute=True),
        ):
            queued = notify_epic_activity(story, changes=CHANGES, actor=self.owner)

        self.assertFalse(queued)
        task.delay.assert_not_called()

    @override_settings(EPIC_ACTIVITY_ADMIN_CC=[])
    def test_assignee_removed_from_workspace_is_not_emailed(self):
        """A former member would only receive a link they can no longer open."""
        story = self._story_under(self.outsider)

        with (
            patch("apps.notifications.services.send_epic_activity_email") as task,
            self.captureOnCommitCallbacks(execute=True),
        ):
            queued = notify_epic_activity(story, changes=CHANGES, actor=self.owner)

        self.assertFalse(queued)
        task.delay.assert_not_called()


class EpicActivityIsolationTest(TestCase):
    """The parent epic must never be resolved across a workspace boundary."""

    def test_epic_is_resolved_within_the_items_own_project(self):
        ws_a, ws_b = WorkspaceFactory(), WorkspaceFactory()
        project_a = ProjectFactory(workspace=ws_a)
        project_b = ProjectFactory(workspace=ws_b)

        owner_a, owner_b = UserFactory(), UserFactory()
        Membership.objects.create(workspace=ws_a, user=owner_a, role=roles.ROLE_MEMBER)
        Membership.objects.create(workspace=ws_b, user=owner_b, role=roles.ROLE_MEMBER)

        epic_a = Epic.add_root(project=project_a, title="A", assignee=owner_a)
        Epic.add_root(project=project_b, title="B", assignee=owner_b)

        story = epic_a.add_child(instance=Story(project=project_a, title="S"))
        story.refresh_from_db()

        with (
            patch("apps.notifications.services.send_epic_activity_email") as task,
            self.captureOnCommitCallbacks(execute=True),
        ):
            notify_epic_activity(story, changes=CHANGES, actor=owner_b)

        # The epic in the payload is workspace A's, never workspace B's.
        self.assertEqual(task.delay.call_args.args[1], epic_a.pk)


class EpicActivityTransactionTest(TestCase):
    """A rolled-back edit must not email anybody about a change that did not happen."""

    def test_queue_waits_for_commit(self):
        workspace = WorkspaceFactory()
        project = ProjectFactory(workspace=workspace)
        owner = UserFactory()
        Membership.objects.create(workspace=workspace, user=owner, role=roles.ROLE_MEMBER)
        epic = Epic.add_root(project=project, title="E", assignee=owner, status=IssueStatus.IN_PROGRESS)
        story = epic.add_child(instance=Story(project=project, title="S"))
        story.refresh_from_db()

        # captureOnCommitCallbacks(execute=False) leaves the callback pending,
        # which is what a rollback does: the task must not have been sent.
        with patch("apps.notifications.services.send_epic_activity_email") as task:
            with self.captureOnCommitCallbacks(execute=False):
                notify_epic_activity(story, changes=CHANGES, actor=UserFactory())
            task.delay.assert_not_called()


class EpicActivityQueryCostTest(TestCase):
    """Notifying must not make editing a story noticeably more expensive."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory()
        cls.editor = UserFactory()
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role=roles.ROLE_MEMBER)
        Membership.objects.create(workspace=cls.workspace, user=cls.editor, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = Epic.add_root(project=self.project, title="E", assignee=self.owner)
        self.story = self.epic.add_child(instance=Story(project=self.project, title="S"))
        self.story.refresh_from_db()

    def test_notifying_costs_two_queries(self):
        """One to resolve the parent epic, one to confirm the assignee is still a member.

        Finding the parent costs nothing extra: its tree path is derived in Python
        rather than fetched, so there is no separate lookup for the relationship.
        """
        with (
            patch("apps.notifications.services.send_epic_activity_email"),
            CaptureQueriesContext(connection) as queries,
            self.captureOnCommitCallbacks(execute=True),
        ):
            notify_epic_activity(self.story, changes=CHANGES, actor=self.editor)

        self.assertEqual(len(queries), 2)

    def test_unchanged_save_costs_nothing(self):
        """The common case — a save with no notifiable edit — must touch no database."""
        with (
            patch("apps.notifications.services.send_epic_activity_email"),
            CaptureQueriesContext(connection) as queries,
            self.captureOnCommitCallbacks(execute=True),
        ):
            notify_epic_activity(self.story, changes=[], actor=self.editor)

        self.assertEqual(len(queries), 0)
