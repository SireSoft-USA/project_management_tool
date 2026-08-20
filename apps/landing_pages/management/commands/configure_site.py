"""Sync the django.contrib.sites row with SITE_DOMAIN / SITE_NAME.

The 0001_configure_site migration does this on first run, but migrations only run
once. Use this command when SITE_DOMAIN changes afterwards (new domain, staging
clone of a production database, etc.).
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Sync the sites framework row with settings.SITE_DOMAIN / SITE_NAME."

    def add_arguments(self, parser):
        parser.add_argument(
            "--domain",
            help="Override settings.SITE_DOMAIN (e.g. app.example.org).",
        )
        parser.add_argument(
            "--name",
            help="Override settings.SITE_NAME.",
        )

    def handle(self, *args, **options):
        domain = options["domain"] or getattr(settings, "SITE_DOMAIN", "localhost:8000")
        name = options["name"] or getattr(settings, "SITE_NAME", "Siresoft")
        site_id = getattr(settings, "SITE_ID", 1)

        site, created = Site.objects.update_or_create(
            pk=site_id,
            defaults={"domain": domain, "name": name},
        )

        # get_current() caches per-process; clear it so the new value is visible
        # immediately to anything running in this process.
        Site.objects.clear_cache()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} site {site_id}: domain={site.domain!r} name={site.name!r}"))
