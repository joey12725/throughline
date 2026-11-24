"""`throughline lib infisical`: inspect and verify Infisical secret references.

    throughline lib infisical status       configuration summary (no secrets)
    throughline lib infisical test         verify auth and project access
    throughline lib infisical list         list accessible secret names (no values)
    throughline lib infisical check        verify every infisical: reference in env / config
"""

from __future__ import annotations

import json as jsonlib
import os
from pathlib import Path
from typing import Optional

import typer


def _collect_config_refs(path: Path) -> list[tuple[str, object]]:
    """Find every `infisical:` reference in a companion.json / connectors.json file."""
    data = jsonlib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    from .. import secrets

    found: list[tuple[str, object]] = []
    for plugin_name, entry in sorted((data.get("plugins") or {}).items()):
        if not isinstance(entry, dict):
            continue
        if secrets.is_ref(entry.get("token")):
            found.append((f"plugins.{plugin_name}.token", entry["token"]))
        for key, value in sorted((entry.get("env") or {}).items()):
            if secrets.is_ref(value):
                found.append((f"plugins.{plugin_name}.env.{key}", value))
    for job in data.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        jname = job.get("name") or "job"
        if secrets.is_ref(job.get("token")):
            found.append((f"jobs.{jname}.token", job["token"]))
        for key, value in sorted((job.get("env") or {}).items()):
            if secrets.is_ref(value):
                found.append((f"jobs.{jname}.env.{key}", value))
    api_key = ((data.get("capture") or {}).get("transcription") or {}).get("api_key")
    if secrets.is_ref(api_key):
        found.append(("capture.transcription.api_key", api_key))
    return found


def register(group):
    app = typer.Typer(no_args_is_help=True)
    group.add_typer(
        app,
        name="infisical",
        short_help="Infisical secret reference status, listing and checks.",
    )

    @app.command("status")
    def status_cmd(json: bool = False):
        """Show the current Infisical connection settings (never prints credentials)."""
        from .. import secrets
        from ..cli import output

        output(secrets.status(), json)

    @app.command("test")
    def test_cmd(json: bool = False):
        """Authenticate with Infisical and verify access to the configured project."""
        from .. import secrets
        from ..cli import output

        try:
            result = secrets.test()
        except (secrets.InfisicalError, ValueError) as exc:
            typer.echo(f"infisical test failed: {exc}", err=True)
            raise typer.Exit(1) from None
        output(result, json)

    @app.command("list")
    def list_cmd(
        path: Optional[str] = typer.Option(
            None, "--path", help="Secret folder path (default: INFISICAL_SECRET_PATH or /)"
        ),
        environment: Optional[str] = typer.Option(
            None, "--env", help="Environment slug (default: INFISICAL_ENVIRONMENT or prod)"
        ),
        recursive: bool = typer.Option(
            False, "--recursive", "-r", help="Include secrets inside subfolders"
        ),
        json: bool = False,
    ):
        """List available secret names in Infisical (values are never shown)."""
        from .. import secrets
        from ..cli import output

        try:
            items = secrets.list_secret_names(
                secret_path=path,
                environment=environment,
                recursive=recursive,
            )
        except (secrets.InfisicalError, ValueError) as exc:
            typer.echo(f"infisical list failed: {exc}", err=True)
            raise typer.Exit(1) from None
        if json:
            output(items, True)
            return
        if not items:
            typer.echo("No secrets found.")
            return
        for item in items:
            tag = " [imported]" if item.get("imported") else ""
            typer.echo(f"{item['ref']:<42} {item['path']} ({item['environment']}){tag}")

    @app.command("check")
    def check_cmd(
        config: Optional[str] = typer.Option(
            None,
            "--config",
            help="Also check infisical: references in a connectors.json / companion.json file",
        ),
        json: bool = False,
    ):
        """Verify that every configured infisical: reference resolves (never prints values)."""
        from .. import secrets
        from ..cli import output

        refs: list[tuple[str, object]] = [
            (f"env:{k}", v)
            for k, v in sorted(os.environ.items())
            if secrets.is_ref(v)
        ]
        config_infisical: dict | None = None
        if config:
            try:
                cfg_path = Path(config)
                raw_cfg = jsonlib.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(raw_cfg, dict) and isinstance(raw_cfg.get("infisical"), dict):
                    config_infisical = raw_cfg["infisical"]
                refs.extend(_collect_config_refs(cfg_path))
            except (OSError, ValueError) as exc:
                typer.echo(f"could not read {config}: {exc}", err=True)
                raise typer.Exit(1) from None

        check_client = None
        if config_infisical:
            merged_env = dict(os.environ)
            for cfg_key, env_key in (
                ("site_url", "INFISICAL_SITE_URL"),
                ("client_id", "INFISICAL_CLIENT_ID"),
                ("client_secret", "INFISICAL_CLIENT_SECRET"),
                ("token", "INFISICAL_TOKEN"),
                ("project_id", "INFISICAL_PROJECT_ID"),
                ("project_slug", "INFISICAL_PROJECT_SLUG"),
                ("environment", "INFISICAL_ENVIRONMENT"),
                ("secret_path", "INFISICAL_SECRET_PATH"),
            ):
                val = str(config_infisical.get(cfg_key) or "").strip()
                if val.startswith("env:"):
                    val = (os.getenv(val[4:]) or "").strip()
                if val:
                    merged_env[env_key] = val
            check_client = secrets.InfisicalClient(secrets.InfisicalConfig.from_env(merged_env))

        results = []
        failed = 0
        for source, raw in refs:
            try:
                parsed = secrets.parse_ref(raw)
                if check_client is not None:
                    check_client.get_secret(parsed)
                else:
                    secrets.resolve(parsed)
                results.append(dict(source=source, ref=parsed.label(), ok=True, error=None))
            except (secrets.InfisicalError, ValueError) as exc:
                failed += 1
                label = raw if isinstance(raw, str) else str(raw.get("infisical") if isinstance(raw, dict) else raw)
                results.append(dict(source=source, ref=label, ok=False, error=str(exc)))

        summary = dict(ok=(failed == 0), checked=len(results), failed=failed, refs=results)
        if json:
            output(summary, True)
        elif not results:
            typer.echo("No infisical: references found.")
        else:
            for row in results:
                mark = "OK  " if row["ok"] else "FAIL"
                detail = f"  ({row['error']})" if row["error"] else ""
                typer.echo(f"{mark} {row['source']:<36} -> {row['ref']}{detail}")
            typer.echo(f"{len(results) - failed}/{len(results)} resolved")
        if failed:
            raise typer.Exit(1)
