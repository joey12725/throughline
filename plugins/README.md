# Plugins

Every connector that ships with Throughline lives here, one directory per
plugin, each its own Python package (`throughline-<name>`) built on the
[SDK](../sdk). They are members of the repository's uv workspace, so
`uv sync --all-packages` installs all of them for development.

- [`throughline-plugins.toml`](throughline-plugins.toml) is the catalog the
  web onboarding reads (format: [docs/plugin-onboarding-contract.md](../docs/plugin-onboarding-contract.md)).
  It names each plugin, its channels and their roles, the settings and
  credentials it needs, and how to install it.
- [`catalog.json`](catalog.json) is the same catalog as JSON for other
  installers, plus the bundled pieces that are not onboarding entries
  (`capture`, `media`). It is generated: edit the TOML, then run
  `python3 plugins/build_catalog.py` (`--check` verifies it is current).
- [`template`](template) (Python) and [`template-node`](template-node) (plain
  Node) are starting points for a new plugin; see
  [docs/writing-a-plugin.md](../docs/writing-a-plugin.md). Both are
  Apache-2.0 so your plugin can use any license; the plugins in this directory
  are GPL-3.0-or-later unless their `pyproject.toml` says otherwise.

Run one plugin's tests:

```sh
uv sync --all-packages
THROUGHLINE_DEV=1 uv run pytest -q plugins/<name>
```

Tests use recorded, invented fixtures only; tests that need a database create
a throwaway one on the test Postgres (`THROUGHLINE_TEST_DATABASE_URL`).

## Remote-fetch pattern

Several plugins (calendar, email, consumption, notion, reddit, spotify,
homeassistant, hermes, instagram-feed and others) are split in two layers:

1. **A fetcher that uses only the Python standard library** (`fetch.py`, or
   `remote_fetch.py` / `remote_snapshot.py`). It has no third-party imports,
   so you can copy that one file to whichever machine holds the credentials
   (a server, a container, a NAS) and run it there with nothing but Python
   3.12+:

   ```sh
   python3 fetch.py export --out export-$(date -u +%Y%m%dT%H%M%SZ).jsonl --state state.json
   ```

   It reads credentials only from environment variables (or, where offered,
   from a command you name that prints them, such as your secret manager),
   never from the command line, and never writes them to its output, state or
   logs. It writes raw JSONL records plus a small non-secret incremental state
   file.

2. **The plugin** maps those raw records to Throughline messages. Point it at
   the shipped files with `<PLUGIN>_SOURCE=export:<file or directory>` (or the
   plugin's equivalent setting), or let it run the same fetcher in process
   (`<PLUGIN>_SOURCE=live`) when Throughline itself holds the credentials.

So the secret stays on the host that already has it, and only data and
non-secret state travel to Throughline. How you move the files (scp, rsync,
a shared volume, a scheduled job) is up to you; re-reading an export is
harmless because the mapping is idempotent.
