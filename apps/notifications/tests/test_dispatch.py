"""Tests for the message builders in dispatch.py (Phase 2).

Assignment emails are addressed to the assignee ("assigned to you"), which reads
fine to that person but leaves anyone else seeing the email — namely the
NOTIFICATION_COPY_TO monitor — with no way to tell who "you" is. These tests
cover the recipient_name field added to fix that: it must be present, correct,
safe to render, and reflect the assignee at the moment the event happened.
"""

from django.contrib.sites.models import Site
from django.core import mail
from django.test import TestCase

from apps.issues.factories import EpicFactory
from apps.notifications.dispatch import build_assignment_digest_message, build_assignment_message
from apps.notifications.emails import send_notification
from apps.users.factories import UserFactory


class BuildAssignmentMessageRecipientNameTests(TestCase):
    """recipient_name on the single-issue assignment message."""

    def test_recipient_name_is_the_assignee_display_name(self):
        assignee = UserFactory(first_name="Ada", last_name="Lovelace")
        epic = EpicFactory(assignee=assignee)

        message = build_assignment_message(issue=epic, recipient=assignee)

        self.assertEqual(message["context"]["recipient_name"], "Ada Lovelace")

    def test_falls_back_to_email_when_no_display_name(self):
        """get_display_name() already handles this; the builder must not bypass it."""
        assignee = UserFactory(first_name="", last_name="", email="noname@siresoft.com")
        epic = EpicFactory(assignee=assignee)

        message = build_assignment_message(issue=epic, recipient=assignee)

        self.assertEqual(message["context"]["recipient_name"], "noname@siresoft.com")

    def test_html_in_name_is_not_marked_safe(self):
        """Template auto-escaping is the real guard; this proves we didn't defeat it
        by pre-rendering or mark_safe-ing the value in the builder."""
        assignee = UserFactory(first_name="<script>alert(1)</script>", last_name="")
        epic = EpicFactory(assignee=assignee)

        message = build_assignment_message(issue=epic, recipient=assignee)

        # The raw value is allowed to contain the markup; only rendering must escape it.
        self.assertIn("<script>", message["context"]["recipient_name"])
        self.assertNotIsInstance(message["context"]["recipient_name"], type(None))

    def test_recipient_name_reflects_the_recipient_passed_in_not_the_issues_current_assignee(self):
        """Event semantics: the builder must describe the assignment event it was
        called for, even if the issue's live assignee has since moved on (e.g. a
        second reassignment queued its own task before this one ran)."""
        original_assignee = UserFactory(first_name="Grace", last_name="Hopper")
        epic = EpicFactory(assignee=original_assignee)

        # Simulate a second reassignment happening before this queued task executes.
        new_assignee = UserFactory(first_name="Katherine", last_name="Johnson")
        epic.assignee = new_assignee
        epic.save(update_fields=["assignee"])

        message = build_assignment_message(issue=epic, recipient=original_assignee)

        self.assertEqual(message["context"]["recipient_name"], "Grace Hopper")

    def test_does_not_add_extra_queries(self):
        """recipient is already a loaded User instance by the time the builder runs;
        get_display_name() only touches fields already on that row."""
        assignee = UserFactory(first_name="Ada", last_name="Lovelace")
        epic = EpicFactory(assignee=assignee)

        with self.assertNumQueries(0):
            build_assignment_message(issue=epic, recipient=assignee)


class BuildAssignmentDigestMessageRecipientNameTests(TestCase):
    """recipient_name on the bulk-assignment digest message."""

    def test_recipient_name_is_the_assignee_display_name(self):
        assignee = UserFactory(first_name="Ada", last_name="Lovelace")
        epics = [EpicFactory(assignee=assignee), EpicFactory(assignee=assignee)]

        message = build_assignment_digest_message(issues=epics, recipient=assignee)

        self.assertEqual(message["context"]["recipient_name"], "Ada Lovelace")

    def test_falls_back_to_email_when_no_display_name(self):
        assignee = UserFactory(first_name="", last_name="", email="noname@siresoft.com")
        epics = [EpicFactory(assignee=assignee)]

        message = build_assignment_digest_message(issues=epics, recipient=assignee)

        self.assertEqual(message["context"]["recipient_name"], "noname@siresoft.com")


class AssignmentEmailRendersRecipientNameTests(TestCase):
    """End-to-end: the rendered email (what a CC'd reader actually sees) must
    name the assignee, in both the HTML and plain-text parts."""

    def setUp(self):
        Site.objects.update_or_create(pk=1, defaults={"domain": "example.com", "name": "Matorral"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

    def test_single_assignment_email_shows_the_assignee_name(self):
        assignee = UserFactory(first_name="Ada", last_name="Lovelace")
        epic = EpicFactory(assignee=assignee)

        message = build_assignment_message(issue=epic, recipient=assignee)
        send_notification(**message)

        sent = mail.outbox[0]
        self.assertIn("Ada Lovelace", sent.body)
        html_body = sent.alternatives[0][0]
        self.assertIn("Ada Lovelace", html_body)

    def test_single_assignment_email_escapes_html_in_the_name(self):
        assignee = UserFactory(first_name="<b>Ada</b>", last_name="")
        epic = EpicFactory(assignee=assignee)

        message = build_assignment_message(issue=epic, recipient=assignee)
        send_notification(**message)

        html_body = mail.outbox[0].alternatives[0][0]
        self.assertNotIn("<b>Ada</b>", html_body)
        self.assertIn("&lt;b&gt;Ada&lt;/b&gt;", html_body)

    def test_digest_email_shows_the_assignee_name(self):
        assignee = UserFactory(first_name="Grace", last_name="Hopper")
        epics = [EpicFactory(assignee=assignee), EpicFactory(assignee=assignee)]

        message = build_assignment_digest_message(issues=epics, recipient=assignee)
        send_notification(**message)

        sent = mail.outbox[0]
        self.assertIn("Grace Hopper", sent.body)
        html_body = sent.alternatives[0][0]
        self.assertIn("Grace Hopper", html_body)
