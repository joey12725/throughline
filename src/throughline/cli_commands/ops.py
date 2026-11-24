"""`throughline lib backup`: snapshot-consistent backups, the restore drill and
the staleness check. Uses the owner database connection (THROUGHLINE_DATABASE_URL),
like `throughline dump` and `restore`."""

import os
from pathlib import Path

import typer


def register(group):
    backup = typer.Typer(no_args_is_help=True)
    group.add_typer(backup, name="backup", short_help="Backups and restore drill.")

    @backup.command("run")
    def backup_run(
        dir: Path | None = typer.Option(
            None, "--dir", help="Directory (default THROUGHLINE_BACKUP_DIR)"
        ),
        allow_plaintext: bool = typer.Option(
            False, help="Write an unencrypted dump when no encrypt command is set"
        ),
        allow_plaintext_offsite: bool = typer.Option(
            False, help="Copy an unencrypted dump off-site (implies --allow-plaintext)"
        ),
        json: bool = False,
    ):
        """Dump with counts and hashes from the same snapshot; encrypt, copy."""
        from .. import ops
        from ..cli import output

        directory = dir or os.getenv("THROUGHLINE_BACKUP_DIR")
        if not directory:
            raise typer.BadParameter("--dir or THROUGHLINE_BACKUP_DIR is required")
        try:
            result = ops.backup(
                directory,
                allow_plaintext=allow_plaintext,
                allow_plaintext_offsite=allow_plaintext_offsite,
            )
        except ops.OpsError as exc:
            typer.echo(f"backup failed: {exc}", err=True)
            raise typer.Exit(1)
        if json:
            output(result, True)
        else:
            output(
                {
                    key: result[key]
                    for key in ("ok", "file", "encrypted", "sha256", "bytes")
                }
                | {"offsite": (result["offsite"] or {}).get("status")}
            )
        if not result["ok"]:
            raise typer.Exit(1)

    @backup.command("verify-restore")
    def backup_verify(
        file: Path,
        scratch_url: str | None = typer.Option(
            None, help="Scratch database (default THROUGHLINE_SCRATCH_DATABASE_URL)"
        ),
        json: bool = False,
    ):
        """Restore into a scratch database and compare with the stored values."""
        from .. import ops
        from ..cli import output

        result = ops.verify_restore(file, scratch_url=scratch_url)
        if json:
            output(result, True)
        else:
            output(
                {
                    key: result.get(key)
                    for key in ("ok", "counts_unavailable", "duration_s", "error")
                }
                | {
                    "tables_unequal": [
                        t["name"] for t in result["tables"] if t["equal"] is False
                    ],
                    "hashes_unequal": [
                        h["name"] for h in result["hashes"] if h["equal"] is False
                    ],
                    "probes_failed": [
                        p["name"] for p in result["probes"] if not p["ok"]
                    ],
                }
            )
        if not result["ok"]:
            raise typer.Exit(1)

    @backup.command("check")
    def backup_check(
        stale_hours: float | None = typer.Option(
            None, help="Default THROUGHLINE_BACKUP_STALE_HOURS or 36"
        ),
        json: bool = False,
    ):
        """Exit 2 when the last dump or restore drill is stale (for an alarm)."""
        from .. import ops
        from ..cli import output

        result = ops.check(stale_hours)
        output(result, json)
        if not result["ok"]:
            raise typer.Exit(2)

    @backup.command("list")
    def backup_list(limit: int = 20, json: bool = False):
        """Recent backup runs (counts and hashes only)."""
        from .. import ops
        from ..cli import output

        rows = ops.runs(limit)
        if json:
            output(rows, True)
            return
        for row in rows:
            output(
                {
                    key: row[key]
                    for key in (
                        "id",
                        "kind",
                        "status",
                        "finished_at",
                        "file_sha256",
                        "bytes",
                    )
                }
            )

    # --- whole-install bundles (throughline.backups) ----------------------------

    @backup.command("settings")
    def backup_settings(
        set_: list[str] = typer.Option(
            [],
            "--set",
            help="dotted.key=value (JSON or string), e.g. schedule.every_hours=12",
        ),
        every_hours: float | None = typer.Option(None, help="Schedule interval"),
        at: str | None = typer.Option(None, help="Schedule anchor, HH:MM local"),
        dest: str | None = typer.Option(None, help="Local destination directory"),
        keep: int | None = typer.Option(None, help="Bundles kept locally"),
        offsite: str | None = typer.Option(
            None, help="Off-site rsync target host:path or a path; 'none' turns it off"
        ),
        offsite_keep: int | None = typer.Option(None, help="Bundles kept off-site"),
        verify_every_hours: float | None = typer.Option(
            None, help="Restore drill interval (0 = off)"
        ),
        enable: bool | None = typer.Option(None, "--enable/--disable"),
        json: bool = False,
    ):
        """Show or change backup settings (schedule, destination, off-site,
        what is included). Same file as GET/PUT /v1/backup/settings."""
        from .. import backups
        from ..cli import output

        changes = {}
        for item in set_:
            key, sep, value = item.partition("=")
            if not sep:
                raise typer.BadParameter(f"--set needs key=value, got {item!r}")
            backups.set_value(changes, key.strip(), value)
        pairs = (
            ("schedule.every_hours", every_hours),
            ("schedule.at", at),
            ("destination.dir", dest),
            ("destination.keep", keep),
            ("offsite.keep", offsite_keep),
            ("verify.every_hours", verify_every_hours),
            ("enabled", enable),
        )
        for key, value in pairs:
            if value is not None:
                node = changes
                *head, last = key.split(".")
                for part in head:
                    node = node.setdefault(part, {})
                node[last] = value
        if offsite is not None:
            changes.setdefault("offsite", {}).update(
                {"kind": "none"}
                if offsite == "none"
                else {"kind": "rsync", "target": offsite}
            )
        try:
            settings = backups.save(changes) if changes else backups.load()
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        output(backups.public(settings), json)

    @backup.command("init-key")
    def backup_init_key():
        """Create the gpg passphrase file if it is missing. Keep a copy off
        this machine (a password manager): without it no bundle restores."""
        from .. import backups
        from ..cli import output

        output(backups.init_key())

    @backup.command("now")
    def backup_now(json: bool = False):
        """Write a whole-install bundle now: every database, roles, container
        databases and files; encrypt, copy off-site, apply retention."""
        from .. import backups, ops
        from ..cli import output

        def progress(part):
            if "error" in part:
                typer.echo(f"  {part['name']}: FAILED {part['error']}", err=True)
            else:
                typer.echo(f"  {part['name']}: {part['bytes']:,} bytes", err=True)

        try:
            manifest = backups.run(on_part=progress)
        except (ops.OpsError, ValueError) as exc:
            typer.echo(f"backup failed: {exc}", err=True)
            raise typer.Exit(1)
        if json:
            output(manifest, True)
        else:
            output(
                {
                    k: manifest.get(k)
                    for k in ("ok", "dir", "bytes", "errors", "removed")
                }
                | {"offsite": (manifest.get("offsite") or {}).get("status")}
            )
        if not manifest["ok"]:
            raise typer.Exit(1)

    @backup.command("verify")
    def backup_verify_bundle(
        bundle: Path | None = typer.Argument(None, help="Bundle dir (default newest)"),
        json: bool = False,
    ):
        """Restore drill of a bundle's Throughline dump plus a decrypt-and-read
        check of every other part."""
        from .. import backups, ops
        from ..cli import output

        try:
            result = backups.verify(bundle)
        except (ops.OpsError, ValueError) as exc:
            typer.echo(f"verify failed: {exc}", err=True)
            raise typer.Exit(1)
        output(result, json)
        if not result["ok"]:
            raise typer.Exit(1)

    @backup.command("restore")
    def backup_restore_bundle(
        bundle: Path | None = typer.Argument(None, help="Bundle dir (default newest)"),
        only: list[str] = typer.Option(
            [],
            "--only",
            help="Partial restore scopes: database, config, lenses, pages (repeatable or comma-separated)",
        ),
        overwrite: bool = typer.Option(
            False, help="Replace existing config keys and lens/page files instead of merging"
        ),
        clean: bool = typer.Option(
            False, help="Replace existing Throughline database objects if the database is non-empty"
        ),
        continue_onboarding: bool = typer.Option(
            True,
            "--continue-onboarding/--no-continue-onboarding",
            help="Keep onboarding open in restore mode so you can add more sources",
        ),
        passphrase_file: Path | None = typer.Option(
            None, "--passphrase-file", help="Override gpg passphrase file for this restore"
        ),
        json: bool = False,
    ):
        """Restore a whole bundle or selected parts (database, config, lenses, pages)."""
        from .. import backups, ops
        from ..cli import output

        try:
            result = backups.restore_bundle(
                bundle,
                only=only or None,
                overwrite=overwrite,
                clean=clean,
                continue_onboarding=continue_onboarding,
                passphrase_file=passphrase_file,
            )
        except (ops.OpsError, ValueError) as exc:
            typer.echo(f"restore failed: {exc}", err=True)
            raise typer.Exit(1)
        output(result, json)

    @backup.command("tick")
    def backup_tick(json: bool = False):
        """Run a bundle and/or the drill when the schedule says so (systemd
        timer). Exit 1 when something that ran failed."""
        from .. import backups
        from ..cli import output

        try:
            result = backups.tick()
        except ValueError as exc:
            typer.echo(f"backup settings invalid: {exc}", err=True)
            raise typer.Exit(1)
        output(result, json)
        if any((result.get(k) or {}).get("ok") is False for k in ("backup", "verify")):
            raise typer.Exit(1)

    @backup.command("status")
    def backup_status(json: bool = False):
        """Last bundle, last drill, next run, staleness. Exit 2 when not ok."""
        from .. import backups
        from ..cli import output
        from ..db import connect

        with connect() as conn:
            result = backups.status(conn)
        output(result, json)
        if not result["ok"]:
            raise typer.Exit(2)
