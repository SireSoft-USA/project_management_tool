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
