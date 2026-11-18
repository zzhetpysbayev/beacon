# Contributing to Beacon

Thanks for your interest in Beacon. Bug reports, feature ideas, documentation fixes and pull requests are all welcome.

## Reporting bugs and requesting features

Open an issue on GitHub and use the matching template. For bugs, include the Beacon version or commit, how you run it, and the relevant logs. Remove secrets (`SECRET_KEY`, database passwords, bot tokens) before pasting anything.

Security problems should not go in public issues. See [SECURITY.md](SECURITY.md).

## Development setup

1. Fork the repository and clone your fork.
2. Create a virtual environment and install the dependencies:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Start a local PostgreSQL database with `docker compose up -d db`. It listens on `127.0.0.1:5432` with user `beacon` and password `beacon`. Create the test database once with `docker compose exec db createdb -U beacon beacon_test`. Any PostgreSQL 14+ server works too.
4. Run the smoke test as described in the [README](README.md#development), using `postgresql://beacon:beacon@127.0.0.1:5432/beacon_test` as `DATABASE_URL`.

## Making a change

- Create a branch from `main` with a short descriptive name, for example `fix/keyword-check-timeout`.
- Keep pull requests focused. One logical change per PR is easier to review.
- Add or update a check in `tests/smoke_test.py` when you change behavior.
- Do not edit a database migration that has already been released. Add a new entry to `MIGRATIONS` in `app/db.py`.
- Write user-facing text, code comments and documentation in English.
- Update `README.md`, `.env.example` and `CHANGELOG.md` when you change configuration or behavior.

## Commit messages

Write short, imperative subject lines, for example `Add SSL warning threshold setting`. Use the body to explain why when the reason is not obvious.

## Pull requests

Fill in the pull request template. Make sure CI is green before asking for review. A maintainer will review your PR and may ask for changes.

## Code of conduct

Participation in this project is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
