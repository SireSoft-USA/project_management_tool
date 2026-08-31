"""Decide whether an event deserves an email, then queue it.

Views call these functions; they own the "should we?" rules. Delivery mechanics
live in tasks.py, and the final recipient guards (opt-out, no address) live in
emails.py — this layer is about the *event*, not the recipient.

Every dispatch goes through transaction.on_commit() so a rolled-back request can
never email somebody about a change that did not happen.
"""

import logging

from django.conf import settings
from django.db import transaction

from apps.notifications.tasks import (
    send_assignment_digest_email,
    send_assignment_email,
    send_membership_email,
)
from apps.workspaces.roles import is_member

logger = logging.getLogger(__name__)


def notify_assignment(issue, *, new_assignee, actor=None, old_assignee=None) -> bool:
    """Queue an "assigned to you" email for a single issue.

    Returns True if an email was queued. Skips silently when:
      * the issue was unassigned (new_assignee is None)
      * the assignee did not actually change
      * somebody assigned an issue to themselves
      * the assignee is not a member of the issue's workspace

    Args:
        issue: The issue that was assigned.
        new_assignee: User the issue now belongs to, or None.
        actor: User who made the change; None for system-driven changes.
        old_assignee: Previous assignee, used to detect a no-op change.
    """
    if not _should_notify_assignment(issue, new_assignee, actor, old_assignee):
        return False

    _dispatch(send_assignment_email, issue.pk, new_assignee.pk, actor.pk if actor else None)
    return True


def notify_bulk_assignment(issues, *, new_assignee, actor=None) -> bool:
    """Queue assignment email for issues assigned together in one action.

    Sends one email per issue up to NOTIFICATION_BULK_THRESHOLD, and a single
    digest beyond it — assigning 200 issues must not put 200 emails in an inbox
    (or trip the mail provider's rate limit).
    """
    if not issues:
        return False

    eligible = [issue for issue in issues if _should_notify_assignment(issue, new_assignee, actor, None)]
    if not eligible:
        return False

    threshold = getattr(settings, "NOTIFICATION_BULK_THRESHOLD", 10)
    actor_id = actor.pk if actor else None
    issue_ids = [issue.pk for issue in eligible]

    if len(eligible) > threshold:
        _dispatch(send_assignment_digest_email, issue_ids, new_assignee.pk, actor_id)
    else:
        for issue_id in issue_ids:
            _dispatch(send_assignment_email, issue_id, new_assignee.pk, actor_id)

    return True


def notify_member_added(workspace, *, member, actor=None) -> bool:
    """Queue a "you've been added to a workspace" email.

    Skips when somebody added themselves — the common case for the person who
    creates a workspace, who does not need telling.
    """
    if member is None:
        return False

    if actor is not None and actor.pk == member.pk:
        logger.debug("Membership notification skipped: self-add (user=%s)", member.pk)
        return False

    _dispatch(send_membership_email, workspace.pk, member.pk, actor.pk if actor else None)
    return True


def _should_notify_assignment(issue, new_assignee, actor, old_assignee) -> bool:
    """Shared eligibility rules for single and bulk assignment."""
    if new_assignee is None:
        # Unassignment is not an event worth emailing about.
        return False

    if old_assignee is not None and old_assignee.pk == new_assignee.pk:
        # A save that did not change the assignee (e.g. only the title was edited).
        return False

    if actor is not None and actor.pk == new_assignee.pk:
        # You already know you assigned it to yourself.
        logger.debug("Assignment notification skipped: self-assignment (user=%s)", new_assignee.pk)
        return False

    # A user removed from the workspace would only get a link they cannot open.
    workspace = issue.project.workspace
    if not is_member(new_assignee, workspace):
        logger.info(
            "Assignment notification skipped: user %s is not a member of workspace %s",
            new_assignee.pk,
            workspace.pk,
        )
        return False

    return True


def _dispatch(task, *args) -> None:
    """Queue ``task`` after the current transaction commits.

    on_commit is the important part: without it, a request that raises after the
    assignment would still have emailed the assignee about a change that was
    rolled back. Outside a transaction, Django runs the callback immediately.

    A broker that is unreachable must not break the request that triggered it, so
    the send falls back to running inline. Locally (no Redis) that is the normal
    path; in production it is the safety net for a Redis blip.
    """

    def _queue():
        try:
            task.delay(*args)
        except Exception:
            logger.warning("Could not queue %s; sending inline instead", task.name, exc_info=True)
            try:
                task(*args)
            except Exception:
                # Never let a notification failure surface as a request error.
                logger.exception("Inline fallback for %s failed", task.name)

    transaction.on_commit(_queue)
