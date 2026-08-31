import logging

from django.conf import settings
from django.contrib.sites.models import Site
from django.db import transaction
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _

from apps.notifications.services import notify_member_added
from apps.users.models import User

from .models import Invitation
from .tasks import send_invitation_email
from matorral.context_processors import get_root

logger = logging.getLogger(__name__)


def render_invitation_email(invitation) -> dict:
    """Build the subject and both body parts for an invitation email."""
    current_site = Site.objects.get_current()
    email_context = {
        "invitation": invitation,
        "project_name": current_site.name,
        # The shared base template renders the footer link from current_site;
        # without it the footer emits href="https://" with no host.
        "current_site": current_site,
        # Correct scheme for the environment (http locally, https in production).
        "server_url": get_root(),
    }
    return {
        "subject": _("You're invited to {}!").format(current_site.name),
        "message": render_to_string("workspaces/email/invitation.txt", context=email_context),
        "html_message": render_to_string("workspaces/email/invitation.html", context=email_context),
        "recipient_list": [invitation.email],
        "from_email": settings.DEFAULT_FROM_EMAIL,
    }


def send_invitation(invitation):
    """Queue the invitation email, falling back to an inline send.

    Delivery is deferred to Celery so a slow or unavailable mail server cannot
    turn "invite a member" into a 500 — the invitation row is already saved, and
    the task retries on transport failures. If the broker is unreachable the send
    happens inline instead, which is the normal path in local development.
    """

    def _queue():
        try:
            send_invitation_email.delay(str(invitation.pk))
        except Exception:
            logger.warning("Could not queue invitation email; sending inline instead", exc_info=True)
            send_invitation_email(str(invitation.pk))

    transaction.on_commit(_queue)


def process_invitation(invitation: Invitation, user: User):
    invitation.workspace.members.add(user, through_defaults={"role": invitation.role})
    invitation.is_accepted = True
    invitation.accepted_by = user
    invitation.save()

    # Confirms the new member now has access and links them straight into the
    # workspace. invited_by is the actor, so a self-accepted invitation (someone
    # inviting their own address) stays silent.
    notify_member_added(invitation.workspace, member=user, actor=invitation.invited_by)


def get_invitation_id_from_request(request):
    return request.GET.get("invitation_id") or request.session.get("invitation_id")


def clear_invite_from_session(request):
    if "invitation_id" in request.session:
        del request.session["invitation_id"]
