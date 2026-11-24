"""`throughline lib classifiers ...`: annotation classifiers (message kind:
one Jev/LLM question per message; python kind: agent-written code over whole
threads). tick and run work in this process against THROUGHLINE_DATABASE_URL
(for the maintenance timer); the rest go over HTTP with THROUGHLINE_TOKEN."""

import json as json_module
from pathlib import Path

import typer


def _json(text, name):
    try:
        return json_module.loads(text)
    except ValueError:
        raise typer.BadParameter(f"{name} must be JSON") from None


def _definition(path, code_path):
    data = _json(Path(path).read_text(), "definition") if path else {}
    if code_path:
        data["code"] = Path(code_path).read_text()
        data.setdefault("kind", "python")
    return data


def register(group):
    app = typer.Typer(no_args_is_help=True)
    group.add_typer(app, name="classifiers", short_help="Classifiers that write annotations.")

    @app.command("guide")
    def guide(as_json: bool = typer.Option(False, "--json")):
        """How to write a classifier: workflow, contract and examples."""
        from ..classifiers import guide as g

        out = g()
        if as_json:
            typer.echo(json_module.dumps(out, indent=2))
            return
        typer.echo(out["summary"] + "\n")
        for step in out["workflow"]:
            typer.echo(step)
        typer.echo("\n" + out["python"]["contract"])

    @app.command("list")
    def list_(as_json: bool = typer.Option(False, "--json")):
        from ..cli import output, request

        output(request("GET", "classifiers"), as_json)

    @app.command("show")
    def show(name: str, as_json: bool = typer.Option(False, "--json")):
        from ..cli import output, request

        output(request("GET", f"classifiers/{name}"), as_json)

    @app.command("check")
    def check_(
        definition: str | None = typer.Option(None, "--definition", help="JSON file with the definition"),
        code: str | None = typer.Option(None, "--code", help="Python file (kind python)"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        from ..cli import output, request

        output(request("POST", "classifiers/check", {"definition": _definition(definition, code)}), as_json)

    @app.command("try")
    def try_(
        definition: str | None = typer.Option(None, "--definition"),
        code: str | None = typer.Option(None, "--code"),
        name: str | None = typer.Option(None, "--name", help="A stored classifier instead of a definition"),
        sample: str | None = typer.Option(None, "--sample", help='JSON, e.g. {"limit": 5} or {"thread_ids": [1]}'),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """Run on a sample; writes nothing (spends model calls)."""
        from ..cli import output, request

        body = {"sample": _json(sample, "sample") if sample else {}}
        if name:
            body["name"] = name
        else:
            body["definition"] = _definition(definition, code)
        output(request("POST", "classifiers/try", body), as_json)

    @app.command("define")
    def define(
        definition: str = typer.Option(..., "--definition"),
        code: str | None = typer.Option(None, "--code"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """Create or change a classifier (owner: active; agent: proposed)."""
        from ..cli import output, request

        output(request("POST", "classifiers", _definition(definition, code)), as_json)

    for verb, status in (("approve", "active"), ("pause", "paused"), ("resume", "active"), ("reject", "rejected")):
        def make(status=status):
            def cmd(name: str, as_json: bool = typer.Option(False, "--json")):
                from ..cli import output, request

                output(request("POST", f"classifiers/{name}/status", {"status": status}), as_json)

            return cmd

        app.command(verb, help=f"Set a classifier {status} (owner).")(make())

    @app.command("backfill")
    def backfill(name: str, since: str = typer.Option("all", "--since"), as_json: bool = typer.Option(False, "--json")):
        from ..cli import output, request

        output(request("POST", f"classifiers/{name}/backfill", {"since": since}), as_json)

    @app.command("retract-marks")
    def retract(name: str, as_json: bool = typer.Option(False, "--json")):
        """Take back every annotation a classifier wrote (owner)."""
        from ..cli import output, request

        output(request("POST", f"classifiers/{name}/retract-marks", {}), as_json)

    @app.command("results")
    def results(name: str, limit: int = 20, verdict: str | None = None, as_json: bool = typer.Option(False, "--json")):
        from ..cli import output, request

        params = {"limit": limit}
        if verdict:
            params["verdict"] = verdict
        output(request("GET", f"classifiers/{name}/results", params=params), as_json)

    @app.command("run")
    def run(
        name: str,
        max_items: int | None = typer.Option(None, "--max-items"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """Run one active classifier now, in this process."""
        from ..classifiers import run as run_one
        from ..cli import output

        output(run_one(name, trigger="manual", max_items=max_items), as_json)

    @app.command("tick")
    def tick(as_json: bool = typer.Option(False, "--json")):
        """Run every active classifier once (for the maintenance timer), in this process."""
        from ..classifiers import tick as run_all
        from ..cli import output

        output(run_all(), as_json)
