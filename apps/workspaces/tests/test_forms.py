"""Form validation tests for the workspaces app.

TestWorkspaceSignupFormInvitationEmailValidation used to live here. It exercised
WorkspaceSignupForm, which is commented out in apps/workspaces/forms.py because
open signup is disabled (ACCOUNT_ALLOW_SIGNUPS defaults to False and the
ACCOUNT_FORMS entry is commented out in settings). The import of that missing
form raised at module load, which meant *this whole module* failed to import -
so the InvitationForm tests below never ran either. The dead class is removed
rather than skipped; if signup is reinstated, the form and its tests come back
together.
"""

from django.test import TestCase

from apps.users.factories import UserFactory
from apps.workspaces.factories import InvitationFactory, MembershipFactory, WorkspaceFactory
from apps.workspaces.forms import InvitationForm
from apps.workspaces.roles import ROLE_ADMIN


class TestInvitationFormEmailValidation(TestCase):
    def setUp(self):
        self.workspace = WorkspaceFactory()
        admin = UserFactory()
        MembershipFactory(workspace=self.workspace, user=admin, role=ROLE_ADMIN)

    def test_existing_member_email_raises_validation_error(self):
        member = UserFactory(email="member@example.com")
        MembershipFactory(workspace=self.workspace, user=member)

        form = InvitationForm(self.workspace, data={"email": "member@example.com", "role": "member"})
        self.assertFalse(form.is_valid())
        self.assertIn("is already a member of this workspace", str(form.errors))

    def test_pending_invitation_email_raises_validation_error(self):
        InvitationFactory(workspace=self.workspace, email="pending@example.com")

        form = InvitationForm(self.workspace, data={"email": "pending@example.com", "role": "member"})
        self.assertFalse(form.is_valid())
        self.assertIn("There is already a pending invitation", str(form.errors))
