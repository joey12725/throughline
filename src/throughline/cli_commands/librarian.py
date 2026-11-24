"""`throughline lib librarian investigate GOAL` and `throughline lib investigation ID`.

investigate runs over HTTP with THROUGHLINE_TOKEN (the investigation runs
under that token's scope; the asker comes from the token, never from an
option); `throughline lib librarian GOAL ...` still means investigate.
"""

import json as json_module

import typer
from typer.core import TyperGroup


class _InvestigateByDefault(TyperGroup):
    """`lib librarian GOAL ...` (the form before the modes) runs investigate."""

    def parse_args(self, ctx, args):
        if args and args[0] not in self.commands and args[0] not in ("--help", "-h"):
            args = ["investigate", *args]
        return super().parse_args(ctx, args)


def register(group):
    librarian_app = typer.Typer(cls=_InvestigateByDefault, no_args_is_help=True)
    group.add_typer(
        librarian_app,
        name="librarian",
        short_help="Investigate a goal on demand.",
    )

    @librarian_app.command("investigate")
    def investigate(
        goal: str = typer.Argument(..., help="The retrieval objective."),
        question: str | None = typer.Option(None, help="The original question."),
        context: str | None = typer.Option(None, help="Clarification for the goal."),
        channel: list[str] = typer.Option(None, help="Channel scope (repeatable)."),
        kind: list[str] = typer.Option(None, help="Record kind scope (repeatable)."),
        after: str | None = typer.Option(None, help="Time scope start (ISO)."),
        before: str | None = typer.Option(None, help="Time scope end (ISO)."),
        as_of: str | None = typer.Option(None, help="Record-time cutoff."),
        known_by: str | None = typer.Option(None, help="Ingest-time cutoff."),
        starting_set: list[str] = typer.Option(
            None, help="Result set already obtained (repeatable)."
        ),
        must_include: list[str] = typer.Option(
            None, help="Record id or term that must be represented (repeatable)."
        ),
        deadline_ms: int | None = typer.Option(None, help="Deadline budget."),
        tokens: int | None = typer.Option(None, help="Returned-token budget."),
        model_cost: float | None = typer.Option(None, help="Model cost budget (USD)."),
        turns: int | None = typer.Option(None, help="Controller turns (default 2)."),
        approval_id: str | None = None,
        json: bool = False,
    ):
        """Investigate a goal on demand: returns an evidence packet."""
        from ..cli import output, request

        scope = dict(
            channels=channel or None,
            kinds=kind or None,
            after=after,
            before=before,
        )
        budgets = dict(
            deadline_ms=deadline_ms,
            tokens=tokens,
            model_cost=model_cost,
            controller_turns=turns,
        )
        body = dict(
            goal=goal,
            question=question,
            context=context,
            scope={k: v for k, v in scope.items() if v is not None},
            starting_sets=starting_set or [],
            coverage_requirement=dict(must_include=must_include or []),
            budgets={k: v for k, v in budgets.items() if v is not None},
            as_of=as_of,
            known_by=known_by,
            approval_id=approval_id,
        )
        body = {k: v for k, v in body.items() if v is not None}
        packet = request("POST", "librarian", data=body)
        if json:
            output(packet, True)
            return
        for record in packet["records"]:
            excerpt = (record.get("original_excerpt") or "").replace("\n", " ")
            output(
                dict(
                    record_id=record["record_id"],
                    kind=record["kind"],
                    evidence_class=record.get("evidence_class"),
                    excerpt=excerpt[:120],
                )
            )
        for note in packet.get("orientation") or []:
            typer.echo("note (generated): " + note["generated_text"][:200])
        for need in packet.get("unresolved_needs") or []:
            typer.echo("unresolved: " + str(need))
        typer.echo(
            f"{len(packet['records'])} records  status={packet['execution_status']}"
            + (
                f"  reason={packet['incompleteness_reason']}"
                if packet.get("incompleteness_reason")
                else ""
            )
            + f"  investigation={packet['investigation_id']}"
        )

    @group.command("investigation")
    def investigation(investigation_id: str, json: bool = False):
        """An investigation of yours, rebuilt under your current scope."""
        from urllib.parse import quote

        from ..cli import output, request

        result = request("GET", "investigations/" + quote(investigation_id, safe=""))
        if json:
            output(result, True)
            return
        output(
            dict(
                id=result["id"],
                status=result["status"],
                reason=result.get("reason"),
                plans=len(result.get("plans") or []),
                failed_avenues=len(result.get("failed_avenues") or []),
                records=len((result.get("packet") or {}).get("records") or []),
            )
        )
        typer.echo(json_module.dumps(result.get("coverage") or {}, default=str))
