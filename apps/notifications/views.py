"""Notification preference views.

The unsubscribe endpoint is deliberately unauthenticated: someone who no longer
wants email must be able to stop it without remembering a password. The UUID
token in the URL identifies the user and grants nothing beyond turning their own
notifications off. The preferences page, by contrast, requires a login.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.notifications.forms import NotificationPreferenceForm
from apps.notifications.models import NotificationPreference


@require_http_methods(["GET", "POST"])
def unsubscribe(request, token):
    """Show a confirmation page (GET) and apply the opt-out (POST).

    GET must not change anything: mail clients and security scanners prefetch
    links, and a mutating GET would silently unsubscribe people who never clicked.
    RFC 8058 one-click unsubscribe uses POST, which is what the
    List-Unsubscribe-Post header we send advertises.
    """
    preference = get_object_or_404(NotificationPreference.objects.select_related("user"), unsubscribe_token=token)

    if request.method == "POST":
        preference.disable_all()
        return render(
            request,
            "notifications/unsubscribed.html",
            {"preference": preference, "done": True},
        )

    return render(
        request,
        "notifications/unsubscribe_confirm.html",
        {"preference": preference},
    )


@login_required
def preferences(request):
    """Let a signed-in user turn individual notification types on or off."""
    preference = NotificationPreference.objects.for_user(request.user)

    if request.method == "POST":
        form = NotificationPreferenceForm(request.POST, instance=preference)
        if form.is_valid():
            form.save()
            messages.success(request, _("Notification preferences saved."))
            return redirect(reverse("notifications:preferences"))
    else:
        form = NotificationPreferenceForm(instance=preference)

    return render(
        request,
        "notifications/preferences.html",
        {"form": form, "preference": preference, "page_title": _("Notifications")},
    )
