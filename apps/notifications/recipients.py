"""Who an epic-activity email should go to.

Shared by two callers that must agree, for different reasons:

* ``services`` asks at edit time, to decide whether queueing a task is worth it
  at all — an epic nobody can be told about should not reach the queue.
* ``tasks`` asks again at send time, because the epic may have been reassigned
  in between. Trusting the queue-time answer would email whoever *used* to own
  the epic and withhold it from whoever owns it now.

Keeping the rule in one module is what makes those two answers the same rule
rather than two implementations that drift apart.
"""

import logging

from django.conf import settings

from apps.workspaces.roles import is_member

logger = logging.getLogger(__name__)


def epic_activity_recipient(epic, actor):
    """Return the user to address an epic-activity email to, or None.

    The recipient is the Epic's assignee: the notification is about the Epic, not
    about whoever happens to own the work item that changed.

    Returns None — meaning "no direct recipient", though an audit copy may still
    apply — when:

      * the epic is unassigned, so there is no natural recipient
      * the assignee is the person who just made the change, who does not need
        telling what they did
      * the assignee has left the workspace, and would only get a link they can
        no longer open
    """
    assignee = getattr(epic, "assignee", None)
    if assignee is None:
        return None

    if actor is not None and actor.pk == assignee.pk:
        logger.debug("Epic activity notification: skipping self-action for user %s", assignee.pk)
        return None

    if not is_member(assignee, epic.project.workspace):
        logger.info(
            "Epic activity notification: user %s is no longer a member of workspace %s",
            assignee.pk,
            epic.project.workspace_id,
        )
        return None

    return assignee


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


__all__ = ["epic_activity_admin_copies", "epic_activity_recipient"]
