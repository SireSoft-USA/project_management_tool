"""Keep the django.contrib.sites row in sync with SITE_DOMAIN / SITE_NAME.

django.contrib.sites ships a single row hardcoded to "example.com". Absolute URLs
built outside the request cycle — emails, most importantly — read that row via
matorral.context_processors.get_root(), so leaving the default in place produces
links pointing at example.com that no recipient can open.

This migration is deliberately re-runnable: `migrate` is part of every deploy, so
pointing SITE_DOMAIN at a new host and deploying is enough to update the row.
"""

from django.conf import settings
from django.db import migrations


def configure_site(apps, schema_editor):
    """Point the SITE_ID row at the configured domain, creating it if absent."""
    Site = apps.get_model("sites", "Site")
    site_id = getattr(settings, "SITE_ID", 1)
    domain = getattr(settings, "SITE_DOMAIN", "localhost:8000")
    name = getattr(settings, "SITE_NAME", "Siresoft")

    # update_or_create (not .get) so a fresh database with no sites row still works.
    Site.objects.using(schema_editor.connection.alias).update_or_create(
        pk=site_id,
        defaults={"domain": domain, "name": name},
    )


def restore_example_com(apps, schema_editor):
    """Restore the django.contrib.sites default so the migration is reversible."""
    Site = apps.get_model("sites", "Site")
    site_id = getattr(settings, "SITE_ID", 1)

    Site.objects.using(schema_editor.connection.alias).filter(pk=site_id).update(
        domain="example.com",
        name="example.com",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0002_alter_domain_unique"),
    ]

    operations = [
        migrations.RunPython(configure_site, restore_example_com),
    ]
