"""Tests for choosing several assignees on an Epic.

Three layers are covered:

* ``set_epic_assignees`` - the service that turns a requested set into rows, and
  reports what changed;
* the forms - whose querysets are the authorization control, since a posted id
  outside the workspace must be refused rather than trusted;
* the views - that a save through the UI actually persists the selection.
"""

from django.test import TestCase
from django.urls import reverse

from apps.issues.assignments import set_epic_assignees
from apps.issues.factories import EpicAssignmentFactory, EpicFactory
from apps.issues.forms import MAX_EPIC_ASSIGNEES, EpicDetailInlineEditForm, EpicForm
from apps.issues.models import Epic, EpicAssignment, IssuePriority, IssueStatus
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.users.models import User
from apps.workspaces import roles
from apps.workspaces.factories import MembershipFactory, WorkspaceFactory


class SetEpicAssigneesTest(TestCase):
    """The service that applies a requested assignee set."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.owner = UserFactory()
        cls.second = UserFactory()
        cls.third = UserFactory()
        cls.actor = UserFactory()
        for user in (cls.owner, cls.second, cls.third, cls.actor):
            MembershipFactory(workspace=cls.workspace, user=user, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = EpicFactory(project=self.project, assignee=self.owner)

    def _assigned(self):
        return {assignment.user for assignment in self.epic.assignments.all()}

    def test_adding_assignees_creates_rows(self):
        set_epic_assignees(self.epic, [self.second, self.third])

        self.assertEqual({self.owner, self.second, self.third}, self._assigned())

    def test_removing_an_assignee_deletes_the_row(self):
        set_epic_assignees(self.epic, [self.second, self.third])

        set_epic_assignees(self.epic, [self.second])

        self.assertEqual({self.owner, self.second}, self._assigned())

    def test_the_primary_assignee_is_never_removed(self):
        """Other parts of the app read Epic.assignee, so the two must agree.

        Changing who the primary owner is, is an edit to that field - not
        something the assignee list can do by omission.
        """
        set_epic_assignees(self.epic, [self.second])

        self.assertIn(self.owner, self._assigned())

    def test_an_empty_set_leaves_only_the_primary_assignee(self):
        set_epic_assignees(self.epic, [self.second, self.third])

        set_epic_assignees(self.epic, [])

        self.assertEqual({self.owner}, self._assigned())

    def test_an_unassigned_epic_can_be_emptied_completely(self):
        epic = EpicFactory(project=self.project, assignee=None)
        set_epic_assignees(epic, [self.second])

        set_epic_assignees(epic, [])

        self.assertEqual(0, epic.assignments.count())

    def test_it_reports_who_was_added(self):
        diff = set_epic_assignees(self.epic, [self.second])

        self.assertEqual([self.second], diff.added)

    def test_it_reports_who_was_removed(self):
        set_epic_assignees(self.epic, [self.second, self.third])

        diff = set_epic_assignees(self.epic, [self.second])

        self.assertEqual([self.third], diff.removed)

    def test_it_reports_who_stayed(self):
        set_epic_assignees(self.epic, [self.second, self.third])

        diff = set_epic_assignees(self.epic, [self.second])

        self.assertIn(self.second, diff.remaining)

    def test_the_three_groups_do_not_overlap(self):
        set_epic_assignees(self.epic, [self.second, self.third])

        diff = set_epic_assignees(self.epic, [self.second, self.actor])

        self.assertEqual(set(), set(diff.added) & set(diff.removed))
        self.assertEqual(set(), set(diff.added) & set(diff.remaining))
        self.assertEqual(set(), set(diff.removed) & set(diff.remaining))

    def test_a_no_op_change_reports_nothing(self):
        """Re-submitting the same people must not look like an edit."""
        set_epic_assignees(self.epic, [self.second])

        diff = set_epic_assignees(self.epic, [self.second])

        self.assertFalse(diff)
        self.assertEqual([], diff.added)
        self.assertEqual([], diff.removed)

    def test_a_real_change_reports_truthy(self):
        diff = set_epic_assignees(self.epic, [self.second])

        self.assertTrue(diff)

    def test_it_records_who_made_the_assignment(self):
        set_epic_assignees(self.epic, [self.second], actor=self.actor)

        assignment = EpicAssignment.objects.get(epic=self.epic, user=self.second)
        self.assertEqual(self.actor, assignment.added_by)

    def test_applying_the_same_set_twice_creates_no_duplicate(self):
        set_epic_assignees(self.epic, [self.second])
        set_epic_assignees(self.epic, [self.second])

        self.assertEqual(1, EpicAssignment.objects.filter(epic=self.epic, user=self.second).count())

    def test_it_does_not_touch_another_epic(self):
        other = EpicFactory(project=self.project, assignee=None)
        EpicAssignmentFactory(epic=other, user=self.third)

        set_epic_assignees(self.epic, [self.second])

        self.assertEqual({self.third}, {a.user for a in other.assignments.all()})


class EpicFormAssigneesTest(TestCase):
    """The create/edit form. Its queryset is the authorization control."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.member = UserFactory()
        cls.other_member = UserFactory()
        MembershipFactory(workspace=cls.workspace, user=cls.member, role=roles.ROLE_MEMBER)
        MembershipFactory(workspace=cls.workspace, user=cls.other_member, role=roles.ROLE_MEMBER)

        cls.stranger_workspace = WorkspaceFactory()
        cls.stranger = UserFactory()
        MembershipFactory(workspace=cls.stranger_workspace, user=cls.stranger, role=roles.ROLE_MEMBER)

    def _form(self, **data):
        payload = {
            "project": self.project.pk,
            "title": "Payment System",
            "status": IssueStatus.DRAFT,
            "priority": IssuePriority.MEDIUM,
        }
        payload.update(data)
        return EpicForm(payload, project=self.project, workspace=self.workspace)

    def test_several_assignees_are_accepted(self):
        form = self._form(assignees=[self.member.pk, self.other_member.pk])

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual({self.member, self.other_member}, set(form.cleaned_data["assignees"]))

    def test_no_assignees_is_valid(self):
        """An epic may legitimately have nobody on it."""
        form = self._form()

        self.assertTrue(form.is_valid(), form.errors)

    def test_a_user_from_another_workspace_is_rejected(self):
        """The attack this guards against: assigning by guessing an id.

        Rejection comes from the field's queryset, not from a hand-written check,
        which is what makes it impossible to bypass by posting directly.
        """
        form = self._form(assignees=[self.stranger.pk])

        self.assertFalse(form.is_valid())
        self.assertIn("assignees", form.errors)

    def test_a_nonexistent_user_is_rejected(self):
        form = self._form(assignees=[999999])

        self.assertFalse(form.is_valid())
        self.assertIn("assignees", form.errors)

    def test_a_valid_user_alongside_an_invalid_one_is_rejected(self):
        """Partial acceptance would silently drop the rejected id."""
        form = self._form(assignees=[self.member.pk, self.stranger.pk])

        self.assertFalse(form.is_valid())

    def test_duplicate_ids_are_collapsed(self):
        form = self._form(assignees=[self.member.pk, self.member.pk])

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(1, len(form.cleaned_data["assignees"]))

    def test_more_than_the_limit_is_rejected(self):
        """Every assignee costs an email per change, so the fan-out is bounded."""
        users = []
        for _ in range(MAX_EPIC_ASSIGNEES + 1):
            user = UserFactory()
            MembershipFactory(workspace=self.workspace, user=user, role=roles.ROLE_MEMBER)
            users.append(user)

        form = self._form(assignees=[user.pk for user in users])

        self.assertFalse(form.is_valid())
        self.assertIn("assignees", form.errors)

    def test_exactly_the_limit_is_accepted(self):
        users = []
        for _ in range(MAX_EPIC_ASSIGNEES):
            user = UserFactory()
            MembershipFactory(workspace=self.workspace, user=user, role=roles.ROLE_MEMBER)
            users.append(user)

        form = self._form(assignees=[user.pk for user in users])

        self.assertTrue(form.is_valid(), form.errors)

    def test_the_choices_are_limited_to_workspace_members(self):
        """What the dropdown offers matches what the form will accept."""
        form = EpicForm(project=self.project, workspace=self.workspace)

        self.assertNotIn(self.stranger, form.fields["assignees"].queryset)
        self.assertIn(self.member, form.fields["assignees"].queryset)

    def test_editing_prefills_the_current_assignees(self):
        epic = EpicFactory(project=self.project, assignee=self.member)
        EpicAssignmentFactory(epic=epic, user=self.other_member)

        form = EpicForm(instance=epic, project=self.project, workspace=self.workspace)

        self.assertEqual(
            {self.member.pk, self.other_member.pk},
            set(form.initial["assignees"]),
        )


class EpicInlineEditFormAssigneesTest(TestCase):
    """The same rules must hold on the inline header editor."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.member = UserFactory()
        MembershipFactory(workspace=cls.workspace, user=cls.member, role=roles.ROLE_MEMBER)
        cls.stranger = UserFactory()

    def _members(self):
        return User.objects.for_workspace(self.workspace)

    def _form(self, **data):
        payload = {"title": "Epic", "status": IssueStatus.DRAFT}
        payload.update(data)
        return EpicDetailInlineEditForm(payload, workspace_members=self._members())

    def test_a_workspace_member_is_accepted(self):
        form = self._form(assignees=[self.member.pk])

        self.assertTrue(form.is_valid(), form.errors)

    def test_a_user_from_outside_the_workspace_is_rejected(self):
        form = self._form(assignees=[self.stranger.pk])

        self.assertFalse(form.is_valid())
        self.assertIn("assignees", form.errors)

    def test_more_than_the_limit_is_rejected(self):
        users = []
        for _ in range(MAX_EPIC_ASSIGNEES + 1):
            user = UserFactory()
            MembershipFactory(workspace=self.workspace, user=user, role=roles.ROLE_MEMBER)
            users.append(user)

        form = self._form(assignees=[user.pk for user in users])

        self.assertFalse(form.is_valid())


class EpicAssigneeViewTest(TestCase):
    """A save through the UI must persist the selection."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.editor = UserFactory()
        cls.member = UserFactory()
        cls.other_member = UserFactory()
        for user in (cls.editor, cls.member, cls.other_member):
            MembershipFactory(workspace=cls.workspace, user=user, role=roles.ROLE_MEMBER)

        cls.stranger = UserFactory()

    def setUp(self):
        self.epic = EpicFactory(project=self.project, title="Payments", assignee=self.member)
        self.client.force_login(self.editor)

    def _inline_url(self):
        return reverse(
            "issues:epic_detail_inline_edit",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "project_key": self.project.key,
                "key": self.epic.key,
            },
        )

    def _post(self, assignees):
        return self.client.post(
            self._inline_url(),
            {
                "title": self.epic.title,
                "status": self.epic.status,
                "priority": self.epic.priority,
                "assignee": self.member.pk,
                "assignees": assignees,
            },
        )

    def test_saving_adds_the_selected_assignees(self):
        self._post([self.member.pk, self.other_member.pk])

        self.assertEqual(
            {self.member, self.other_member},
            {assignment.user for assignment in self.epic.assignments.all()},
        )

    def test_saving_removes_a_deselected_assignee(self):
        EpicAssignmentFactory(epic=self.epic, user=self.other_member)

        self._post([self.member.pk])

        self.assertEqual({self.member}, {a.user for a in self.epic.assignments.all()})

    def test_a_stranger_cannot_be_assigned_through_the_view(self):
        """Posting a foreign id directly must not create a row."""
        self._post([self.stranger.pk])

        self.assertFalse(self.epic.assignments.filter(user=self.stranger).exists())

    def test_the_header_shows_every_assignee_after_saving(self):
        response = self._post([self.member.pk, self.other_member.pk])

        self.assertContains(response, self.member.get_display_name())
        self.assertContains(response, self.other_member.get_display_name())

    def test_saving_shows_a_confirmation_toast(self):
        """The header is swapped in place, so the message has to arrive out of
        band - otherwise it is queued and only surfaces on the next full page
        load, long after the save it refers to."""
        response = self.client.post(
            self._inline_url(),
            {
                "title": self.epic.title,
                "status": self.epic.status,
                "priority": self.epic.priority,
                "assignee": self.member.pk,
                "assignees": [self.member.pk, self.other_member.pk],
            },
            headers={"HX-Request": "true"},
        )

        self.assertContains(response, "Epic updated successfully")
        self.assertContains(response, "hx-swap-oob")

    def test_the_edit_form_offers_the_multi_select(self):
        response = self.client.get(self._inline_url())

        self.assertContains(response, 'name="assignees"')

    def test_the_project_epic_list_shows_every_assignee(self):
        """The list column must not show only the primary owner.

        Everyone assigned is emailed about the epic, so a column naming one
        person misrepresents who is involved.
        """
        EpicAssignmentFactory(epic=self.epic, user=self.other_member)

        url = reverse(
            "projects:project_epics_embed",
            kwargs={"workspace_slug": self.workspace.slug, "key": self.project.key},
        )
        response = self.client.get(url)

        self.assertContains(response, self.member.get_display_name())
        self.assertContains(response, self.other_member.get_display_name())

    def test_the_detail_page_lists_every_assignee(self):
        """Showing only the primary owner would misrepresent who is notified."""
        EpicAssignmentFactory(epic=self.epic, user=self.other_member)

        response = self.client.get(self.epic.get_absolute_url())

        self.assertContains(response, self.member.get_display_name())
        self.assertContains(response, self.other_member.get_display_name())

    def test_creating_an_epic_with_assignees_persists_them(self):
        url = reverse(
            "issues:issue_create_typed",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "project_key": self.project.key,
                "issue_type": "epic",
            },
        )

        self.client.post(
            url,
            {
                "project": self.project.pk,
                "title": "Brand New Epic",
                "status": IssueStatus.DRAFT,
                "priority": IssuePriority.MEDIUM,
                "assignees": [self.member.pk, self.other_member.pk],
            },
        )

        epic = Epic.objects.get(title="Brand New Epic")
        self.assertEqual(
            {self.member, self.other_member},
            {assignment.user for assignment in epic.assignments.all()},
        )
