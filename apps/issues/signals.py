"""Signal handler that keeps each Epic's inactivity clock up to date.

Stories are created and edited from many views across the issues app (project-
scoped create, epic-scoped create, clone, the update form, and three different
inline-edit endpoints). Wiring each one individually would be fragile — a new
view would silently stop resetting the clock, and an Epic would be reported
inactive while work was actively happening on it. Creation and single-row edits
both go through Model.save(), so one post_save handler covers them all.

This mirrors the existing apps.notifications.signals module, which solved the
same "one event, many call sites" problem for assignment email.

Bulk actions deliberately do NOT rely on this handler: queryset.update() never
fires signals, so those views call IssueManager.record_story_activity*()
explicitly. See apps.issues.activity for what counts as activity in the first
place.
"""

from django.db.models.signals import post_save

from apps.issues.activity import story_parent_path
from apps.issues.models import Story


def _record_epic_activity(instance, **kwargs):
    """Push the parent Epic's inactivity clock forward after a Story is saved.

    Fires for both creation and edits: adding a Story to an Epic and doing work
    on an existing one are equally "activity" for inactivity purposes.

    Uses the derived parent path rather than get_parent() so this costs one
    UPDATE and no extra SELECT on the Story save path. A Story with no parent, or
    one parented directly to a Milestone, matches no Epic row and records
    nothing — which is the intended behaviour, not an error.
    """
    parent_path = story_parent_path(instance)
    if parent_path is None:
        return

    Story.objects.record_story_activity_by_parent_path(parent_path)


def register():
    """Connect the handler. Only Story counts: the requirement is specifically
    about Stories being added to, or worked on within, an Epic."""
    post_save.connect(
        _record_epic_activity,
        sender=Story,
        dispatch_uid="issues.epic_activity.story",
    )


__all__ = ["register"]
