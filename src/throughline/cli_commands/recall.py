"""`throughline lib recall QUERY`: the shared retrieval library over HTTP."""

import typer


def register(group):
    @group.command("recall")
    def recall(
        query: str = typer.Argument(
            "", help="Words to look for; empty lists the scope."
        ),
        k: int = typer.Option(20, help="How many records."),
        strategy: str = typer.Option("auto", help="auto | keyword | meaning | fused"),
        leg: list[str] = typer.Option(None, help="Run only these legs (repeatable)."),
        channel: list[str] = typer.Option(None, help="Channel scope (repeatable)."),
        kind: list[str] = typer.Option(None, help="Record kind scope (repeatable)."),
        person: list[int] = typer.Option(None, help="Person id scope (repeatable)."),
        involved: bool = typer.Option(False, help="Person scope includes involvement."),
        after: str | None = typer.Option(
            None, help="Time scope start (ISO, with offset)."
        ),
        before: str | None = typer.Option(
            None, help="Time scope end (ISO, with offset)."
        ),
        time_basis: str = typer.Option("record", help="record | subject | either"),
        place: str | None = typer.Option(None, help="Named place scope."),
        meters: float = typer.Option(100, help="Radius of the place scope."),
        as_of: str | None = typer.Option(None, help="Record-time cutoff."),
        known_by: str | None = typer.Option(None, help="Ingest-time cutoff."),
        raw_only: bool = typer.Option(False, help="Raw messages only."),
        exhaustive: bool = typer.Option(False, help="Enumerate the whole scope."),
        save: bool = typer.Option(False, help="Keep the result as a result set."),
        trace: bool = typer.Option(False, help="Write a trace."),
        json: bool = False,
    ):
        """Recall records of every kind under a hard scope."""
        from ..cli import output, request

        scope = dict(
            channels=channel or None,
            kinds=kind or None,
            persons=person or None,
            person_role="involved" if involved else "author",
            after=after,
            before=before,
            time_basis=time_basis,
            place=dict(name=place, meters=meters) if place else None,
            as_of=as_of,
            known_by=known_by,
        )
        body = dict(
            query=query,
            k=k,
            strategy=strategy,
            legs=leg or None,
            scope={key: value for key, value in scope.items() if value is not None},
            raw_only=raw_only,
            exhaustive=exhaustive,
            save=save,
            trace=trace,
        )
        result = request("POST", "recall", data=body)
        if json:
            output(result, True)
            return
        for record in result["records"]:
            content = (record.get("content") or "").replace("\n", " ")
            output(
                dict(
                    record_id=record["record_id"],
                    kind=record["kind"],
                    record_time=record.get("record_time"),
                    score=record.get("score"),
                    content=content[:120],
                )
            )
        execution = result["execution"]
        typer.echo(
            f"{len(result['records'])} records  status={execution['status']}"
            f"  completeness={execution['completeness']}"
            + (
                f"  result_set={result['result_set']}"
                if result.get("result_set")
                else ""
            )
        )
