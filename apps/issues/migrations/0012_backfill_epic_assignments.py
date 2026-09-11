"""Seed EpicAssignment from the existing Epic.assignee column.

Multi-assignee support reads its recipients from EpicAssignment. Without this,
every Epic that already has an assignee would come out of the migration with an
empty assignment set, and the people currently receiving mail about those Epics
would silently stop - the worst kind of regression, because nothing errors and
nobody is told.

Deliberately does not import apps.issues.models. A data migration runs against
whatever the code looked like at this point in history, so it uses the frozen
model registry; importing live model code would make this migration change
meaning every time the model does.
"""

from django.db import migrations


def backfill_assignments(apps_registry, schema_editor):
    """Create one assignment per Epic that already has a primary assignee.

    added_by is left NULL: nobody performed this assignment, the migration
    inferred it. A NULL there reads correctly as "not recorded" rather than
    attributing the action to an arbitrary user.

    bulk_create with ignore_conflicts so a re-run cannot fail on the
    unique_epic_assignee constraint - the migration is then safe to apply to a
    database where some rows already exist (a partially applied deploy, or a
    restored snapshot).
    """
    Epic = apps_registry.get_model("issues", "Epic")
    EpicAssignment = apps_registry.get_model("issues", "EpicAssignment")

    assignments = [
        EpicAssignment(epic_id=epic_id, user_id=assignee_id, added_by=None)
        for epic_id, assignee_id in Epic.objects.exclude(assignee__isnull=True).values_list("pk", "assignee_id")
    ]

    if assignments:
        # batch_size keeps the statement bounded on a large installation rather
        # than building one enormous INSERT.
        EpicAssignment.objects.bulk_create(assignments, batch_size=500, ignore_conflicts=True)


def remove_backfilled_assignments(apps_registry, schema_editor):
    """Reverse: drop only the rows this migration could have created.

    Scoped to assignments that mirror the Epic's own primary assignee, so
    rolling back does not discard additional assignees added through the UI
    after the migration ran.
    """
    Epic = apps_registry.get_model("issues", "Epic")
    EpicAssignment = apps_registry.get_model("issues", "EpicAssignment")

    for epic_id, assignee_id in Epic.objects.exclude(assignee__isnull=True).values_list("pk", "assignee_id"):
        EpicAssignment.objects.filter(epic_id=epic_id, user_id=assignee_id, added_by__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("issues", "0011_epicassignment"),
    ]

    operations = [
        migrations.RunPython(backfill_assignments, remove_backfilled_assignments),
    ]
