# =============================================================================
# Matorral Justfile - Task Runner for Development
# https://github.com/casey/just
# =============================================================================
#
# Runs natively — no Docker. Requires PostgreSQL, Redis, Node.js and `uv`
# installed and reachable locally (see README.md / deploy/README.md).
#
# Quick start:
#   just init              # First-time setup
#   just start             # Start all services
#   just test              # Run the test suite
#
# For LLMs/Agents:
#   just --list            # List all recipes with descriptions
#   just check             # Run all checks (tests, migrations, pre-commit)
#   just doctor            # Verify environment is ready
# =============================================================================

# Default: List all available recipes with descriptions
[private]
default:
    @just --list --unsorted

# =============================================================================
# Development Environment
# =============================================================================

# First-time setup: copy .env, install deps, migrate
[doc("Initialize project for first-time development")]
init:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "🚀 Initializing Matorral development environment..."
    if [ ! -f .env ]; then
        echo "📋 Copying .env.example to .env..."
        cp .env.example .env
        echo "   Edit .env: set DATABASE_URL/REDIS_URL if not using the defaults."
    fi
    echo "📦 Installing Python dependencies (uv sync)..."
    uv sync
    echo "📦 Installing Node dependencies..."
    npm install
    echo "🗄️  Running migrations..."
    just migrate
    echo "✅ Initialization complete! Run 'just doctor' to verify."

# Verify environment is ready (check .env, venv, DB connectivity, migrations)
[doc("Check if development environment is properly configured")]
doctor:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "🔍 Running environment checks..."
    FAILED=0
    if [ ! -f .env ]; then
        echo "❌ .env file missing - run 'just init'"
        FAILED=1
    else
        echo "✅ .env file exists"
    fi
    if uv run python -c "import django" 2>/dev/null; then
        echo "✅ Virtualenv OK (Django importable)"
    else
        echo "❌ Virtualenv not ready - run 'just init' (uv sync)"
        FAILED=1
    fi
    if uv run python manage.py showmigrations --plan >/dev/null 2>&1; then
        echo "✅ Database reachable"
        if uv run python manage.py showmigrations --plan 2>/dev/null | grep -q "\[ \]"; then
            echo "⚠️  Unapplied migrations found - run 'just migrate'"
        else
            echo "✅ All migrations applied"
        fi
    else
        echo "❌ Cannot reach the database - check DATABASE_URL in .env and that PostgreSQL is running"
        FAILED=1
    fi
    if [ $FAILED -eq 0 ]; then
        echo "🎉 Environment is healthy!"
        exit 0
    else
        echo "❌ Environment has issues - see above"
        exit 1
    fi

# Refresh the virtualenv after pyproject.toml/uv.lock changes
[doc("Refresh the virtualenv after dependency changes")]
rebuild:
    uv sync
    @echo "✅ Virtualenv refreshed."

# =============================================================================
# Dev Process Lifecycle (Django + Celery + Vite)
# =============================================================================

# Start Django, Celery and Vite together in the foreground (Ctrl+C stops all)
[doc("Start all services in foreground (logs visible, Ctrl+C to stop)")]
start:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
    uv run python manage.py runserver 0.0.0.0:8000 &
    uv run celery -A matorral worker -l INFO --beat --pool=prefork --concurrency=2 &
    npm run dev -- --host &
    wait

# Start the same three processes in the background
[doc("Start all services in background")]
start-detached:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p .dev-pids
    nohup uv run python manage.py runserver 0.0.0.0:8000 > .dev-pids/django.log 2>&1 & echo $! > .dev-pids/django.pid
    nohup uv run celery -A matorral worker -l INFO --beat --pool=prefork --concurrency=2 > .dev-pids/celery.log 2>&1 & echo $! > .dev-pids/celery.pid
    nohup npm run dev -- --host > .dev-pids/vite.log 2>&1 & echo $! > .dev-pids/vite.pid
    echo "✅ Services started. Logs in .dev-pids/*.log. Use 'just status' / 'just stop'."

# Stop the background processes started by start-detached
[doc("Stop all services")]
stop:
    #!/usr/bin/env bash
    set -euo pipefail
    for name in django celery vite; do
        pidfile=".dev-pids/$name.pid"
        if [ -f "$pidfile" ]; then
            pid="$(cat "$pidfile")"
            if kill "$pid" 2>/dev/null; then
                echo "🛑 Stopped $name (pid $pid)"
            fi
            rm -f "$pidfile"
        fi
    done

# Restart all processes in the foreground (runs sequentially)
[doc("Restart all services in foreground")]
restart:
    just stop
    just start

# Restart all processes in the background
[doc("Restart all services in background")]
restart-detached:
    just stop
    just start-detached

# Show status of the background dev processes
[doc("Show status of all services")]
status:
    #!/usr/bin/env bash
    set -euo pipefail
    for name in django celery vite; do
        pidfile=".dev-pids/$name.pid"
        if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
            echo "✅ $name running (pid $(cat "$pidfile"))"
        else
            echo "❌ $name not running"
        fi
    done

# =============================================================================
# Django Commands
# =============================================================================

# Run arbitrary Django management command (e.g., `just manage shell`, `just manage dbshell`)
[doc("Run any Django management command: just manage <command>")]
manage *args:
    uv run python manage.py {{args}}

# Apply pending Django database migrations
[doc("Apply database migrations")]
migrate:
    uv run python manage.py migrate

# Generate new Django database migrations
[doc("Create new database migrations")]
make-migrations *args:
    uv run python manage.py makemigrations {{args}}

# Check for missing migrations (CI-friendly)
[doc("Check for uncreated migrations (CI-friendly)")]
check-migrations:
    uv run python manage.py makemigrations --check --dry-run

# Open an interactive Django Python shell
[doc("Open Django shell")]
shell:
    uv run python manage.py shell

# Open a PostgreSQL database shell (uses DATABASE_URL from .env)
[doc("Open PostgreSQL shell")]
dbshell:
    uv run python manage.py dbshell

# Run the Django createsuperuser management command
[doc("Create a superuser interactively")]
createsuperuser:
    uv run python manage.py createsuperuser

# Promote an existing user to staff and superuser by email
[doc("Promote user to superuser: just make-superuser <email>")]
make-superuser email:
    uv run python manage.py make_superuser {{email}}

# Load fixture data (e.g., `just loaddata initial_data`)
[doc("Load fixture data: just loaddata <fixture_name>")]
loaddata *args:
    uv run python manage.py loaddata {{args}}

# Dump data to fixture (e.g., `just dumpdata auth.User > users.json`)
[doc("Dump data to fixture")]
dumpdata *args:
    uv run python manage.py dumpdata {{args}}

# =============================================================================
# Testing
# =============================================================================

# Run Django tests (e.g., `just test apps.issues`, `just test apps.issues.tests.test_models`)
[doc("Run Django tests: just test [path.to.module]")]
test *args:
    uv run python manage.py test {{args}}

# Run tests under coverage
[doc("Run tests with coverage reporting")]
test-cov *args:
    uv run coverage run manage.py test apps {{args}}

# Generate coverage JSON + terminal report
[doc("Generate coverage reports")]
cov-report:
    uv run coverage json && uv run coverage report

# Run tests under coverage and generate reports
[doc("Run tests with coverage and generate reports")]
cov *args: (test-cov args) cov-report

# =============================================================================
# Code Quality
# =============================================================================

# Run pre-commit on all files
[doc("Run all pre-commit hooks on all files")]
pre-commit:
    pre-commit run --all-files

# Run pre-commit on staged files only
[doc("Run pre-commit on staged files only")]
pre-commit-staged:
    pre-commit run

# Run linter (ruff) via pre-commit
[doc("Run linter (ruff) on all files")]
lint:
    pre-commit run ruff --all-files

# Run code formatter (ruff format) via pre-commit
[doc("Run code formatter on all files")]
fmt:
    pre-commit run ruff-format --all-files

# Run all checks (tests, migrations check, pre-commit)
[doc("Run complete check suite (tests, migrations, pre-commit)")]
check: test check-migrations pre-commit
    @echo "✅ All checks passed!"

# =============================================================================
# Translations
# =============================================================================

# Extract and compile Django translation messages (.po/.mo files)
[doc("Update translation files (.po/.mo)")]
make-translations:
    uv run python manage.py makemessages --all --ignore node_modules --ignore venv --ignore .venv
    uv run python manage.py makemessages -d djangojs --all --ignore node_modules --ignore venv --ignore .venv
    uv run python manage.py compilemessages --ignore venv --ignore .venv

# =============================================================================
# Frontend (Node.js/Vite)
# =============================================================================

# Install all Node.js dependencies
[doc("Install all Node.js dependencies")]
npm-install-all:
    npm install

# Install specific Node.js packages (e.g., `just npm-install react`)
[doc("Install specific npm packages: just npm-install <package>")]
npm-install *args:
    npm install {{args}}

# Uninstall specific Node.js packages (e.g., `just npm-uninstall react`)
[doc("Uninstall specific npm packages: just npm-uninstall <package>")]
npm-uninstall *args:
    npm uninstall {{args}}

# Build frontend assets for production using Vite
[doc("Build frontend assets for production")]
npm-build:
    npm run build

# Start the Vite development server for frontend assets
[doc("Start Vite dev server (foreground)")]
npm-dev:
    npm run dev

# Run TypeScript type checking on frontend code
[doc("Run TypeScript type checker")]
npm-type-check:
    npm run type-check

# Run TypeScript type checking in watch mode
[doc("Run TypeScript type checker in watch mode")]
npm-type-check-watch:
    npm run type-check-watch

# =============================================================================
# Production Deployment (systemd + nginx, see deploy/README.md)
# =============================================================================

# Restart the production services
[doc("Restart the production services (django + celery)")]
prod-restart:
    sudo systemctl restart matorral-django matorral-celery

# Show status of the production services
[doc("Show status of the production services")]
prod-status:
    sudo systemctl status matorral-django matorral-celery nginx --no-pager

# Follow Django production logs
[doc("Follow Django production logs")]
prod-logs-django:
    sudo journalctl -u matorral-django -f

# Follow Celery production logs
[doc("Follow Celery production logs")]
prod-logs-celery:
    sudo journalctl -u matorral-celery -f

# Follow nginx production logs
[doc("Follow nginx production logs")]
prod-logs-nginx:
    sudo journalctl -u nginx -f

# =============================================================================
# Cleanup
# =============================================================================

# Remove the virtualenv and node_modules (does NOT touch the database)
[doc("Remove virtualenv and node_modules - does not touch the database")]
clean:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "⚠️  This removes .venv and node_modules. The database is left untouched."
    read -p "Are you sure? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf .venv node_modules
        echo "✅ Cleanup complete. Run 'just init' to start fresh."
    else
        echo "❌ Cancelled."
    fi

# =============================================================================
# Legacy/Deprecated (kept for compatibility)
# =============================================================================

# Refresh the virtualenv (alias for 'just rebuild')
[doc("Alias for 'just rebuild'")]
requirements: rebuild
