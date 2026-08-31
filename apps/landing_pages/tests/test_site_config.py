"""Tests for site-domain configuration (Step 0.1).

Covers the three pieces that make emailed links resolve:
  * get_root() building absolute URLs
  * the configure_site management command
  * the system check that catches domain drift
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.landing_pages.checks import W001_PLACEHOLDER, W002_DRIFT, check_site_domain

from io import StringIO

from matorral.context_processors import get_root


class GetRootTests(TestCase):
    """get_root() is the single source of absolute URLs for emails."""

    def setUp(self):
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

    def _set_domain(self, domain):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": domain, "name": "Test"})
        Site.objects.clear_cache()

    @override_settings(USE_HTTPS_IN_ABSOLUTE_URLS=False)
    def test_uses_http_when_https_disabled(self):
        self._set_domain("localhost:8000")
        self.assertEqual(get_root(), "http://localhost:8000")

    @override_settings(USE_HTTPS_IN_ABSOLUTE_URLS=True)
    def test_uses_https_when_enabled(self):
        """Regression: the setting was read at import time, so overrides were ignored."""
        self._set_domain("app.example.org")
        self.assertEqual(get_root(), "https://app.example.org")

    @override_settings(USE_HTTPS_IN_ABSOLUTE_URLS=False)
    def test_is_secure_argument_forces_https(self):
        """Regression: the is_secure argument was declared but never used."""
        self._set_domain("app.example.org")
        self.assertEqual(get_root(is_secure=True), "https://app.example.org")

    @override_settings(USE_HTTPS_IN_ABSOLUTE_URLS=True)
    def test_is_secure_argument_forces_http(self):
        self._set_domain("app.example.org")
        self.assertEqual(get_root(is_secure=False), "http://app.example.org")

    def test_has_no_trailing_slash(self):
        """Callers concatenate get_root() + a path that already starts with '/'."""
        self._set_domain("app.example.org")
        self.assertFalse(get_root().endswith("/"))

    def test_concatenates_with_absolute_url_into_valid_link(self):
        """The exact pattern emails use: get_root() + obj.get_absolute_url()."""
        self._set_domain("app.example.org")
        self.assertEqual(get_root() + "/w/acme/p/DP/", "http://app.example.org/w/acme/p/DP/")

    def test_reflects_domain_change_without_restart(self):
        self._set_domain("first.example.org")
        self.assertIn("first.example.org", get_root())
        self._set_domain("second.example.org")
        self.assertIn("second.example.org", get_root())

    def test_preserves_port_in_domain(self):
        """Local/staging hosts carry a port; it must survive into the URL."""
        self._set_domain("127.0.0.1:8000")
        self.assertEqual(get_root(), "http://127.0.0.1:8000")


class ConfigureSiteCommandTests(TestCase):
    """The escape hatch for when SITE_DOMAIN changes after the migration ran."""

    def setUp(self):
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

    def _run(self, **kwargs):
        out = StringIO()
        call_command("configure_site", stdout=out, **kwargs)
        return out.getvalue()

    @override_settings(SITE_DOMAIN="fromsettings.example.org", SITE_NAME="FromSettings")
    def test_uses_settings_by_default(self):
        self._run()
        site = Site.objects.get(pk=settings.SITE_ID)
        self.assertEqual(site.domain, "fromsettings.example.org")
        self.assertEqual(site.name, "FromSettings")

    def test_domain_argument_overrides_settings(self):
        self._run(domain="override.example.org", name="Override")
        site = Site.objects.get(pk=settings.SITE_ID)
        self.assertEqual(site.domain, "override.example.org")
        self.assertEqual(site.name, "Override")

    def test_is_idempotent(self):
        """Runs on every deploy; running twice must not duplicate or error."""
        self._run(domain="stable.example.org")
        self._run(domain="stable.example.org")
        self.assertEqual(Site.objects.filter(pk=settings.SITE_ID).count(), 1)
        self.assertEqual(Site.objects.get(pk=settings.SITE_ID).domain, "stable.example.org")

    def test_creates_row_when_missing(self):
        """A fresh DB (or a wiped sites table) must not crash the command."""
        Site.objects.all().delete()
        self._run(domain="recreated.example.org")
        self.assertEqual(Site.objects.get(pk=settings.SITE_ID).domain, "recreated.example.org")

    def test_clears_cache_so_get_root_sees_new_value(self):
        """Site.objects.get_current() caches per process; stale cache = wrong links."""
        self._run(domain="cached.example.org")
        self.assertEqual(get_root(), "http://cached.example.org")

    def test_reports_what_it_did(self):
        output = self._run(domain="reported.example.org")
        self.assertIn("reported.example.org", output)


class SiteDomainCheckTests(TestCase):
    """The system check that catches a misconfigured domain before users do."""

    def setUp(self):
        Site.objects.clear_cache()
        self.addCleanup(Site.objects.clear_cache)

    def _set_domain(self, domain):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": domain, "name": "Test"})
        Site.objects.clear_cache()

    @override_settings(SITE_DOMAIN="app.example.org")
    def test_no_warning_when_domain_matches(self):
        self._set_domain("app.example.org")
        self.assertEqual(check_site_domain(None), [])

    @override_settings(SITE_DOMAIN="app.example.org")
    def test_warns_on_placeholder_domain(self):
        self._set_domain("example.com")
        warnings = check_site_domain(None)
        self.assertEqual([w.id for w in warnings], [W001_PLACEHOLDER])

    @override_settings(SITE_DOMAIN="expected.example.org")
    def test_warns_when_db_drifts_from_settings(self):
        self._set_domain("stale.example.org")
        warnings = check_site_domain(None)
        self.assertEqual([w.id for w in warnings], [W002_DRIFT])
        self.assertIn("stale.example.org", warnings[0].msg)

    @override_settings(SITE_DOMAIN="app.example.org")
    def test_no_crash_when_site_row_missing(self):
        """`manage.py check` may run before `migrate` on a fresh database."""
        Site.objects.all().delete()
        Site.objects.clear_cache()
        self.assertEqual(check_site_domain(None), [])

    @override_settings(SITE_DOMAIN="")
    def test_no_drift_warning_when_setting_is_blank(self):
        """Blank SITE_DOMAIN means 'unmanaged' — only the placeholder is worth flagging."""
        self._set_domain("whatever.example.org")
        self.assertEqual(check_site_domain(None), [])


class SiteMigrationTests(TestCase):
    """The migration is what makes this work on a real deploy."""

    def test_migration_leaves_no_placeholder_domain(self):
        """After the full migration run, the test DB must not be on example.com."""
        site = Site.objects.get(pk=settings.SITE_ID)
        self.assertNotEqual(
            site.domain,
            "example.com",
            "0001_configure_site did not run; emailed links would point at example.com.",
        )

    def test_migration_applied_configured_domain(self):
        site = Site.objects.get(pk=settings.SITE_ID)
        self.assertEqual(site.domain, settings.SITE_DOMAIN)
