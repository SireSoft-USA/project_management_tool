"""Message builders: turn domain objects into send_notification() keyword arguments.

Kept separate from tasks.py so subject wording and template context can be tested
directly, without Celery, and separate from emails.py so the delivery choke point
stays free of per-notification detail.
"""

from django.utils.translation import gettext as _

from apps.notifications.emails import build_issue_url
from apps.notifications.models import NotificationKind

from matorral.context_processors import get_root

TEMPLATE_ASSIGNMENT = "notifications/email/assignment"
TEMPLATE_ASSIGNMENT_DIGEST = "notifications/email/assignment_digest"
TEMPLATE_MEMBERSHIP = "notifications/email/membership"
TEMPLATE_EPIC_INACTIVITY = "notifications/email/epic_inactivity"
TEMPLATE_EPIC_ACTIVITY = "notifications/email/epic_activity"


def build_assignment_message(*, issue, recipient, actor=None) -> dict:
    """Build the payload for "an issue was assigned to you"."""
    return {
        "recipient": recipient,
        "kind": NotificationKind.ASSIGNMENT,
        # The key is in the subject so replies and inbox search stay useful.
        "subject": _("[%(key)s] %(title)s was assigned to you") % {"key": issue.key, "title": issue.title},
        "template": TEMPLATE_ASSIGNMENT,
        "context": {
            "issue": issue,
            "issue_url": build_issue_url(issue),
            "recipient": recipient,
            "recipient_name": recipient.get_display_name(),
            "actor": actor,
            "actor_name": actor.get_display_name() if actor else None,
            "project": issue.project,
            "issue_type": issue.get_issue_type_display(),
        },
    }


def build_assignment_digest_message(*, issues, recipient, actor=None) -> dict:
    """Build the payload for "N issues were assigned to you" (bulk actions)."""
    count = len(issues)
    workspace = issues[0].project.workspace
    return {
        "recipient": recipient,
        "kind": NotificationKind.ASSIGNMENT,
        "subject": _("%(count)d issues were assigned to you") % {"count": count},
        "template": TEMPLATE_ASSIGNMENT_DIGEST,
        "context": {
            "issues": [{"issue": issue, "url": build_issue_url(issue)} for issue in issues],
            "count": count,
            "recipient": recipient,
            "recipient_name": recipient.get_display_name(),
            "actor": actor,
            "actor_name": actor.get_display_name() if actor else None,
            "workspace": workspace,
            "workspace_url": f"{get_root()}{workspace.get_absolute_url()}",
        },
    }


def build_membership_message(*, workspace, recipient, actor=None) -> dict:
    """Build the payload for "you were added to a workspace"."""
    return {
        "recipient": recipient,
        "kind": NotificationKind.MEMBERSHIP,
        "subject": _("You've been added to %(workspace)s") % {"workspace": workspace.name},
        "template": TEMPLATE_MEMBERSHIP,
        "context": {
            "workspace": workspace,
            "workspace_url": f"{get_root()}{workspace.get_absolute_url()}",
            "recipient": recipient,
            "actor": actor,
            "actor_name": actor.get_display_name() if actor else None,
        },
    }


def build_epic_inactivity_message(*, epic, recipient, inactive_days: int) -> dict:
    """Build the payload for "this epic has had no story activity for a week".

    Unlike the assignment and membership messages there is no actor: nobody
    performed an action, the alert is raised by the scheduler noticing an absence
    of one. The email therefore says what has *not* happened and links straight to
    the epic so the recipient can act.
    """
    return {
        "recipient": recipient,
        "kind": NotificationKind.EPIC_INACTIVITY,
        # The key is in the subject so replies and inbox search stay useful, and
        # so repeated alerts about different epics do not look identical.
        "subject": _("[%(key)s] No activity on %(title)s for %(days)d days")
        % {"key": epic.key, "title": epic.title, "days": inactive_days},
        "template": TEMPLATE_EPIC_INACTIVITY,
        "context": {
            "epic": epic,
            "epic_url": build_issue_url(epic),
            "recipient": recipient,
            "recipient_name": recipient.get_display_name(),
            "project": epic.project,
            "inactive_days": inactive_days,
            "last_activity_at": epic.last_activity_at,
        },
    }


def build_epic_activity_message(*, issue, epic, changes, recipient, actor=None, created=False) -> dict:
    """Build the payload for "something happened on an epic you own".

    The change list arrives ready-rendered from apps.issues.changes: the caller
    resolved each value to display text while the objects were still in memory,
    before the row was overwritten. Nothing here re-reads the issue to describe
    what it used to be, because by now that information only exists in ``changes``.

    ``created`` selects between two readings of the same data. A new work item has
    no previous state, so its fields are listed as they stand; an edit shows each
    field as old versus new. One template renders both, keyed off this flag.
    """
    project = issue.project
    verb = _("created") if created else _("updated")

    # An edit to the Epic itself passes the same object as both subject and
    # context. Saying "a epic in Website Redesign" and printing the title twice
    # would read as a bug, so the template drops the redundant half instead.
    is_epic_itself = issue.pk == epic.pk

    if is_epic_itself:
        subject = _("[%(key)s] Epic %(title)s %(verb)s") % {"key": epic.key, "title": epic.title, "verb": verb}
    else:
        # Leads with the work item key, so inbox search and threading work the
        # same way as the assignment mails, and names the epic for context.
        subject = _("[%(key)s] %(title)s %(verb)s in %(epic)s") % {
            "key": issue.key,
            "title": issue.title,
            "verb": verb,
            "epic": epic.title,
        }

    return {
        "recipient": recipient,
        "kind": NotificationKind.EPIC_ACTIVITY,
        "subject": subject,
        "template": TEMPLATE_EPIC_ACTIVITY,
        "context": {
            "issue": issue,
            "issue_url": build_issue_url(issue),
            "issue_type": issue.get_issue_type_display(),
            "epic": epic,
            "epic_url": build_issue_url(epic),
            "is_epic_itself": is_epic_itself,
            "changes": changes,
            "created": created,
            "recipient": recipient,
            "recipient_name": recipient.get_display_name() if recipient else None,
            "actor": actor,
            "actor_name": actor.get_display_name() if actor else None,
            "project": project,
            "workspace": project.workspace,
        },
    }
