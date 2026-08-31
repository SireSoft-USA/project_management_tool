"""Deploy-time safety checks for site and email configuration.

The sites row drives every absolute URL sent in an email. If it drifts from
settings.SITE_DOMAIN — because the migration already ran and SITE_DOMAIN changed
afterwards — links silently point at the wrong host. These checks surface that
at `manage.py check` time instead of in users' inboxes.
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.checks import Warning as CheckWarning
from django.core.checks import register
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import DatabaseError
from django.utils.translation import gettext_lazy as _

W001_PLACEHOLDER = "landing_pages.W001"
W002_DRIFT = "landing_pages.W002"
W003_FROM_ADDRESS = "landing_pages.W003"

# Backends that never open a network connection, so an unroutable From address
# is harmless (it is printed, not delivered).
_LOCAL_EMAIL_BACKENDS = ("console", "locmem", "dummy", "filebased")


def _extract_address(from_email: str) -> str:
    """Return the bare address from a 'Display Name <addr@host>' value."""
    value = from_email.strip()
    if value.endswith(">") and "<" in value:
        return value[value.rindex("<") + 1 : -1].strip()
    return value


@register
def check_site_domain(app_configs, **kwargs):
    """Warn when the sites row is unset or has drifted from SITE_DOMAIN."""
    configured = getattr(settings, "SITE_DOMAIN", "")

    try:
        site = Site.objects.get(pk=getattr(settings, "SITE_ID", 1))
    except (Site.DoesNotExist, DatabaseError):
        # No database yet (fresh checkout, or `check` before `migrate`).
        # The migration will create the row; nothing to warn about.
        return []

    if site.domain == "example.com":
        return [
            CheckWarning(
                "The site domain is still django.contrib.sites' default 'example.com'.",
                hint=(
                    "Emailed links will point at example.com and be unusable. "
                    "Set SITE_DOMAIN in .env and run `manage.py migrate`."
                ),
                id=W001_PLACEHOLDER,
            )
        ]

    if configured and site.domain != configured:
        return [
            CheckWarning(
                f"Site domain '{site.domain}' does not match SITE_DOMAIN '{configured}'.",
                hint=("Emailed links use the database value. Run `manage.py configure_site` to sync it with settings."),
                id=W002_DRIFT,
            )
        ]

    return []


@register
def check_default_from_email(app_configs, **kwargs):
    """Warn when DEFAULT_FROM_EMAIL is not a deliverable address.

    The default is derived from SITE_DOMAIN, which is usually a host:port such as
    "10.0.2.11:8000". That is fine for the console backend but is rejected by real
    SMTP servers, so the failure would only appear on the first live send.
    """
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if any(local in backend for local in _LOCAL_EMAIL_BACKENDS):
        # Nothing is delivered, so an unroutable address cannot cause a failure.
        return []

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "")
    address = _extract_address(from_email)

    try:
        validate_email(address)
    except ValidationError:
        return [
            CheckWarning(
                f"DEFAULT_FROM_EMAIL {from_email!r} is not a valid email address.",
                hint=_(
                    "Set DEFAULT_FROM_EMAIL to a real mailbox on a domain you control, "
                    'e.g. "Siresoft <noreply@siresoft.com>". The default is derived from '
                    "SITE_DOMAIN, which includes a port and is not a valid mail domain."
                ),
                id=W003_FROM_ADDRESS,
            )
        ]

    return []
