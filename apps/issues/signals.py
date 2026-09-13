"""Signal handlers for Epic bookkeeping.

Two things are kept up to date here: an Epic's inactivity clock (from work-item
activity) and its assignment rows (mirroring the primary assignee).

Work items are created and edited from many views across the issues app
(project-scoped create, epic-scoped create, clone, the update form, and three
different inline-edit endpoints). Wiring each one individually would be fragile —
a new view would silently stop resetting the clock, and an Epic would be reported
inactive while work was actively happening on it. Creation and single-row edits
both go through Model.save(), so one post_save handler covers them all.

All three work-item types reset the clock, not Story alone. An Epic's children
are Stories, Bugs and Chores, and a team that spends a week fixing bugs under an
Epic has demonstrably not abandoned it. Registering only Story meant that work
was invisible to the clock and the Epic was reported inactive while people were
actively working on it. BaseIssue is polymorphic, so post_save fires for the
concrete subclass rather than the base — each type has to be registered
explicitly, which is why omitting two of them failed silently.

This mirrors the existing apps.notifications.signals module, which solved the
same "one event, many call sites" problem for assignment email, and which
already treats the same three types as work items.

Bulk actions deliberately do NOT rely on this handler: queryset.update() never
fires signals, so those views call IssueManager.record_story_activity*()
explicitly. See apps.issues.activity for what counts as activity in the first
place.
"""

from django.db.models.signals import post_save

from apps.issues.activity import story_parent_path
from apps.issues.models import Bug, Chore, Epic, EpicAssignment, Story

# Every issue type whose creation or edit counts as work on its parent Epic.
# Mirrors apps.notifications.signals.WORK_ITEM_MODELS: the two modules answer the
# same question ("what is work under an Epic?") and must not drift apart.
WORK_ITEM_MODELS = (Story, Bug, Chore)


def _record_epic_activity(instance, **kwargs):
    """Push the parent Epic's inactivity clock forward after a work item is saved.

    Fires for both creation and edits, and for all three work-item types: adding
    a Story, Bug or Chore to an Epic and doing work on an existing one are
    equally "activity" for inactivity purposes.

    Uses the derived parent path rather than get_parent() so this costs one
    UPDATE and no extra SELECT on the save path. A work item with no parent, or
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

    Work-item activity drives the inactivity clock. Every concrete work-item type
    is registered: BaseIssue is polymorphic, so post_save fires for the subclass
    (Bug, Chore) and never for the base, meaning an unregistered type resets
    nothing and fails silently. Epic saves keep the assignment rows in step with
    the primary assignee.
    """
    for model in WORK_ITEM_MODELS:
        post_save.connect(
            _record_epic_activity,
            sender=model,
            dispatch_uid=f"issues.epic_activity.{model._meta.model_name}",
        )
    post_save.connect(
        _mirror_primary_assignee,
        sender=Epic,
        dispatch_uid="issues.epic_assignment.mirror_primary",
    )


__all__ = ["WORK_ITEM_MODELS", "register"]
