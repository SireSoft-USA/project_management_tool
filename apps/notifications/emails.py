"""The single choke point every notification email passes through.

Centralising delivery here means the guards that stop us mailing the wrong person
— no address, opted out, notifications disabled globally — exist in exactly one
place and cannot be forgotten by a new caller. Callers describe *what* to send;
this module decides *whether* to send it.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.contrib.sites.models import Site
from django.contrib.staticfiles import finders
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse

from apps.notifications.models import NotificationKind, NotificationPreference

from matorral.context_processors import get_root

logger = logging.getLogger(__name__)

# Content-ID for the inline logo. Templates reference it as src="cid:siresoft-logo".
LOGO_CID = "siresoft-logo"

# Light wordmark: the email header is dark navy, so the dark-text variant is unreadable.
LOGO_STATIC_PATH = "images/header-logo2.png"


class NotificationSkipped(Exception):
    """Raised internally when a guard declines to send. Never propagated to callers."""


def send_notification(*, recipient, kind: str, subject: str, template: str, context: dict) -> bool:
    """Render and send one notification email.

    Args:
        recipient: User to email.
        kind: A NotificationKind value; selects the opt-out flag to honour.
        subject: Pre-rendered subject line (no prefix is added).
        template: Base template path without extension; ``.txt`` and ``.html``
            are both rendered so every client gets a readable part.
        context: Template context. ``site``, ``server_url`` and the unsubscribe
            URL are added automatically.

    Returns:
        True if the message was handed to the mail backend, False if a guard
        declined. Never raises for a declined send — only a genuine backend
        failure propagates, so Celery can retry it.
    """
    try:
        preference = _check_guards(recipient, kind)
    except NotificationSkipped as exc:
        logger.info("Notification skipped (kind=%s, user=%s): %s", kind, getattr(recipient, "pk", None), exc)
        return False

    logo = logo_bytes()
    full_context = _build_context(context, preference, logo)
    text_body = render_to_string(f"{template}.txt", full_context)
    html_body = render_to_string(f"{template}.html", full_context)

    message = LogoEmailMessage(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient.email],
        headers=_unsubscribe_headers(full_context["unsubscribe_url"]),
        logo=logo,
        **copy_recipients(exclude=[recipient.email]),
    )
    message.attach_alternative(html_body, "text/html")

    # fail_silently=False so a broken backend raises and the Celery task retries.
    message.send(fail_silently=False)
    logger.info("Notification sent (kind=%s, to=%s)", kind, recipient.email)
    return True


def _check_guards(recipient, kind: str) -> NotificationPreference:
    """Raise NotificationSkipped unless this recipient should be emailed."""
    if not getattr(settings, "NOTIFICATIONS_ENABLED", True):
        raise NotificationSkipped("notifications disabled globally")

    if recipient is None:
        raise NotificationSkipped("no recipient")

    if not getattr(recipient, "email", ""):
        raise NotificationSkipped("user has no email address")

    if not getattr(recipient, "is_active", True):
        raise NotificationSkipped("user is inactive")

    preference = NotificationPreference.objects.for_user(recipient)
    if not preference.is_enabled_for(kind):
        raise NotificationSkipped(f"user opted out of {kind}")

    return preference


def _build_context(context: dict, preference: NotificationPreference, logo: bytes | None = None) -> dict:
    """Add the values every notification template needs."""
    server_url = get_root()
    unsubscribe_path = reverse(
        "notifications:unsubscribe",
        kwargs={"token": preference.unsubscribe_token},
    )
    return {
        **context,
        "site": Site.objects.get_current(),
        # The shared email base template renders its footer link from current_site.
        "current_site": Site.objects.get_current(),
        "server_url": server_url,
        "unsubscribe_url": f"{server_url}{unsubscribe_path}",
        # logo_cid is only set when the image was actually embedded.
        **build_logo_context(logo),
    }


def copy_recipients(*, exclude: list[str] | None = None) -> dict:
    """Return {"cc": [...]} or {"bcc": [...]} for NOTIFICATION_COPY_TO.

    Used for monitoring or record keeping: every notification and invitation is
    copied to these addresses. Returns an empty dict when unconfigured, so it can
    be splatted into EmailMultiAlternatives(...) unconditionally.

    Args:
        exclude: Addresses to drop from the copy list — normally the recipient,
            so that mailing the monitoring address itself does not duplicate it
            into both To and Cc.
    """
    addresses = [a.strip() for a in getattr(settings, "NOTIFICATION_COPY_TO", []) if a and a.strip()]
    if not addresses:
        return {}

    skip = {a.lower() for a in (exclude or [])}
    addresses = [a for a in addresses if a.lower() not in skip]
    if not addresses:
        return {}

    # Anything other than an explicit "cc" is treated as bcc: hiding the address
    # is the safer default if the setting is mistyped.
    field = "cc" if getattr(settings, "NOTIFICATION_COPY_MODE", "bcc").lower() == "cc" else "bcc"
    return {field: addresses}


def _unsubscribe_headers(unsubscribe_url: str) -> dict:
    """Return List-Unsubscribe headers so mail clients show a native opt-out.

    One-Click is advertised because the endpoint accepts POST; without the
    matching header, providers fall back to opening the URL in a browser.
    """
    return {
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def build_issue_url(issue) -> str:
    """Return the absolute, clickable URL for an issue.

    Emails leave the request cycle, so the host cannot be derived from a request;
    it comes from the configured site domain instead.
    """
    return f"{get_root()}{issue.get_absolute_url()}"


def build_logo_url() -> str:
    """Return the absolute URL of the logo, used as the fallback when it cannot
    be embedded.

    Linking is a last resort: a remote image has to survive the mail client's
    remote-image blocking, mixed-content rules when webmail is served over HTTPS,
    and being reachable from wherever the recipient reads mail. ``attach_logo()``
    embeds the file instead and is what normally applies.
    """
    return f"{get_root()}{static(LOGO_STATIC_PATH)}"


def logo_bytes() -> bytes | None:
    """Return the raw logo image, or None when it cannot be read.

    A missing or unreadable logo must never stop a notification going out, so
    callers treat None as "render the linked fallback instead".
    """
    path = finders.find(LOGO_STATIC_PATH)
    if not path:
        logger.warning("Email logo %s not found in staticfiles; falling back to a linked image", LOGO_STATIC_PATH)
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        logger.warning("Could not read email logo %s; falling back to a linked image", path, exc_info=True)
        return None


class LogoEmailMessage(EmailMultiAlternatives):
    """Email whose HTML part carries the logo embedded as an inline image.

    Embedding rather than linking is what makes the logo appear at all in the
    common case: mail clients block remote images by default, webmail served over
    HTTPS refuses an http:// image as mixed content, and a private-network host is
    unreachable from outside. There is nothing to fetch, so none of that applies.

    Django 6 assembles the MIME tree in ``_add_bodies``; adding the image there
    (rather than as a plain attachment) is what produces the multipart/related
    structure that Outlook and Roundcube need to resolve a ``cid:`` reference.
    """

    def __init__(self, *args, logo=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.logo = logo

    def _add_bodies(self, msg):
        msg = super()._add_bodies(msg)
        if self.logo:
            # Attaches to the HTML part specifically, so text-only clients are
            # unaffected and the image never shows as a separate attachment.
            html_part = msg.get_payload()[-1] if msg.is_multipart() else msg
            html_part.add_related(
                self.logo,
                maintype="image",
                subtype="png",
                cid=f"<{LOGO_CID}>",
                disposition="inline",
                filename=Path(LOGO_STATIC_PATH).name,
            )
        return msg


def build_logo_context(logo: bytes | None) -> dict:
    """Return the template variables that select embedded vs linked logo.

    ``logo_cid`` is only set when the image is actually embedded, so a failed
    read degrades to the linked image rather than a broken ``cid:`` reference
    pointing at nothing.
    """
    if logo:
        return {"logo_cid": LOGO_CID, "logo_url": build_logo_url()}
    return {"logo_cid": "", "logo_url": build_logo_url()}


__all__ = [
    "LOGO_CID",
    "LOGO_STATIC_PATH",
    "LogoEmailMessage",
    "NotificationKind",
    "NotificationSkipped",
    "build_issue_url",
    "build_logo_context",
    "build_logo_url",
    "logo_bytes",
    "copy_recipients",
    "send_notification",
]
