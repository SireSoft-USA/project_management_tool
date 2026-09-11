from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from apps.projects.factories import ProjectFactory
from apps.projects.models import Project, ProjectStatus
from apps.projects.views.bulk import ProjectBulkActionView
from apps.projects.views.crud import (
    ProjectCloneView,
    ProjectCreateView,
    ProjectDeleteView,
    ProjectDetailInlineEditView,
    ProjectMoveView,
    ProjectRowInlineEditView,
    ProjectUpdateView,
)
from apps.users.factories import UserFactory
from apps.workspaces.factories import MembershipFactory, WorkspaceFactory
from apps.workspaces.roles import ROLE_ADMIN, ROLE_MEMBER


class ProjectPermissionTestCase(TestCase):
    """Base test case providing common fixtures for permission tests."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.admin = UserFactory()
        cls.member = UserFactory()
        cls.outsider = UserFactory()

        MembershipFactory(workspace=cls.workspace, user=cls.admin, role=ROLE_ADMIN)
        MembershipFactory(workspace=cls.workspace, user=cls.member, role=ROLE_MEMBER)

        cls.project = ProjectFactory(workspace=cls.workspace, name="Test Project")

    def _get_list_url(self):
        return reverse(
            "projects:project_list",
            kwargs={"workspace_slug": self.workspace.slug},
        )

    def _get_detail_url(self, project=None):
        project = project or self.project
        return reverse(
            "projects:project_detail",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "key": project.key,
            },
        )

    def _get_create_url(self):
        return reverse(
            "projects:project_create",
            kwargs={"workspace_slug": self.workspace.slug},
        )

    def _get_update_url(self, project=None):
        project = project or self.project
        return reverse(
            "projects:project_update",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "key": project.key,
            },
        )

    def _get_delete_url(self, project=None):
        project = project or self.project
        return reverse(
            "projects:project_delete",
            kwargs={
                "workspace_slug": self.workspace.slug,
                "key": project.key,
            },
        )

    def _get_bulk_delete_url(self):
        return reverse(
            "projects:project_bulk_action",
            kwargs={"workspace_slug": self.workspace.slug, "action_name": "delete"},
        )

    def _get_bulk_status_url(self, status=ProjectStatus.ACTIVE):
        return reverse(
            "projects:project_bulk_action",
            kwargs={"workspace_slug": self.workspace.slug, "action_name": f"status-{status}"},
        )


class AuthenticationTest(ProjectPermissionTestCase):
    """Tests that unauthenticated users are redirected to login."""

    def test_unauthenticated_user_redirected_to_login_list(self):
        response = Client().get(self._get_list_url())

        self.assertEqual(302, response.status_code)
        self.assertIn("/login/", response.url)

    def test_unauthenticated_user_redirected_to_login_detail(self):
        response = Client().get(self._get_detail_url())

        self.assertEqual(302, response.status_code)
        self.assertIn("/login/", response.url)

    def test_unauthenticated_user_redirected_to_login_create(self):
        response = Client().get(self._get_create_url())

        self.assertEqual(302, response.status_code)
        self.assertIn("/login/", response.url)

    def test_unauthenticated_user_redirected_to_login_update(self):
        response = Client().get(self._get_update_url())

        self.assertEqual(302, response.status_code)
        self.assertIn("/login/", response.url)

    def test_unauthenticated_user_redirected_to_login_delete(self):
        response = Client().get(self._get_delete_url())

        self.assertEqual(302, response.status_code)
        self.assertIn("/login/", response.url)

    def test_unauthenticated_user_redirected_to_login_bulk_delete(self):
        response = Client().post(self._get_bulk_delete_url(), {"projects": [self.project.key]})

        self.assertEqual(302, response.status_code)
        self.assertIn("/login/", response.url)

    def test_unauthenticated_user_redirected_to_login_bulk_status(self):
        response = Client().post(
            self._get_bulk_status_url(),
            {"projects": [self.project.key], "status": ProjectStatus.ACTIVE},
        )

        self.assertEqual(302, response.status_code)
        self.assertIn("/login/", response.url)


class WorkspaceMembershipTest(ProjectPermissionTestCase):
    """Tests that non-workspace members cannot access project views."""

    def test_non_workspace_member_cannot_access_list(self):
        client = Client()
        client.force_login(self.outsider)

        response = client.get(self._get_list_url())

        self.assertEqual(404, response.status_code)

    def test_non_workspace_member_cannot_access_detail(self):
        client = Client()
        client.force_login(self.outsider)

        response = client.get(self._get_detail_url())

        self.assertEqual(404, response.status_code)

    def test_non_workspace_member_cannot_access_create(self):
        client = Client()
        client.force_login(self.outsider)

        response = client.get(self._get_create_url())

        self.assertEqual(404, response.status_code)

    def test_non_workspace_member_cannot_access_update(self):
        client = Client()
        client.force_login(self.outsider)

        response = client.get(self._get_update_url())

        self.assertEqual(404, response.status_code)

    def test_non_workspace_member_cannot_access_delete(self):
        client = Client()
        client.force_login(self.outsider)

        response = client.get(self._get_delete_url())

        self.assertEqual(404, response.status_code)

    def test_non_workspace_member_cannot_bulk_delete(self):
        client = Client()
        client.force_login(self.outsider)

        response = client.post(self._get_bulk_delete_url(), {"projects": [self.project.key]})

        self.assertEqual(404, response.status_code)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_non_workspace_member_cannot_bulk_status(self):
        client = Client()
        client.force_login(self.outsider)

        response = client.post(
            self._get_bulk_status_url(),
            {"projects": [self.project.key], "status": ProjectStatus.ACTIVE},
        )

        self.assertEqual(404, response.status_code)
        self.project.refresh_from_db()
        self.assertEqual(ProjectStatus.DRAFT, self.project.status)


class WorkspaceMemberAccessTest(ProjectPermissionTestCase):
    """Tests that workspace members can access project views."""

    def test_workspace_member_can_access_list(self):
        client = Client()
        client.force_login(self.member)

        response = client.get(self._get_list_url())

        self.assertEqual(200, response.status_code)

    def test_workspace_member_can_access_detail(self):
        client = Client()
        client.force_login(self.member)

        response = client.get(self._get_detail_url())

        self.assertEqual(200, response.status_code)

    def test_workspace_admin_can_create_project(self):
        """Creating a project is administrator-only; ProjectAdminGateTest covers
        the member-is-refused half of the rule."""
        client = Client()
        client.force_login(self.admin)

        response = client.post(
            self._get_create_url(),
            {"name": "New Project", "status": ProjectStatus.DRAFT, "description": ""},
        )

        # Successful create redirects to detail page
        self.assertEqual(302, response.status_code)
        self.assertTrue(Project.objects.filter(name="New Project").exists())

    def test_workspace_admin_can_update_project(self):
        """Updating a project is administrator-only."""
        client = Client()
        client.force_login(self.admin)

        response = client.post(
            self._get_update_url(),
            {
                "name": "Updated Name",
                "status": ProjectStatus.ACTIVE,
                "description": "Updated",
            },
        )

        # Successful update redirects to detail page
        self.assertEqual(302, response.status_code)
        self.project.refresh_from_db()
        self.assertEqual("Updated Name", self.project.name)

    def test_workspace_admin_can_delete_project(self):
        """Deleting a project is administrator-only."""
        client = Client()
        client.force_login(self.admin)
        project_pk = self.project.pk

        response = client.post(self._get_delete_url())

        # Successful delete redirects to list page
        self.assertEqual(302, response.status_code)
        self.assertFalse(Project.objects.filter(pk=project_pk).exists())


class WorkspaceIsolationTest(TestCase):
    """Tests that users cannot access projects in other workspaces."""

    @classmethod
    def setUpTestData(cls):
        # Workspace 1 with member and project
        cls.workspace1 = WorkspaceFactory()
        cls.user1 = UserFactory()
        MembershipFactory(workspace=cls.workspace1, user=cls.user1, role=ROLE_MEMBER)
        cls.project1 = ProjectFactory(workspace=cls.workspace1, name="Workspace 1 Project")

        # Workspace 2 with member and project
        cls.workspace2 = WorkspaceFactory()
        cls.user2 = UserFactory()
        MembershipFactory(workspace=cls.workspace2, user=cls.user2, role=ROLE_MEMBER)
        cls.project2 = ProjectFactory(workspace=cls.workspace2, name="Workspace 2 Project")

    def test_cannot_access_project_in_other_teams_workspace(self):
        """User from workspace1 cannot view project in workspace2."""
        client = Client()
        client.force_login(self.user1)

        url = reverse(
            "projects:project_detail",
            kwargs={
                "workspace_slug": self.workspace2.slug,
                "key": self.project2.key,
            },
        )
        response = client.get(url)

        self.assertEqual(404, response.status_code)

    def test_cannot_modify_project_in_other_teams_workspace(self):
        """User from workspace1 cannot update project in workspace2."""
        client = Client()
        client.force_login(self.user1)

        url = reverse(
            "projects:project_update",
            kwargs={
                "workspace_slug": self.workspace2.slug,
                "key": self.project2.key,
            },
        )
        response = client.post(url, {"name": "Hacked!", "status": ProjectStatus.ACTIVE, "description": ""})

        self.assertEqual(404, response.status_code)
        self.project2.refresh_from_db()
        self.assertEqual("Workspace 2 Project", self.project2.name)

    def test_cannot_delete_project_in_other_teams_workspace(self):
        """User from workspace1 cannot delete project in workspace2."""
        client = Client()
        client.force_login(self.user1)

        url = reverse(
            "projects:project_delete",
            kwargs={
                "workspace_slug": self.workspace2.slug,
                "key": self.project2.key,
            },
        )
        response = client.post(url)

        self.assertEqual(404, response.status_code)
        self.assertTrue(Project.objects.filter(pk=self.project2.pk).exists())

    def test_project_list_only_shows_workspace_projects(self):
        """Project list only shows projects from the current workspace."""
        client = Client()
        client.force_login(self.user1)

        url = reverse(
            "projects:project_list",
            kwargs={"workspace_slug": self.workspace1.slug},
        )
        response = client.get(url)

        self.assertEqual(200, response.status_code)
        self.assertContains(response, "Workspace 1 Project")
        self.assertNotContains(response, "Workspace 2 Project")


class ProjectMoveAdminGateTest(TestCase):
    """The staff gate on ProjectMoveView must actually run.

    ProjectMoveView once listed its permission guard *after* View. Python resolves
    dispatch() left to right along the MRO and View defines its own, so the guard
    was never reached: any workspace member could move a project - and every
    issue in it - into another workspace, rekeying the issues and clearing their
    sprints. These tests assert on the effect (whether the move actually starts)
    rather than on the status code, because the gate redirect and the view's own
    success redirect are both a 302 to the same URL.
    """

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.target_workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace, name="Movable Project")

        # Permission is the workspace role, not the global is_staff flag, so the
        # allowed user here is an administrator who is deliberately NOT staff.
        cls.staff = UserFactory(is_staff=False)
        cls.member = UserFactory(is_staff=False)
        cls.outsider = UserFactory(is_staff=False)
        cls.staff_outsider = UserFactory(is_staff=True)

        MembershipFactory(workspace=cls.workspace, user=cls.staff, role=ROLE_ADMIN)
        MembershipFactory(workspace=cls.target_workspace, user=cls.staff, role=ROLE_ADMIN)
        MembershipFactory(workspace=cls.workspace, user=cls.member, role=ROLE_MEMBER)
        MembershipFactory(workspace=cls.target_workspace, user=cls.member, role=ROLE_MEMBER)

    def _move_url(self, workspace_slug=None, key=None):
        return reverse(
            "projects:project_move",
            kwargs={
                "workspace_slug": workspace_slug or self.workspace.slug,
                "key": key or self.project.key,
            },
        )

    def _post_move(self, user=None, **url_kwargs):
        """POST a move and report whether the move actually started."""
        client = Client()
        if user is not None:
            client.force_login(user)

        with patch("apps.projects.views.crud.start_move_operation") as mocked:
            response = client.post(self._move_url(**url_kwargs), {"workspace": self.target_workspace.pk})

        return response, mocked

    def test_non_staff_member_cannot_start_a_move(self):
        """The regression this class exists for: the gate must block the move."""
        _response, mocked = self._post_move(self.member)

        self.assertFalse(mocked.called, "non-staff member reached start_move_operation")

    def test_non_staff_member_is_redirected_to_the_project_list(self):
        response, _mocked = self._post_move(self.member)

        self.assertEqual(302, response.status_code)
        self.assertEqual(
            reverse("projects:project_list", kwargs={"workspace_slug": self.workspace.slug}),
            response.url,
        )

    def test_staff_member_can_start_a_move(self):
        """The gate must not block the people it is meant to allow."""
        _response, mocked = self._post_move(self.staff)

        mocked.assert_called_once_with([self.project.pk], self.target_workspace.pk)

    def test_anonymous_user_is_sent_to_login_not_the_project_list(self):
        """Login must be checked before staff status.

        AnonymousUser.is_staff is False, so a staff check running first would
        bounce anonymous users to a project list they cannot see, losing the
        ?next= that returns them here after signing in.
        """
        response, mocked = self._post_move(None)

        self.assertFalse(mocked.called)
        self.assertEqual(302, response.status_code)
        self.assertIn("/login/", response.url)
        self.assertIn("next=", response.url)

    def test_non_member_gets_404_rather_than_a_redirect(self):
        """Membership must be checked before staff status.

        A non-member redirected to the workspace project list would learn that
        the workspace exists; 404 tells them nothing.
        """
        response, mocked = self._post_move(self.outsider)

        self.assertFalse(mocked.called)
        self.assertEqual(404, response.status_code)

    def test_staff_from_another_workspace_gets_404(self):
        """is_staff is global; it must not grant access to another workspace."""
        response, mocked = self._post_move(self.staff_outsider)

        self.assertFalse(mocked.called)
        self.assertEqual(404, response.status_code)

    def test_move_is_post_only(self):
        """http_method_names allows POST only; GET must not reach the view body."""
        client = Client()
        client.force_login(self.staff)

        with patch("apps.projects.views.crud.start_move_operation") as mocked:
            response = client.get(self._move_url())

        self.assertFalse(mocked.called)
        self.assertEqual(405, response.status_code)

    def test_unknown_workspace_slug_gets_404_for_anonymous_user(self):
        """request.workspace is None here; no guard may crash dereferencing it."""
        response, mocked = self._post_move(None, workspace_slug="no-such-workspace")

        self.assertFalse(mocked.called)
        self.assertEqual(404, response.status_code)

    def test_staff_cannot_move_a_project_from_another_workspace(self):
        """Passing a valid key under the wrong workspace slug must not move it."""
        other_workspace = WorkspaceFactory()
        MembershipFactory(workspace=other_workspace, user=self.staff, role=ROLE_MEMBER)
        foreign_project = ProjectFactory(workspace=other_workspace, name="Foreign")

        response, mocked = self._post_move(self.staff, key=foreign_project.key)

        self.assertFalse(mocked.called)
        self.assertEqual(404, response.status_code)

    def test_staff_cannot_move_into_a_workspace_they_do_not_belong_to(self):
        """The target workspace is scoped to the memberships of the requesting user."""
        stranger_workspace = WorkspaceFactory()
        client = Client()
        client.force_login(self.staff)

        with patch("apps.projects.views.crud.start_move_operation") as mocked:
            response = client.post(self._move_url(), {"workspace": stranger_workspace.pk})

        self.assertFalse(mocked.called)
        self.assertEqual(404, response.status_code)

    def test_staff_cannot_move_a_project_into_its_current_workspace(self):
        """A no-op move is rejected rather than queued."""
        client = Client()
        client.force_login(self.staff)

        with patch("apps.projects.views.crud.start_move_operation") as mocked:
            response = client.post(self._move_url(), {"workspace": self.workspace.pk})

        self.assertFalse(mocked.called)
        self.assertEqual(404, response.status_code)

    def test_missing_target_workspace_is_rejected(self):
        """A POST with no target must 404, not raise."""
        client = Client()
        client.force_login(self.staff)

        with patch("apps.projects.views.crud.start_move_operation") as mocked:
            response = client.post(self._move_url(), {})

        self.assertFalse(mocked.called)
        self.assertEqual(404, response.status_code)

    def test_the_staff_guard_runs_before_the_view_body(self):
        """Structural guard against the bug returning.

        The regression was invisible at the call site: the class still *listed*
        the guard. Asserting on the resolved MRO catches a future
        reordering directly, rather than waiting for a behavioural test to
        notice a bypass.
        """
        names = [cls.__name__ for cls in ProjectMoveView.__mro__]

        self.assertLess(
            names.index("WorkspaceAdminForProjectsMixin"),
            names.index("View"),
            "the guard must precede View or its dispatch() never runs",
        )
        self.assertLess(
            names.index("LoginAndWorkspaceRequiredMixin"),
            names.index("WorkspaceAdminForProjectsMixin"),
            "authentication must be checked before the workspace role",
        )

        owner = next(cls for cls in ProjectMoveView.__mro__ if "dispatch" in cls.__dict__)
        self.assertEqual("LoginAndWorkspaceRequiredMixin", owner.__name__)


class ProjectAdminGateTest(TestCase):
    """Only workspace administrators may create, update or delete projects.

    Two bugs are covered here. ProjectUpdateView and ProjectDeleteView both
    documented themselves as staff-only but carried no guard at all, so any
    workspace member could rename or delete a project. And the guard the other
    views did use keyed on User.is_staff - a global Django-admin flag with no
    tenant meaning - rather than on the workspace membership role.

    Each mutating endpoint is checked twice: an administrator must be allowed
    through, and a plain member must be stopped *and leave the data unchanged*.
    Asserting on the database rather than the status code matters because the
    denial redirect and several success redirects are both a 302.
    """

    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.admin = UserFactory(is_staff=False)
        self.member = UserFactory(is_staff=False)
        self.django_staff = UserFactory(is_staff=True)

        MembershipFactory(workspace=self.workspace, user=self.admin, role=ROLE_ADMIN)
        MembershipFactory(workspace=self.workspace, user=self.member, role=ROLE_MEMBER)
        MembershipFactory(workspace=self.workspace, user=self.django_staff, role=ROLE_MEMBER)

        self.project = ProjectFactory(workspace=self.workspace, name="Original Name")

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _url(self, name, **extra):
        kwargs = {"workspace_slug": self.workspace.slug}
        kwargs.update(extra)
        return reverse(f"projects:{name}", kwargs=kwargs)

    def _update_url(self):
        return self._url("project_update", key=self.project.key)

    def _delete_url(self):
        return self._url("project_delete", key=self.project.key)

    def _bulk_delete_url(self):
        return self._url("project_bulk_action", action_name="delete")

    # --- update -----------------------------------------------------------

    def test_member_cannot_rename_a_project(self):
        """The first of the two unguarded views."""
        self._client(self.member).post(
            self._update_url(),
            {"name": "Renamed By Member", "status": self.project.status, "description": ""},
        )

        self.project.refresh_from_db()
        self.assertEqual("Original Name", self.project.name)

    def test_admin_can_rename_a_project(self):
        self._client(self.admin).post(
            self._update_url(),
            {"name": "Renamed By Admin", "status": self.project.status, "description": ""},
        )

        self.project.refresh_from_db()
        self.assertEqual("Renamed By Admin", self.project.name)

    def test_member_is_told_why_the_update_was_refused(self):
        """A member may read this workspace, so an explanation beats a bare 404."""
        response = self._client(self.member).post(
            self._update_url(),
            {"name": "Nope", "status": self.project.status, "description": ""},
            follow=True,
        )

        self.assertContains(response, "not authorized")

    # --- delete -----------------------------------------------------------

    def test_member_cannot_delete_a_project(self):
        """The second unguarded view - and the destructive one."""
        self._client(self.member).post(self._delete_url())

        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_admin_can_delete_a_project(self):
        self._client(self.admin).post(self._delete_url())

        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())

    def test_member_cannot_reach_the_delete_confirmation(self):
        """The guard is on dispatch, so GET is refused too, not just the POST."""
        response = self._client(self.member).get(self._delete_url())

        self.assertEqual(302, response.status_code)

    # --- bulk actions -----------------------------------------------------

    def test_member_cannot_bulk_delete_projects(self):
        """Bulk delete must not be a way around the single-delete guard."""
        self._client(self.member).post(self._bulk_delete_url(), {"projects": [self.project.key], "page": 1})

        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_admin_can_bulk_delete_projects(self):
        self._client(self.admin).post(self._bulk_delete_url(), {"projects": [self.project.key], "page": 1})

        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())

    # --- create -----------------------------------------------------------

    def test_member_cannot_open_the_create_form(self):
        response = self._client(self.member).get(self._url("project_create"))

        self.assertEqual(302, response.status_code)

    def test_member_cannot_create_a_project(self):
        self._client(self.member).post(
            self._url("project_create"),
            {"name": "Sneaky Project", "status": ProjectStatus.DRAFT, "description": ""},
        )

        self.assertFalse(Project.objects.filter(name="Sneaky Project").exists())

    def test_admin_can_create_a_project(self):
        self._client(self.admin).post(
            self._url("project_create"),
            {"name": "Legitimate Project", "status": ProjectStatus.DRAFT, "description": ""},
        )

        self.assertTrue(Project.objects.filter(name="Legitimate Project").exists())

    # --- clone and inline edit -------------------------------------------

    def test_member_cannot_clone_a_project(self):
        self._client(self.member).post(self._url("project_clone", key=self.project.key))

        self.assertEqual(1, Project.objects.filter(workspace=self.workspace).count())

    def test_member_cannot_use_the_detail_inline_edit(self):
        response = self._client(self.member).get(self._url("project_detail_inline_edit", key=self.project.key))

        self.assertEqual(302, response.status_code)

    def test_member_cannot_use_the_row_inline_edit(self):
        response = self._client(self.member).get(self._url("project_inline_edit", key=self.project.key))

        self.assertEqual(302, response.status_code)

    # --- is_staff must not be the permission ------------------------------

    def test_django_staff_flag_does_not_grant_project_management(self):
        """The reason the guard was rewritten.

        is_staff means "may open the Django admin". It says nothing about which
        workspace somebody belongs to or what role they hold in it, so it must
        not let an ordinary member administer this workspace's projects.
        """
        self._client(self.django_staff).post(self._delete_url())

        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_workspace_admin_without_the_staff_flag_is_allowed(self):
        """The mirror image: the workspace's own administrator is not staff."""
        self.assertFalse(self.admin.is_staff)

        self._client(self.admin).post(self._delete_url())

        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())

    # --- non-members and anonymous users ----------------------------------

    def test_non_member_gets_404_not_a_redirect(self):
        """Membership is checked before role, so an outsider learns nothing."""
        outsider = UserFactory(is_staff=False)

        response = self._client(outsider).post(self._delete_url())

        self.assertEqual(404, response.status_code)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_anonymous_user_is_sent_to_login(self):
        response = Client().post(self._delete_url())

        self.assertEqual(302, response.status_code)
        self.assertIn("/login/", response.url)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    # --- read access is unchanged -----------------------------------------

    def test_member_can_still_read_the_project_list(self):
        """The gate covers mutation only; members keep full read access."""
        response = self._client(self.member).get(self._url("project_list"))

        self.assertEqual(200, response.status_code)

    def test_member_can_still_read_a_project_detail_page(self):
        response = self._client(self.member).get(self._url("project_detail", key=self.project.key))

        self.assertEqual(200, response.status_code)

    # --- the UI must match the permission ---------------------------------

    def test_member_is_not_offered_a_new_project_button(self):
        """An action the view will refuse must not be presented as available."""
        response = self._client(self.member).get(self._url("project_list"))

        self.assertNotContains(response, "New Project")

    def test_admin_is_offered_a_new_project_button(self):
        response = self._client(self.admin).get(self._url("project_list"))

        self.assertContains(response, "New Project")

    def test_can_manage_projects_flag_is_false_for_a_member(self):
        response = self._client(self.member).get(self._url("project_list"))

        self.assertFalse(response.context["can_manage_projects"])

    def test_can_manage_projects_flag_is_true_for_an_admin(self):
        response = self._client(self.admin).get(self._url("project_list"))

        self.assertTrue(response.context["can_manage_projects"])

    # --- structural guard --------------------------------------------------

    def test_every_mutating_view_orders_its_guards_correctly(self):
        """Catch a future reordering directly rather than via a bypass.

        The guard must sit after the login/membership check (so anonymous users
        reach login and outsiders get a 404) and before View (whose dispatch()
        would otherwise run first and skip the guard entirely).
        """
        views = [
            ProjectCreateView,
            ProjectUpdateView,
            ProjectDeleteView,
            ProjectCloneView,
            ProjectRowInlineEditView,
            ProjectDetailInlineEditView,
            ProjectMoveView,
            ProjectBulkActionView,
        ]

        for view in views:
            with self.subTest(view=view.__name__):
                names = [cls.__name__ for cls in view.__mro__]

                self.assertLess(
                    names.index("LoginAndWorkspaceRequiredMixin"),
                    names.index("WorkspaceAdminForProjectsMixin"),
                    "authentication must be checked before the workspace role",
                )
                self.assertLess(
                    names.index("WorkspaceAdminForProjectsMixin"),
                    names.index("View"),
                    "the guard must precede View or its dispatch() never runs",
                )
