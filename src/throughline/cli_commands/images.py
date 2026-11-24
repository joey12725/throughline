"""`throughline lib images ...`: the image pipeline (docs/images.md).

Runs in-process against the database (THROUGHLINE_DATABASE_URL) and Immich
(IMMICH_URL, IMMICH_API_KEY, ...; see throughline.images.config). Env files
named by IMAGES_ENV_FILES are loaded for names that are not set.
"""

import json as json_module
from contextlib import contextmanager
from pathlib import Path

import typer

STAGES = ("upload", "pull", "locate", "nsfw", "tags", "gemini", "native", "native-messages")


def _settings():
    from ..images.config import Settings, load_env_files

    load_env_files()
    return Settings.from_env()


@contextmanager
def _readonly():
    from psycopg.rows import dict_row

    from ..db import direct_connection

    conn = direct_connection(row_factory=dict_row)
    try:
        conn.execute("SET default_transaction_read_only = on")
        conn.commit()
        yield conn
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def _pipeline(readonly=False):
    import logging

    from ..db import connect
    from ..images.pipeline import Pipeline

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    settings = _settings()
    ctx = _readonly() if readonly else connect()
    with ctx as conn:
        p = Pipeline(conn, settings)
        try:
            yield p
        finally:
            p.close()


def _show(value, as_json):
    from ..cli import output

    if as_json:
        output(value, True)
        return
    typer.echo(json_module.dumps(value, indent=1, default=str, ensure_ascii=False))


def register(group):
    images = typer.Typer(no_args_is_help=True)
    group.add_typer(images, name="images", short_help="Image pipeline (Immich).")
    people = typer.Typer(no_args_is_help=True)
    images.add_typer(people, name="people", short_help="Face names to and from Immich.")

    @images.command("status")
    def status(json: bool = False):
        """Assets by origin/status, stages done, links, source readiness."""
        from ..images.sources import beeper, email

        settings = _settings()
        with _readonly() as conn:
            from ..images.pipeline import SqlQueries

            q = SqlQueries(conn)
            out = q.status() if q.tables_ready() else {"assets": "tl.assets missing (migrate)"}
        out["sources"] = {
            "beeper_manifest_rows": len(beeper.read_manifest(settings.beeper_import_dir)),
            "email_index_rows": len(email.read_index(settings.email_import_dir)),
            "hermes_export": bool(settings.hermes_export and settings.hermes_export.exists()),
            "t3_state": (settings.t3_home / "userdata" / "statev2.sqlite").exists(),
            "nudenet": bool(settings.nudenet_python),
            "immich": bool(settings.immich_url and settings.immich_api_key),
            "immich_db": bool(settings.immich_db_url),
        }
        spool = settings.spool_dir
        out["spool_files"] = len(list(spool.glob("*"))) if spool.exists() else 0
        _show(out, json)

    @images.command("backfill")
    def backfill(
        source: str = typer.Option(..., help="beeper | t3code | email | hermes | immich"),
        limit: int = typer.Option(100, help="Most items fetched this run"),
        dry_run: bool = typer.Option(False, help="Read only: count discovered, fetchable and skipped"),
        json: bool = False,
    ):
        """Discover a source's images, fetch, store and link them."""
        with _pipeline(readonly=dry_run) as p:
            if source == "immich":
                if dry_run:
                    known = p.q.known_immich_ids() if p.q.tables_ready() else set()
                    found = [a for a in p.immich.search() if str(a["id"]) not in known]
                    _show({"discovered": len(found)}, json)
                    return
                _show(dict(p.native(limit)), json)
                return
            _show(dict(sorted(p.backfill(source, limit=limit, dry_run=dry_run).items())), json)

    @images.command("enrich")
    def enrich(
        stage: str = typer.Option(..., help=" | ".join(STAGES)),
        limit: int = typer.Option(100, help="Most assets this run"),
        json: bool = False,
    ):
        """Run one stage alone."""
        if stage not in STAGES:
            raise typer.BadParameter("stage must be one of " + ", ".join(STAGES))
        with _pipeline() as p:
            fn = {
                "upload": p.upload,
                "pull": p.pull,
                "locate": p.locate,
                "nsfw": p.nsfw,
                "tags": p.tags,
                "gemini": p.gemini,
                "native": p.native,
                "native-messages": lambda limit: p.native_messages(),
            }[stage]
            _show(dict(fn(limit)), json)

    @images.command("sync")
    def sync(
        limit: int = typer.Option(0, help="Per-stage limit (0: the defaults)"),
        source: list[str] = typer.Option(None, help="Sources to backfill (default all)"),
        json: bool = False,
    ):
        """Every stage once, with per-stage limits (the 15-minute timer runs this)."""
        from ..images.sources import SOURCES

        limits = {k: limit for k in ("backfill", "native", "upload", "pull", "locate", "nsfw", "tags", "gemini")} if limit else None
        with _pipeline() as p:
            _show(p.sync(limits, sources=tuple(source) if source else SOURCES), json)

    @images.command("redescribe")
    def redescribe(
        limit: int = typer.Option(500, help="Most assets this run"),
        json: bool = False,
    ):
        """Rewrite older Immich descriptions (they held message text) to source · thread · author.

        Idempotent: progress is enrich.describe_v; run again until redescribed is 0."""
        with _pipeline() as p:
            _show(dict(p.redescribe(limit)), json)

    @images.command("settings")
    def settings_command(
        set_: list[str] = typer.Option(None, "--set", help="key=value (repeatable), e.g. nsfw_mode=blur"),
        json: bool = False,
    ):
        """Show the image settings (tl.image_settings), or change them with --set key=value.

        Keys: nsfw_mode (hide|blur|show), nsfw_threshold (0-1], unscanned (hide|show),
        restricted_nsfw_mode (hide|blur|show), shared_previews (include|exclude),
        location_fallback (off|throughline|throughline_and_immich), location_window_min,
        location_max_accuracy_m. Filtering follows a new nsfw_threshold at once; the
        nsfw tag follows at the next tags stage (sync, or enrich --stage tags)."""
        from ..db import connect
        from ..images import user_settings

        changes = {}
        for item in set_ or []:
            key, sep, value = item.partition("=")
            if not sep:
                raise typer.BadParameter(f"--set takes key=value, not {item!r}")
            changes[key.strip()] = value.strip()
        if not changes:
            with _readonly() as conn:
                _show(user_settings.public(user_settings.read(conn)) | {"available": user_settings.available(conn)}, json)
            return
        try:
            with connect() as conn:
                out = user_settings.write(conn, True, changes)
        except (ValueError, LookupError) as error:
            raise typer.BadParameter(str(error)) from None
        _show(user_settings.public(out), json)

    @images.command("reclassify-previews")
    def reclassify_previews(
        dry_run: bool = typer.Option(False, help="Read only: count per network what would change"),
        json: bool = False,
    ):
        """Mark existing shared-post covers (a message whose only text is a link to a post or
        reel) as role preview, tag shared-post (also in Immich, album 'Shared posts'), and
        drop their depicts links. Idempotent."""
        with _pipeline(readonly=dry_run) as p:
            try:
                _show(p.reclassify_previews(dry_run=dry_run), json)
            except RuntimeError as error:
                raise typer.BadParameter(str(error)) from None

    @images.command("beeper-requests")
    def beeper_requests(
        out: Path = typer.Option(..., help="JSON file for images/beeper_fetch.py"),
        channel: list[str] = typer.Option(None, help="Only these channels (e.g. chat.sms)"),
        limit: int = typer.Option(0, help="At most this many requests"),
    ):
        """Write the Beeper request list (attachments that need the Beeper host)."""
        from ..images.pipeline import beeper_requests as build

        settings = _settings()
        with _readonly() as conn:
            rows = build(conn, settings, channels=channel or None, limit=limit or None)
        out.write_text(json_module.dumps(rows, indent=0), encoding="utf-8")
        typer.echo(f"{len(rows)} requests -> {out}")

    @images.command("email-requests")
    def email_requests(out: Path = typer.Option(..., help="JSON file for images/email_fetch.py")):
        """Write the Gmail attachment request list (personal senders, images >= 15 KB)."""
        from ..images.pipeline import email_requests as build

        settings = _settings()
        with _readonly() as conn:
            rows = build(conn, settings)
        out.write_text(json_module.dumps(rows, indent=0), encoding="utf-8")
        typer.echo(f"{len(rows)} requests -> {out}")

    @images.command("calibrate")
    def calibrate(json: bool = False):
        """Kind and tag distribution over enriched assets (tune tags.json)."""
        with _readonly() as conn:
            from ..images.pipeline import SqlQueries

            q = SqlQueries(conn)
            out = {
                "kinds": q.status()["kinds"],
                "tags": {r["tag"]: r["n"] for r in q.tag_distribution()},
            }
        _show(out, json)

    @people.command("sync")
    def people_sync(dry_run: bool = False, json: bool = False):
        """Push confirmed Throughline names to Immich; print name-match suggestions (never confirms)."""
        with _pipeline(readonly=dry_run) as p:
            _show(p.people_sync(dry_run=dry_run), json)
