# Security Policy

## Supported versions

Only the latest commit on the `main` branch receives security fixes.

## Reporting a vulnerability

Please do not open a public issue for security problems. Report them privately instead:

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability** and describe the problem.

Include the Beacon version or commit, the steps to reproduce, and the expected impact. We aim to acknowledge a report within 7 days and to publish a fix or a mitigation plan within 30 days, depending on severity.

## Hardening notes for operators

- Use a long random `SECRET_KEY`, and never commit `.env`.
- Serve Beacon over HTTPS and set `COOKIE_SECURE=1`.
- Rate-limit `/login` and `/invite/*` at your reverse proxy or ingress, because the application does not do this itself.
- Restrict access to PostgreSQL to the application network.
