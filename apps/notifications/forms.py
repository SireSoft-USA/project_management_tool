from django import forms
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import NotificationPreference


class NotificationPreferenceForm(forms.ModelForm):
    """Lets a signed-in user choose which notification emails they receive."""

    class Meta:
        model = NotificationPreference
        fields = ["notify_on_assignment", "notify_on_membership"]
        labels = {
            "notify_on_assignment": _("Task assignments"),
            "notify_on_membership": _("Workspace invitations"),
        }
        help_texts = {
            "notify_on_assignment": _("Email me when someone assigns a task to me."),
            "notify_on_membership": _("Email me when I'm added to a workspace."),
        }
        widgets = {
            "notify_on_assignment": forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
            "notify_on_membership": forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
        }
