# Backups, the restore drill and the staleness alarm

Module `throughline.ops`, commands `throughline lib backup run | verify-restore
| check | list`. No migration: runs are written to `tl.backup_runs`
(migration `0046_coverage.sql`), which `GET /v1/status` reads for
`staleness.backup`.

Every command uses the owner database connection (`THROUGHLINE_DATABASE_URL`),
like the root `throughline dump`, `restore` and `purge` commands, which are
unchanged. `pg_dump` and `pg_restore` are found through `THROUGHLINE_PG_BIN` or
`PATH`. The database password is handed to them through `PGPASSWORD` in
their environment, never on their command line.

Client tools newer than the server are supported: `pg_restore` 17 and newer
opens a restore with `SET transaction_timeout = 0`, which a PostgreSQL 16
server refuses before anything is restored. When the direct restore fails
on exactly that statement, both `throughline restore` and the restore drill
retry once through a plain SQL script (`pg_restore --file`, that one line
removed, then `psql` with `ON_ERROR_STOP`). The script holds the dump's data:
it is written to a private temporary directory (0600 files in the drill) and
removed afterwards. `psql` is found the same way as `pg_restore`.

After a restore, `tl.harden_reader()` re-applies the reader role's settings.
Those are cluster-wide rows, so the drill and `throughline restore` run it
under the same cluster lock as migrations (`db.cluster_lock`, an advisory
lock in the cluster's `postgres` database): a drill beside a migration of
another database on the same server no longer fails with "tuple
concurrently updated".

## Dumps hold everything: treat them as secrets

A dump contains every message, payload, person, identifier, token hash and
derived record in the archive. Treat each file, encrypted or not, like the
database itself: keep the backup directory private, encrypt anything that
leaves the machine and keep the decryption key somewhere other than the
backups.

`tl.backup_runs` never holds content: only file hashes, sizes, row counts,
content hashes (md5 digests), the snapshot id, durations and redacted,
truncated error text.

## The snapshot rule

`lib backup run` opens an owner connection, starts a `REPEATABLE READ`
transaction and exports its snapshot (`pg_export_snapshot()`). While that
transaction stays open it

1. runs `pg_dump --snapshot=<id>` into a new file (mode 0600) in the backup
   directory, and
2. computes, inside the same transaction, the row count of every `tl` table
   and the content hashes below.

The counts and hashes therefore describe exactly the rows the dump holds,
even while plugins keep ingesting. A row committed by another connection
after the export is in neither the dump nor the stored counts.

Content hashes are md5 over the ordered rows of key columns; text enters only
as its own md5:

| table | columns |
| --- | --- |
| messages | id, channel_id, external_id, version, payload_hash, md5(content) |
| raw_payloads | message_id, md5(payload::text) |
| links | id, type, endpoints, status, validity, superseded_by |
| identifiers | id, person_id, system, kind, md5(value_norm), status |
| people | id, md5(names), is_owner, status, merged_into, kind |
| provenance | derived and source keys, relation, origin, job |
| claims | id, subject, md5(statement), status, superseded_by, sources |
| annotations | id, target, attribute, md5(value), status, superseded_by |
| passages | id, message_id, version, ordinal, char range, text_hash |

The hashes do not depend on the session or the server: they are read with
fixed output settings (time zone GMT, ISO dates, `extra_float_digits=1`) and
the rows are ordered by their text in the `"C"` collation, so a drill on a
scratch server with other defaults still compares equal.

## Encryption and the off-site copy

No crypto library is bundled. Both steps are commands the owner configures;
they run without a shell (the template is split into arguments, and the
placeholders are filled inside each argument), with a timeout
(`THROUGHLINE_BACKUP_TIMEOUT_S`, default 3600) and the exit code checked.
Their stderr is truncated and redacted before it is stored or shown.

| variable | placeholders | meaning |
| --- | --- | --- |
| `THROUGHLINE_BACKUP_DIR` | | default for `--dir` |
| `THROUGHLINE_BACKUP_ENCRYPT_CMD` | `{in}` `{out}` | encrypt the dump to `<name>.dump.enc` |
| `THROUGHLINE_BACKUP_DECRYPT_CMD` | `{in}` `{out}` | used by the restore drill |
| `THROUGHLINE_BACKUP_OFFSITE_CMD` | `{file}` | copy the (encrypted) file off the machine |
| `THROUGHLINE_BACKUP_KEEP` | | local files kept, default 7 |
| `THROUGHLINE_BACKUP_STALE_HOURS` | | alarm threshold, default 36 |
| `THROUGHLINE_SCRATCH_DATABASE_URL` | | restore drill target (optional) |

With age and rclone (the key file and the remote name are your own):

```sh
THROUGHLINE_BACKUP_ENCRYPT_CMD='age -r age1yourpublickey... -o {out} {in}'
THROUGHLINE_BACKUP_DECRYPT_CMD='age -d -i /secure/place/backup-key.txt -o {out} {in}'
THROUGHLINE_BACKUP_OFFSITE_CMD='rclone copy {file} offsite:throughline-backups'
```

Rules:

- When encryption is configured, the plaintext dump is deleted in a `finally`
  block: it never outlives the command, even when encryption fails or times
  out. An encrypt command whose output still looks like a plain dump fails.
- Without an encrypt command `lib backup run` writes nothing and fails
  (a `dump` run with error `plaintext_refused`, exit code 1). With
  `--allow-plaintext` the plaintext dump is written and stays in the local
  backup directory only. The off-site command is refused for a plaintext file (a failed
  `offsite` run, exit code 1) unless `--allow-plaintext-offsite` is given, and
  that is never the default.
- After a successful run only the newest `THROUGHLINE_BACKUP_KEEP` files named
  `throughline-*.dump` / `throughline-*.dump.enc` are kept; other files in the
  directory are never touched.
- Each step writes a `tl.backup_runs` row: `dump` (file hash, size, snapshot,
  counts, hashes) and `offsite` (same file hash, the dump run id).

## The restore drill

```sh
throughline lib backup verify-restore /backups/throughline-20260101T030000000000Z-a1b2c3.dump.enc
```

1. An encrypted file is decrypted with `THROUGHLINE_BACKUP_DECRYPT_CMD` into a
   private temporary file, deleted afterwards.
2. The target is `--scratch-url` / `THROUGHLINE_SCRATCH_DATABASE_URL`, or a
   temporary database `tl_verify_<random>` created on the live server and
   dropped afterwards. The live database is refused as a target (same host,
   port and name, or the same database seen through another address), and so
   is a target that already holds a `tl` schema.
3. `pg_restore --exit-on-error` runs with
   `PGOPTIONS='-c throughline.bulk_restore=on'`, then `tl.harden_reader()`.
4. Counts and hashes of the restored database are compared only with the
   values stored for that dump (found by file sha256 in `tl.backup_runs`),
   never with the live database, so ingest after the dump does not matter. A
   dump not produced by `lib backup run` reports `counts_unavailable` and is
   only checked for a successful restore and the probes.
5. Probes: `api.catalog()` and every free SQL example of the catalogue
   (`tl.api_functions`) must run on the restored database. The examples come
   from the file being tested, so each runs in its own `READ ONLY`
   transaction with a 30 s statement timeout and is rolled back; only the
   error class of a failing probe is recorded.

The result is `{ok, counts_unavailable, tables:[{name, expected, restored,
equal}], hashes:[{name, equal}], probes, duration_s}` and a `verify` run is
written (exit code 1 when not ok).

## Nightly schedule and the alarm

`lib backup check` exits 0 when the last successful dump and the last
successful restore drill are both younger than
`THROUGHLINE_BACKUP_STALE_HOURS` (36 h by default), and 2
otherwise, with reasons (`no_dump`, `dump_stale`, `no_verify`,
`verify_stale`). `GET /v1/status` shows the same dates under
`staleness.backup`.

Example crontab (the environment file holds the variables above):

```cron
# 03:10 nightly backup, encrypted and copied off-site
10 3 * * *  . /etc/throughline.env && throughline lib backup run
# 04:10 restore drill of the newest local file
10 4 * * *  . /etc/throughline.env && throughline lib backup verify-restore "$(ls -1 $THROUGHLINE_BACKUP_DIR/throughline-*.dump* | tail -n 1)"
# hourly alarm: a non-zero exit is the signal for the monitoring system
0 * * * *   . /etc/throughline.env && throughline lib backup check || notify-owner "throughline backup stale"
```

`lib backup list` shows recent runs (hashes and counts only).

## Whole-install bundles (the scheduled backup)

`lib backup run` dumps only the Throughline database. An install holds more:
other databases in the same cluster (for example a GBrain database or a web
dev database), the roles the Throughline database expects, databases inside
Docker containers (Immich), and files no database holds (plugin cursors in
`~/.throughline/state`, configuration and secrets files, raw exports, the
Immich library, the source checkout with uncommitted work, systemd units).
Module `throughline.backups` backs all of it up as one **bundle**:

| Part | How | File |
|---|---|---|
| Throughline database | `ops.backup` (snapshot counts and hashes, restore drill) | `throughline-*.dump.enc` |
| Roles and grants | `pg_dumpall --globals-only` | `globals.sql.gpg` |
| Every other database in the cluster | `pg_dump --format=custom`, discovered at run time; `exclude_databases` patterns (default `postgres`, `template*`, `tl_test_*`, `tl_verify_*`) | `db-<name>.dump.gpg` |
| Container databases | `docker exec <container> pg_dumpall --clean`, zstd | `container-<name>.sql.zst.gpg` |
| Files | `tar --zstd` of `include.paths` minus `exclude_paths` (and minus the backup directory) | `files.tar.zst.gpg` |

A bundle is written to `<destination.dir>/throughline-<stamp>-<hex>.partial/`,
renamed when done, and described by `MANIFEST.json` (names, sizes, sha256;
no content). A part that fails is recorded in the manifest and the others
still run; the bundle then counts as incomplete: its dump row in
`tl.backup_runs` carries `detail.bundle.ok = false`, and `lib backup check`,
`lib backup status`, `/v1/status` and `/v1/backup/status` do not count it as
a backup. The newest `destination.keep` bundles stay locally; with an
off-site target each bundle is copied there (`rsync` over ssh for `host:path`,
a local path, or a command template with `{dir}` and `{name}`), the manifest
last, and the remote is pruned to `offsite.keep`.

Every part is encrypted before it is kept. The default is gpg symmetric
AES256 with a passphrase file (`lib backup init-key` creates it, 0600);
`encryption.kind = command` uses your own `encrypt_cmd` / `decrypt_cmd`
templates instead. **Keep a copy of the passphrase off the machine** (a
password manager): it is inside the files part too, but that part cannot be
opened without it.

### Settings: schedule, destination, contents

Settings live in `THROUGHLINE_BACKUP_CONFIG` (default
`~/.throughline/config/backup.json`, 0600), read by the scheduler even when
the database is down. Only the keys you change are stored; the rest are
defaults (the older `THROUGHLINE_BACKUP_*` variables seed them). Edit with:

```bash
throughline lib backup settings                          # show
throughline lib backup settings --every-hours 12 --at 02:30
throughline lib backup settings --dest /mnt/backup/throughline --keep 3
throughline lib backup settings --offsite user@nas:/srv/backups/throughline --offsite-keep 14 \
    --set offsite.ssh_key=~/.ssh/nas_ed25519
throughline lib backup settings --offsite none
throughline lib backup settings --verify-every-hours 168 --disable
throughline lib backup settings --set 'include.containers=[{"container":"immich_postgres","user":"postgres"}]'
throughline lib backup settings --set 'include.paths=["~/.throughline","~/src/throughline"]'
```

or over HTTP (owner token): `GET /v1/backup/settings`, `PUT
/v1/backup/settings` with a partial JSON body (deep-merged, validated, 400 on
a bad value), `GET /v1/backup/status`. The passphrase is never returned,
only `encryption.passphrase_present`.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | The scheduler runs bundles |
| `schedule.every_hours`, `schedule.at` | `24`, `"03:30"` | Bundles start at `at` (local time) and every `every_hours` from it |
| `verify.every_hours` | `168` | Restore drill + part checks interval; `0` turns it off |
| `destination.dir`, `destination.keep` | `~/.throughline/backups`, `3` | Local bundles and how many are kept |
| `offsite.kind` | `none` | `none`, `rsync` (`target` = `host:path` or a path, optional `ssh_key`), `command` (`command` template) |
| `offsite.keep` | `14` | Bundles kept off-site |
| `encryption.*` | gpg, `~/.throughline/backup.passphrase` | See above |
| `include.databases` | `"all"` | Or a list of database names |
| `include.exclude_databases` | see above | fnmatch patterns |
| `include.roles` | `true` | Dump roles and grants |
| `include.containers`, `include.docker` | `[]` | Container databases; docker command, `context`, `config` (DOCKER_CONFIG) |
| `include.paths`, `include.exclude_paths` | `~/.throughline` minus datadirs, `.venv`, caches | File trees; a bare name excludes at any depth |
| `stale_hours`, `verify_stale_hours` | 1.5 × interval (≥ 36 h) | When status calls them stale |
| `retry_minutes` | `60` | A failed run is retried after this long |

### Running it

`lib backup tick` runs a bundle when the latest scheduled slot has no
complete bundle (a failure is retried after `retry_minutes`) and the drill
when `verify.every_hours` have passed. Run it every 15 minutes; changing the
schedule never needs the timer touched. A systemd user timer:

```ini
# ~/.config/systemd/user/throughline-backup.service
[Service]
Type=oneshot
ExecStart=%h/.throughline/bin/backup-tick.sh   # sources env, flock, `throughline lib backup tick`
TimeoutStartSec=12h
# ~/.config/systemd/user/throughline-backup.timer
[Timer]
OnBootSec=5min
OnUnitInactiveSec=15min
Persistent=true
```

Large databases need `THROUGHLINE_BACKUP_TIMEOUT_S` above its 3600 s default
(the wrapper sets 4 h). Other commands: `lib backup now` (a bundle now),
`lib backup verify [BUNDLE]` (restore drill of the bundle's Throughline dump
plus decrypt and `pg_restore --list` / `tar --list` / `zstd --test` of every
other part, sha256 against the manifest), `lib backup status` (exit 2 when
not ok: `no_backup`, `backup_stale`, `last_run_failed`, `no_verify`,
`verify_stale`, `disabled`).

### Restoring a bundle

```bash
K=~/.throughline/backup.passphrase; B=~/.throughline/backups/throughline-<stamp>-<hex>
dec() { gpg --batch --pinentry-mode loopback --passphrase-file "$K" -d -o "$2" "$1"; }
dec $B/globals.sql.gpg globals.sql && psql -d postgres -f globals.sql        # roles first
throughline restore <decrypted throughline dump>                              # or pg_restore
dec $B/db-otherdb.dump.gpg o.dump && createdb otherdb && pg_restore -d otherdb --no-owner o.dump     # each opted-in database
dec $B/container-immich_postgres.sql.zst.gpg i.sql.zst && zstd -dc i.sql.zst | docker exec -i immich_postgres psql -U postgres
dec $B/files.tar.zst.gpg files.tar.zst && tar --zstd -xf files.tar.zst -C /     # or -C a scratch dir
```

## Model outages and the maintenance tick

Lenses and Librarian curate call a model endpoint. That can be OpenRouter, or
a local proxy for subscription models such as CLIProxyAPI on `127.0.0.1:8317`.
When the endpoint goes away, nothing is lost and nothing is recorded as done.
Once it is back, the work is backfilled, just as a source is backfilled after
it comes back.

- **The cursor stays put.** A lens item whose call fails is not written, and
  the lens cursor stays before it (verify-then-advance). Any of these ends the
  run as `provider_unavailable`: a refused connection, a timeout, an HTTP
  error, or an HTTP 200 that carries no answer (an `error` object, no
  `choices`, an empty message, `finish_reason: "error"`). Proxies answer
  upstream quota and outage errors that way. A reply that has content but
  fails validation is still the model's answer. It counts as `invalid` and the
  cursor moves on. Curate works the same way: a stopped item rolls back and
  its cursor stays before it.
- **`throughline lib lens tick`** (also `POST /v1/lenses/tick`) runs every lens
  that is due, in dependency order. A lens is due when:
  - its last cursor run was interrupted: `provider_unavailable`, `failed`,
    `max_items` (a backlog bigger than one run), the monthly cap, or a
    `running` row whose process died. Retries back off from 5 minutes,
    doubling up to 1 hour, while the endpoint keeps failing.
  - its cron `schedule` (5 fields or `@hourly`/`@daily`/...), in
    `THROUGHLINE_TIMEZONE`, has fired since its last cursor run.

  A lens with no schedule is only resumed, never started. Lazy lenses fill on
  read. Once an endpoint answers `provider_unavailable`, the rest of the tick
  calls it no more and reports its other lenses as `endpoint_down`. A lens
  whose dependency did not finish waits for the next tick. `--dry-run` shows
  what is due and why.
- **One cursor run per lens.** A second cursor run of a lens that is already
  running returns `busy`.
- **Rules.** When a rule triggers a lens run and the run ends without the
  message's rows, the run is queued in `tl.rule_deliveries` (payload kind
  `run_table`). `deliver_pending` (part of every tick) retries it, probing each
  lens once per pass.

The user timer `throughline-maintain.timer` runs
`~/.throughline/bin/maintain-sync.sh` every 15 minutes. The script runs the
tick, and then `lib librarian curate run` when `THROUGHLINE_LIBRARIAN_POLICY`
is set. It sources `~/.throughline/cliproxy.env` for lens model keys such as
`THROUGHLINE_CLIPROXY_KEY`. Its log is `~/.throughline/logs/maintain.log`.

Not covered: transcripts noise judging and commitment extraction are on-demand
jobs. When the provider is down they report `provider_unavailable`/`partial`
and leave their records unjudged, so you rerun them by hand.

