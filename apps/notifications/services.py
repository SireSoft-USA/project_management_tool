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

from apps.issues.activity import parent_epic_of
from apps.issues.changes import EMPTY_DISPLAY, FIELD_LABELS
from apps.notifications.recipients import epic_activity_admin_copies, epic_activity_recipient
from apps.notifications.tasks import (
    send_assignment_digest_email,
    send_assignment_email,
    send_epic_activity_email,
    send_epic_inactivity_email,
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


def notify_epic_inactivity(epic, *, inactive_days: int) -> bool:
    """Queue a "this epic has gone quiet" email to its assignee.

    Returns True if an email was queued. Skips silently when:
      * the epic has no assignee — there is no natural recipient for "your epic
        went stale", so the alert is dropped rather than sent to someone arbitrary
      * the assignee is no longer a member of the epic's workspace

    Unlike the assignment notifications there is no actor and no self-action to
    guard against: the scheduler raises this, not a person.

    The caller must already have claimed the alert
    (Epic.objects.claim_inactivity_alert). This function decides whether the
    claimed alert is worth emailing, not whether it is safe to send twice.
    """
    if epic.assignee_id is None:
        logger.debug("Epic inactivity notification skipped: epic %s has no assignee", epic.pk)
        return False

    workspace = epic.project.workspace
    if not is_member(epic.assignee, workspace):
        logger.info(
            "Epic inactivity notification skipped: user %s is not a member of workspace %s",
            epic.assignee_id,
            workspace.pk,
        )
        return False

    _dispatch(send_epic_inactivity_email, epic.pk, epic.assignee_id, inactive_days)
    return True


def notify_epic_activity(issue, *, changes, actor=None, created=False) -> bool:
    """Queue an "activity on your epic" email for a work item under an Epic.

    Called by the views that create or edit a Story, Bug or Chore, after the
    change is saved but inside the surrounding transaction. The caller supplies
    the change list (from apps.issues.changes) because only the caller saw the
    values before they were overwritten.

    Returns True if an email was queued. Skips silently when:
      * nothing a person would notice changed — the single most important guard,
        since an ordinary save() with no edits must not generate mail
      * the item does not sit under an Epic (root-level, or under a Milestone)
      * the Epic has no assignee and no admin copy is configured, leaving nobody
        to tell
      * the only recipient would be the person who just made the change

    Args:
        issue: The Story, Bug or Chore that was created or edited.
        changes: Change dicts from ``changes.diff`` (edit) or ``changes.describe``
            (creation). Empty means "nothing worth reporting" and stops the send.
        actor: User who made the change; None for system-driven changes.
        created: True when the item was just created, which selects the "new item"
            wording instead of an old/new comparison.
    """
    if not changes:
        # An empty diff is the normal outcome of a save that changed nothing
        # notifiable. Returning here keeps that path free of further work.
        return False

    epic = parent_epic_of(issue)
    if epic is None:
        logger.debug("Epic activity notification skipped: %s has no parent epic", getattr(issue, "pk", None))
        return False

    recipient = epic_activity_recipient(epic, actor)
    if recipient is None and not epic_activity_admin_copies():
        return False

    _dispatch(
        send_epic_activity_email,
        issue.pk,
        epic.pk,
        changes,
        actor.pk if actor else None,
        created,
    )
    return True


def notify_epic_changed(epic, *, changes, actor=None) -> bool:
    """Queue an "your epic changed" email for an edit to the Epic itself.

    Separate from ``notify_epic_activity`` because that one answers "which Epic
    does this work item belong to?", and an Epic belongs to none: it is a root
    node, so the parent lookup returns None and the notification is dropped. An
    edit to an Epic's own title, status or due date would otherwise tell nobody,
    even though an epic's deadline is usually the most consequential one in the
    project.

    The epic stands as both the subject of the change and its own context, which
    lets this reuse the existing task, template, recipient rules and unsubscribe
    link rather than adding a parallel path.

    Returns True if an email was queued. Skips silently when:
      * nothing a person would notice changed
      * the epic is unassigned, or its assignee made the change themselves, or
        they have left the workspace — all decided by the same recipient rules
        the work-item path uses, so the two cannot drift apart

    Args:
        epic: The Epic that was edited.
        changes: Change dicts from ``changes.diff``. Empty stops the send.
        actor: User who made the change; None for system-driven changes.
    """
    if not changes:
        return False

    recipient = epic_activity_recipient(epic, actor)
    if recipient is None and not epic_activity_admin_copies():
        return False

    _dispatch(
        send_epic_activity_email,
        epic.pk,
        epic.pk,
        changes,
        actor.pk if actor else None,
        False,
    )
    return True


def notify_bulk_epic_activity(issues, *, field, old_values, new_display, actor=None) -> int:
    """Queue epic-activity email for issues changed together by one bulk action.

    The bulk views mutate with ``queryset.update()``, which fires no signals and
    leaves the in-memory objects holding their *previous* values. That is exactly
    what this needs: each object still knows what the field used to be, and the
    caller supplies the single new value that was applied to all of them.

    One email per affected issue rather than a digest, because each names a
    different work item and may reach a different epic assignee. Bulk edits are
    the noisiest path here, so callers should pass only the rows that actually
    changed.

    Args:
        issues: Already-loaded issues, snapshotted before the update.
        field: Name of the changed field, as used by apps.issues.changes.
        old_values: Mapping of pk to the previous display value.
        new_display: The new display value, shared by every issue.
        actor: User who made the change.

    Returns:
        How many notifications were queued.
    """
    if not issues:
        return 0

    label = str(FIELD_LABELS.get(field, field))
    empty = str(EMPTY_DISPLAY)
    new_text = str(new_display) if new_display not in (None, "") else empty

    queued = 0
    for issue in issues:
        old_value = (old_values or {}).get(issue.pk)
        old_text = str(old_value) if old_value not in (None, "") else empty
        if old_text == new_text:
            # Re-applying the value somebody already had is not a change.
            continue

        changes = [{"field": field, "label": label, "old_value": old_text, "new_value": new_text}]
        if notify_epic_activity(issue, changes=changes, actor=actor):
            queued += 1

    return queued


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
