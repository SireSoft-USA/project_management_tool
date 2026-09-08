"""Celery tasks that deliver notification email off the request/response cycle.

Sending inline would put SMTP latency — and SMTP failure — directly in the user's
request. These tasks are queued instead, and retried with exponential backoff so a
briefly unavailable mail server does not lose a notification.
"""

import logging
from smtplib import SMTPException

from django.apps import apps
from django.conf import settings
from django.contrib.sites.models import Site
from django.template.loader import render_to_string

from apps.notifications.dispatch import (
    build_assignment_digest_message,
    build_assignment_message,
    build_epic_activity_message,
    build_epic_inactivity_message,
    build_membership_message,
)
from apps.notifications.emails import LogoEmailMessage, build_logo_context, logo_bytes, send_notification
from apps.notifications.recipients import epic_activity_admin_copies, epic_activity_recipient

from matorral.context_processors import get_root

from celery import shared_task

logger = logging.getLogger(__name__)

# Transport-level failures worth retrying. A template or programming error is not
# retryable — retrying it just burns the queue — so it is deliberately excluded.
RETRYABLE_ERRORS = (SMTPException, ConnectionError, TimeoutError, OSError)

# Applied to every task here: exponential backoff with jitter, capped, and bounded
# by a hard time limit so a hung SMTP socket cannot pin a worker forever.
TASK_KWARGS = {
    "autoretry_for": RETRYABLE_ERRORS,
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": 5,
    "soft_time_limit": 30,
    "time_limit": 45,
    "ignore_result": True,
}


@shared_task(**TASK_KWARGS)
def send_assignment_email(issue_id: int, recipient_id: int, actor_id: int | None = None) -> bool:
    """Email one person that a single issue was assigned to them."""
    issue, recipient, actor = _load(issue_id, recipient_id, actor_id)
    if issue is None or recipient is None:
        return False

    return send_notification(**build_assignment_message(issue=issue, recipient=recipient, actor=actor))


@shared_task(**TASK_KWARGS)
def send_assignment_digest_email(issue_ids: list[int], recipient_id: int, actor_id: int | None = None) -> bool:
    """Email one person a summary of several issues assigned in one bulk action."""
    BaseIssue = apps.get_model("issues", "BaseIssue")
    User = apps.get_model("users", "User")

    recipient = _get(User, recipient_id)
    if recipient is None:
        return False

    issues = list(BaseIssue.objects.filter(pk__in=issue_ids).select_related("project", "project__workspace"))
    if not issues:
        logger.info("Assignment digest skipped: no issues remain from %s", issue_ids)
        return False

    actor = _get(User, actor_id) if actor_id else None
    return send_notification(**build_assignment_digest_message(issues=issues, recipient=recipient, actor=actor))


@shared_task(**TASK_KWARGS)
def send_membership_email(workspace_id: int, recipient_id: int, actor_id: int | None = None) -> bool:
    """Email one person that they were added to a workspace."""
    Workspace = apps.get_model("workspaces", "Workspace")
    User = apps.get_model("users", "User")

    workspace = _get(Workspace, workspace_id)
    recipient = _get(User, recipient_id)
    if workspace is None or recipient is None:
        return False

    actor = _get(User, actor_id) if actor_id else None
    return send_notification(**build_membership_message(workspace=workspace, recipient=recipient, actor=actor))


@shared_task(**TASK_KWARGS)
def send_epic_inactivity_email(epic_id: int, recipient_id: int, inactive_days: int) -> bool:
    """Email an epic's assignee that it has had no story activity for a week.

    The alert has already been claimed by the time this runs (see
    Epic.objects.claim_inactivity_alert), so a retry after a transport failure
    re-sends the same alert rather than a duplicate one — the claim is what
    prevents duplicates, not this task.
    """
    Epic = apps.get_model("issues", "Epic")
    User = apps.get_model("users", "User")

    epic = _get(Epic.objects.select_related("project", "project__workspace"), epic_id, label="epic")
    recipient = _get(User, recipient_id, label="recipient")
    if epic is None or recipient is None:
        return False

    return send_notification(
        **build_epic_inactivity_message(epic=epic, recipient=recipient, inactive_days=inactive_days)
    )


@shared_task(**TASK_KWARGS)
def send_epic_activity_email(
    issue_id: int,
    epic_id: int,
    changes: list[dict],
    actor_id: int | None = None,
    created: bool = False,
) -> bool:
    """Email an epic's assignee that one of its work items changed.

    The change list is carried in the payload rather than recomputed here: the
    values it describes were overwritten the moment the row was saved, so the
    worker could not reconstruct them. Carrying them also makes a retry render
    exactly the same email rather than a differently-worded one.

    Recipients are resolved here rather than passed in, because the epic may have
    been reassigned between the edit and this task running. Sending to the address
    captured at queue time would email somebody who no longer owns the epic, and
    withhold it from whoever now does.
    """
    BaseIssue = apps.get_model("issues", "BaseIssue")
    Epic = apps.get_model("issues", "Epic")
    User = apps.get_model("users", "User")

    issue = _get(BaseIssue.objects.select_related("project", "project__workspace"), issue_id, label="issue")
    epic = _get(Epic.objects.select_related("assignee", "project", "project__workspace"), epic_id, label="epic")
    if issue is None or epic is None:
        return False

    actor = _get(User, actor_id) if actor_id else None
    recipient = epic_activity_recipient(epic, actor)
    admin_cc = epic_activity_admin_copies()

    if recipient is None:
        # Nobody to address: with an audit copy configured the message still has
        # somewhere to go, so it is sent from the admin address to itself rather
        # than dropped. Without one there is no message to send.
        if not admin_cc:
            logger.info("Epic activity email skipped: no recipient for epic %s", epic_id)
            return False
        return _send_admin_only_epic_activity(
            issue=issue, epic=epic, changes=changes, actor=actor, created=created, admin_cc=admin_cc
        )

    message = build_epic_activity_message(
        issue=issue, epic=epic, changes=changes, recipient=recipient, actor=actor, created=created
    )
    return send_notification(**message, extra_cc=admin_cc)


def _send_admin_only_epic_activity(*, issue, epic, changes, actor, created, admin_cc) -> bool:
    """Send an epic-activity message that has an audit copy but no assignee.

    send_notification() is built around a recipient: it checks that person's
    opt-out flag and builds their unsubscribe link. With no assignee there is no
    such person, so the message is assembled directly here instead. The audit
    addresses are configured by an operator rather than subscribed by a user, so
    there is no preference to honour and no unsubscribe link to offer.
    """
    if not getattr(settings, "NOTIFICATIONS_ENABLED", True):
        logger.info("Epic activity email skipped: notifications disabled globally")
        return False

    message = build_epic_activity_message(
        issue=issue, epic=epic, changes=changes, recipient=None, actor=actor, created=created
    )

    logo = logo_bytes()
    context = {
        **message["context"],
        "site": Site.objects.get_current(),
        "current_site": Site.objects.get_current(),
        "server_url": get_root(),
        # No subscriber, so no unsubscribe link. Templates render the footer link
        # only when this is set.
        "unsubscribe_url": "",
        **build_logo_context(logo),
    }

    email = LogoEmailMessage(
        subject=message["subject"],
        body=render_to_string(f"{message['template']}.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=admin_cc,
        logo=logo,
    )
    email.attach_alternative(render_to_string(f"{message['template']}.html", context), "text/html")
    email.send(fail_silently=False)
    logger.info("Epic activity email sent to audit copy only (epic=%s)", epic.pk)
    return True


def _load(issue_id: int, recipient_id: int, actor_id: int | None):
    """Fetch the objects a task needs, tolerating rows deleted since queueing."""
    BaseIssue = apps.get_model("issues", "BaseIssue")
    User = apps.get_model("users", "User")

    issue = _get(
        BaseIssue.objects.select_related("project", "project__workspace"),
        issue_id,
        label="issue",
    )
    recipient = _get(User, recipient_id, label="recipient")
    actor = _get(User, actor_id) if actor_id else None
    return issue, recipient, actor


def _get(source, pk, label: str = "object"):
    """Return ``source.get(pk=pk)`` or None, logging rather than raising.

    A task can run after its subject was deleted (the issue was removed between
    queueing and delivery). That is expected, not an error: returning None lets
    the task exit cleanly instead of retrying five times against a missing row.
    """
    if pk is None:
        return None
    manager = source if hasattr(source, "get") else source.objects
    model = getattr(manager, "model", source)
    try:
        return manager.get(pk=pk)
    except model.DoesNotExist:
        logger.info("Notification skipped: %s %s no longer exists", label, pk)
        return None
