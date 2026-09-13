"""Tests for the epic-inactivity notification: rules, payload and rendering.

Covers the three layers the alert passes through — notify_epic_inactivity()
decides whether to send, build_epic_inactivity_message() decides what to say, and
send_notification() renders and delivers it through the shared choke point that
already applies opt-outs, CC and branding.
"""

from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.issues.factories import EpicFactory
from apps.issues.models import Epic, IssueStatus
from apps.notifications.dispatch import build_epic_inactivity_message
from apps.notifications.emails import send_notification
from apps.notifications.models import NotificationKind, NotificationPreference
from apps.notifications.services import notify_epic_inactivity
from apps.notifications.tasks import send_epic_inactivity_email
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces.factories import MembershipFactory, WorkspaceFactory


class NotifyEpicInactivityRulesTest(TestCase):
    """Who gets the alert, and who deliberately does not."""

    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.project = ProjectFactory(workspace=self.workspace)
        self.assignee = UserFactory(email="owner@siresoft.com")
        MembershipFactory(workspace=self.workspace, user=self.assignee)

    def _epic(self, **kwargs):
        return EpicFactory(project=self.project, **kwargs)

    @patch("apps.notifications.services._dispatch")
    def test_queues_an_alert_for_the_assignee(self, mock_dispatch):
        epic = self._epic(assignee=self.assignee)
        # Creating an assigned Epic also queues the assignment email, so assert on
        # the inactivity dispatch specifically rather than the total call count.
        mock_dispatch.reset_mock()

        self.assertTrue(notify_epic_inactivity(epic, inactive_days=7))
        mock_dispatch.assert_called_once()

    @patch("apps.notifications.services._dispatch")
    def test_passes_the_epic_assignee_and_day_count_to_the_task(self, mock_dispatch):
        epic = self._epic(assignee=self.assignee)
        mock_dispatch.reset_mock()

        notify_epic_inactivity(epic, inactive_days=9)

        _task, epic_id, recipient_id, days = mock_dispatch.call_args.args
        self.assertEqual(epic.pk, epic_id)
        self.assertEqual(self.assignee.pk, recipient_id)
        self.assertEqual(9, days)

    @patch("apps.notifications.services._dispatch")
    def test_skips_an_epic_with_no_assignee(self, mock_dispatch):
        """No natural recipient for "your epic went stale", so nothing is sent
        rather than mailing someone arbitrary."""
        epic = self._epic(assignee=None)

        self.assertFalse(notify_epic_inactivity(epic, inactive_days=7))
        mock_dispatch.assert_not_called()

    @patch("apps.notifications.services._dispatch")
    def test_skips_an_assignee_who_left_the_workspace(self, mock_dispatch):
        """They would only get a link they cannot open — same rule the assignment
        notification already applies."""
        outsider = UserFactory(email="gone@siresoft.com")
        epic = self._epic(assignee=outsider)

        self.assertFalse(notify_epic_inactivity(epic, inactive_days=7))
        mock_dispatch.assert_not_called()

    @patch("apps.notifications.services._dispatch")
    def test_dispatch_goes_through_on_commit(self, mock_dispatch):
        """_dispatch is the shared wrapper that defers to transaction.on_commit
        and falls back to an inline send when the broker is down."""
        epic = self._epic(assignee=self.assignee)

        notify_epic_inactivity(epic, inactive_days=7)

        self.assertTrue(mock_dispatch.called)


class BuildEpicInactivityMessageTest(TestCase):
    """What the alert says."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()
        cls.recipient = UserFactory(first_name="Ada", last_name="Lovelace")

    def _message(self, **kwargs):
        epic = EpicFactory(project=self.project, title="Payments rework", **kwargs)
        return epic, build_epic_inactivity_message(epic=epic, recipient=self.recipient, inactive_days=7)

    def test_uses_the_epic_inactivity_kind(self):
        """Selects the right opt-out flag; an unmapped kind would raise."""
        _epic, message = self._message()

        self.assertEqual(NotificationKind.EPIC_INACTIVITY, message["kind"])

    def test_subject_names_the_epic_and_the_day_count(self):
        epic, message = self._message()

        self.assertIn(epic.key, message["subject"])
        self.assertIn("Payments rework", message["subject"])
        self.assertIn("7", message["subject"])

    def test_context_carries_the_recipient_display_name(self):
        _epic, message = self._message()

        self.assertEqual("Ada Lovelace", message["context"]["recipient_name"])

    def test_context_carries_an_absolute_epic_url(self):
        _epic, message = self._message()

        self.assertTrue(message["context"]["epic_url"].startswith("http"))

    def test_context_carries_last_activity_at(self):
        """The recipient needs to know when it actually went quiet."""
        epic, message = self._message()

        self.assertEqual(epic.last_activity_at, message["context"]["last_activity_at"])


@override_settings(NOTIFICATION_COPY_TO=["monitor@siresoft.com"], NOTIFICATION_COPY_MODE="cc")
class EpicInactivityEmailRenderingTest(TestCase):
    """What actually lands in the inbox."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "10.0.2.11:8000", "name": "Siresoft"})
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

        self.project = ProjectFactory()
        self.recipient = UserFactory(email="owner@siresoft.com", first_name="Ada", last_name="Lovelace")
        self.epic = EpicFactory(project=self.project, title="Payments rework")
        Epic.objects.filter(pk=self.epic.pk).update(last_activity_at=timezone.now() - timedelta(days=7))
        self.epic.refresh_from_db()

        send_notification(**build_epic_inactivity_message(epic=self.epic, recipient=self.recipient, inactive_days=7))
        self.message = mail.outbox[0]

    def _html(self):
        for content, mimetype in self.message.alternatives:
            if mimetype == "text/html":
                return content
        self.fail("no text/html alternative")

    def test_is_sent_to_the_assignee(self):
        self.assertEqual(["owner@siresoft.com"], self.message.to)

    def test_is_copied_to_the_monitoring_address(self):
        """Phase 1's CC applies here too — no extra wiring, it rides the choke point."""
        self.assertEqual(["monitor@siresoft.com"], self.message.cc)

    def test_both_parts_name_the_epic(self):
        self.assertIn("Payments rework", self.message.body)
        self.assertIn("Payments rework", self._html())

    def test_both_parts_state_the_inactive_period(self):
        self.assertIn("7 days", self.message.body)
        self.assertIn("7 days", self._html())

    def test_both_parts_link_to_the_epic(self):
        self.assertIn(self.epic.key, self.message.body)
        self.assertIn("10.0.2.11:8000", self.message.body)
        self.assertIn("10.0.2.11:8000", self._html())

    def test_html_uses_the_shared_branded_base(self):
        """Phase 3/4 branding — blue palette and the logo — comes for free."""
        html = self._html()
        self.assertIn("#04306d", html)
        self.assertIn("cid:siresoft-logo", html)

    def test_includes_an_unsubscribe_link(self):
        preference = NotificationPreference.objects.for_user(self.recipient)
        self.assertIn(str(preference.unsubscribe_token), self.message.body)

    def test_html_does_not_leak_template_comment_text(self):
        """Same regression guard as the other templates: a multi-line {# #}
        comment renders as visible text."""
        html = self._html()
        for leaked in ("wordmark", "masthead", "preheader for email"):
            self.assertNotIn(leaked, html.lower())


class EpicInactivityOptOutTest(TestCase):
    """The new kind honours its own preference flag."""

    def setUp(self):
        self.project = ProjectFactory()
        self.recipient = UserFactory(email="owner@siresoft.com")
        self.epic = EpicFactory(project=self.project)

    def _send(self):
        return send_notification(
            **build_epic_inactivity_message(epic=self.epic, recipient=self.recipient, inactive_days=7)
        )

    def test_sends_when_opted_in(self):
        self.assertTrue(self._send())

    def test_skips_when_opted_out_of_epic_inactivity(self):
        preference = NotificationPreference.objects.for_user(self.recipient)
        preference.notify_on_epic_inactivity = False
        preference.save()

        self.assertFalse(self._send())
        self.assertEqual(0, len(mail.outbox))

    def test_opting_out_of_assignments_does_not_silence_inactivity_alerts(self):
        """Each kind maps to its own flag; they must not bleed into each other."""
        preference = NotificationPreference.objects.for_user(self.recipient)
        preference.notify_on_assignment = False
        preference.save()

        self.assertTrue(self._send())

    def test_unsubscribe_all_covers_the_new_kind(self):
        """disable_all() backs the one-click unsubscribe link; a kind missing from
        it would keep emailing someone who asked for silence."""
        preference = NotificationPreference.objects.for_user(self.recipient)
        preference.disable_all()

        preference.refresh_from_db()
        self.assertFalse(preference.notify_on_epic_inactivity)
        self.assertFalse(self._send())


class EpicFinishedBeforeDeliveryTest(TestCase):
    """The epic is completed between the sweep queueing this task and the worker running it.

    The sweep declines a finished epic, but it decides that when it *queues* the
    task. In production the worker picks the task up some time later, and a
    transport retry may run minutes after that. An epic completed anywhere in
    that window used to be mailed about regardless - the alert arrived saying
    work the user had just closed looked abandoned, which is the exact complaint
    this whole feature area exists to avoid.

    The task re-reads the epic anyway in order to render the message, so the
    status check that closes this window is free.
    """

    def setUp(self):
        self.workspace = WorkspaceFactory()
        self.project = ProjectFactory(workspace=self.workspace)
        self.assignee = UserFactory(email="owner@siresoft.com")
        MembershipFactory(workspace=self.workspace, user=self.assignee)

    def _epic(self, status):
        return EpicFactory(project=self.project, assignee=self.assignee, status=status)

    def test_no_email_when_the_epic_finished_before_the_task_ran(self):
        """The regression. Queued while open, executed after completion."""
        epic = self._epic(IssueStatus.IN_PROGRESS)
        # Completed after the sweep queued the task. queryset.update() so the
        # write does not clear the due date and mask what is being tested.
        Epic.objects.filter(pk=epic.pk).update(status=IssueStatus.DONE)

        sent = send_epic_inactivity_email(epic.pk, self.assignee.pk, 8)

        self.assertFalse(sent)
        self.assertEqual([], mail.outbox)

    def test_every_terminal_status_stops_delivery(self):
        """Done is not the only way work ends; Won't Do and Archived are equally
        finished, and a guard that knew only DONE would still mail about an
        abandoned epic."""
        for status in (IssueStatus.DONE, IssueStatus.WONT_DO, IssueStatus.ARCHIVED):
            with self.subTest(status=status):
                mail.outbox = []
                epic = self._epic(IssueStatus.IN_PROGRESS)
                Epic.objects.filter(pk=epic.pk).update(status=status)

                sent = send_epic_inactivity_email(epic.pk, self.assignee.pk, 8)

                self.assertFalse(sent)
                self.assertEqual([], mail.outbox)

    def test_an_open_epic_is_still_delivered(self):
        """The guard must not over-fire. Without this, suppressing every alert
        would pass the rest of this class."""
        epic = self._epic(IssueStatus.IN_PROGRESS)

        sent = send_epic_inactivity_email(epic.pk, self.assignee.pk, 8)

        self.assertTrue(sent)
        self.assertEqual(1, len(mail.outbox))
        self.assertIn(self.assignee.email, mail.outbox[0].to)

    def test_a_blocked_epic_is_still_delivered(self):
        """Blocked is not finished - it is the state that most needs chasing, so
        a guard keyed on "not in progress" rather than "finished" would wrongly
        silence exactly the epics worth alerting on."""
        epic = self._epic(IssueStatus.BLOCKED)

        sent = send_epic_inactivity_email(epic.pk, self.assignee.pk, 8)

        self.assertTrue(sent)
        self.assertEqual(1, len(mail.outbox))
