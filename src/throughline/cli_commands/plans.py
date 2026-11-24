"""`throughline lib plan run FILE` and `throughline lib packet HANDLE`, over
HTTP with THROUGHLINE_TOKEN (plans and packets run under that token's scope)."""

import json as json_module
from pathlib import Path

import typer


def _json_file(path, name):
    try:
        return json_module.loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise typer.BadParameter(f"{name}: {exc.strerror or exc}") from None
    except ValueError:
        raise typer.BadParameter(f"{name} must be a JSON file") from None


def register(group):
    plan = typer.Typer(no_args_is_help=True)
    group.add_typer(plan, name="plan", short_help="Plans of library operations.")

    @plan.command("run")
    def plan_run(
        file: str,
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Validate and estimate only"
        ),
        approval_id: str | None = None,
        budgets: str | None = typer.Option(None, help="JSON object of budgets"),
        json: bool = False,
    ):
        """Run a plan file ({nodes: [{id, op, args, inputs}]})."""
        from ..cli import output, request

        body = dict(plan=_json_file(file, "FILE"), dry_run=dry_run)
        if approval_id:
            body["approval_id"] = approval_id
        if budgets:
            try:
                body["budgets"] = json_module.loads(budgets)
            except ValueError:
                raise typer.BadParameter("budgets must be JSON") from None
        result = request("POST", "plans", data=body)
        if json:
            output(result, True)
            return
        output(result["handles"])
        output(
            dict(
                status=result["execution"].get("status"),
                trace_id=result["execution"].get("trace_id"),
            )
        )

    @group.command("packet")
    def packet(
        handle: str,
        notes: str | None = typer.Option(None, help="JSON file with a list of notes"),
        token_budget: int | None = None,
        json: bool = False,
    ):
        """Evidence packet of one of your result sets."""
        from ..cli import output, request

        body = dict(handle=handle)
        if notes:
            body["notes"] = _json_file(notes, "notes")
        if token_budget is not None:
            body["token_budget"] = token_budget
        output(request("POST", "packets", data=body), json)
