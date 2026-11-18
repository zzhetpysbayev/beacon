# Beacon

[![CI](https://github.com/zzhetpysbayev/beacon/actions/workflows/ci.yml/badge.svg)](https://github.com/zzhetpysbayev/beacon/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)

**Beacon** is a self-hosted uptime monitor for websites and HTTP APIs. It checks your endpoints on a schedule, tracks uptime and response time, warns you before SSL certificates expire, and sends alerts to Telegram. Several organizations can share one installation, and each keeps its monitors, contacts and history isolated from the others.

## Features

- HTTP(S) checks with a configurable method, expected status code, optional keyword in the response body, timeout and interval
- SSL certificate expiry tracking with a configurable warning threshold
- Uptime over the last 24 hours, 7 days and 30 days, check history and response times
- Organizations (tenants) with isolated monitors, alert contacts and history
- Team invitations through one-time links, with owner and member roles
- Telegram alerts when a site goes down, reminders while it stays down, and recovery notices
- Passwords stored as scrypt hashes using the Python standard library
- A small session-authenticated JSON API for monitor status
- One container image, PostgreSQL storage, one process with a built-in scheduler

## Quick start with Docker Compose

```bash
git clone https://github.com/zzhetpysbayev/beacon.git
cd beacon
cp .env.example .env
# Edit .env: set SECRET_KEY, INITIAL_ORG_NAME, INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD
docker compose up --build
```

Open <http://localhost:8000> and log in with the initial admin account. Docker Compose starts PostgreSQL alongside the app and points `DATABASE_URL` at it.

## Running without Docker

You need Python 3.13 and a PostgreSQL 14+ database.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DATABASE_URL=postgresql://beacon:beacon@127.0.0.1:5432/beacon
export INITIAL_ORG_NAME="My Company"
export INITIAL_ADMIN_USERNAME=admin
export INITIAL_ADMIN_PASSWORD='change-me-now'

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Configuration

All settings are environment variables. See [`.env.example`](.env.example) for a commented template.

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | required | Signs session cookies |
| `DATABASE_URL` | required | PostgreSQL connection string |
| `INITIAL_ORG_NAME`, `INITIAL_ADMIN_USERNAME`, `INITIAL_ADMIN_PASSWORD` | empty | First organization and owner. Created once, only when the database has no users |
| `TIMEZONE` | `UTC` | Time zone used when displaying timestamps |
| `COOKIE_SECURE` | `0` | Set to `1` when served over HTTPS |
| `FAIL_THRESHOLD` | `2` | Consecutive failed checks before a monitor is marked down |
| `RENOTIFY_MINUTES` | `60` | Interval between repeated alerts while a monitor is down |
| `SSL_WARN_DAYS` | `14` | Days before expiry to warn about an SSL certificate |
| `HISTORY_DAYS` | `90` | How long check history is kept |
| `INVITE_DAYS` | `7` | How long an invitation link stays valid |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET` | empty | Telegram bot for alerts. Optional |

## Users and organizations

The first owner is created from the `INITIAL_*` variables at startup. Restarts do nothing once a user exists, so the variables can stay set. The password must be at least 8 characters.

To create another organization with its owner from the command line:

```bash
python manage.py create-owner --org "Another Company" --username alice
```

If you omit `--password`, a random password is generated and printed once. Owners invite other members from the **Team** page. Invitation links are single-use and expire after `INVITE_DAYS`.

## Telegram alerts

1. Create a bot with [@BotFather](https://t.me/BotFather) and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME`.
2. Choose a random `TELEGRAM_WEBHOOK_SECRET` and register the webhook. Telegram requires a public HTTPS address:

   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<your-domain>/telegram/webhook/<SECRET>"
   ```

3. In **Contacts**, create a contact, open its link, and press **Start** in the bot. The chat is now linked to the contact.

Alerts go to every verified contact attached to a monitor.

## Deployment

Beacon is a single container. Run it behind a reverse proxy that terminates TLS, and set `COOKIE_SECURE=1`. Start the container with `--proxy-headers` (already set in the `Dockerfile`) so that client addresses are read from the proxy.

Keep exactly one replica. The background scheduler runs inside each process, so several replicas would run every check several times. Use a `Recreate` rollout strategy, or accept a short gap during upgrades.

The image includes a health check on `/login`. The container listens on port 8000.

## Development

Run the end-to-end smoke test against an empty PostgreSQL database:

```bash
createdb beacon_test
DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/beacon_test \
SECRET_KEY=test-key-123456 \
TELEGRAM_WEBHOOK_SECRET=hook TELEGRAM_BOT_USERNAME=bot \
INITIAL_ORG_NAME="Example Org" INITIAL_ADMIN_USERNAME=admin INITIAL_ADMIN_PASSWORD='Secret123!' \
PYTHONPATH=. python tests/smoke_test.py
```

The test starts a local HTTP server, runs the app with its scheduler, and covers login, tenant isolation, invitations, monitor checks, keyword matching and Telegram binding. CI runs the same test on every push and pull request.

To change the database schema, add a new entry to `MIGRATIONS` in `app/db.py`. Never edit a migration that has already shipped.

## Project layout

```
app/
  main.py        Routes, web UI, JSON API and Telegram webhook
  scheduler.py   Background loop: runs checks, updates status, sends alerts
  checker.py     One HTTP check and TLS certificate expiry lookup
  db.py          PostgreSQL schema, migrations and queries
  notify.py      Telegram sender
  auth.py        Password hashing
  config.py      Settings from environment variables
  templates/     Jinja2 templates
manage.py        Administrative commands
tests/           End-to-end smoke test
```

## Known limitations

- One process with an in-process scheduler: running more than one replica is not supported yet.
- Each user belongs to one organization, and there is no way to switch organizations.
- There is no password change page yet.
- Login has no rate limiting. Add it at the reverse proxy or ingress.
- A new database connection is opened for each operation. Add a connection pool for high load.
- Timestamps are stored as ISO 8601 text.

## Contributing

Issues and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first. To report a security issue, follow [SECURITY.md](SECURITY.md) and do not open a public issue.

## License

Copyright 2026 zzhetpysbayev

Beacon is licensed under the [Apache License 2.0](LICENSE).
