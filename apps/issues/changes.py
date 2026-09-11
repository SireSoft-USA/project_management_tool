"""What changed on a work item, expressed as facts rather than prose.

The hard part of "tell the epic assignee what changed" is not the email — it is
knowing *exactly* what changed, and knowing it before the row is overwritten.
This module owns that, and nothing else: no email, no Celery, no HTML. Callers
capture a snapshot before saving, diff it after, and hand the result to
apps.notifications.services.

Why a snapshot and not a post_save signal
-----------------------------------------
After save() the database holds only the new value, so a signal cannot report
"In Progress -> Done" without re-reading the row it just overwrote. The bulk
views make that worse: they mutate with queryset.update(), which fires no
signals at all. Capturing before the write is the only approach that is correct
for both paths, so it is the only one offered here.

What counts as a reportable change
----------------------------------
Only fields a person edits and would recognise in an email (NOTIFIED_FIELDS).
Bookkeeping the user never typed — updated_at, treebeard's path/depth/numchild,
polymorphic_ctype — is deliberately excluded: a diff that says "numchild: 0 -> 1"
is noise, and updated_at changes on every single save, which would make the
"nothing meaningful changed" check below never fire.

Values are resolved to display strings at capture time, on objects the request
already has in memory. Storing a raw assignee_id would force the Celery worker to
re-query users to render "John Smith -> Sarah Khan", and by then the row may have
changed again — the email would describe a state that never existed.
"""

import datetime

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

# Editable fields worth telling somebody about, in the order they appear in the
# email. Anything absent here is invisible to notifications by construction,
# which is what keeps internal saves from generating mail.
NOTIFIED_FIELDS = (
    "title",
    "description",
    "status",
    "priority",
    "assignee",
    "due_date",
    "estimated_points",
    "severity",
    "sprint",
)

FIELD_LABELS = {
    "title": _("Title"),
    "description": _("Description"),
    "status": _("Status"),
    "priority": _("Priority"),
    "assignee": _("Assignee"),
    "due_date": _("Due date"),
    "estimated_points": _("Points"),
    "severity": _("Severity"),
    "sprint": _("Sprint"),
    # Not a model field, so it is absent from NOTIFIED_FIELDS and never produced
    # by diff(). An assignment change is derived by comparing two sets of rows
    # (see apps.issues.assignments), and describe_assignment_change() renders it
    # into this same shape so one email template handles both.
    "assignees": _("Assignees"),
}

# Shown wherever a field had (or now has) no value. "None" is a Python artefact;
# a reader should see "Not set", and "Not set -> 15 Sep 2026" reads correctly in
# a way that "None -> 2026-09-15" does not.
EMPTY_DISPLAY = _("Not set")

# Long text (a rewritten description) is truncated so one edit cannot turn into a
# multi-screen email. The full value is always one click away on the issue itself.
MAX_VALUE_LENGTH = 200


def _is_date_field(issue, field: str) -> bool:
    """True when ``field`` is a plain date column on ``issue``'s model.

    Consulted before any string is read as a date, so the coercion below applies
    only where a date is what the column actually holds. Without this check a
    story titled "2026-09-15" would be reformatted to "September 15, 2026" —
    silently rewriting a text field the user typed by hand.

    DateTimeField is excluded: it is a subclass of DateField, and a timestamp
    rendered as a bare day would drop the time.
    """
    try:
        model_field = issue._meta.get_field(field)
    except FieldDoesNotExist, AttributeError:
        return False
    return isinstance(model_field, models.DateField) and not isinstance(model_field, models.DateTimeField)


def _as_date(value):
    """Coerce an ISO date string to a ``date``, leaving anything else untouched.

    Assigning to a model field does not coerce: ``issue.due_date = "2026-09-15"``
    stores the *string*, since Django only converts during form validation. That
    matters because ``capture_initial`` builds its snapshot by assigning
    ``form.initial`` values onto a throwaway instance.

    Without this, a string on one side and a date on the other would render as
    "2026-09-15" and "September 15, 2026" — different text for the same day, so
    a save that changed nothing would report a due date change and email about
    it. Both sides of every comparison are normalised here so that cannot happen.

    ``form.initial`` currently holds real date objects, so this is a guard on an
    invariant nothing else enforces rather than a fix for a live bug: one custom
    form or serializer passing a string would otherwise reintroduce the spam
    silently.
    """
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            # Not an ISO date — a title, a description, a status label. Returned
            # unchanged so non-date fields are wholly unaffected by this path.
            return value
    return value


def _display(issue, field: str) -> str:
    """Render one field of ``issue`` as the string a person should read.

    Uses get_FOO_display() for choice fields so the email says "In Progress"
    rather than the stored "in_progress", and str() on related objects so an
    assignee shows as a name rather than a primary key. Both are read from the
    in-memory instance: for assignee and sprint the caller has already loaded
    the object, so this costs no query in the normal path.
    """
    if hasattr(issue, f"get_{field}_display"):
        value = getattr(issue, f"get_{field}_display")()
    else:
        value = getattr(issue, field, None)

    if value is None or value == "":
        return str(EMPTY_DISPLAY)

    if _is_date_field(issue, field):
        value = _as_date(value)

    # A DateField reaches str() as "2026-09-15". A due date is the one notified
    # value a person reads as prose rather than data, so it is written the way
    # they would write it. datetime is a subclass of date and is excluded above,
    # so a future DateTimeField cannot silently lose its time here.
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return date_format(value, "F j, Y")

    # User: "John Smith" reads better than the __str__ fallback (an email).
    text = value.get_display_name() if hasattr(value, "get_display_name") else str(value)

    return text.strip()


def _shorten(text: str) -> str:
    """Trim a value to email length.

    Applied when *rendering* a change, never when comparing one. Comparing
    truncated text would hide an edit made past the cut-off: two 5 KB
    descriptions sharing a first 200 characters are not the same description,
    but their trimmed forms are identical.
    """
    if len(text) > MAX_VALUE_LENGTH:
        # Trimmed rather than dropped: seeing the start of the new description is
        # useful, seeing all 4 KB of it in an inbox is not.
        return text[:MAX_VALUE_LENGTH].rstrip() + "…"
    return text


def capture(issue) -> dict:
    """Snapshot the notifiable fields of ``issue`` as display strings.

    Call this *before* mutating the instance. Fields the model does not define
    (severity exists on Bug but not Story) are skipped, so one snapshot function
    serves all three work item types.

    Values are stored at full length; trimming happens only when a change is
    rendered, so an edit made past the display cut-off is still detected.
    """
    if issue is None:
        return {}

    snapshot = {}
    for field in NOTIFIED_FIELDS:
        if hasattr(issue, field):
            snapshot[field] = _display(issue, field)
    return snapshot


def capture_initial(form) -> dict:
    """Snapshot a ModelForm's *previous* values, for use after ``is_valid()``.

    ``capture(instance)`` cannot be used inside ``form_valid``. Django's
    ModelForm writes cleaned data onto ``form.instance`` during ``is_valid()``,
    so by the time a view reaches ``form_valid`` the instance already holds the
    submitted values and diffing it against itself yields nothing.

    ``form.initial`` still holds what was loaded from the database, so the
    snapshot is rebuilt from there. Values there are raw — a status code rather
    than its label, a primary key rather than a user — so each is rendered
    through the same display logic as ``capture`` by temporarily applying it to a
    throwaway copy of the model. That keeps one definition of "how a value is
    shown" rather than a second, subtly different one for forms.

    Fields absent from the form are taken from the instance instead: they were
    not editable in this request, so their current value is also their previous
    one.
    """
    instance = getattr(form, "instance", None)
    if instance is None:
        return {}

    initial = getattr(form, "initial", None) or {}
    previous = instance.__class__()

    snapshot = {}
    for field in NOTIFIED_FIELDS:
        if not hasattr(instance, field):
            continue

        if field not in initial:
            # Not editable in this form, so unchanged by definition.
            snapshot[field] = _display(instance, field)
            continue

        value = initial[field]
        try:
            setattr(previous, field, value)
        except TypeError, ValueError:
            # A raw pk where the model wants an instance: assign through the
            # attname (assignee_id) so the FK resolves on read.
            attname = instance._meta.get_field(field).attname
            setattr(previous, attname, value)

        snapshot[field] = _display(previous, field)

    return snapshot


def diff(before: dict, issue) -> list[dict]:
    """Compare a snapshot against the issue's current state.

    Returns one entry per changed field, ordered as NOTIFIED_FIELDS, each with
    the label and both values already rendered for display:

        [{"field": "status", "label": "Status",
          "old_value": "In Progress", "new_value": "Done"}]

    An empty list means nothing a person would notice changed, which callers
    treat as "send no email". That check is the whole reason this returns a list
    rather than a formatted string.
    """
    if not before or issue is None:
        return []

    after = capture(issue)
    changes = []
    for field in NOTIFIED_FIELDS:
        if field not in before or field not in after:
            continue
        if before[field] == after[field]:
            continue
        changes.append(
            {
                "field": field,
                "label": str(FIELD_LABELS.get(field, field)),
                "old_value": _shorten(before[field]),
                "new_value": _shorten(after[field]),
            }
        )
    return changes


def describe(issue) -> list[dict]:
    """Render an issue's current state as labelled fields, for a creation email.

    Creation has no "before", and inventing one ("Not set -> Todo" for every
    field) would be both noisy and untrue. This returns the same shape as
    ``diff`` so one template renders both, but with new_value only and empty
    fields omitted — a new story rarely has a due date, and listing every blank
    field buries the ones that were actually filled in.
    """
    if issue is None:
        return []

    empty = str(EMPTY_DISPLAY)
    described = []
    for field in NOTIFIED_FIELDS:
        if not hasattr(issue, field):
            continue
        value = _display(issue, field)
        if value == empty:
            continue
        described.append(
            {
                "field": field,
                "label": str(FIELD_LABELS.get(field, field)),
                "old_value": None,
                "new_value": _shorten(value),
            }
        )
    return described


def describe_assignment_change(diff) -> list[dict]:
    """Render an AssignmentDiff as a change entry, or [] when nothing changed.

    Assignment is a set, not a value, so "old -> new" reads poorly: listing the
    whole membership twice makes the reader find the difference themselves. What
    changed is named directly instead - "Added Khadija; Removed Awais" - which is
    the one line a recipient actually needs.

    Returned in the same shape as ``diff()`` so the existing email template
    renders it with no special case. ``old_value`` is None, which the template
    already treats as "no previous value to show", as it does for a newly created
    work item.

    Args:
        diff: An apps.issues.assignments.AssignmentDiff.

    Returns:
        A single-entry list, or [] when the edit added and removed nobody - so a
        save that re-submits the same people generates no email.
    """
    if not diff:
        return []

    parts = []
    if diff.added:
        names = ", ".join(_display_name(user) for user in diff.added)
        parts.append(str(_("Added %(names)s") % {"names": names}))
    if diff.removed:
        names = ", ".join(_display_name(user) for user in diff.removed)
        parts.append(str(_("Removed %(names)s") % {"names": names}))

    return [
        {
            "field": "assignees",
            "label": str(FIELD_LABELS["assignees"]),
            "old_value": None,
            "new_value": _shorten("; ".join(parts)),
        }
    ]


def _display_name(user) -> str:
    """A person's name as an email should show it."""
    if hasattr(user, "get_display_name"):
        return user.get_display_name()
    return str(user)


__all__ = [
    "EMPTY_DISPLAY",
    "FIELD_LABELS",
    "MAX_VALUE_LENGTH",
    "NOTIFIED_FIELDS",
    "capture",
    "capture_initial",
    "describe",
    "describe_assignment_change",
    "diff",
]
