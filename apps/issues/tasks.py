"""Scheduled work for the issues app."""

import logging

from django.utils import timezone

from apps.issues.activity import EPIC_INACTIVITY_ALERT_AFTER
from apps.issues.models import Epic
from apps.notifications.services import notify_epic_inactivity

from celery import shared_task

logger = logging.getLogger(__name__)

# Rows fetched per database round trip. Matches create_next_sprints, and keeps
# memory flat if a large number of epics fall due at once (e.g. the first run
# after this feature ships, or after a long outage).
BATCH_SIZE = 100


@shared_task
def send_epic_inactivity_alerts() -> int:
    """Alert the assignee of every epic that has gone quiet for too long.

    Runs hourly via Celery beat. The sweep itself is one indexed query against
    Epic.inactivity_alert_due_at — no Story rows are read, so the cost depends on
    how many epics are actually due, not on how large the project is.

    Each candidate is claimed before anything is sent. The claim is a conditional
    UPDATE that succeeds exactly once, which is what makes this task safe to run
    concurrently with itself: an overlapping run, a retry, or a second worker
    finds the alert already taken and skips it. It also settles the race against
    real work — a Story saved a moment earlier has already pushed the due date
    into the future, so the claim fails and no stale alert goes out.

    Returns:
        The number of alerts queued, for logging and monitoring.
    """
    # One instant for the whole run, so every epic is judged against the same
    # clock rather than drifting as the batch is processed.
    now = timezone.now()
    queued = 0
    skipped = 0

    candidates = Epic.objects.due_for_inactivity_alert(now=now).select_related(
        "assignee", "project", "project__workspace"
    )

    for epic in candidates.iterator(chunk_size=BATCH_SIZE):
        if not Epic.objects.claim_inactivity_alert(epic.pk, now=now):
            # Someone else took it, or activity landed in the meantime.
            skipped += 1
            continue

        inactive_days = _inactive_days(epic, now)
        if notify_epic_inactivity(epic, inactive_days=inactive_days):
            queued += 1
        else:
            # Claimed but not sent: no assignee, or they left the workspace. The
            # claim still stands, so this epic stays quiet until new activity
            # starts a fresh cycle rather than being retried every hour.
            skipped += 1

    if queued or skipped:
        logger.info("Epic inactivity sweep: %d alert(s) queued, %d skipped", queued, skipped)

    return queued


def _inactive_days(epic, now) -> int:
    """How long this epic has actually been quiet, for the alert wording.

    Derived from last_activity_at rather than assuming the grace period: if the
    worker is backed up, or the epic sat unclaimed through an outage, the email
    should report the real figure instead of always saying seven days.
    """
    if epic.last_activity_at is None:
        return EPIC_INACTIVITY_ALERT_AFTER.days
    return max((now - epic.last_activity_at).days, 0)
