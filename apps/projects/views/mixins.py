from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.utils.translation import gettext_lazy as _

from ..forms import ProjectForm
from ..models import Project


class StaffRequiredMixin:
    """Only staff/admin users can create, update, or delete projects."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_staff:
            messages.error(request, _("You are not authorized for this access."))
            redirect_url = reverse(
                "projects:project_list",
                kwargs={"workspace_slug": request.workspace.slug},
            )
            if request.htmx:
                from django_htmx.http import HttpResponseClientRedirect
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