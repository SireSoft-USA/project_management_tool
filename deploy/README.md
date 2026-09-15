# Native (non-Docker) deployment

This app now runs directly on the host — no Docker. Layout assumed below:

- App user/group: `matorral`
- App directory: `/opt/matorral`
- Python venv: `/opt/matorral/.venv` (created by `uv`)
- Gunicorn listens on `127.0.0.1:8080`; nginx reverse-proxies to it on port 80.

Commands below give both Debian/Ubuntu (`apt`) and RHEL/Rocky/CentOS 9
(`dnf`) variants. Service names differ between them — this matters for the
`After=`/`Wants=` lines in `deploy/systemd/*.service`, which are written for
RHEL/CentOS (`postgresql-17.service`, `redis.service`); on Debian/Ubuntu
change those to `postgresql.service` and `redis-server.service`.

## 1. Install system packages

**Debian/Ubuntu:**
```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib redis-server nginx \
    build-essential libpq-dev gettext git curl

# Node.js (for building frontend assets)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs

# uv (Python package/venv manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**RHEL/Rocky/CentOS 9** (the base OS package is PostgreSQL 13 — this project
needs 17, installed from the official PGDG repo instead):
```bash
sudo dnf install -y redis nginx gcc libpq-devel gettext git curl

sudo dnf -qy module disable postgresql
sudo dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm
sudo dnf install -y postgresql17-server postgresql17-contrib
sudo /usr/pgsql-17/bin/postgresql-17-setup initdb
sudo systemctl enable --now postgresql-17
sudo systemctl enable --now redis
sudo systemctl enable --now nginx

# Node.js
curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash -
sudo dnf install -y nodejs

# uv
curl -LsSf https://astral.sh/uv/install.sh | sh
```
`psql`/`createuser`/`createdb` live under `/usr/pgsql-17/bin/` on this path —
use the full path, or add it to `PATH`.

## 2. Create the app user and directory

```bash
sudo useradd --system --create-home --shell /bin/bash matorral
sudo mkdir -p /opt/matorral
sudo chown matorral:matorral /opt/matorral
```

## 3. Get the code

```bash
sudo -u matorral git clone <repo-url> /opt/matorral
cd /opt/matorral
```
(For an existing checkout, `sudo -u matorral git pull` instead.)

## 4. PostgreSQL database

```bash
sudo -u postgres createuser --pwprompt matorral   # set a password when prompted
sudo -u postgres createdb --owner=matorral matorral
```

## 5. Environment

```bash
sudo -u matorral cp .env.example .env
sudo -u matorral $EDITOR .env
```

Set at minimum:
- `DEBUG=False` (must be explicit — see the comment in `.env.example`; a
  missing `DEBUG` crashes every management command with
  `ModuleNotFoundError: No module named 'daphne'`)
- `SECRET_KEY` (unique, random)
- `DATABASE_URL="postgresql://matorral:<password>@localhost:5432/matorral"`
- `REDIS_URL="redis://localhost:6379"`
- `DOMAIN=` your domain or server IP
- `SECURE_SSL_REDIRECT=False` (this deployment is plain HTTP — see the
  comment already in `.env.example` for why this matters for login/CSRF)
- `ALLOWED_HOSTS` set to your domain (defaults to `*` otherwise)
- `ENABLE_DEBUG_TOOLBAR=False` (it isn't installed in the prod dependency
  group — leaving this `True` will crash the app)

## 6. Python dependencies

```bash
sudo -u matorral /root/.local/bin/uv sync --frozen --no-group dev --group prod
```
(Adjust the `uv` path if it installed elsewhere — check `which uv` under that user.)

## 7. Frontend build + static files

```bash
sudo -u matorral npm ci
sudo -u matorral npm run build
sudo -u matorral env DJANGO_SETTINGS_MODULE=matorral.settings.production \
    .venv/bin/python manage.py collectstatic --noinput
```

## 8. systemd services

```bash
sudo cp deploy/systemd/matorral-django.service deploy/systemd/matorral-celery.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now matorral-django matorral-celery
sudo systemctl status matorral-django matorral-celery
```

`matorral-django.service` runs `manage.py migrate` automatically before
starting Gunicorn on every start/restart.

## 9. nginx

```bash
sudo cp deploy/nginx/matorral.conf /etc/nginx/sites-available/matorral
sudo sed -i "s/DOMAIN_PLACEHOLDER/pmt.siresoft.net/" /etc/nginx/sites-available/matorral
sudo ln -s /etc/nginx/sites-available/matorral /etc/nginx/sites-enabled/matorral
sudo rm -f /etc/nginx/sites-enabled/default   # avoid it winning for unmatched Host headers
sudo nginx -t
sudo systemctl reload nginx
```

## 10. Firewall

```bash
sudo ufw allow 80/tcp     # only 80 — this deployment has no SSL/443
```

## 11. Verify

```bash
curl -I http://<domain-or-ip>/
sudo journalctl -u matorral-django -f
sudo journalctl -u matorral-celery -f
```

---

## Migrating existing data out of Docker

If this replaces a running Docker deployment (`docker-compose.prod.yml`),
move its data over **before** decommissioning the containers:

```bash
# 1. Dump the Postgres database from the running container
docker compose -f docker-compose.prod.yml exec -T db \
    pg_dump -U postgres -d matorral --no-owner --clean --if-exists \
    > /tmp/matorral.sql

# 2. Restore it into the new native Postgres
sudo -u postgres psql -d matorral -f /tmp/matorral.sql

# 3. Copy media files (docker-compose.prod.yml already bind-mounts these
#    to ./media on the host, so this is just a directory copy)
sudo -u matorral cp -r /path/to/old/checkout/media/. /opt/matorral/media/

# 4. Bring the native services up (steps 8-9 above), verify the app works,
#    THEN stop the old containers:
docker compose -f docker-compose.prod.yml down
```

Keep the old Docker volumes untouched (don't `down -v`) until the native
deployment has been verified working for a few days.
