"""Compact local administration and HTTP client."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import typer
from fastapi.encoders import jsonable_encoder

from .db import cluster_lock, connect, create_token, set_location_source
from .db import migrate as migrate_db

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help=None,
)
# The whole help text has a size budget: a group is described once, in the root
# command list, and not again at the top of its own help.
tokens = typer.Typer()
identities = typer.Typer()
track = typer.Typer()
app.add_typer(tokens, name="token", short_help="Local token administration.")
app.add_typer(identities, name="identity", short_help="Identity operations.")
app.add_typer(track, name="track", short_help="Location operations.")
# Every newer command lives under this one group, so the root help stays small.
# Its commands come from the modules of throughline.cli_commands (see the end
# of this file); nothing is added to the root or to the groups above.
lib = typer.Typer(no_args_is_help=True)
app.add_typer(lib, name="lib", short_help="Library operations.")


def output(value, as_json=False):
    value = jsonable_encoder(value)
    if as_json:
        typer.echo(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return
    if isinstance(value, dict) and "rows" in value:
        output(value["rows"])
        typer.echo(
            f"{value['row_count']} rows"
            + (" (truncated)" if value["truncated"] else "")
        )
    elif isinstance(value, list):
        for row in value:
            output(row)
    elif isinstance(value, dict):
        typer.echo(
            "  ".join(
                f"{key}={json.dumps(item, ensure_ascii=False, separators=(',', ':')) if isinstance(item, (list, dict)) else item}"
                for key, item in value.items()
            )
        )
    else:
        typer.echo(value)


def request(method, path, data=None, params=None):
    from . import secrets

    token = secrets.require("THROUGHLINE_TOKEN")
    with httpx.Client(
        base_url=os.getenv("THROUGHLINE_URL", "http://127.0.0.1:8000"),
        headers={"Authorization": "Bearer " + token},
        timeout=60,
    ) as client:
        response = client.request(method, "/v1/" + path, json=data, params=params)
        if response.is_error:
            raise typer.BadParameter(f"HTTP {response.status_code}: {response.text}")
        return response.json()


@app.command()
def migrate(
    no_snapshot: bool = typer.Option(
        False, "--no-snapshot", help="Skip the pre-upgrade database snapshot"
    ),
):
    """Apply migrations."""
    from .ops import OpsError
    from .upgrades import before_migrate

    try:
        before_migrate(snapshot=not no_snapshot, echo=typer.echo)
    except OpsError as exc:
        typer.echo(f"update stopped before any change: {exc}", err=True)
        raise typer.Exit(1)
    migrate_db()
    typer.echo("Migrated")


upgrade = typer.Typer(
    no_args_is_help=True,
    help="Updates: schema state and pre-upgrade snapshots (docs/updating.md).",
)
app.add_typer(upgrade, name="upgrade", short_help="Update snapshots.")


@upgrade.command("status")
def upgrade_status(json: bool = False):
    """Version, applied/pending migrations and the snapshots kept."""
    from . import upgrades

    state = upgrades.schema_state()
    data = dict(
        version=upgrades.version(),
        applied=len(state["applied"]),
        latest=state["applied"][-1] if state["applied"] else None,
        pending=state["pending"],
        newer_than_this_version=state["unknown"],
        snapshots=[
            dict(path=str(p), mb=p.stat().st_size // 2**20) for p in upgrades.snapshots()
        ],
    )
    output(data, json)


@upgrade.command("snapshot")
def upgrade_snapshot():
    """Write a snapshot now (updates do this on their own)."""
    from .ops import OpsError
    from .upgrades import take_snapshot

    try:
        typer.echo(f"pre-upgrade snapshot: {take_snapshot(label='manual')}")
    except OpsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@upgrade.command("restore")
def upgrade_restore(
    snapshot: str = typer.Argument(None, help="Snapshot file; default the newest"),
    yes: bool = typer.Option(False, "--yes", help="Confirm replacing the database"),
):
    """Replace the database with a pre-upgrade snapshot (rollback). Stop the
    API and connectors first; data written since the snapshot is lost."""
    from .ops import OpsError
    from . import upgrades

    if snapshot is None:
        kept = upgrades.snapshots()
        if not kept:
            typer.echo("no pre-upgrade snapshots", err=True)
            raise typer.Exit(1)
        snapshot = str(kept[-1])
    if not yes:
        typer.echo(
            f"This replaces the whole database with {snapshot}; anything written "
            "since then is lost. Run again with --yes to do it.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        upgrades.restore_snapshot(snapshot)
    except OpsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"restored {snapshot}")


@app.command()
def configure_passwords():
    """Set role passwords."""
    from .db import configure_role_passwords

    configure_role_passwords()
    typer.echo("Configured runtime role passwords")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Run the API."""
    import os

    import uvicorn

    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    os.environ.setdefault("THROUGHLINE_SETUP_ANNOUNCE", f"http://{shown}:{port}")
    uvicorn.run("throughline.api:create_app", factory=True, host=host, port=port)


@tokens.command("create")
def token_create(
    name: str,
    kind: str = "owner",
    plugin: str | None = None,
    asker: list[str] = typer.Option([]),
    location_source: bool = False,
    json: bool = False,
):
    if kind not in ("owner", "agent", "plugin"):
        raise typer.BadParameter("invalid token kind")
    if kind == "plugin" and not plugin:
        raise typer.BadParameter("plugin name required")
    output(create_token(name, kind, plugin, asker, location_source), json)


@tokens.command("list")
def token_list(json: bool = False):
    with connect() as conn:
        output(
            conn.execute(
                "SELECT id,name,kind,plugin,askers,location_source,revoked_at FROM tl.tokens ORDER BY id"
            ).fetchall(),
            json,
        )


@tokens.command("location-source")
def token_location_source(token_id: int, off: bool = False, json: bool = False):
    """Let an existing plugin token send track points (--off withdraws)."""
    try:
        output(set_location_source(token_id, not off), json)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None


@tokens.command("revoke")
def token_revoke(token_id: int):
    with connect() as conn:
        conn.execute("UPDATE tl.tokens SET revoked_at=now() WHERE id=%s", (token_id,))
    typer.echo("Revoked")


@app.command("sql")
def run_sql(
    statement: str,
    json: bool = False,
    deadline_ms: int | None = None,
    token_budget: int | None = None,
):
    data = {"statement": statement}
    if deadline_ms is not None:
        data["deadline_ms"] = deadline_ms
    if token_budget is not None:
        data["token_budget"] = token_budget
    output(request("POST", "sql", data), json)


@app.command()
def search(
    query: str,
    json: bool = False,
    mode: str = "keyword",
    person: int | None = None,
    channel: str | None = None,
    after: str | None = None,
    before: str | None = None,
    place: str | None = None,
    max_results: int | None = None,
):
    params = {"q": query, "mode": mode}
    for name, value in (
        ("person", person),
        ("channel", channel),
        ("after_at", after),
        ("before_at", before),
        ("place", place),
        ("max_results", max_results),
    ):
        if value is not None:
            params[name] = value
    output(request("GET", "search", params=params), json)


@app.command()
def context(message_id: int, before: int = 3, after: int = 3, json: bool = False):
    output(
        request(
            "GET",
            "context",
            params=dict(message_id=message_id, before=before, after=after),
        ),
        json,
    )


@app.command()
def person(query: str, json: bool = False):
    output(request("GET", "person", params={"q": query}), json)


@app.command()
def catalog(json: bool = False):
    output(request("GET", "catalog"), json)


@app.command()
def status(json: bool = False):
    output(request("GET", "status"), json)


@app.command()
def channels(json: bool = False):
    output(request("GET", "channels"), json)


@app.command()
def api(method: str, path: str, body: str | None = None, json: bool = False):
    """Call any /v1 endpoint; BODY is JSON or @filename."""
    data = None
    if body:
        text = Path(body[1:]).read_text() if body.startswith("@") else body
        import json as codec

        data = codec.loads(text)
    output(request(method.upper(), path, data), json)


@identities.command("unclaimed")
def unclaimed(json: bool = False):
    output(request("GET", "identity/unclaimed"), json)


@identities.command("merge")
def merge(source: int, target: int, json: bool = False):
    output(request("POST", "identity/merge", dict(source=source, target=target)), json)


@identities.command("split")
def split(event_id: int, json: bool = False):
    output(request("POST", "identity/split", dict(event_id=event_id)), json)


@identities.command("attach")
def attach(identifier_id: int, person_id: int, json: bool = False):
    output(
        request(
            "POST",
            "identity/attach",
            dict(identifier_id=identifier_id, person_id=person_id),
        ),
        json,
    )


@identities.command("detach")
def detach(identifier_id: int, json: bool = False):
    output(request("POST", "identity/detach", dict(identifier_id=identifier_id)), json)


@identities.command("ignore")
def ignore(identifier_id: int, json: bool = False):
    output(request("POST", "identity/ignore", dict(identifier_id=identifier_id)), json)


@track.command("detect-visits")
def detect_visits(
    radius_m: float = 100,
    min_stay_s: int = 600,
    max_gap_s: float | None = None,
    json: bool = False,
):
    """Recompute visits; --max-gap-s is the longest silence inside one stay."""
    params = dict(radius_m=radius_m, min_stay_s=min_stay_s)
    if max_gap_s is not None:
        params["max_gap_s"] = max_gap_s
    output(request("POST", "track/detect-visits", params=params), json)


@track.command("retire")
def track_retire(
    source: str | None = None,
    point_id: list[int] = typer.Option([]),
    reason: str | None = None,
    restore: bool = False,
    json: bool = False,
):
    """Withdraw a bad source or points (owner role); then rerun detect-visits."""
    from .location import retire_track

    if (source is None) == (not point_id):
        raise typer.BadParameter("give --source or --point-id")
    with connect() as conn:
        if conn.execute("SELECT current_user AS role").fetchone()["role"] in (
            "throughline_app",
            "throughline_reader",
        ):
            raise typer.BadParameter("owner database role required")
        output(retire_track(conn, source, point_id, reason, restore), json)


@app.command()
def reparse(channel: str, json: bool = False):
    output(request("POST", "reparse", {"channel": channel}), json)


@app.command()
def purge(channel: str, confirm: bool = False):
    """Purge a channel (direct owner DB)."""
    if not confirm:
        raise typer.BadParameter("--confirm is required")
    with connect() as conn:
        if conn.execute("SELECT current_user AS role").fetchone()["role"] in (
            "throughline_app",
            "throughline_reader",
        ):
            raise typer.BadParameter("owner database role required")
        conn.execute("SET LOCAL throughline.purge='on'")
        # The hooks semi-join against every purged id; keep those hash
        # tables in memory so a large channel does not spill to disk.
        conn.execute("SET LOCAL work_mem='256MB'")
        # Components that keep text derived from messages (caches, claims,
        # passages) registered a statement in tl.purge_hooks; they run first,
        # while the channel's messages can still be joined.
        purged = conn.execute(
            "SELECT id FROM tl.channels WHERE name=%s", (channel,)
        ).fetchone()
        if purged:
            conn.execute("SELECT tl.run_purge_hooks(%s)", (purged["id"],))
        conn.execute(
            "DELETE FROM tl.conversations WHERE thread_id IN (SELECT t.id FROM tl.threads t JOIN tl.channels c ON c.id=t.channel_id WHERE c.name=%s)",
            (channel,),
        )
        conn.execute(
            "DELETE FROM tl.links WHERE (from_kind='message' AND from_id IN (SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s)) OR (to_kind='message' AND to_id IN (SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s))",
            (channel, channel),
        )
        conn.execute(
            "DELETE FROM tl.message_locations WHERE message_id IN (SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s)",
            (channel,),
        )
        conn.execute(
            "DELETE FROM tl.message_envelopes WHERE message_id IN (SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s)",
            (channel,),
        )
        conn.execute(
            "DELETE FROM tl.raw_payloads WHERE message_id IN (SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s)",
            (channel,),
        )
        conn.execute(
            "UPDATE tl.messages SET superseded_by=NULL WHERE channel_id=(SELECT id FROM tl.channels WHERE name=%s)",
            (channel,),
        )
        conn.execute(
            "DELETE FROM tl.messages WHERE channel_id=(SELECT id FROM tl.channels WHERE name=%s)",
            (channel,),
        )
        conn.execute(
            "DELETE FROM tl.threads WHERE channel_id=(SELECT id FROM tl.channels WHERE name=%s)",
            (channel,),
        )
    typer.echo("Purged")


def pg_program(name):
    directory = os.getenv("THROUGHLINE_PG_BIN")
    if directory:
        candidate = Path(directory) / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)
    program = shutil.which(name)
    if not program:
        raise typer.BadParameter(f"{name} not found; set THROUGHLINE_PG_BIN")
    return program


@app.command()
def dump(path: Path):
    """Dump the database in PostgreSQL custom format."""
    subprocess.run(
        [
            pg_program("pg_dump"),
            "--dbname",
            os.environ["THROUGHLINE_DATABASE_URL"],
            "--format=custom",
            "--file",
            str(path),
            "--no-owner",
        ],
        check=True,
    )


# pg_restore 17 and newer opens every restore with this statement, a setting
# that servers before PostgreSQL 17 do not know; with --exit-on-error the
# restore then stops before it has restored anything. Client tools newer than
# the server are common (a distribution's current client against the pinned
# server image), so such a restore is retried through a plain SQL script with
# that one line removed.
TRANSACTION_TIMEOUT_SET = b"SET transaction_timeout = 0;"


def refused_transaction_timeout(stderr):
    """True when a pg_restore failure is the newer-client preamble refused by
    an older server (nothing has been restored yet)."""
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8", "replace")
    return TRANSACTION_TIMEOUT_SET in (stderr or b"")


def restore_through_script(path, dsn, run, script, filtered):
    """Restore the custom-format dump `path` into `dsn` through psql.

    pg_restore writes the archive as SQL to `script`; every line that is
    exactly the transaction_timeout preamble is dropped into `filtered`, which
    psql runs with ON_ERROR_STOP (the --exit-on-error of the direct path).
    `run(args)` runs one program and raises when it fails. Both files hold
    the dump's data: the caller places them in a private directory and
    removes them."""
    run(
        [
            pg_program("pg_restore"),
            "--no-owner",
            "--file",
            str(script),
            str(path),
        ]
    )
    with open(script, "rb") as source, open(filtered, "wb") as target:
        for line in source:
            if line.rstrip(b"\r\n") != TRANSACTION_TIMEOUT_SET:
                target.write(line)
    Path(script).unlink()
    run(
        [
            pg_program("psql"),
            "--dbname",
            dsn,
            "--no-psqlrc",
            "--quiet",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            str(filtered),
        ]
    )


@app.command()
def restore(path: Path):
    """Restore into an empty owner database."""
    dsn = os.environ["THROUGHLINE_DATABASE_URL"]
    # Restored rows keep their original ingested_at and replace feed
    # state with them: exempt from the feed settle window (0046).
    env = {
        **os.environ,
        "PGOPTIONS": (
            os.environ.get("PGOPTIONS", "") + " -c throughline.bulk_restore=on"
            # Serial index builds: parallel ones need dynamic shared memory,
            # which a container's default 64 MB /dev/shm cannot hold for
            # large (vector) indexes.
            + " -c max_parallel_maintenance_workers=0"
        ).strip(),
    }
    args = [
        pg_program("pg_restore"),
        "--dbname",
        dsn,
        "--no-owner",
        "--exit-on-error",
        str(path),
    ]
    result = subprocess.run(args, env=env, capture_output=True)
    if result.returncode != 0 and refused_transaction_timeout(result.stderr):

        def run(command):
            done = subprocess.run(
                command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            if done.returncode != 0:
                typer.echo(done.stderr.decode("utf-8", "replace"), err=True)
                raise subprocess.CalledProcessError(done.returncode, command)

        with tempfile.TemporaryDirectory(prefix="tl-restore-") as folder:
            restore_through_script(
                path,
                dsn,
                run,
                Path(folder) / "restore.sql",
                Path(folder) / "restore.filtered.sql",
            )
    elif result.returncode != 0:
        typer.echo(result.stderr.decode("utf-8", "replace"), err=True)
        raise subprocess.CalledProcessError(result.returncode, args)
    with cluster_lock(), connect() as conn:
        ready = conn.execute(
            "SELECT to_regprocedure('tl.harden_reader()') IS NOT NULL ready"
        ).fetchone()["ready"]
        if ready:
            conn.execute("SELECT tl.harden_reader()")
    if not ready:
        migrate_db()


embeddings = typer.Typer(help="Embeddings: worker, channels, batch jobs.")
app.add_typer(embeddings, name="embed")


@embeddings.command("run")
def embed_run(
    once: bool = False,
    until_idle: bool = typer.Option(
        False, help="Embed until nothing is pending, then exit (for a scheduler)."
    ),
    max_seconds: int = typer.Option(
        0, help="With --until-idle: stop after this many seconds (0: no limit)."
    ),
):
    """Embed what is pending (the passive worker); --once runs one batch."""
    from .embeddings import run, run_until_idle

    if until_idle:
        output(run_until_idle(max_seconds or None), True)
        return
    output(run(once))


@embeddings.command("status")
def embed_status():
    """Provider, models, what is pending, per-channel choices and batch jobs."""
    from . import embedding_batches, embeddings as worker, passages, providers

    report = {}
    try:
        provider = providers.get("embedding")
        report["provider"] = dict(
            name=provider.name,
            model=provider.model,
            dimensions=provider.dimensions,
            query_and_document_types=bool(getattr(provider, "asymmetric", False)),
            contextual=bool(getattr(provider, "contextual", False)),
            batch_interface=provider.batch_client() is not None,
            historic=embedding_batches.mode(),
        )
        model = provider.model
    except ValueError as error:
        report["provider"] = dict(name="none", detail=str(error))
        model = None
    with connect() as conn:
        report["models"] = conn.execute(
            "SELECT m.model,m.dimensions,m.active,"
            "to_regclass(format('tl.%I',tl.embedding_index_name('embeddings',m.model,m.dimensions))) IS NOT NULL AS indexed,"
            "(SELECT count(*) FROM tl.embeddings e WHERE e.model=m.model) AS conversation_vectors,"
            "(SELECT count(*) FROM tl.passage_embeddings e WHERE e.model=m.model) AS passage_vectors "
            "FROM tl.embedding_models m ORDER BY m.model"
        ).fetchall()
        if model is not None:
            report["pending"] = dict(
                conversations=conn.execute(
                    "SELECT count(*) n FROM (SELECT c.id "
                    + worker._PENDING
                    + " OFFSET 0) c WHERE true"
                    + worker.egress_clause(),
                    (model, model),
                ).fetchone()["n"],
                **passages.pending(conn, model),
            )
        report["channels"] = conn.execute(
            "SELECT ch.name,ch.embed,ch.record_kind,"
            "(SELECT count(*) FROM tl.conversations c JOIN tl.threads t ON t.id=c.thread_id WHERE t.channel_id=ch.id AND c.closed) AS closed_conversations,"
            "(SELECT count(*) FROM tl.embeddings e JOIN tl.embedding_models em ON em.model=e.model AND em.active JOIN tl.conversations c ON c.id=e.conversation_id AND c.text_hash=e.text_hash JOIN tl.threads t ON t.id=c.thread_id WHERE t.channel_id=ch.id) AS embedded_conversations "
            "FROM tl.channels ch ORDER BY ch.name"
        ).fetchall()
        report["batches"] = embedding_batches.status(conn)
    output(report)


@embeddings.command("channels")
def embed_channels(
    enable: list[str] = typer.Option(
        [], "--enable", help="Channel to embed (repeatable)."
    ),
    disable: list[str] = typer.Option(
        [],
        "--disable",
        help="Channel to stop embedding; its vectors are deleted (repeatable).",
    ),
):
    """List which channels are embedded, or switch channels on and off.

    A channel that is not embedded has no vectors: meaning search does not
    cover it, every other search still does."""
    changes = []
    with connect() as conn:
        try:
            for name, flag in [(n, True) for n in enable] + [
                (n, False) for n in disable
            ]:
                changes.append(
                    conn.execute(
                        "SELECT tl.set_channel_embedding(%s,%s) r", (name, flag)
                    ).fetchone()["r"]
                )
        except Exception as error:
            if getattr(error, "sqlstate", None) != "P0002":
                raise
            conn.rollback()
            typer.echo(str(error).splitlines()[0], err=True)
            raise typer.Exit(1) from None
        channels = conn.execute(
            "SELECT name,embed FROM tl.channels ORDER BY name"
        ).fetchall()
    output(dict(changes=changes, channels=channels))


@embeddings.command("backfill")
def embed_backfill(
    collect_only: bool = typer.Option(
        False, "--collect", help="Only store answered jobs; submit nothing."
    ),
    wait: bool = typer.Option(
        False, "--wait", help="Keep collecting until no job is open."
    ),
    cancel: int = typer.Option(
        None, "--cancel", help="Cancel the open job with this id."
    ),
):
    """Historic load: hand the whole backlog to the vendor's batch interface
    (cheaper, answered within hours) and store what has been answered."""
    import time

    from . import embedding_batches, embeddings as worker

    try:
        if cancel is not None:
            output(embedding_batches.cancel(cancel))
            return
        with worker.worker_lock():
            submitted = (
                dict(jobs=[], texts=0) if collect_only else embedding_batches.submit()
            )
            collected = embedding_batches.collect(force=True)
        while wait and (collected["pending"] or collected["other_provider"]):
            if collected["other_provider"] and not collected["pending"]:
                break
            time.sleep(max(30, embedding_batches.poll_seconds()))
            with worker.worker_lock():
                step = embedding_batches.collect(force=True)
            for key in ("completed", "failed", "stored"):
                collected[key] += step[key]
            collected["pending"] = step["pending"]
            collected["other_provider"] = step["other_provider"]
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    output(dict(submitted=submitted, collected=collected))


@app.command("rebuild-conversations")
def conversations_rebuild():
    with connect() as conn:
        # Lock in ingest's order; the subquery fixes evaluation order.
        conn.execute(
            "SELECT pg_advisory_xact_lock(id) FROM (SELECT id FROM tl.channels ORDER BY id OFFSET 0) c"
        )
        # Deferred threads have unnumbered rows; regrouping orders by number.
        conn.execute("SELECT tl.refresh_derived()")
        conn.execute("SELECT tl.regroup_conversations()")
    typer.echo("Rebuilt")


# Last, so command modules can import output and request from this module.
from .cli_commands import load as _load_lib_commands  # noqa: E402

_load_lib_commands(lib)
