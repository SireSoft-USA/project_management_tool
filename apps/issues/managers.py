from typing import TYPE_CHECKING

from django.apps import apps
from django.db import models
from django.db.models import Case, F, Func, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.issues.activity import EPIC_INACTIVITY_ALERT_AFTER
from apps.issues.utils import get_work_item_ctype_ids

from polymorphic.managers import PolymorphicManager
from polymorphic.query import PolymorphicQuerySet
from treebeard.mp_tree import MP_NodeQuerySet

if TYPE_CHECKING:
    from apps.issues.models import BaseIssue, IssueStatus, Milestone
    from apps.projects.models import Project
    from apps.sprints.models import Sprint
    from apps.users.models import User
    from apps.workspaces.models import Workspace


class KeyNumber(Func):
    """
    Database function to extract the numeric suffix from an issue key.
    Keys have format '{PROJECT_KEY}-{NUMBER}' (e.g., 'SRM-123').
    Returns the numeric part as an integer for proper sorting.
    """

    function = "CAST"
    template = "CAST(SUBSTRING(%(expressions)s FROM '-([0-9]+)$') AS INTEGER)"
    output_field = IntegerField()


class IssueQuerySet(MP_NodeQuerySet, PolymorphicQuerySet):
    """Custom QuerySet for Issue models.

    Inherits from both MP_NodeQuerySet and PolymorphicQuerySet to ensure:
    - Tree-aware deletion (MP_NodeQuerySet.delete() cascades to descendants)
    - Polymorphic model support (returns correct subclass instances)
    """

    def for_project(self, project: Project) -> IssueQuerySet:
        """Filter issues by project."""
        return self.filter(project=project)

    def for_choices(self) -> IssueQuerySet:
        """
        Return only the fields needed for form choice fields.
        Use this when populating ModelChoiceField/ModelMultipleChoiceField querysets.
        Note: polymorphic_ctype_id is required for polymorphic model functionality.
        """
        return self.only("id", "key", "title", "polymorphic_ctype_id")

    def for_workspace(self, workspace: Workspace) -> IssueQuerySet:
        """Filter issues by workspace (through project)."""
        return self.filter(project__workspace=workspace)

    def for_sprint(self, sprint: Sprint) -> IssueQuerySet:
        """Filter to work items assigned to a sprint."""

        # Check if we're already querying a specific work item type
        # (not BaseIssue polymorphic queryset)
        Story = apps.get_model("issues", "Story")
        Bug = apps.get_model("issues", "Bug")
        Chore = apps.get_model("issues", "Chore")
        model = getattr(self, "model", None)
        if model in (Story, Bug, Chore):
            # Already a specific work item type, filter directly on sprint field
            return self.filter(sprint=sprint)

        return self.filter(Q(story__sprint=sprint) | Q(bug__sprint=sprint) | Q(chore__sprint=sprint))

    def work_items(self) -> IssueQuerySet:
        """Filter to work items only (Story, Bug, Chore)."""
        Story = apps.get_model("issues", "Story")
        Bug = apps.get_model("issues", "Bug")
        Chore = apps.get_model("issues", "Chore")
        return self.instance_of(Story, Bug, Chore)

    def backlog(self) -> IssueQuerySet:
        """Filter to work items NOT assigned to any sprint.

        Returns only work items (Story, Bug, Chore) that have no sprint.
        Epics don't have sprints and are excluded from this filter.
        """
        # Get work items and exclude those that have a sprint assigned.
        # Since sprint is on each subclass, we need to exclude based on each relation.

        return (
            self.work_items()
            .exclude(story__sprint__isnull=False)
            .exclude(bug__sprint__isnull=False)
            .exclude(chore__sprint__isnull=False)
        )

    def of_type(self, *types: type) -> IssueQuerySet:
        """Filter to specific issue types."""
        return self.instance_of(*types)

    def roots(self) -> IssueQuerySet:
        """Filter to root-level issues (no parent)."""
        return self.filter(depth=1)

    def with_status(self, status: IssueStatus) -> IssueQuerySet:
        """Filter by status."""
        return self.filter(status=status)

    def active(self) -> IssueQuerySet:
        """Filter to active issues (not archived, done, or won't do)."""
        return self.exclude(
            status__in=[self.model.status_model.ARCHIVED, self.model.status_model.DONE, self.model.status_model.WONT_DO]
        )

    def done(self) -> IssueQuerySet:
        """Filter to done issues (done, archived, or won't do)."""
        return self.filter(
            status__in=[self.model.status_model.DONE, self.model.status_model.ARCHIVED, self.model.status_model.WONT_DO]
        )

    def search(self, query: str) -> IssueQuerySet:
        """Search issues by title or key (case-insensitive)."""
        if not query:
            return self
        return self.filter(models.Q(title__icontains=query) | models.Q(key__icontains=query))

    def with_assignee(self, user: User) -> IssueQuerySet:
        """Filter to issues assigned to a user."""
        return self.filter(assignee=user)

    def unassigned(self) -> IssueQuerySet:
        """Filter to unassigned issues."""
        return self.filter(assignee__isnull=True)

    def overdue(self) -> IssueQuerySet:
        """Filter to overdue issues (due_date in the past and not done/archived/won't do)."""
        return self.filter(due_date__lt=timezone.now().date()).exclude(
            status__in=[self.model.status_model.DONE, self.model.status_model.ARCHIVED, self.model.status_model.WONT_DO]
        )

    def due_for_inactivity_alert(self, now=None) -> IssueQuerySet:
        """Epics that have gone long enough without Story activity to be alerted.

        The entire check is `inactivity_alert_due_at <= now` against an indexed,
        nullable column. Nothing here reads Stories: activity was recorded to that
        column as it happened (see apps.issues.activity), so the scheduler never
        aggregates or scans child rows no matter how large the tree grows.

        NULL means no alert is pending — either the Epic is finished, or one was
        already sent for the current period of silence — and NULL rows are
        excluded automatically by the comparison.

        Terminal Epics are filtered out as well. Step 7 already clears their due
        date on the way into a terminal status, so this is belt and braces: a row
        that somehow kept a stale date (an older migration, a direct SQL edit)
        must still never produce an alert about finished work.

        Must be called on Epic.objects: inactivity_alert_due_at is a column on the
        Epic table, not on BaseIssue. Calling it on the polymorphic base manager
        would otherwise fail with an opaque FieldError about an unknown keyword.

        Args:
            now: Override the comparison instant. Tests use it to travel in time
                without touching rows; production leaves it unset.
        """
        Epic = apps.get_model("issues", "Epic")
        if self.model is not Epic:
            raise TypeError(
                f"due_for_inactivity_alert() is an Epic query, but was called on "
                f"{self.model.__name__}.objects. Use Epic.objects.due_for_inactivity_alert()."
            )

        return self.filter(inactivity_alert_due_at__lte=now or timezone.now()).exclude(
            status__in=[self.model.status_model.DONE, self.model.status_model.ARCHIVED, self.model.status_model.WONT_DO]
        )

    def with_key(self, key: str, exclude: BaseIssue | None = None) -> IssueQuerySet:
        """Filter by key, optionally excluding a specific instance."""
        qs = self.filter(key=key)
        if exclude:
            qs = qs.exclude(pk=exclude.pk)
        return qs

    def milestones(self) -> IssueQuerySet:
        """Filter to Milestone issues only."""
        Milestone = apps.get_model("issues", "Milestone")
        return self.instance_of(Milestone)

    def for_milestone(self, milestone: Milestone) -> IssueQuerySet:
        """Filter to all descendant issues of a milestone (excludes the milestone itself)."""
        return self.filter(path__startswith=milestone.path).exclude(pk=milestone.pk)

    def ordered_by_key(self) -> IssueQuerySet:
        """
        Order issues by their key numerically.
        Keys have format '{PROJECT_KEY}-{NUMBER}', so this sorts by the numeric suffix.
        """
        return self.annotate(key_number=KeyNumber("key")).order_by("key_number")

    def for_user_dashboard(self, user: User, workspace: Workspace) -> IssueQuerySet:
        """Return work items assigned to user in active sprint (or workspace if no sprint)."""
        Sprint = apps.get_model("sprints", "Sprint")

        try:
            active_sprint = Sprint.objects.for_workspace(workspace).active().get()
            return self.for_sprint(active_sprint).work_items().with_assignee(user)
        except Sprint.DoesNotExist:
            return self.for_workspace(workspace).work_items().with_assignee(user).active()

    def set_status(self, status: IssueStatus) -> int:
        """Bulk update status for all issues in queryset. Returns count of updated rows."""
        return self.update(status=status)

    def ordered_by_priority(self) -> IssueQuerySet:
        """Order by priority: critical > high > medium > low, then by key.

        Priority is now on BaseIssue, so we can order directly.
        Uses descending order so higher priority (critical) appears first.
        """

        priority_order = Case(
            *[When(priority=choice[0], then=Value(i)) for i, choice in enumerate(self.model.get_priority_choices())],
            default=Value(-1),
            output_field=IntegerField(),
        )
        return self.annotate(
            priority_order=priority_order,
            key_number=KeyNumber("key"),
        ).order_by("-priority_order", "key_number")

    def with_progress(self):
        """Annotate issues with progress weights from descendant work items.

        Adds total_estimated_points, total_done_points, total_in_progress_points,
        and total_todo_points annotations by summing estimated_points across all
        descendants that have it set. Only Story, Bug, and Chore ever have
        estimated_points set — Epics, Milestones, and Subtasks always have it as
        None — so no further type filtering is needed.

        Works for both Epics (direct work item children) and Milestones (work items
        at any depth beneath them).
        """
        BaseIssue = apps.get_model("issues", "BaseIssue")
        status_categories = self.model.status_categories

        done_statuses = [s for s, cat in status_categories.items() if cat == "done"]
        in_progress_statuses = [s for s, cat in status_categories.items() if cat == "in_progress"]
        todo_statuses = [s for s, cat in status_categories.items() if cat == "todo"]

        def descendants_sum(statuses=None):
            """Sum estimated_points across all descendant work items (Story, Bug, Chore).

            Work items with null estimated_points count as 1 point.
            Filters by polymorphic content type to exclude Epics, Milestones, and Subtasks.
            """
            qs = BaseIssue.objects.non_polymorphic().filter(
                project=OuterRef("project"),
                path__startswith=OuterRef("path"),
                polymorphic_ctype_id__in=get_work_item_ctype_ids(),
            )
            if statuses:
                qs = qs.filter(status__in=statuses)

            return Coalesce(
                Subquery(
                    qs.values("project")
                    .annotate(total=Sum(Coalesce("estimated_points", Value(1))))
                    .values("total")[:1],
                    output_field=IntegerField(),
                ),
                Value(0),
            )

        qs = self.annotate(
            total_done_points=descendants_sum(done_statuses),
            total_in_progress_points=descendants_sum(in_progress_statuses),
            total_todo_points=descendants_sum(todo_statuses),
        )

        return qs.annotate(
            total_estimated_points=F("total_done_points") + F("total_in_progress_points") + F("total_todo_points")
        )


class IssueManager(PolymorphicManager):
    """Custom Manager for Issue models."""

    def get_queryset(self) -> IssueQuerySet:
        return IssueQuerySet(self.model, using=self._db)

    def for_project(self, project: Project) -> IssueQuerySet:
        return self.get_queryset().for_project(project)

    def for_workspace(self, workspace: Workspace) -> IssueQuerySet:
        return self.get_queryset().for_workspace(workspace)

    def for_sprint(self, sprint: Sprint) -> IssueQuerySet:
        return self.get_queryset().for_sprint(sprint)

    def for_milestone(self, milestone: Milestone) -> IssueQuerySet:
        return self.get_queryset().for_milestone(milestone)

    def milestones(self) -> IssueQuerySet:
        return self.get_queryset().milestones()

    def work_items(self) -> IssueQuerySet:
        return self.get_queryset().work_items()

    def for_user_dashboard(self, user: User, workspace: Workspace) -> IssueQuerySet:
        return self.get_queryset().for_user_dashboard(user, workspace)

    def with_progress(self) -> IssueQuerySet:
        return self.get_queryset().with_progress()

    def done(self) -> IssueQuerySet:
        return self.get_queryset().done()

    def due_for_inactivity_alert(self, now=None) -> IssueQuerySet:
        return self.get_queryset().due_for_inactivity_alert(now=now)

    def claim_inactivity_alert(self, epic_id, now=None) -> bool:
        """Take ownership of one Epic's pending inactivity alert.

        Returns True exactly once per period of silence. The caller may send the
        email only if it gets True.

        The claim is a single conditional UPDATE: clear inactivity_alert_due_at,
        but only for a row where it is still set and still in the past. The
        database applies that atomically, so if two workers (or an overlapping
        run of the same hourly task) reach the same Epic, only one UPDATE matches
        a row — the other sees zero rows affected and declines. No lock is taken
        and no row is read first, so there is nothing to dead-lock on.

        The same mechanism handles the race against real work: if someone adds a
        Story microseconds before the claim, the recording call has already
        pushed the due date into the future, so the `__lte=now` condition no
        longer matches and the alert is correctly abandoned rather than sent
        about an Epic that just became active.

        Args:
            epic_id: The Epic to claim.
            now: The instant the run is judged against; the scheduler passes one
                value for the whole batch so every Epic is treated consistently.

        Returns:
            True if this call won the claim and must send the alert.
        """
        Epic = apps.get_model("issues", "Epic")

        claimed = Epic.objects.filter(
            pk=epic_id,
            inactivity_alert_due_at__lte=now or timezone.now(),
        ).update(inactivity_alert_due_at=None)

        return claimed == 1

    def record_story_activity(self, epic_ids) -> int:
        """Mark meaningful Story work against one or more parent Epics.

        This is the single write point for Epic inactivity tracking. Every caller
        that changes a Story in a way that counts as work (apps.issues.activity
        defines exactly what does and does not count) funnels through here, so
        the rules live in one place instead of being re-derived at each call site.

        Deliberately a single UPDATE against the Epic table rather than
        load-modify-save: it costs one query regardless of how many Epics are
        passed, never loads an Epic into Python, and cannot fire Epic's own
        save()/signals (which would re-enter the notification and auditlog paths
        for what is internal bookkeeping, not a user edit).

        Terminal Epics are excluded rather than updated: work logged against an
        Epic that is already Done/Won't Do/Archived must not resurrect a pending
        alert for it.

        Args:
            epic_ids: A single Epic pk or an iterable of them. Empty input is a
                no-op, so bulk callers can pass a possibly-empty set unguarded.

        Returns:
            The number of Epic rows actually updated.
        """
        Epic = apps.get_model("issues", "Epic")
        BaseIssue = apps.get_model("issues", "BaseIssue")

        if isinstance(epic_ids, int):
            epic_ids = [epic_ids]
        epic_ids = [pk for pk in epic_ids if pk is not None]
        if not epic_ids:
            return 0

        now = timezone.now()
        terminal_statuses = [s for s, category in BaseIssue.status_categories.items() if category == "done"]

        return (
            Epic.objects.filter(pk__in=epic_ids)
            .exclude(status__in=terminal_statuses)
            .update(
                last_activity_at=now,
                inactivity_alert_due_at=now + EPIC_INACTIVITY_ALERT_AFTER,
            )
        )

    def sync_inactivity_clock_for_status(self, epic_ids) -> None:
        """Align Epics' inactivity clocks with their current status after a bulk
        status change.

        Epic.save() handles this for every path that saves a model instance, but
        the bulk status view and the cascade helper both use queryset.update(),
        which never calls save(). Those call this instead.

        Two statements rather than one because the two transitions are opposites:
        finishing an Epic clears its pending alert, reopening one starts a fresh
        grace period. Both are filtered so they only touch rows that actually need
        changing, keeping this cheap when a bulk action mixes Epics and work items.

        Args:
            epic_ids: An iterable of candidate Epic pks. Non-Epic pks are ignored
                because they simply do not match rows in the Epic table. Passing
                an empty iterable costs no query, so callers that usually select
                no Epics (most bulk actions) pay nothing.
        """
        Epic = apps.get_model("issues", "Epic")
        BaseIssue = apps.get_model("issues", "BaseIssue")

        epic_ids = [pk for pk in epic_ids if pk is not None]
        if not epic_ids:
            return

        terminal_statuses = [s for s, category in BaseIssue.status_categories.items() if category == "done"]

        # Finished: drop any pending alert.
        Epic.objects.filter(pk__in=epic_ids, status__in=terminal_statuses).exclude(inactivity_alert_due_at=None).update(
            inactivity_alert_due_at=None
        )

        # Reopened: restart the countdown from now rather than resuming a stale one.
        Epic.objects.filter(pk__in=epic_ids, inactivity_alert_due_at=None).exclude(status__in=terminal_statuses).update(
            inactivity_alert_due_at=timezone.now() + EPIC_INACTIVITY_ALERT_AFTER
        )

    def record_story_activity_by_parent_path(self, parent_paths) -> int:
        """Record Story activity against parent Epics identified by tree path.

        Treebeard's get_parent() costs a query per node, which would tax every
        Story save just to discover a pk. The parent's path is derivable from the
        child's in pure Python (see story_parent_path), so callers pass that
        instead and this resolves and updates in the same statement.

        Rows whose path belongs to a Milestone rather than an Epic simply do not
        match the Epic table, so no type check is needed — a Story parented
        directly to a Milestone correctly records nothing.

        Args:
            parent_paths: One path string or an iterable of them. Empty input is
                a no-op costing no query.

        Returns:
            The number of Epic rows actually updated.
        """
        Epic = apps.get_model("issues", "Epic")
        BaseIssue = apps.get_model("issues", "BaseIssue")

        if isinstance(parent_paths, str):
            parent_paths = [parent_paths]
        parent_paths = [path for path in parent_paths if path]
        if not parent_paths:
            return 0

        now = timezone.now()
        terminal_statuses = [s for s, category in BaseIssue.status_categories.items() if category == "done"]

        return (
            Epic.objects.filter(path__in=parent_paths)
            .exclude(status__in=terminal_statuses)
            .update(
                last_activity_at=now,
                inactivity_alert_due_at=now + EPIC_INACTIVITY_ALERT_AFTER,
            )
        )
