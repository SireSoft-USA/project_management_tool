"""Applying a change to an Epic's set of assignees.

Setting assignments is a read-modify-write: what to add and what to remove can
only be known by comparing the requested set against the current one. Two people
saving the same Epic at once would otherwise interleave those steps and leave the
rows reflecting neither request. So the whole comparison happens inside one
transaction, with the existing rows locked for its duration.

The diff is returned rather than discarded because the next step needs it: an
assignment change is reported in the notification email as "added X, removed Y",
and it can only be described by whoever saw both sides.
"""

from dataclasses import dataclass, field

from django.db import transaction

from apps.issues.models import EpicAssignment


@dataclass(frozen=True)
class AssignmentDiff:
    """What changed when an Epic's assignees were set.

    ``added`` and ``removed`` describe the edit; ``remaining`` is everyone who
    was assigned before and still is. The three are disjoint, and together cover
    everybody involved on either side of the change.
    """

    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    remaining: list = field(default_factory=list)

    def __bool__(self) -> bool:
        """True when anything actually changed.

        Callers use this to decide whether an edit is worth reporting, so a save
        that re-submits the same people generates no notification.
        """
        return bool(self.added or self.removed)


@transaction.atomic
def set_epic_assignees(epic, users, *, actor=None) -> AssignmentDiff:
    """Make ``users`` the complete set of assignees for ``epic``.

    Args:
        epic: The Epic whose assignments are being set.
        users: The users who should be assigned afterwards. Anyone currently
            assigned and absent from this is removed.
        actor: Who is making the change, recorded on the rows they add.

    Returns:
        An AssignmentDiff describing the change, for the caller to report.

    The primary ``epic.assignee`` is treated as always assigned: it is what the
    audit log, list grouping and inactivity alert read, so an assignment set that
    excluded it would disagree with the rest of the application. Passing a set
    without the primary owner therefore does not remove them - changing the
    primary owner is a separate edit to that field.

    select_for_update locks the existing rows for the transaction, so two
    concurrent saves are serialised rather than interleaved. Without it, both
    could read the same "before" state and each write a diff computed against
    it, losing one of the two edits.
    """
    requested_ids = {user.pk for user in users}
    if epic.assignee_id is not None:
        requested_ids.add(epic.assignee_id)

    existing = {
        assignment.user_id: assignment
        for assignment in EpicAssignment.objects.select_for_update().filter(epic=epic).select_related("user")
    }
    existing_ids = set(existing)

    added_ids = requested_ids - existing_ids
    removed_ids = existing_ids - requested_ids
    remaining_ids = existing_ids & requested_ids

    if removed_ids:
        EpicAssignment.objects.filter(epic=epic, user_id__in=removed_ids).delete()

    by_id = {user.pk: user for user in users}
    if added_ids:
        EpicAssignment.objects.bulk_create(
            [EpicAssignment(epic=epic, user_id=user_id, added_by=actor) for user_id in added_ids],
            # A row may already exist if another writer won the race between the
            # lock being released and this insert; the constraint makes that
            # harmless rather than an error.
            ignore_conflicts=True,
        )

    # Users are returned rather than ids so the caller can render names without
    # re-querying. Added users come from the caller's own objects; removed ones
    # from the rows that were just deleted, which were select_related above.
    return AssignmentDiff(
        added=[by_id[user_id] for user_id in added_ids if user_id in by_id],
        removed=[existing[user_id].user for user_id in removed_ids],
        remaining=[existing[user_id].user for user_id in remaining_ids],
    )


def apply_epic_assignees(form, epic, *, actor=None):
    """Write a form's submitted assignee set and notify the people affected.

    Epics can be created from four views and edited from two more, so the
    "set the rows, then tell the right people" pair lives here rather than being
    repeated at each call site - a new view that forgot the notification half
    would silently stop informing new assignees.

    Forms other than the Epic ones carry no ``assignees`` field, so this is a
    no-op for stories, bugs and chores. That is what lets the shared create views
    call it without branching on the issue type.

    Args:
        form: A validated form, which may or may not offer an assignees field.
        epic: The saved issue. Must already have a primary key.
        actor: Who is making the change.

    Returns:
        The AssignmentDiff, or None when the form carried no assignee set.
    """
    # Imported here rather than at module scope: apps.notifications.services
    # imports apps.issues.changes, and a top-level import would make the two
    # packages import each other at startup.
    from apps.notifications.services import notify_epic_assignment_change  # noqa: PLC0415

    if "assignees" not in form.fields:
        return None

    diff = set_epic_assignees(epic, form.cleaned_data.get("assignees") or [], actor=actor)
    notify_epic_assignment_change(epic, diff, actor=actor)
    return diff


__all__ = ["AssignmentDiff", "apply_epic_assignees", "set_epic_assignees"]
