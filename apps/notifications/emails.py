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


def send_notification(
    *,
    recipient,
    kind: str,
    subject: str,
    template: str,
    context: dict,
    extra_cc: list[str] | None = None,
    exclude_copies: list[str] | None = None,
) -> bool:
    """Render and send one notification email.

    Args:
        recipient: User to email.
        kind: A NotificationKind value; selects the opt-out flag to honour.
        subject: Pre-rendered subject line (no prefix is added).
        template: Base template path without extension; ``.txt`` and ``.html``
            are both rendered so every client gets a readable part.
        context: Template context. ``site``, ``server_url`` and the unsubscribe
            URL are added automatically.
        extra_cc: Additional addresses to copy on this one message, on top of the
            site-wide NOTIFICATION_COPY_TO. Used where a notification type has its
            own audit copy. Deduplicated case-insensitively against the recipient
            and against the site-wide list, so nobody is addressed twice.
        exclude_copies: Further addresses to keep out of the copy list. Used when
            one event fans out to several recipients: each of them is getting
            their own message, so an audit address that belongs to one of them
            must not also be copied here. Without it, a monitoring address that
            is also an assignee receives the mail twice.

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

    # Every address that is already receiving this event in its own right, so it
    # is never also added as a copy.
    excluded = [recipient.email, *(exclude_copies or [])]

    copies = copy_recipients(exclude=excluded)
    if extra_cc:
        copies = _merge_extra_cc(copies, extra_cc, exclude=excluded)

    message = LogoEmailMessage(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient.email],
        headers=_unsubscribe_headers(full_context["unsubscribe_url"]),
        logo=logo,
        **copies,
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


def _merge_extra_cc(copies: dict, extra_cc: list[str], *, exclude: list[str]) -> dict:
    """Fold per-message copy addresses into whatever copy_recipients() returned.

    The site-wide copy list may be cc or bcc, or absent entirely. Per-message
    addresses are added to that same field so one message never carries both a Cc
    and a Bcc copy of itself; with no site-wide list they default to cc, which is
    what an explicitly configured audit address is for.

    Addresses already present — as the recipient, or in the site-wide list — are
    dropped, compared case-insensitively, so nobody receives two copies.
    """
    field = next(iter(copies), "cc")
    existing = list(copies.get(field, []))

    seen = {address.lower() for address in existing}
    seen.update(address.lower() for address in exclude)

    for address in extra_cc:
        address = (address or "").strip()
        if address and address.lower() not in seen:
            existing.append(address)
            seen.add(address.lower())

    return {field: existing} if existing else {}


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
