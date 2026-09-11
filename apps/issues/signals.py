"""Signal handlers for Epic bookkeeping.

Two things are kept up to date here: an Epic's inactivity clock (from Story
activity) and its assignment rows (mirroring the primary assignee).

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
from apps.issues.models import Epic, EpicAssignment, Story


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


def _mirror_primary_assignee(instance, **kwargs):
    """Keep an Epic's primary assignee present in its assignment rows.

    Epic.assignee remains the primary owner and is what the audit log, list
    grouping and the inactivity alert read. EpicAssignment is the full set of
    people to notify. The two must not disagree: an Epic whose owner is missing
    from its own assignment rows would either stop notifying them, or force every
    reader to remember to check both places.

    The backfill migration established this for existing Epics; this keeps it
    true for every Epic created or reassigned afterwards, whichever view or
    factory did it.

    Only ever adds. Removing the row for a previous assignee is deliberately not
    done here: somebody may have been added as an additional assignee on purpose,
    and losing that because the *primary* field changed would silently drop a
    recipient. Removal is an explicit action, handled by the assignment form.

    get_or_create rather than create: the row usually already exists (the form
    writes both), and the unique constraint would otherwise raise on a plain save
    of an unchanged Epic.
    """
    if instance.assignee_id is None:
        return

    EpicAssignment.objects.get_or_create(epic=instance, user_id=instance.assignee_id)


def register():
    """Connect the handlers.

    Story activity drives the inactivity clock - only Story counts, because the
    requirement is specifically about Stories being added to, or worked on
    within, an Epic. Epic saves keep the assignment rows in step with the primary
    assignee.
    """
    post_save.connect(
        _record_epic_activity,
        sender=Story,
        dispatch_uid="issues.epic_activity.story",
    )
    post_save.connect(
        _mirror_primary_assignee,
        sender=Epic,
        dispatch_uid="issues.epic_assignment.mirror_primary",
    )


__all__ = ["register"]
