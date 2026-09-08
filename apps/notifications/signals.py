"""Signal handler covering issue *creation*.

Issues are created from eight different views across three apps (project-scoped,
workspace-scoped, epic-scoped, milestone-scoped, subtask, clone…). Wiring each
one individually would be fragile — a ninth view would silently send nothing.
Creation always goes through Model.save(), so one post_save handler covers them
all, and cannot be forgotten by a new view.

Updates deliberately do NOT use a signal: the bulk path uses queryset.update(),
which never fires signals, and a signal cannot see the previous assignee without
an extra query per row. Those paths call notify_assignment() explicitly.
"""

from django.db.models.signals import post_save

from apps.issues.changes import describe
from apps.issues.models import Bug, Chore, Epic, Milestone, Story, Subtask
from apps.notifications.services import notify_assignment, notify_epic_activity

# Every concrete issue type. BaseIssue is polymorphic, so post_save fires for the
# concrete subclass, not the base — each has to be registered explicitly.
ISSUE_MODELS = (Story, Bug, Chore, Epic, Milestone, Subtask)

# Work item types whose creation counts as activity on their parent Epic. Epics
# and Milestones are containers rather than work, and a Subtask belongs to a work
# item rather than directly to an Epic, so neither raises an epic-activity event.
WORK_ITEM_MODELS = (Story, Bug, Chore)


def _notify_new_assignment(instance, created, **kwargs):
    """Email the assignee when an issue is created already assigned."""
    if not created or instance.assignee_id is None:
        return

    # The actor is resolved by the caller for updates; on creation the creator is
    # the best available actor, and it makes self-assignment detection work.
    actor = instance.created_by
    notify_assignment(instance, new_assignee=instance.assignee, actor=actor)


def _notify_epic_of_new_work_item(instance, created, **kwargs):
    """Tell an epic's assignee that a work item was added underneath it.

    Creation uses a signal where updates deliberately do not. The objections to
    signals are all about updates: they cannot see the previous value, and they
    never fire for queryset.update(). Neither applies here — a new item has no
    previous value to lose, and creation always goes through save(). What a
    signal does buy is coverage: work items are created from eight views across
    three apps, and wiring each one individually would mean a ninth view silently
    sending nothing.
    """
    if not created:
        return

    # created_by is the closest thing to an actor available here, and it is what
    # makes "do not email me about my own edit" work on the creation path.
    notify_epic_activity(
        instance,
        changes=describe(instance),
        actor=instance.created_by,
        created=True,
    )


def register():
    """Connect the handlers for every concrete issue model."""
    for model in ISSUE_MODELS:
        post_save.connect(
            _notify_new_assignment,
            sender=model,
            dispatch_uid=f"notifications.new_assignment.{model._meta.label_lower}",
        )

    for model in WORK_ITEM_MODELS:
        post_save.connect(
            _notify_epic_of_new_work_item,
            sender=model,
            dispatch_uid=f"notifications.epic_activity_created.{model._meta.label_lower}",
        )


__all__ = ["ISSUE_MODELS", "WORK_ITEM_MODELS", "register"]
