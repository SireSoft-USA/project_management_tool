"""Tests for apps.issues.changes — the before/after diff used by notifications.

These cover the cases that make "report exactly what changed" hard in practice:
values that are absent, values that are zero, related objects that must render as
names, and saves where nothing a person cares about actually moved.
"""

import datetime

from django.test import TestCase, override_settings

from apps.issues.changes import (
    _is_date_field,
    EMPTY_DISPLAY,
    MAX_VALUE_LENGTH,
    NOTIFIED_FIELDS,
    capture,
    describe,
    diff,
)
from apps.issues.models import Bug, IssuePriority, IssueStatus, Story
from apps.users.factories import UserFactory


class CaptureTest(TestCase):
    """capture() turns a work item into readable before-values."""

    def test_choice_field_uses_human_label(self):
        story = Story(title="T", status=IssueStatus.IN_PROGRESS)
        self.assertEqual(capture(story)["status"], "In Progress")

    def test_absent_values_render_as_not_set(self):
        snapshot = capture(Story(title="T"))
        self.assertEqual(snapshot["assignee"], str(EMPTY_DISPLAY))
        self.assertEqual(snapshot["due_date"], str(EMPTY_DISPLAY))
        self.assertEqual(snapshot["estimated_points"], str(EMPTY_DISPLAY))

    def test_zero_points_is_a_value_not_an_absence(self):
        """0 is falsy in Python but is a real estimate; it must not read 'Not set'."""
        snapshot = capture(Story(title="T", estimated_points=0))
        self.assertEqual(snapshot["estimated_points"], "0")

    def test_assignee_renders_as_person_name(self):
        user = UserFactory(first_name="John", last_name="Smith")
        story = Story(title="T", assignee=user)
        self.assertEqual(capture(story)["assignee"], "John Smith")

    def test_missing_field_is_skipped_not_invented(self):
        """severity exists on Bug but not Story; one snapshot serves both."""
        self.assertNotIn("severity", capture(Story(title="T")))
        self.assertIn("severity", capture(Bug(title="B")))

    def test_long_text_is_stored_in_full(self):
        """Snapshots keep full text so comparison is exact; trimming is a render concern."""
        story = Story(title="T", description="x" * 5000)
        self.assertEqual(len(capture(story)["description"]), 5000)

    def test_none_issue_returns_empty_snapshot(self):
        self.assertEqual(capture(None), {})


class DiffTest(TestCase):
    """diff() reports only what actually moved."""

    def test_no_change_returns_empty(self):
        """The gate that stops a plain save() from emailing anybody."""
        story = Story(title="T", status=IssueStatus.DRAFT)
        self.assertEqual(diff(capture(story), story), [])

    def test_single_field_change(self):
        story = Story(title="T", status=IssueStatus.IN_PROGRESS)
        before = capture(story)
        story.status = IssueStatus.DONE

        changes = diff(before, story)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["field"], "status")
        self.assertEqual(changes[0]["label"], "Status")
        self.assertEqual(changes[0]["old_value"], "In Progress")
        self.assertEqual(changes[0]["new_value"], "Done")

    def test_multiple_changes_reported_together(self):
        """One edit of five fields must be one batch, not five events."""
        story = Story(title="Old", status=IssueStatus.DRAFT, priority=IssuePriority.MEDIUM)
        before = capture(story)
        story.title = "New"
        story.status = IssueStatus.DONE
        story.priority = IssuePriority.HIGH

        fields = [c["field"] for c in diff(before, story)]

        self.assertEqual(fields, ["title", "status", "priority"])

    def test_changes_follow_declared_field_order(self):
        """Order comes from NOTIFIED_FIELDS, not dict insertion, so emails read consistently."""
        story = Story(title="T")
        before = capture(story)
        story.estimated_points = 5
        story.title = "New"

        fields = [c["field"] for c in diff(before, story)]

        self.assertEqual(fields, sorted(fields, key=NOTIFIED_FIELDS.index))

    def test_setting_a_previously_absent_value(self):
        story = Story(title="T")
        before = capture(story)
        story.estimated_points = 8

        change = diff(before, story)[0]

        self.assertEqual(change["old_value"], str(EMPTY_DISPLAY))
        self.assertEqual(change["new_value"], "8")

    def test_clearing_a_value(self):
        user = UserFactory(first_name="John", last_name="Smith")
        story = Story(title="T", assignee=user)
        before = capture(story)
        story.assignee = None

        change = diff(before, story)[0]

        self.assertEqual(change["old_value"], "John Smith")
        self.assertEqual(change["new_value"], str(EMPTY_DISPLAY))

    def test_assignee_change_shows_names_not_ids(self):
        old_user = UserFactory(first_name="John", last_name="Smith")
        new_user = UserFactory(first_name="Sarah", last_name="Khan")
        story = Story(title="T", assignee=old_user)
        before = capture(story)
        story.assignee = new_user

        change = diff(before, story)[0]

        self.assertEqual(change["old_value"], "John Smith")
        self.assertEqual(change["new_value"], "Sarah Khan")

    def test_empty_snapshot_reports_nothing(self):
        """A caller that forgot to capture must not produce a bogus 'everything changed'."""
        self.assertEqual(diff({}, Story(title="T")), [])

    def test_none_issue_reports_nothing(self):
        self.assertEqual(diff({"title": "T"}, None), [])

    def test_edit_past_the_display_cutoff_is_still_detected(self):
        """Comparison uses full text; only the rendered value is trimmed.

        Two 5 KB descriptions sharing their first 200 characters are different
        descriptions. Comparing trimmed values would silently drop the edit.
        """
        story = Story(title="T", description="x" * 5000)
        before = capture(story)
        story.description = "x" * 5000 + " different tail"

        changes = diff(before, story)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["field"], "description")

    def test_rendered_change_values_are_trimmed_for_email(self):
        story = Story(title="T", description="a" * 5000)
        before = capture(story)
        story.description = "b" * 5000

        change = diff(before, story)[0]

        self.assertLessEqual(len(change["old_value"]), MAX_VALUE_LENGTH + 1)
        self.assertLessEqual(len(change["new_value"]), MAX_VALUE_LENGTH + 1)
        self.assertTrue(change["new_value"].endswith("…"))


class DescribeTest(TestCase):
    """describe() renders a newly created item, which has no 'before'."""

    def test_lists_populated_fields_only(self):
        story = Story(title="Implement auth", status=IssueStatus.DRAFT)

        described = describe(story)
        fields = [d["field"] for d in described]

        self.assertIn("title", fields)
        self.assertIn("status", fields)
        self.assertNotIn("due_date", fields)
        self.assertNotIn("assignee", fields)

    def test_creation_has_no_old_values(self):
        """A new story must not claim 'Not set -> Todo'; there was no previous state."""
        for entry in describe(Story(title="T")):
            self.assertIsNone(entry["old_value"])

    def test_none_issue_returns_empty(self):
        self.assertEqual(describe(None), [])


@override_settings(LANGUAGE_CODE="en-us", USE_L10N=False)
class DueDateDisplayTest(TestCase):
    """A due date reads as prose in email, and compares safely underneath.

    Two separate concerns share this class because they are the same line of
    code: how a date is *rendered*, and the guarantee that two spellings of the
    same day never look like a change. The second is the one that would spam
    people if it broke.
    """

    def test_due_date_renders_in_long_form(self):
        """An ISO date is data; "September 15, 2026" is what a person reads."""
        story = Story(title="T", due_date=datetime.date(2026, 9, 15))
        self.assertEqual(capture(story)["due_date"], "September 15, 2026")

    def test_single_digit_day_is_not_zero_padded(self):
        story = Story(title="T", due_date=datetime.date(2026, 9, 5))
        self.assertEqual(capture(story)["due_date"], "September 5, 2026")

    def test_change_reports_both_dates_in_long_form(self):
        story = Story(title="T", due_date=datetime.date(2026, 9, 15))
        before = capture(story)
        story.due_date = datetime.date(2026, 9, 20)

        (change,) = diff(before, story)
        self.assertEqual(change["field"], "due_date")
        self.assertEqual(change["old_value"], "September 15, 2026")
        self.assertEqual(change["new_value"], "September 20, 2026")

    def test_same_date_is_not_a_change(self):
        """The guard that keeps an ordinary save from emailing anybody."""
        story = Story(title="T", due_date=datetime.date(2026, 9, 15))
        before = capture(story)
        story.due_date = datetime.date(2026, 9, 15)

        self.assertEqual(diff(before, story), [])

    def test_clearing_a_due_date_is_a_change(self):
        """Removing a deadline is news, so null must not read as 'no change'."""
        story = Story(title="T", due_date=datetime.date(2026, 9, 15))
        before = capture(story)
        story.due_date = None

        (change,) = diff(before, story)
        self.assertEqual(change["old_value"], "September 15, 2026")
        self.assertEqual(change["new_value"], str(EMPTY_DISPLAY))

    def test_setting_a_first_due_date_is_a_change(self):
        story = Story(title="T")
        before = capture(story)
        story.due_date = datetime.date(2026, 9, 20)

        (change,) = diff(before, story)
        self.assertEqual(change["old_value"], str(EMPTY_DISPLAY))
        self.assertEqual(change["new_value"], "September 20, 2026")

    def test_no_due_date_either_side_is_not_a_change(self):
        story = Story(title="T")
        before = capture(story)

        self.assertEqual(diff(before, story), [])

    def test_editing_another_field_reports_no_due_date_change(self):
        """A title edit must not drag the untouched due date into the email."""
        story = Story(title="T", due_date=datetime.date(2026, 9, 15))
        before = capture(story)
        story.title = "Renamed"

        fields = [change["field"] for change in diff(before, story)]
        self.assertEqual(fields, ["title"])


@override_settings(LANGUAGE_CODE="en-us", USE_L10N=False)
class DueDateNormalisationTest(TestCase):
    """The same day written two ways must never look like an edit.

    Assigning to a model field does not coerce — ``story.due_date = "2026-09-15"``
    stores a str, because Django only converts during form validation. Since
    ``capture_initial`` snapshots by assigning ``form.initial`` values onto an
    instance, an unconverted string on one side would render as "2026-09-15"
    against "September 15, 2026" on the other: a due-date-changed email for a
    save that changed nothing.
    """

    def test_string_and_date_render_identically(self):
        as_date = Story(title="T", due_date=datetime.date(2026, 9, 15))
        as_string = Story(title="T")
        as_string.due_date = "2026-09-15"

        self.assertEqual(capture(as_string)["due_date"], capture(as_date)["due_date"])

    def test_string_before_against_date_after_is_not_a_change(self):
        """The false-positive this guard exists to prevent."""
        story = Story(title="T")
        story.due_date = "2026-09-15"
        before = capture(story)

        story.due_date = datetime.date(2026, 9, 15)
        self.assertEqual(diff(before, story), [])

    def test_a_real_change_is_still_detected_through_a_string(self):
        """Normalising must not flatten genuinely different days into equality."""
        story = Story(title="T")
        story.due_date = "2026-09-15"
        before = capture(story)

        story.due_date = datetime.date(2026, 9, 20)
        (change,) = diff(before, story)
        self.assertEqual(change["old_value"], "September 15, 2026")
        self.assertEqual(change["new_value"], "September 20, 2026")

    def test_title_that_looks_like_a_date_is_left_alone(self):
        """Coercion is scoped to date columns, so text is never rewritten."""
        story = Story(title="2026-09-15")
        self.assertEqual(capture(story)["title"], "2026-09-15")

    def test_title_that_looks_like_a_date_is_not_a_false_change(self):
        story = Story(title="2026-09-15", due_date=datetime.date(2026, 9, 15))
        before = capture(story)

        self.assertEqual(diff(before, story), [])

    def test_unparseable_date_string_is_left_as_written(self):
        """A malformed value is shown, not swallowed or crashed on."""
        story = Story(title="T")
        story.due_date = "not-a-date"

        self.assertEqual(capture(story)["due_date"], "not-a-date")

    def test_describe_renders_a_creation_due_date_in_long_form(self):
        """Creation email shares the display path, so it must format too."""
        story = Story(title="T", due_date=datetime.date(2026, 9, 20))
        described = {entry["field"]: entry["new_value"] for entry in describe(story)}

        self.assertEqual(described["due_date"], "September 20, 2026")


class DateFieldDetectionTest(TestCase):
    """_is_date_field decides whether a string may be read as a date.

    It answers from the model's own schema rather than by guessing at the value,
    which is what keeps text that happens to look like a date from being
    rewritten. The non-model cases are here because it must never raise: it runs
    inside display, and an exception there would break an unrelated email.
    """

    def test_identifies_a_date_column(self):
        self.assertIs(_is_date_field(Story(), "due_date"), True)

    def test_rejects_a_text_column(self):
        self.assertIs(_is_date_field(Story(), "title"), False)

    def test_rejects_a_datetime_column(self):
        """A timestamp must keep its time, so it is not treated as a date."""
        self.assertIs(_is_date_field(Story(), "created_at"), False)

    def test_unknown_field_is_not_a_date(self):
        self.assertIs(_is_date_field(Story(), "no_such_field"), False)

    def test_non_model_object_does_not_raise(self):
        self.assertIs(_is_date_field(object(), "due_date"), False)

    def test_none_does_not_raise(self):
        self.assertIs(_is_date_field(None, "due_date"), False)
