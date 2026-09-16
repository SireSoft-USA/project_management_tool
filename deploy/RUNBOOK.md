# Deployment runbook

Day-to-day operations for the native (non-Docker) deployment. First-time
install is in [README.md](README.md); this file is what you run afterwards.

Assumed layout:

- App directory: `/opt/matorral`
- App user: `matorral`
- Domain: `pmt.siresoft.net` (plain HTTP — this deployment has no TLS)
- Services: `matorral-django` (Gunicorn), `matorral-celery` (worker + beat)

---

## Deploy a code update

The standard update. Safe to run every time, even when some steps are
unnecessary.

```bash
cd /opt/matorral

# 1. Get the new code
sudo -u matorral git pull origin main

# 2. Python dependencies
sudo -u matorral /root/.local/bin/uv sync --frozen --no-group dev --group prod

# 3. Frontend build + static files
sudo -u matorral npm ci
sudo -u matorral npm run build
sudo -u matorral env DJANGO_SETTINGS_MODULE=matorral.settings.production \
    .venv/bin/python manage.py collectstatic --noinput

# 4. Restart BOTH services (migrations run automatically on django start)
sudo systemctl restart matorral-django matorral-celery

# 5. Confirm both came up
sudo systemctl status matorral-django matorral-celery --no-pager | grep -E "Active:|Main PID"

# 6. Confirm the site responds
curl -s -o /dev/null -w "HTTP %{http_code}\n" -H "Host: pmt.siresoft.net" http://127.0.0.1/
```

Expected: both services `Active: active (running)`, and `HTTP 302` — the
redirect to the login page, which is the correct answer for a request with no
session.

### Why each step matters

| Step | What breaks if you skip it |
| --- | --- |
| `npm run build` + `collectstatic` | CSS, JS and the email logo stop resolving |
| Restarting **celery** | Scheduled work (the epic inactivity sweep, notification email) keeps running the old code |
| Migrations | Handled automatically — `matorral-django.service` runs `manage.py migrate` in `ExecStartPre` on every start |

**The most common mistake is restarting only django.** Notification email is
sent from celery, so a django-only restart leaves the old email code running
and the change appears not to have deployed.

### Quick version

When the update touched no Python dependencies and no frontend files:

```bash
cd /opt/matorral && sudo -u matorral git pull origin main && sudo systemctl restart matorral-django matorral-celery
```

---

## Check what a pull will bring, before pulling

```bash
cd /opt/matorral
sudo -u matorral git fetch origin
sudo -u matorral git log --oneline HEAD..origin/main     # commits you will get
sudo -u matorral git diff --stat HEAD..origin/main       # files that change
```

Read the file list and pick a path: `package.json` or `pyproject.toml` in the
diff means use the full runbook; only `apps/**.py` means the quick version is
enough.

---

## After editing .env

`.env` is read once, at service start, so a change has no effect until:

```bash
sudo systemctl restart matorral-django matorral-celery
```

`SITE_NAME` and `SITE_DOMAIN` additionally live in the database, and **emails
read the database value, not the file**. Changing them in `.env` alone leaves
email headers and links on the old value:

```bash
cd /opt/matorral && sudo -u matorral env DJANGO_SETTINGS_MODULE=matorral.settings.production \
    .venv/bin/python manage.py configure_site
```

### Check .env for the formatting faults that fail silently

```bash
sudo grep -nE "^[[:space:]]+[A-Z_]+=|==" /opt/matorral/.env
```

Empty output means clean. Anything printed is a line the parser will reject:

- **A leading space** before the key. django-environ prints
  `Invalid line: EMAIL_BACKEND=...` and ignores the setting — the app then runs
  on defaults with no other symptom. This is what once left every notification
  email undeliverable while `.env` looked correct.
- **A doubled `=`** (`KEY=="value"`), same outcome.

---

## Required .env values

These have each caused an outage or silent failure when wrong:

| Setting | Value | Why |
| --- | --- | --- |
| `DEBUG` | `False` | Must be set *explicitly*. `settings.py` inserts `daphne` into `INSTALLED_APPS` while its own `DEBUG` default is still `True`, and `production.py` setting `DEBUG = False` later does not undo that. `daphne` is not in the prod dependency group, so leaving it unset crashes every management command with `ModuleNotFoundError: No module named 'daphne'`. Leaving it `True` also exposes settings, SQL and file paths on any error page. |
| `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` | `production.py` defaults to the Mailgun backend. Without an API key that returns `401 Unauthorized` on every send, and nothing is delivered. |
| `SECURE_SSL_REDIRECT` | `False` | This deployment serves plain HTTP. `True` marks cookies Secure, which browsers silently drop over HTTP, breaking login and CSRF. |
| `ENABLE_DEBUG_TOOLBAR` | `False` | Not installed in the prod dependency group; `True` crashes the app. |
| `ALLOWED_HOSTS` | `pmt.siresoft.net,10.0.3.6` | Defaults to `*` otherwise. |
| `SITE_DOMAIN` | `pmt.siresoft.net` | Emailed links are built from this (via the database — run `configure_site`). |
| `SITE_NAME` | `SireSoft` | Appears in email headers (via the database — run `configure_site`). |

---

## Health check

Run any time to see the state of the deployment at a glance:

```bash
cd /opt/matorral && sudo -u matorral env DJANGO_SETTINGS_MODULE=matorral.settings.production .venv/bin/python manage.py shell -c "
from django.conf import settings
from django.contrib.sites.models import Site
print('DEBUG        :', settings.DEBUG)
print('EMAIL_BACKEND:', settings.EMAIL_BACKEND)
print('EMAIL_HOST   :', settings.EMAIL_HOST)
print('site domain  :', Site.objects.get_current().domain)
print('site name    :', Site.objects.get_current().name)
"
```

```bash
cd /opt/matorral && sudo -u matorral git log --oneline -1    # deployed commit
```

`DEBUG` must be `False` and `EMAIL_BACKEND` must contain `smtp`. Those two
settings account for most of the failures seen so far.

---

## Send a test email

Proves the whole delivery path — SMTP credentials, the branded template, the
embedded logo and the absolute URLs — in one message.

```bash
cd /opt/matorral && sudo -u matorral env DJANGO_SETTINGS_MODULE=matorral.settings.production .venv/bin/python manage.py shell -c "
from django.core.mail import send_mail
from django.conf import settings
print('sent:', send_mail('SireSoft SMTP test', 'Delivery works.',
      settings.DEFAULT_FROM_EMAIL, ['you@example.com'], fail_silently=False))
"
```

`sent: 1` means the message reached the mail server. Replace the address, and
expect an exception rather than a silent failure if the credentials are wrong —
`fail_silently=False` is deliberate.

To exercise a real notification template instead of a plain message, send an
epic inactivity alert for one open epic:

```bash
cd /opt/matorral && sudo -u matorral env DJANGO_SETTINGS_MODULE=matorral.settings.production .venv/bin/python manage.py shell -c "
from apps.issues.models import Epic
from apps.notifications.tasks import send_epic_inactivity_email
e = Epic.objects.filter(assignee__isnull=False).exclude(status__in=['done','wont_do','archived']).first()
print('epic:', e.key, '-> to:', e.assignee.email)
print('result:', send_epic_inactivity_email(e.pk, e.assignee_id, 9))
"
```

`result: False` is not necessarily a fault — it also means a guard declined the
send (the recipient opted out, or the epic reached a terminal status before
delivery). The service log says which.

---

## When a deploy goes wrong

```bash
sudo journalctl -u matorral-django -n 50 --no-pager
sudo journalctl -u matorral-celery -n 50 --no-pager
```

Follow them live while reproducing:

```bash
sudo journalctl -u matorral-django -f
```

### Roll back

```bash
cd /opt/matorral
sudo -u matorral git log --oneline -5           # note the commit to return to
sudo -u matorral git reset --hard <commit>
sudo systemctl restart matorral-django matorral-celery
```

A rollback that crosses a migration needs the migration reversed first —
`manage.py migrate <app> <previous_migration>` — before the code is reset, or
the old code meets a newer schema.

---

## Diagnosing a 404 on an emailed link

A link that 404s in a browser is usually authorisation, not routing. Workspace
views are wrapped in `@login_and_workspace_membership_required`, which returns
**404 rather than 403** for a user who is not a member — it hides the existence
of resources they cannot see, so a 404 does not distinguish "missing" from
"not yours".

Check the layers separately:

```bash
# Does Django serve it to an authorised user?
cd /opt/matorral && sudo -u matorral env DJANGO_SETTINGS_MODULE=matorral.settings.production .venv/bin/python manage.py shell -c "
from django.test import Client
from django.contrib.auth import get_user_model
from apps.issues.models import Epic
e = Epic.objects.get(key='SWA-1')
u = get_user_model().objects.get(email='someone@example.com')
c = Client(SERVER_NAME='pmt.siresoft.net')   # must be in ALLOWED_HOSTS
print('anonymous :', c.get(e.get_absolute_url()).status_code)
c.force_login(u)
print('logged in :', c.get(e.get_absolute_url()).status_code)
"
```

`Client()` without `SERVER_NAME` sends `Host: testserver` and every request
returns **400 DisallowedHost** — an artefact of the test client, not a fault in
the app.

```bash
# Is it Django or nginx answering?
curl -s -o /dev/null -w "gunicorn: %{http_code}\n" -H "Host: pmt.siresoft.net" http://127.0.0.1:8080/w/sr/p/SWA/issues/SWA-1/
curl -s -o /dev/null -w "nginx   : %{http_code}\n" -H "Host: pmt.siresoft.net" http://127.0.0.1/w/sr/p/SWA/issues/SWA-1/
```

- Both `302` — the app is fine; the browser 404 was a session or membership
  issue.
- gunicorn `302`, nginx `404` — nginx is not routing that hostname to the app.
- Both `404` — the URL genuinely does not resolve.

A plain, unstyled "404 Not Found" page with `nginx/1.27` in the footer came
from nginx; a styled one came from Django. That detail alone identifies the
layer.

```bash
# Which workspaces can a user actually see?
cd /opt/matorral && sudo -u matorral env DJANGO_SETTINGS_MODULE=matorral.settings.production .venv/bin/python manage.py shell -c "
from django.contrib.auth import get_user_model
for u in get_user_model().objects.all():
    print(f'{u.email:35}', [m.workspace.slug for m in u.workspace_memberships.all()])
"
```

---

## nginx

Config lives at `/etc/nginx/conf.d/matorral.conf`: `server_name
pmt.siresoft.net`, proxying to `127.0.0.1:8080`.

```bash
sudo nginx -t                      # validate before reloading
sudo systemctl reload nginx        # reload, no dropped connections
sudo grep -n "server_name\|proxy_pass" /etc/nginx/conf.d/matorral.conf
```

Reload rather than restart; a restart drops in-flight requests for no benefit.

---

## Secrets

`.env` holds `EMAIL_HOST_PASSWORD` in plaintext and must never be committed.
Confirm git is ignoring it:

```bash
cd /opt/matorral && git check-ignore -v .env
```

No output means it is **not** ignored — fix that before the next `git add`.

When rotating the mail password: change it on the mail server, update `.env`,
restart both services, then send a test email. Delete any `.env.bak` left
behind — a backup holds the old secret just as readably as the original.
