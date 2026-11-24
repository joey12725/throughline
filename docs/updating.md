# Updating Throughline

An update keeps everything: your records, tokens, people, connector
progress, backup settings and every desktop's pairing. Nothing has to be
re-entered or re-paired.

## Server (Docker Compose)

```sh
scripts/update.sh
```

That is the whole update. It:

1. fetches the new version and lists what changed (`--check` stops here);
2. builds the new images while the current version keeps serving;
3. pauses the API and connectors, takes a **pre-upgrade snapshot** of the
   database if the update brings migrations, and migrates;
4. starts the new version (and the `connectors` / `maintenance` services if
   they were running) and waits until the API reports healthy.

If the build fails, nothing changes. If a migration fails, the script
restores the snapshot and puts the previous version back on its own: you
end up exactly where you started. Desktops and connectors retry while the API
is paused and catch up afterwards; plugin cursors live in the database, so no
record is skipped or ingested twice.

To undo an update that worked but that you don't want:

```sh
scripts/update.sh --rollback
```

It restores the database from the snapshot of the last update (asking first:
anything stored since that update is lost) and runs the previous version
again. The next `scripts/update.sh` goes forward again.

### What is kept, and where

| Kept in | What |
|---|---|
| `postgres` volume | the database: every record, token, person, cursor, setting stored in Throughline |
| `throughline-home` volume | `~/.throughline` of the API, migrate and maintenance containers: backup settings, backup passphrase and local bundles, page specs, pre-upgrade snapshots |
| `connectors-state`, `maintenance-state` volumes | the server-side companion's state |
| `connectors-pairing` volume | the connectors service's tokens, kept current by `migrate` |
| `.env` | database and role passwords, owner token, `COMPOSE_PROFILES`, timezone and port (written by `scripts/install.sh`) |
| `throughline.env` | optional settings (embeddings, egress allow-list, models: see `.env.example`) |
| `connectors.env`, `docker/connectors.json`, `docker/maintenance.json` | connector secrets and schedules |
| `docker-compose.override.yml` | your changes to services (ports, extra mounts) |

None of these files is tracked by git, so an update never overwrites them.
Don't edit `docker-compose.yml` or other tracked files: `scripts/update.sh`
refuses to run over local edits, and tells you which files to move into
`throughline.env` or `docker-compose.override.yml`. For example, to publish
the API on another port:

```yaml
# docker-compose.override.yml
services:
  throughline:
    ports: !override
      - "0.0.0.0:8000:8000"
```

### Snapshots

`throughline migrate` writes a snapshot (`pg_dump`, custom format, owner-only
permissions) before it changes a database that already holds data, whoever
runs it: `scripts/update.sh`, `docker compose up`, or a local install. The two
newest are kept in `~/.throughline/upgrade-snapshots` (in Docker: the
`throughline-home` volume). Backup bundles skip that folder; they hold the
database already.

If the snapshot can't be written (too little disk, no `pg_dump`), the
migration does not start and nothing changes.

| Variable | Default | |
|---|---|---|
| `THROUGHLINE_UPGRADE_SNAPSHOT` | `on` | `off` skips snapshots (keep backups instead) |
| `THROUGHLINE_UPGRADE_SNAPSHOT_DIR` | `~/.throughline/upgrade-snapshots` | |
| `THROUGHLINE_UPGRADE_SNAPSHOT_KEEP` | `2` | newest kept |

```sh
docker compose run --rm migrate throughline upgrade status    # version, pending migrations, snapshots
docker compose run --rm migrate throughline upgrade snapshot  # one now
docker compose run --rm migrate throughline upgrade restore [FILE] --yes  # stop the API first
```

### Updating by hand

`git pull && docker compose up -d --build` is also safe: migrate still takes
its snapshot first. The next `scripts/update.sh` notices that the running
containers were built from an older checkout and redeploys.

## Local install (uv)

```sh
git pull
uv sync
uv run throughline migrate     # snapshot first, then migrations
# restart `throughline serve` and your timers / services
```

## Desktops (Throughline Companion)

The companion keeps its settings, pairing and connector tokens in your
per-user folders (`%APPDATA%\Throughline Companion`,
`~/Library/Application Support/Throughline Companion`,
`~/.config/throughline-companion`), never next to the program, so
installing a new version over the old one keeps them.

- **Downloaded build:** quit the companion, replace the program with the new
  build, start it.
- **Installed with the installer or `throughline-companion deploy`:** run the
  same command again with the new version. The installer stops the running
  service where the OS needs it (Windows), replaces the program and restarts
  the service on the new version. No pairing code is needed the second time.
- **From a checkout:** `git pull`, `uv sync --all-packages`, then
  `throughline-companion service restart` (or restart `run`).
