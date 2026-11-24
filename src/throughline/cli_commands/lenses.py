"""CLI commands under `throughline lib lens` and `throughline lib rules`."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..cli import output, request
from ..lenses import load_spec_file


def register(group):
    lens_app = typer.Typer(
        no_args_is_help=True, short_help="Lenses: standing questions that keep a typed, cited table."
    )
    rules_app = typer.Typer(no_args_is_help=True, short_help="Arrival rules.")
    group.add_typer(lens_app, name="lens")
    group.add_typer(rules_app, name="rules")

    @lens_app.command("define")
    def lens_define(
        spec_file: Path = typer.Argument(..., help="TOML or JSON lens definition file."),
        as_json: bool = typer.Option(False, "--json"),
    ):
        spec = load_spec_file(spec_file)
        output(request("POST", "lenses", data=spec), as_json)

    @lens_app.command("list")
    def lens_list(as_json: bool = typer.Option(False, "--json")):
        output(request("GET", "lenses"), as_json)

    @lens_app.command("show")
    def lens_show(name: str, as_json: bool = typer.Option(False, "--json")):
        output(request("GET", f"lenses/{name}"), as_json)

    @lens_app.command("delete")
    def lens_delete(name: str, as_json: bool = typer.Option(False, "--json")):
        output(request("DELETE", f"lenses/{name}"), as_json)

    @lens_app.command("quote")
    def lens_quote(
        name: str,
        backfill: bool = typer.Option(False, "--backfill"),
        limit: int | None = typer.Option(None, "--limit"),
        since: str | None = typer.Option(None, "--since"),
        until: str | None = typer.Option(None, "--until"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        body = {"backfill": backfill}
        if limit is not None:
            body["limit"] = limit
        if since is not None:
            body["since"] = since
        if until is not None:
            body["until"] = until
        output(request("POST", f"lenses/{name}/quote", data=body), as_json)

    @lens_app.command("run")
    def lens_run(
        name: str,
        backfill: bool = typer.Option(False, "--backfill"),
        dry_run: bool = typer.Option(False, "--dry-run"),
        limit: int | None = typer.Option(None, "--limit"),
        since: str | None = typer.Option(None, "--since"),
        until: str | None = typer.Option(None, "--until"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        body = {"backfill": backfill, "dry_run": dry_run}
        if limit is not None:
            body["limit"] = limit
        if since is not None:
            body["since"] = since
        if until is not None:
            body["until"] = until
        output(request("POST", f"lenses/{name}/run", data=body), as_json)

    @lens_app.command("run-batch")
    def lens_run_batch(
        names: list[str] = typer.Option([], "--name"),
        limit: int | None = typer.Option(None, "--limit"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        body = {}
        if names:
            body["names"] = names
        if limit is not None:
            body["limit"] = limit
        output(request("POST", "lenses/run-batch", data=body), as_json)

    @lens_app.command("tick")
    def lens_tick(
        names: list[str] = typer.Option([], "--name", help="Only these lenses."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Show what is due; run nothing."),
        no_rules: bool = typer.Option(False, "--no-rules", help="Skip arrival rules and their retry queue."),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """Run due lenses and resume the ones an outage interrupted (for a timer).

        Runs in this process against THROUGHLINE_DATABASE_URL, not through the
        API, so a long backlog is not bound to an HTTP request."""
        from ..lenses import scheduler

        output(scheduler.tick(names=names or None, dry_run=dry_run, rules=not no_rules), as_json)

    @lens_app.command("rows")
    def lens_rows(
        name: str,
        limit: int = typer.Option(50, "--limit"),
        versions: bool = typer.Option(False, "--versions"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        output(
            request("GET", f"lenses/{name}/rows", params={"limit": limit, "versions": versions}),
            as_json,
        )

    @lens_app.command("edit-row")
    def lens_edit_row(
        name: str,
        row_id: int,
        data: str = typer.Option(..., "--data", help="JSON object of column updates."),
        note: str | None = typer.Option(None, "--note"),
        expected_hash: str | None = typer.Option(None, "--expected-hash"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        body = {"data": json.loads(data)}
        if note is not None:
            body["note"] = note
        if expected_hash is not None:
            body["expected_hash"] = expected_hash
        output(request("PATCH", f"lenses/{name}/rows/{row_id}", data=body), as_json)

    @rules_app.command("define")
    def rules_define(
        spec_file: Path = typer.Argument(..., help="JSON rule definition file."),
        as_json: bool = typer.Option(False, "--json"),
    ):
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        output(request("POST", "rules", data=spec), as_json)

    @rules_app.command("list")
    def rules_list(as_json: bool = typer.Option(False, "--json")):
        output(request("GET", "rules"), as_json)

    @rules_app.command("evaluate")
    def rules_evaluate(
        name: str | None = typer.Option(None, "--name"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        body = {"name": name} if name else {}
        output(request("POST", "rules/evaluate", data=body), as_json)
