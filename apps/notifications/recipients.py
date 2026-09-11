"""Who an epic-activity email should go to.

Shared by two callers that must agree, for different reasons:

* ``services`` asks at edit time, to decide whether queueing a task is worth it
  at all — an epic nobody can be told about should not reach the queue.
* ``tasks`` asks again at send time, because the epic may have been reassigned
  in between. Trusting the queue-time answer would email whoever *used* to own
  the epic and withhold it from whoever owns it now.

Keeping the rule in one module is what makes those two answers the same rule
rather than two implementations that drift apart.

An Epic can be owned by several people (see issues.EpicAssignment), so the rule
is expressed once, in plural form, by ``epic_activity_recipients``. The singular
``epic_activity_recipient`` is a thin wrapper over it, which is what stops the
one-recipient and many-recipient answers from drifting apart the way two
independent implementations would.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

User = get_user_model()


def epic_activity_recipients(epic, actor) -> list:
    """Return every user to address an epic-activity email to, in a stable order.

    An Epic's recipients are its assignment rows (issues.EpicAssignment) plus its
    primary ``assignee``. Both are consulted because the primary owner is the
    field the rest of the app still reads — the audit log, list grouping and the
    inactivity alert all use it — while the assignment rows carry everybody else.
    Taking the union means an Epic assigned the old way is still notified even if
    its backfilled row were ever missing.

    Excluded, each for a reason a recipient would recognise:

      * **the actor** — you do not need an email telling you what you just did;
      * **anyone who has left the workspace** — the email links to an epic they
        can no longer open;
      * **duplicates** — the same person reached through both the primary field
        and an assignment row is one recipient, not two emails.

    Deliberately does *not* filter on the user being active, having an address,
    or having opted out. Those live in ``emails._check_guards``, which every send
    passes through; duplicating them here would create a second place to keep in
    step, and a rule enforced twice is a rule that eventually disagrees with
    itself.

    Costs one query regardless of how many people are assigned, because
    workspace membership is applied as a JOIN rather than checked per user. The
    obvious implementation — calling ``roles.is_member`` for each candidate —
    would cost one query *per recipient*. A second query is spent only for an
    Epic whose primary owner has no assignment row, which the backfill migration
    and the assignment form both prevent.

    Returns:
        Users to email, ordered oldest assignment first with the primary owner
        leading. Order is stable so a rendered recipient list does not reshuffle
        between two emails about the same epic.
    """
    workspace_id = epic.project.workspace_id
    actor_id = getattr(actor, "pk", None)
    primary_id = epic.assignee_id

    # A single query for every assigned user who is still a member of the epic's
    # workspace. Membership is a JOIN rather than a per-user check, so the cost
    # does not grow with the number of assignees. The actor is excluded in SQL
    # because a self-action needs no email.
    #
    # This covers the primary owner too: the backfill migration gave every
    # already-assigned Epic a matching assignment row, and the form keeps the two
    # in step, so in the normal case the primary owner is returned here and the
    # fallback below costs nothing.
    assigned = list(
        User.objects.filter(
            epic_assignments__epic=epic,
            workspace_memberships__workspace_id=workspace_id,
        )
        .exclude(pk=actor_id)
        .order_by("epic_assignments__created_at", "pk")
        .distinct()
    )

    recipients = []
    seen = set()

    # The primary owner leads the list when present among the assigned users, so
    # ordering does not depend on when their assignment row happened to be
    # written.
    for user in assigned:
        if user.pk == primary_id:
            recipients.append(user)
            seen.add(user.pk)
            break

    for user in assigned:
        if user.pk not in seen:
            recipients.append(user)
            seen.add(user.pk)

    # Fallback for an Epic whose primary owner has no assignment row - possible
    # only if one was deleted directly, since the backfill and the form both
    # maintain it. Costs one extra query, and only in that case, rather than
    # letting the owner silently stop being notified.
    if primary_id is not None and primary_id not in seen and primary_id != actor_id:
        primary = User.objects.filter(
            pk=primary_id,
            workspace_memberships__workspace_id=workspace_id,
        ).first()
        if primary is not None:
            logger.info("Epic activity notification: epic %s has a primary assignee with no assignment row", epic.pk)
            recipients.insert(0, primary)
        else:
            logger.info(
                "Epic activity notification: user %s is no longer a member of workspace %s",
                primary_id,
                workspace_id,
            )

    return recipients


def epic_activity_recipient(epic, actor):
    """Return the first user to address an epic-activity email to, or None.

    Kept for callers that only need to answer "is there anybody to tell?" — the
    service layer asks exactly that before queueing a task. Delegates to
    ``epic_activity_recipients`` rather than repeating its rules, so the singular
    and plural answers cannot disagree.

    Returns None when the epic has no eligible recipient at all, which callers
    read as "no direct recipient" — an audit copy may still apply.
    """
    return next(iter(epic_activity_recipients(epic, actor)), None)


def epic_activity_admin_copies() -> list[str]:
    """Return the configured audit addresses for epic-activity mail.

    Read from a setting rather than hardcoded so staging and local environments
    do not mail the production admin every time somebody edits a story. Accepts a
    single address as well as a list, since an operator setting one address in
    the environment will reasonably write it as a bare string.
    """
    raw = getattr(settings, "EPIC_ACTIVITY_ADMIN_CC", []) or []
    if isinstance(raw, str):
        raw = [raw]
    return [address.strip() for address in raw if address and address.strip()]


__all__ = ["epic_activity_admin_copies", "epic_activity_recipient", "epic_activity_recipients"]
