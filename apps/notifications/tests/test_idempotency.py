"""Tests for notification delivery idempotency.

Celery retries the whole task on a transport failure. That is correct - a mail
server that was briefly down should not cost somebody their notification - but it
means a task that failed *partway* through a fan-out runs again from the top, and
the recipients who already received the email get it twice.

NotificationDelivery claims each (event, recipient) pair before the message is
built. These tests cover the two halves of that: the claim mechanism itself, and
the behaviour of a retry that follows a real failure.
"""

from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings

from apps.issues.factories import EpicAssignmentFactory, EpicFactory, StoryFactory
from apps.notifications.models import (
    NotificationDelivery,
    NotificationKind,
    NotificationPreference,
    build_idempotency_key,
)
from apps.notifications.tasks import send_epic_activity_email
from apps.projects.factories import ProjectFactory
from apps.users.factories import UserFactory
from apps.workspaces import roles
from apps.workspaces.factories import WorkspaceFactory
from apps.workspaces.models import Membership

CHANGES = [{"field": "status", "label": "Status", "old_value": "Draft", "new_value": "Done"}]
OTHER_CHANGES = [{"field": "status", "label": "Status", "old_value": "Draft", "new_value": "Blocked"}]


class IdempotencyKeyTest(TestCase):
    """The key must be stable for one event and distinct between events."""

    def _key(self, **overrides):
        parts = {
            "kind": NotificationKind.EPIC_ACTIVITY,
            "recipient_id": 1,
            "issue_id": 10,
            "epic_id": 20,
            "actor_id": 30,
            "created": False,
            "changes": CHANGES,
        }
        parts.update(overrides)
        return build_idempotency_key(**parts)

    def test_the_same_event_produces_the_same_key(self):
        """Otherwise a retry would never recognise its own earlier attempt."""
        self.assertEqual(self._key(), self._key())

    def test_a_different_recipient_produces_a_different_key(self):
        """Fanning one event to five people must yield five separate claims."""
        self.assertNotEqual(self._key(recipient_id=1), self._key(recipient_id=2))

    def test_a_different_issue_produces_a_different_key(self):
        self.assertNotEqual(self._key(issue_id=10), self._key(issue_id=11))

    def test_a_different_epic_produces_a_different_key(self):
        self.assertNotEqual(self._key(epic_id=20), self._key(epic_id=21))

    def test_a_different_actor_produces_a_different_key(self):
        self.assertNotEqual(self._key(actor_id=30), self._key(actor_id=31))

    def test_a_different_change_set_produces_a_different_key(self):
        """Two edits to one issue are two events, not a duplicate of each other."""
        self.assertNotEqual(self._key(changes=CHANGES), self._key(changes=OTHER_CHANGES))

    def test_creation_and_edit_produce_different_keys(self):
        self.assertNotEqual(self._key(created=True), self._key(created=False))

    def test_a_different_kind_produces_a_different_key(self):
        self.assertNotEqual(
            self._key(kind=NotificationKind.EPIC_ACTIVITY),
            self._key(kind=NotificationKind.ASSIGNMENT),
        )

    def test_key_order_does_not_matter(self):
        """Inputs are sorted before hashing, so call order cannot change identity."""
        first = build_idempotency_key(kind="k", recipient_id=1, a=1, b=2)
        second = build_idempotency_key(kind="k", recipient_id=1, b=2, a=1)

        self.assertEqual(first, second)

    def test_the_key_fits_the_column(self):
        """sha256 hex is 64 characters, which is the field's max_length."""
        self.assertEqual(64, len(self._key()))


class NotificationDeliveryClaimTest(TestCase):
    """claim / confirm / release, the three states a delivery can be in."""

    @classmethod
    def setUpTestData(cls):
        cls.user = UserFactory()

    def _claim(self, key="key-1"):
        return NotificationDelivery.objects.claim(
            idempotency_key=key,
            recipient=self.user,
            kind=NotificationKind.EPIC_ACTIVITY,
        )

    def test_a_first_claim_succeeds(self):
        self.assertIsNotNone(self._claim())

    def test_a_second_claim_on_a_sent_delivery_is_refused(self):
        """The duplicate this whole mechanism exists to prevent."""
        delivery = self._claim()
        NotificationDelivery.objects.confirm(delivery)

        self.assertIsNone(self._claim())

    def test_a_second_claim_on_an_unsent_delivery_is_allowed(self):
        """A claim with no sent_at means an earlier attempt failed mid-send.

        Refusing here would turn one failed send into a permanently lost
        notification - worse than the duplicate being guarded against.
        """
        self._claim()

        self.assertIsNotNone(self._claim())

    def test_claiming_does_not_duplicate_the_row(self):
        self._claim()
        self._claim()

        self.assertEqual(1, NotificationDelivery.objects.count())

    def test_different_keys_are_independent(self):
        self.assertIsNotNone(self._claim("key-1"))
        self.assertIsNotNone(self._claim("key-2"))
        self.assertEqual(2, NotificationDelivery.objects.count())

    def test_confirm_records_when_the_message_left(self):
        delivery = self._claim()

        NotificationDelivery.objects.confirm(delivery)
        delivery.refresh_from_db()

        self.assertIsNotNone(delivery.sent_at)

    def test_release_removes_an_unsent_claim(self):
        """A declined send is not a delivery and must not block a later one."""
        delivery = self._claim()

        NotificationDelivery.objects.release(delivery)

        self.assertEqual(0, NotificationDelivery.objects.count())
        self.assertIsNotNone(self._claim())

    def test_release_does_not_remove_a_confirmed_delivery(self):
        """Releasing something already sent would re-open it to a duplicate."""
        delivery = self._claim()
        NotificationDelivery.objects.confirm(delivery)

        NotificationDelivery.objects.release(delivery)

        self.assertEqual(1, NotificationDelivery.objects.count())

    def test_deleting_the_recipient_removes_their_deliveries(self):
        self._claim()

        self.user.delete()

        self.assertEqual(0, NotificationDelivery.objects.count())


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EPIC_ACTIVITY_ADMIN_CC=[],
    NOTIFICATION_COPY_TO=[],
)
class EpicActivityRetryTest(TestCase):
    """What a retry actually does after a partway failure."""

    @classmethod
    def setUpTestData(cls):
        cls.workspace = WorkspaceFactory()
        cls.project = ProjectFactory(workspace=cls.workspace)
        cls.first = UserFactory(email="first@example.com")
        cls.second = UserFactory(email="second@example.com")
        cls.third = UserFactory(email="third@example.com")
        cls.editor = UserFactory(email="editor@example.com")
        for user in (cls.first, cls.second, cls.third, cls.editor):
            Membership.objects.create(workspace=cls.workspace, user=user, role=roles.ROLE_MEMBER)

    def setUp(self):
        self.epic = EpicFactory(project=self.project, assignee=self.first)
        EpicAssignmentFactory(epic=self.epic, user=self.second)
        EpicAssignmentFactory(epic=self.epic, user=self.third)
        self.story = StoryFactory(project=self.project, parent=self.epic)
        mail.outbox = []

    def _send(self):
        return send_epic_activity_email(self.story.pk, self.epic.pk, CHANGES, self.editor.pk, False)

    def _addressed(self):
        return sorted(address for message in mail.outbox for address in message.to)

    def test_rerunning_the_task_does_not_resend(self):
        """The core guarantee: the same event twice is not two emails."""
        self._send()
        self.assertEqual(3, len(mail.outbox))

        mail.outbox = []
        self._send()

        self.assertEqual(0, len(mail.outbox))

    def test_a_retry_after_a_partway_failure_sends_only_the_remainder(self):
        """Two delivered, one failed: the retry sends one email, not three."""
        real_send = mail.EmailMultiAlternatives.send
        calls = {"n": 0}

        def fail_on_third(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise ConnectionError("mail server went away")
            return real_send(self, *args, **kwargs)

        with patch.object(mail.EmailMultiAlternatives, "send", fail_on_third), self.assertRaises(ConnectionError):
            self._send()

        delivered_first_time = self._addressed()
        self.assertEqual(2, len(delivered_first_time))

        mail.outbox = []
        self._send()

        # Exactly the one that failed, and nobody else.
        self.assertEqual(1, len(mail.outbox))
        self.assertNotIn(mail.outbox[0].to[0], delivered_first_time)

    def test_everybody_receives_exactly_one_email_across_the_failure_and_retry(self):
        """End to end: three assignees, one failure, three emails in total."""
        real_send = mail.EmailMultiAlternatives.send
        calls = {"n": 0}

        def fail_once(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ConnectionError("transient")
            return real_send(self, *args, **kwargs)

        with patch.object(mail.EmailMultiAlternatives, "send", fail_once), self.assertRaises(ConnectionError):
            self._send()

        self._send()

        self.assertEqual(
            ["first@example.com", "second@example.com", "third@example.com"],
            self._addressed(),
        )

    def test_a_failed_send_leaves_no_claim_behind(self):
        """Otherwise the retry would mistake the failure for a delivery."""
        with (
            patch.object(mail.EmailMultiAlternatives, "send", side_effect=ConnectionError("down")),
            self.assertRaises(ConnectionError),
        ):
            self._send()

        self.assertEqual(0, NotificationDelivery.objects.filter(sent_at__isnull=False).count())
        self.assertEqual(0, NotificationDelivery.objects.count())

    def test_a_successful_send_records_one_delivery_per_recipient(self):
        self._send()

        self.assertEqual(3, NotificationDelivery.objects.filter(sent_at__isnull=False).count())

    def test_an_opted_out_recipient_leaves_no_claim(self):
        """A declined send is not a delivery; a later legitimate one must work."""
        preference = NotificationPreference.objects.for_user(self.second)
        preference.notify_on_epic_activity = False
        preference.save(update_fields=["notify_on_epic_activity"])

        self._send()

        self.assertFalse(NotificationDelivery.objects.filter(recipient=self.second).exists())

    def test_opting_back_in_allows_a_later_event_to_send(self):
        preference = NotificationPreference.objects.for_user(self.second)
        preference.notify_on_epic_activity = False
        preference.save(update_fields=["notify_on_epic_activity"])
        self._send()

        preference.notify_on_epic_activity = True
        preference.save(update_fields=["notify_on_epic_activity"])
        mail.outbox = []
        send_epic_activity_email(self.story.pk, self.epic.pk, OTHER_CHANGES, self.editor.pk, False)

        self.assertIn("second@example.com", self._addressed())

    def test_a_different_change_is_a_new_event_and_does_send(self):
        """Idempotency must not suppress genuinely new activity."""
        self._send()
        mail.outbox = []

        send_epic_activity_email(self.story.pk, self.epic.pk, OTHER_CHANGES, self.editor.pk, False)

        self.assertEqual(3, len(mail.outbox))

    def test_a_recipient_added_after_the_first_send_still_receives_the_event(self):
        """Their claim does not exist yet, so they are not skipped."""
        self._send()
        mail.outbox = []

        newcomer = UserFactory(email="newcomer@example.com")
        Membership.objects.create(workspace=self.workspace, user=newcomer, role=roles.ROLE_MEMBER)
        EpicAssignmentFactory(epic=self.epic, user=newcomer)

        self._send()

        self.assertEqual(["newcomer@example.com"], self._addressed())

    def test_the_second_run_reports_no_send(self):
        """Nothing left, so False - the same signal as "everybody declined"."""
        self._send()

        self.assertFalse(self._send())
