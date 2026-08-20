# Running Locally (without Docker)

This machine cannot run Docker: **CPU virtualization is disabled in the BIOS/UEFI**,
so WSL2 (and therefore Docker Desktop) will not start. The documented `just init`
workflow needs Docker, so this project is set up to run natively instead.

To restore the standard Docker workflow, see "Re-enabling Docker" at the bottom.

## What's installed

| Component | Where |
|---|---|
| Python venv | `.venv/` (Python 3.14.5) |
| PostgreSQL | Native Windows service `postgresql-x64-18`, port 5432, DB `matorral` |
| Redis | **Not installed** — not required for normal page loads (see Limitations) |
| Frontend | Built into `static/` via `npm run build` |

## Start the app

```bash
.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8000
```

Open <http://127.0.0.1:8000>

**Login** (email-only — the username will not work):

| Field | Value |
|---|---|
| Email | `admin@siresoft.com` |
| Password | `siresoft2468` |

Seeded data: workspace `siresoft`, project `Demo Project` (key `DP`), epic `DP-1`
with child story `DP-2`.

## Common commands

Replace `just <recipe>` from AGENTS.md with these native equivalents:

```bash
.venv/Scripts/python.exe manage.py migrate
.venv/Scripts/python.exe manage.py makemigrations
.venv/Scripts/python.exe manage.py test apps
.venv/Scripts/python.exe manage.py shell
.venv/Scripts/python.exe manage.py createsuperuser

npm run build        # rebuild frontend after asset changes
npm run dev          # Vite watch mode (set DJANGO_VITE_DEV_MODE=True in .env)
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format .
```

Note: `manage.py test` sets `CELERY_TASK_ALWAYS_EAGER`, so tests do not need a
Celery worker. They **do** need PostgreSQL running, and the `postgres` user must
be able to create the `test_matorral` database.

## Dependency pins that differ from `pyproject.toml`

`pyproject.toml` leaves these unpinned, and their current releases are
incompatible with this codebase. The working versions are captured in
`requirements-local.txt`:

- **`django-health-check==3.20.0`** — 4.5.0 removed the `health_check.db`,
  `health_check.contrib.celery` and `health_check.contrib.redis` submodules that
  `INSTALLED_APPS` still lists, so Django fails to start on 4.x.
- **`django-treebeard==5.3.1`** — 6.0+ turns the `treebeard.E001` manager check
  into a hard error. `IssueManager` must subclass `PolymorphicManager` (for
  django-polymorphic) rather than `MP_NodeManager`, so it cannot satisfy that
  check. On 5.3.1 it is a warning and everything works; on 6.x/7.x the app will
  not boot.

The seven `treebeard.E001` warnings at startup are expected and harmless.

To recreate this environment exactly:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-local.txt
```

## Two fixes applied to make the UI render

**1. `DJANGO_VITE_DEV_MODE=False` in `.env`.** This setting defaults to `DEBUG`
(True), which makes templates emit asset URLs pointing at the Vite dev server on
`http://localhost:5173`. That server only runs under Docker, so every stylesheet
and script 404'd and the site rendered as unstyled HTML. With it set to `False`,
`django-vite` reads `static/.vite/manifest.json` and serves the built bundles
from `/static/` instead.

If you later run `npm run dev`, flip this back to `True`.

**2. Placeholder branding images.** `static/` is gitignored
(`.gitignore:21`), so two images the fork's templates reference were never
committed and do not exist in this clone:

- `static/images/siresoftlogo.png` — favicon, apple-touch-icon, sidebar logo
- `static/images/breadcrumb-bg.jpg` — landing page and `anonymous_base.html` hero

I generated solid `#0525A8` placeholders so the layout renders correctly.
**Replace them with the real artwork** — copy the originals over these files;
no template changes are needed. Because `static/` is gitignored, the real images
must be shared out-of-band or the ignore rule narrowed to keep `static/images/`
tracked.

## Limitations without Docker

- **No Redis**, so no Celery worker/beat. Everything in the UI works because
  `DEBUG=True` uses a dummy cache, but these background features are inert:
  - auto-creating next sprints (`apps.sprints.tasks.create_next_sprints`)
  - nightly demo-data reset (`apps.workspaces.tasks.reset_demo_workspace_data`)
  - **cross-workspace project move** (`apps/projects/tasks.py`) — the UI accepts
    it and returns, but the move never runs.

  To enable these, install Redis (e.g. Memurai for Windows, or `redis-server` in
  WSL once virtualization is on) and run:
  `.venv/Scripts/celery.exe -A matorral worker -l INFO --beat --pool=solo`
- **Vite dev server** is not running; `static/` holds a production build. Re-run
  `npm run build` after changing anything in `assets/`.

## Re-enabling Docker

1. Reboot into BIOS/UEFI (usually Del / F2 / F10 at boot).
2. Enable the CPU virtualization option — `Intel VT-x` / `Intel Virtualization
   Technology`, or `AMD-V` / `SVM Mode`. It is often under Advanced → CPU
   Configuration.
3. Save and boot into Windows, then in an **admin** PowerShell:
   ```powershell
   wsl --install --no-distribution
   ```
   Reboot again if prompted.
4. Verify: `wsl --status` should succeed, and
   `(Get-CimInstance Win32_ComputerSystem).HypervisorPresent` should be `True`.
5. Start Docker Desktop, then use the documented workflow. Before running
   `just init`, restore the Docker hostnames in `.env`:
   ```
   DATABASE_URL="postgresql://postgres:postgres@db:5432/matorral"
   REDIS_URL="redis://redis:6379"
   ```
   **`just init` deletes the existing database**, so back up first if you want to
   keep the local data.
