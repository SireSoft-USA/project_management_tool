"""What counts as meaningful work on an Epic, and how long silence is tolerated.

An Epic is considered inactive when nobody has added a Story to it or done work
on one of its existing Stories for EPIC_INACTIVITY_ALERT_AFTER. Rather than
deriving that by scanning Stories on a schedule, callers record activity as it
happens (see IssueManager.record_story_activity) and the scheduler reads a single
indexed column.

That only works if "activity" means the same thing everywhere, so the definition
lives here rather than being re-decided at each call site.

Counts as activity (resets the 7-day clock):
  * A Story is created under the Epic.
  * A Story under the Epic is edited by a user — title, description, status,
    priority, points, assignee, or sprint — whether through a single-row edit
    view or a bulk action.

Does NOT count as activity:
  * Editing the Epic itself (renaming it, reassigning it, changing its status).
    The requirement is about work on the Epic's Stories, not bookkeeping on the
    Epic. An Epic nobody is progressing should still go stale even if its title
    keeps getting tweaked.
  * Deleting a Story, or moving one out to a different Epic. Removing work is not
    progress, and treating it as activity would let an Epic be kept "fresh"
    indefinitely without anything actually being delivered.
  * Anything happening under a different Epic, or under a Milestone directly.
  * System-driven saves that no user initiated (data migrations, backfills,
    maintenance commands). These do not route through the recording call.

Both apps.issues.models and apps.issues.managers import from this module, so it
must not import them back at module scope. The one function that needs models
imports them inside its body instead.
"""

from datetime import timedelta

# How long an Epic can go without Story activity before it becomes eligible for
# an inactivity alert. Named once because three places must agree on it: the
# default for Epic.inactivity_alert_due_at, the activity-recording update, and
# the scheduler that reads the resulting column.
EPIC_INACTIVITY_ALERT_AFTER = timedelta(days=7)


def story_parent_path(story) -> str | None:
    """Return the tree path of a Story's parent, without hitting the database.

    Treebeard encodes ancestry in the path string in fixed-width steps, so a
    node's parent path is just its own path with the last step removed. Deriving
    it here keeps the activity hooks off get_parent(), which costs a query per
    call and would tax every Story save.

    Returns None for a root-level Story (one with no parent at all), which
    records no activity because it belongs to no Epic.
    """
    path = getattr(story, "path", None)
    if not path:
        return None

    steplen = story.steplen
    if len(path) <= steplen:
        # Depth 1: the Story is itself a root node, so there is no parent.
        return None
    return path[:-steplen]


def record_activity_for_issues(issues) -> int:
    """Record Story activity for the parent Epics of an already-loaded issue list.

    The bulk views mutate issues with queryset.update(), which never fires
    post_save, so apps.issues.signals cannot see those edits. They call this
    instead. Each view already materialises the affected rows (for the audit log
    and success message), so the parent paths come from objects that are in
    memory anyway — no query is spent finding them.

    Non-Story issues in the list are ignored: bulk actions operate on a mixed
    selection, and only Story work counts toward an Epic's activity.

    Args:
        issues: An iterable of already-loaded BaseIssue instances.

    Returns:
        The number of Epic rows updated.
    """
    # Imported here rather than at module scope: models.py imports this module,
    # so a top-level import would be circular.
    from apps.issues.models import BaseIssue, Story  # noqa: PLC0415
    from apps.issues.utils import get_cached_content_type  # noqa: PLC0415

    # Compared by content type id rather than get_real_instance(), which costs a
    # query per object when the caller loaded rows non-polymorphically. The
    # content type is process-cached, so this stays free regardless of list size.
    story_ctype_id = get_cached_content_type(Story).id

    parent_paths = {story_parent_path(issue) for issue in issues if issue.polymorphic_ctype_id == story_ctype_id}
    parent_paths.discard(None)

    return BaseIssue.objects.record_story_activity_by_parent_path(parent_paths)


def epic_ids_in(issues) -> list:
    """Return the pks of the Epics in an already-loaded issue list.

    Bulk selections are usually all work items, so letting callers skip the
    clock-sync entirely when this comes back empty keeps the common case free of
    extra statements. Uses the cached content type rather than isinstance() or
    get_real_instance(), which would cost a query per row.
    """
    from apps.issues.models import Epic  # noqa: PLC0415
    from apps.issues.utils import get_cached_content_type  # noqa: PLC0415

    epic_ctype_id = get_cached_content_type(Epic).id
    return [issue.pk for issue in issues if issue.polymorphic_ctype_id == epic_ctype_id]


__all__ = [
    "EPIC_INACTIVITY_ALERT_AFTER",
    "epic_ids_in",
    "record_activity_for_issues",
    "story_parent_path",
]
