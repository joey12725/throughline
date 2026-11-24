# Contributing to Throughline

Thanks for helping. Throughline stores someone's whole life, so the bar for
changes is correctness and privacy first, features second. Please read the
[design scope](docs/design-scope.md) before proposing larger changes.

By contributing you agree that your contribution is licensed under the
license of the directory it lands in: GPL-3.0-or-later for the core,
Apache-2.0 for `sdk/` and the plugin templates (see [NOTICE](NOTICE)).
Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Never use real personal data

Issues, pull requests, tests, fixtures, screenshots and logs must use
**invented data only**: the public twin (`python -m fixtures --seed 47`), the
zoo (`python -m fixtures.zoo`), the benchmark twin (`bench/`) or records you
make up. No real names, messages, e-mail addresses, phone numbers,
locations, hostnames or tokens, including your own. `scripts/leak-check.sh`
runs in CI and fails on known personal identifiers; recorded plugin fixtures
must be synthetic.

## Set up

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 24 with
pnpm (web interface and the Node plugin template) and Docker (for the test
database).

```sh
git clone https://github.com/joey12725/throughline.git throughline
cd throughline
uv sync --all-packages
```

### Test database

Tests create and drop throwaway `tl_test_<random>` databases on an
administrative PostgreSQL 16 server with pgvector. They refuse any URL whose
host is not `127.0.0.1` or whose database is not `postgres`, so they cannot
touch an application database. Start one:

```sh
docker run -d --name throughline-test-pg -p 127.0.0.1:54331:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust pgvector/pgvector:pg16
```

The default `THROUGHLINE_TEST_DATABASE_URL` is
`postgresql://postgres@127.0.0.1:54331/postgres`; set it to use another port.
`THROUGHLINE_DEV=1` lets the code connect without the runtime role passwords
(trust authentication on a local server); never set it in production.

### Run the checks

```sh
uv run ruff check .
THROUGHLINE_DEV=1 uv run pytest -q -n auto --dist loadfile   # pytest-xdist; plain -q works too
cd web && pnpm install --frozen-lockfile && pnpm typecheck && pnpm test && pnpm build
scripts/leak-check.sh
python3 scripts/check-links.py
```

Backup and restore tests need `pg_dump`, `pg_restore` and `psql` of
PostgreSQL 16 or newer on `PATH` (or `THROUGHLINE_PG_BIN`); they skip
otherwise. No model keys or external credentials are needed for any test.

## Making changes

- **Small, focused pull requests**, each with tests. Describe the behaviour
  change and how you verified it.
- **The core never imports plugins or the SDK.** Plugins talk to the core
  only over the [HTTP contract](docs/plugin-http-contract.md).
- **Keep the database the source of truth.** Guarantees (access, idempotency,
  versioning, coverage) belong in PostgreSQL constraints, views and
  transactions, not in agent prose or handler conventions.
- **Access control is not optional.** Any new read path goes through the
  security-barrier `api.*` views or the asker gate; add a case to
  `tests/test_v11_security_matrix.py` for new routes.
- **Document what you change.** Update the matching page under `docs/` and
  `docs/configuration.md` for new settings. Every relative link must resolve
  (`scripts/check-links.py`).
- **Add a line to [CHANGELOG.md](CHANGELOG.md)** under "Unreleased".

### Migrations

Migrations live in `src/throughline/migrations/NNNN_name.sql` and are applied
in filename order. The runner records each applied **file name** in
`tl.schema_migrations` and has no checksum, so:

- Migrations are **forward-only**. To change something, add a new migration;
  never edit the statements of a released one (comment fixes are fine).
- **Never rename or renumber** a migration file: an install that applied the
  old name would apply it again.
- Take the next free number. Gaps are fine.
- A migration must work on an empty database and on an upgraded one, and
  should be safe to run inside one transaction.
- Recipes (`src/throughline/recipes/`) are content-hashed; retire a removed
  recipe explicitly instead of deleting its file.

### Writing a plugin

Start from `plugins/template` (Python, with the SDK) or
`plugins/template-node` (plain Node.js). Read
[docs/writing-a-plugin.md](docs/writing-a-plugin.md), the
[HTTP contract](docs/plugin-http-contract.md) and the
[onboarding contract](docs/plugin-onboarding-contract.md). A plugin in this
repository needs a README (setup, settings, limitations), a license field,
recorded synthetic fixtures and tests that run without network access or
credentials; `throughline-contract` checks the process contract. Plugins can
also live in their own repositories and be installed from a GitHub link in
the onboarding wizard.

## Reporting bugs and proposing features

Use the issue templates. For a bug, include a **synthetic reproduction**:
the commands, the invented records that trigger it, what you expected and
what happened. Security problems go to [SECURITY.md](SECURITY.md), never to
a public issue.
