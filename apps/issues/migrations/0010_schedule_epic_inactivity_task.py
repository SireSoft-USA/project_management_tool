from django.db import migrations

TASK_NAME = "send-epic-inactivity-alerts"


def create_periodic_task(apps, schema_editor):
    """Register the hourly epic-inactivity sweep with Celery beat.

    Mirrors SCHEDULED_TASKS["send-epic-inactivity-alerts"] in settings. The
    settings entry documents the intent; this row is what the database-backed
    beat scheduler actually reads.
    """
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    interval, _ = IntervalSchedule.objects.get_or_create(every=60, period="minutes")
    PeriodicTask.objects.update_or_create(
        name=TASK_NAME,
        defaults={
            "task": "apps.issues.tasks.send_epic_inactivity_alerts",
            "interval": interval,
            # Expire before the next run: a task still queued an hour later is
            # stale, and the following sweep re-selects whatever is still due.
            "expire_seconds": 60 * 60,
            "enabled": True,
        },
    )


def delete_periodic_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("issues", "0009_add_epic_inactivity_tracking"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_periodic_task, delete_periodic_task),
    ]
