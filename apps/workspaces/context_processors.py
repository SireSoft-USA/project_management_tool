from apps.workspaces.models import Workspace

from .helpers import get_onboarding_session_key, get_onboarding_status


def onboarding_context(request):
    """Add onboarding pending count to all templates for sidebar badge.

    Uses the session to avoid re-running DB queries on every request. The cache is
    invalidated whenever the user completes an onboarding step (project created,
    invitation sent, sprint created, demo explored, or onboarding dismissed).
    """
    if (
        hasattr(request, "user")
        and request.user.is_authenticated
        and hasattr(request, "workspace")
        and request.workspace
    ):
        try:
            if request.user.onboarding_completed:
                return {"onboarding_pending_count": 0}
            session_key = get_onboarding_session_key(request.workspace)
            count = request.session.get(session_key)
            if count is None:
                count = get_onboarding_status(request.user, request.workspace)["pending_count"]
                request.session[session_key] = count
            return {"onboarding_pending_count": count}
        except Exception:
            pass
    return {"onboarding_pending_count": 0}


def default_workspace(request):
    """Provide the default workspace for the current request in templates."""
    workspace = getattr(request, "workspace", None)
    if workspace:
        return {"default_workspace": workspace}

    # Fallback: try to find user's first workspace
    if hasattr(request, "user") and request.user.is_authenticated:
        ws = Workspace.objects.for_user(request.user).first()
        if ws:
            return {"default_workspace": ws}

    return {}


def workspace_permissions(request):
    """Expose the current user's workspace role to every template.

    Templates use ``can_manage_projects`` to hide create/edit/delete controls
    from members who are not workspace administrators, so the UI never offers an
    action the view would refuse.

    This is a context processor rather than per-view context because the project
    templates are rendered from several places - the list view, the bulk action
    view, and two inline-edit views all render them directly with hand-built
    context dicts. Setting the flag in one view would leave the others silently
    missing it, which fails the wrong way: an administrator would lose their
    buttons. Defining it once here covers every render path.

    Costs no query: the middleware has already resolved and cached the
    membership on the request.

    This hides controls; it does not grant anything. The view guard
    (WorkspaceAdminForProjectsMixin) remains the actual authorization check.
    """
    membership = getattr(request, "workspace_membership", None)
    return {"can_manage_projects": bool(membership and membership.is_admin())}
