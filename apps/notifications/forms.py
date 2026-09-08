from django import forms
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import NotificationPreference


class NotificationPreferenceForm(forms.ModelForm):
    """Lets a signed-in user choose which notification emails they receive."""

    class Meta:
        model = NotificationPreference
        fields = [
            "notify_on_assignment",
            "notify_on_membership",
            "notify_on_epic_inactivity",
            "notify_on_epic_activity",
        ]
        labels = {
            "notify_on_assignment": _("Task assignments"),
            "notify_on_membership": _("Workspace invitations"),
            "notify_on_epic_inactivity": _("Inactive epics"),
            "notify_on_epic_activity": _("Activity on my epics"),
        }
        help_texts = {
            "notify_on_assignment": _("Email me when someone assigns a task to me."),
            "notify_on_membership": _("Email me when I'm added to a workspace."),
            "notify_on_epic_inactivity": _(
                "Email me when an epic assigned to me has had no story activity for a week."
            ),
            "notify_on_epic_activity": _(
                "Email me when a story, bug or chore under an epic I own is created or edited."
            ),
        }
        widgets = {
            "notify_on_assignment": forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
            "notify_on_membership": forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
            "notify_on_epic_inactivity": forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
            "notify_on_epic_activity": forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
        }
