"""Celery tasks that deliver notification email off the request/response cycle.

Sending inline would put SMTP latency — and SMTP failure — directly in the user's
request. These tasks are queued instead, and retried with exponential backoff so a
briefly unavailable mail server does not lose a notification.
"""

import logging
from smtplib import SMTPException

from django.apps import apps

from apps.notifications.dispatch import (
    build_assignment_digest_message,
    build_assignment_message,
    build_membership_message,
)
from apps.notifications.emails import send_notification

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
