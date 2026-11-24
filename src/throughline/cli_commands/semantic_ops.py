"""`throughline lib expand HANDLE MODE` and `throughline lib match HANDLE CONDITION`:
the semantic operators over HTTP (POST /v1/expand, POST /v1/match)."""

import typer


def _summary(result):
    from ..cli import output

    for record in result["records"]:
        content = (record.get("content") or "").replace("\n", " ")
        meta = record.get("meta") or {}
        output(
            dict(
                record_id=record["record_id"],
                kind=record["kind"],
                role=record.get("role"),
                depends_on=record.get("depends_on"),
                verdict=meta.get("verdict"),
                content=content[:120],
            )
        )
    execution = result["execution"]
    line = (
        f"{len(result['records'])} records  status={execution['status']}"
        f"  completeness={execution['completeness']}"
    )
    if result.get("result_set"):
        line += f"  result_set={result['result_set']}"
    if execution.get("approval"):
        line += f"  approval_id={execution['approval'].get('approval_id')}"
    typer.echo(line)


def register(group):
    @group.command("expand")
    def expand(
        handle: str = typer.Argument(..., help="Result set to expand (rs_...)."),
        mode: str = typer.Argument(
            ..., help="context | thread | sources | links | time | place | focus"
        ),
        before: int = typer.Option(3, help="Thread neighbours before (context)."),
        after: int = typer.Option(3, help="Thread neighbours after (context)."),
        link_type: str | None = typer.Option(None, help="Only links of this type."),
        depth: int = typer.Option(1, help="Link steps (links, focus)."),
        window_s: int = typer.Option(0, help="Time window in seconds (time)."),
        meters: float = typer.Option(100, help="Radius in meters (place)."),
        focus: str | None = typer.Option(None, help="Focus text (focus)."),
        k: int = typer.Option(20, help="How many focus members."),
        as_of: str | None = typer.Option(None, help="Record-time cutoff."),
        known_by: str | None = typer.Option(None, help="Ingest-time cutoff."),
        drop_input: bool = typer.Option(False, help="Leave the input members out."),
        json: bool = False,
    ):
        """Expand a result set by a mechanical relation."""
        from ..cli import output, request

        body = dict(
            input=handle,
            mode=mode,
            before=before,
            after=after,
            link_type=link_type,
            depth=depth,
            window_s=window_s,
            meters=meters,
            focus=focus,
            k=k,
            as_of=as_of,
            known_by=known_by,
            keep_input=not drop_input,
        )
        result = request(
            "POST",
            "expand",
            data={key: value for key, value in body.items() if value is not None},
        )
        if json:
            output(result, True)
            return
        _summary(result)

    @group.command("match")
    def match(
        handle: str = typer.Argument(..., help="Result set to judge (rs_...)."),
        condition: str = typer.Argument(..., help="What a member must satisfy."),
        max_rows: int = typer.Option(200, help="Refuse sets with more members."),
        grain: str = typer.Option("record", help="record | episode"),
        approval_id: str | None = typer.Option(None, help="An approved request."),
        dry_run: bool = typer.Option(False, help="Estimate only; nothing is sent."),
        json: bool = False,
    ):
        """Judge every member of a set: match, nonmatch or uncertain."""
        from ..cli import output, request

        body = dict(
            input=handle,
            condition=condition,
            max_rows=max_rows,
            grain=grain,
            approval_id=approval_id,
            dry_run=dry_run,
        )
        result = request(
            "POST",
            "match",
            data={key: value for key, value in body.items() if value is not None},
        )
        if json or dry_run:
            output(result, True)
            return
        _summary(result)
