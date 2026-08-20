from django.conf import settings
from django.contrib.sites.models import Site


def get_root(is_secure: bool | None = None) -> str:
    """Return the site root URL (scheme + domain), with no trailing slash.

    Used to build absolute URLs for links that leave the request cycle — emails,
    most importantly — where Django cannot derive the host from the request.

    Args:
        is_secure: Force the scheme. Defaults to USE_HTTPS_IN_ABSOLUTE_URLS.

    The setting is read at call time (not as a default argument) so that
    @override_settings works in tests and at runtime.
    """
    if is_secure is None:
        is_secure = settings.USE_HTTPS_IN_ABSOLUTE_URLS
    protocol = "https" if is_secure else "http"
    return f"{protocol}://{Site.objects.get_current().domain}"


_ENV_BADGE_LABELS = {
    "local": "local",
    "production": "beta",
}


def base_context(request):
    environment = getattr(settings, "ENVIRONMENT", "local")
    return {
        "site": Site.objects.get_current(request),
        "server_url": get_root(),
        "page_url": get_root() + request.path,
        "site_description": getattr(settings, "SITE_DESCRIPTION", ""),
        "site_keywords": getattr(settings, "SITE_KEYWORDS", ""),
        "is_debug": settings.DEBUG,
        "env_badge": _ENV_BADGE_LABELS.get(environment, "demo"),
        "DEMO_CREDENTIALS_EMAIL": getattr(settings, "DEMO_CREDENTIALS_EMAIL", ""),
        "DEMO_CREDENTIALS_PASSWORD": getattr(settings, "DEMO_CREDENTIALS_PASSWORD", ""),
    }


def google_analytics_id(request):
    """Adds google analytics id to all requests."""
    if settings.GOOGLE_ANALYTICS_ID:
        return {"GOOGLE_ANALYTICS_ID": settings.GOOGLE_ANALYTICS_ID}
    return {}
