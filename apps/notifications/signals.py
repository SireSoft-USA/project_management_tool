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

from apps.issues.models import Bug, Chore, Epic, Milestone, Story, Subtask
from apps.notifications.services import notify_assignment

# Every concrete issue type. BaseIssue is polymorphic, so post_save fires for the
# concrete subclass, not the base — each has to be registered explicitly.
ISSUE_MODELS = (Story, Bug, Chore, Epic, Milestone, Subtask)


def _notify_new_assignment(instance, created, **kwargs):
    """Email the assignee when an issue is created already assigned."""
    if not created or instance.assignee_id is None:
        return

    # The actor is resolved by the caller for updates; on creation the creator is
    # the best available actor, and it makes self-assignment detection work.
    actor = instance.created_by
    notify_assignment(instance, new_assignee=instance.assignee, actor=actor)


def register():
    """Connect the handler for every concrete issue model."""
    for model in ISSUE_MODELS:
        post_save.connect(
            _notify_new_assignment,
            sender=model,
            dispatch_uid=f"notifications.new_assignment.{model._meta.label_lower}",
        )


__all__ = ["ISSUE_MODELS", "register"]
