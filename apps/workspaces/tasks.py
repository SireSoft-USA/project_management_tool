import logging
from smtplib import SMTPException

from django.apps import apps

from apps.notifications.emails import LogoEmailMessage, copy_recipients
from apps.workspaces.demo_data import create_demo_project

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    autoretry_for=(SMTPException, ConnectionError, TimeoutError, OSError),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
    soft_time_limit=30,
    time_limit=45,
    ignore_result=True,
)
def send_invitation_email(invitation_id: str) -> bool:
    """Deliver a workspace invitation email.

    Runs off the request cycle so a slow mail server cannot fail the invite, and
    retries on transport errors instead of losing the message.
    """
    # Imported here to avoid a circular import: invitations.py imports this task.
    from apps.workspaces.invitations import render_invitation_email  # noqa: PLC0415

    Invitation = apps.get_model("workspaces", "Invitation")

    try:
        invitation = Invitation.objects.select_related("workspace", "invited_by").get(pk=invitation_id)
    except Invitation.DoesNotExist:
        # Cancelled between queueing and delivery; nothing to send.
        logger.info("Invitation %s no longer exists, skipping email", invitation_id)
        return False

    parts = render_invitation_email(invitation)
    message = LogoEmailMessage(
        subject=parts["subject"],
        body=parts["message"],
        from_email=parts["from_email"],
        to=parts["recipient_list"],
        logo=parts["logo"],
        # Copy the monitoring addresses, same as notification email.
        **copy_recipients(exclude=parts["recipient_list"]),
    )
    message.attach_alternative(parts["html_message"], "text/html")
    # fail_silently=False so transport errors raise and the retry policy applies.
    message.send(fail_silently=False)
    logger.info("Invitation email sent to %s", invitation.email)
    return True


@shared_task
def create_demo_project_task(workspace_id: int, user_id: int):
    """Create demo project data for a newly created workspace."""
    Workspace = apps.get_model("workspaces", "Workspace")
    User = apps.get_model("users", "User")

    try:
        workspace = Workspace.objects.get(pk=workspace_id)
        user = User.objects.get(pk=user_id)
    except Workspace.DoesNotExist, User.DoesNotExist:
        logger.warning(
            "Workspace %s or user %s not found for demo project creation",
            workspace_id,
            user_id,
        )
        return

    create_demo_project(workspace, user)


DEMO_USER_EMAIL = "demo@siresoft.com"
DEMO_USER_PASSWORD = "demouser789"


@shared_task
def reset_demo_workspace_data():
    """Delete all projects, sprints, and related data for the demo user's workspace.

    Runs daily to keep the demo environment fresh for new visitors.
    DB cascades handle deletion of milestones, epics, stories, bugs, chores, issues, and subtasks.
    """
    User = apps.get_model("users", "User")
    Workspace = apps.get_model("workspaces", "Workspace")
    Project = apps.get_model("projects", "Project")
    Sprint = apps.get_model("sprints", "Sprint")

    try:
        user = User.objects.get(email=DEMO_USER_EMAIL)
    except User.DoesNotExist:
        logger.warning("Demo user '%s' not found, skipping workspace reset", DEMO_USER_EMAIL)
        return

    user.set_password(DEMO_USER_PASSWORD)
    user.save(update_fields=["password"])
    logger.info("Reset demo user password for '%s'", DEMO_USER_EMAIL)

    workspaces = Workspace.objects.for_user(user)

    if not workspaces.exists():
        logger.warning("No workspaces found for demo user '%s', skipping reset", DEMO_USER_EMAIL)
        return

    # there should be just one, but just in case there are multiple
    for workspace in workspaces.iterator():
        sprint_count, _ = Sprint.objects.filter(workspace=workspace).delete()
        project_count, _ = Project.objects.filter(workspace=workspace).delete()
        logger.info(
            "Reset demo workspace '%s': deleted %d sprints, %d projects (and cascaded children)",
            workspace.slug,
            sprint_count,
            project_count,
        )

        # then recreate the demo project
        create_demo_project(workspace, user)
