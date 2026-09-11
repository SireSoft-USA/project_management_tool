from django.contrib import messages
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.utils.translation import gettext_lazy as _

from django_htmx.http import HttpResponseClientRedirect

from ..forms import ProjectForm
from ..models import Project


class WorkspaceAdminForProjectsMixin:
    """Only workspace administrators may create, update, or delete projects.

    Permission is the workspace membership role, not ``User.is_staff``. is_staff
    is a global Django-admin flag with no tenant meaning: it would let a staff
    user administer *every* workspace they happen to belong to, while denying a
    workspace's own administrator the right to manage their own projects. The
    role on Membership is the tenant-scoped answer, and is what the workspaces
    app already gates on (see workspaces.decorators.workspace_admin_required).

    Must be listed AFTER LoginAndWorkspaceRequiredMixin and BEFORE View:

    * after the login/membership guard, so anonymous users reach the login page
      and non-members get a 404 - being redirected to a workspace's project list
      would otherwise confirm to an outsider that the workspace exists;
    * before View, because Python resolves dispatch() left to right and View
      defines its own, so a guard listed after it never runs at all.

    Denial is a redirect with a message rather than a 404: the user is a genuine
    member of this workspace and may read the project list, so sending them there
    with an explanation is more honest than pretending the page does not exist.
    """

    def dispatch(self, request, *args, **kwargs):
        membership = request.workspace_membership
        if membership is None or not membership.is_admin():
            messages.error(request, _("You are not authorized for this access."))
            redirect_url = reverse(
                "projects:project_list",
                kwargs={"workspace_slug": request.workspace.slug},
            )
            # HTMX ignores a plain 302 body-swap for a full-page redirect, so the
            # client-side header is required for the browser to actually navigate.
            if request.htmx:
                return HttpResponseClientRedirect(redirect_url)
            return HttpResponseRedirect(redirect_url)
        return super().dispatch(request, *args, **kwargs)


class ProjectViewMixin:
    """Base mixin for all project views."""

    model = Project

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        if not request.workspace:
            raise Http404
        self.workspace = request.workspace

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        patch_vary_headers(response, ("HX-Request",))
        return response

    def get_template_names(self):
        if self.request.htmx and not self.request.htmx.history_restore_request:
            return [f"{self.template_name}#page-content"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["workspace"] = self.workspace
        return context


class ProjectSingleObjectMixin:
    """Mixin for views that operate on a single project (detail, update, delete)."""

    context_object_name = "project"
    slug_field = "key"
    slug_url_kwarg = "key"


class ProjectFormMixin:
    """Mixin for views with project forms (create, update)."""

    form_class = ProjectForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["workspace"] = self.workspace
        kwargs["workspace_members"] = self.request.workspace_members
        return kwargs
