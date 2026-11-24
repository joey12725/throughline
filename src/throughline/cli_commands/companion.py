"""`throughline lib companion`: enroll desktops running the Companion app.

Uses the owner database connection, like `throughline token create`: device
and plugin tokens are only ever created on the box.

    throughline lib companion enroll laptop --url https://tl.example \\
        --plugin claude_code --plugin activitywatch
                                          prints the pairing code (once)
    throughline lib companion add-plugin laptop youtube
    throughline lib companion devices     what each device offers, online state
    throughline lib companion revoke laptop
    throughline lib companion provision docker --url URL --config F --out F
                                          keep the Docker connectors enrolled
"""

import typer


def register(group):
    app = typer.Typer()
    group.add_typer(
        app, name="companion", short_help="Desktop companion devices and the bridge."
    )

    @app.command("enroll")
    def enroll(
        name: str,
        url: str = typer.Option(
            ..., help="Throughline URL as the desktop reaches it (https://...)"
        ),
        plugin: list[str] = typer.Option(
            [], help="Desktop plugin to issue a token for (repeat)"
        ),
        location_plugin: list[str] = typer.Option(
            [], help="Enrolled plugin that may also send track points"
        ),
        json: bool = False,
    ):
        """Enroll a desktop and print its pairing code (shown only once)."""
        from .. import companion
        from ..cli import output

        try:
            result = companion.enroll(
                name, url, plugin, location_plugins=location_plugin
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from None
        if json:
            output(result, True)
            return
        typer.echo(f"Enrolled {name} with {len(result['token_ids'])} plugin token(s).")
        typer.echo("Paste this pairing code into the Throughline Companion app:")
        typer.echo("")
        typer.echo(result["pairing"])
        typer.echo("")
        typer.echo("It contains tokens: treat it like a password.")

    @app.command("add-plugin")
    def add_plugin(
        name: str, plugin: str, location_source: bool = False, json: bool = False
    ):
        """Issue a token for one more desktop plugin on an enrolled device."""
        from .. import companion
        from ..cli import output

        try:
            result = companion.add_plugin_token(
                name, plugin, location_source=location_source
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from None
        if json:
            output(result, True)
            return
        typer.echo(
            f"Token for {plugin} (add it in the companion app under {plugin} > token):"
        )
        typer.echo(result["token"])

    @app.command("devices")
    def devices(json: bool = False):
        """List enrolled devices, whether they are online and what they offer."""
        from .. import companion
        from ..cli import output
        from ..db import connect

        with connect() as conn:
            rows = companion.devices(conn)
        if json:
            output(rows, True)
            return
        if not rows:
            typer.echo("No companion devices enrolled.")
        for row in rows:
            state = (
                "revoked"
                if row["revoked_at"]
                else "online"
                if row["online"]
                else "offline"
            )
            typer.echo(
                f"{row['name']:<16} {state:<8} {row['platform'] or '-':<10} "
                f"{row['app_version'] or '-'}"
            )
            for cap in row["capabilities"] or []:
                allowed = ", ".join(cap.get("allowed_plugins") or []) or "nobody"
                typer.echo(f"    offers {cap['name']:<22} to {allowed}")
            for plugin in row["plugins"] or []:
                typer.echo(
                    f"    runs   {plugin['name']:<22} {plugin.get('state') or '-'}"
                )

    @app.command("revoke")
    def revoke(name: str):
        """Revoke a device and every token issued for it."""
        from .. import companion

        try:
            result = companion.revoke(name)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from None
        typer.echo(f"Revoked {name} ({result['tokens_revoked']} token(s)).")

    @app.command("provision")
    def provision(
        name: str,
        url: str = typer.Option(..., help="Throughline URL as the device reaches it"),
        config: str = typer.Option(
            ..., help="Companion settings file; its enabled plugins get tokens"
        ),
        out: str = typer.Option(..., help="File the pairing code is kept in (0600)"),
    ):
        """Keep a server device enrolled for the enabled plugins of a settings
        file (the Docker connectors service; run by migrate on every start)."""
        import json as jsonlib
        import os
        from pathlib import Path

        from .. import companion

        if os.getenv("THROUGHLINE_COMPANION_PAIRING"):
            # A code pasted into .env wins over the provisioned one; leave its
            # device alone instead of revoking it for a missing file.
            typer.echo(f"connectors device {name}: THROUGHLINE_COMPANION_PAIRING is set, skipped")
            return
        path = Path(config)
        settings = {}
        if path.is_file():
            try:
                settings = jsonlib.loads(path.read_text(encoding="utf-8"))
            except ValueError as error:
                raise typer.BadParameter(f"{config}: {error}") from None
        entries = settings.get("plugins") if isinstance(settings, dict) else None
        if not isinstance(entries, dict) or not all(
            isinstance(e, dict) for e in entries.values()
        ):
            if entries is not None or not isinstance(settings, dict):
                raise typer.BadParameter(f"{config}: plugins must be an object of objects")
            entries = {}
        # A plugin given its own token in the file needs none from here.
        wanted = [n for n, e in entries.items() if e.get("enabled") and not e.get("token")]
        location = [n for n in wanted if entries[n].get("location_source")]
        try:
            result = companion.provision(name, url, wanted, out, location_plugins=location)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from None
        typer.echo(
            f"connectors device {name}: {result['status']}"
            f" ({len(result['plugins'])} plugin token(s))"
        )
