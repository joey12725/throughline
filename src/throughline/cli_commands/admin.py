"""`throughline lib trace` and `throughline lib token-limits`."""

import typer


def register(group):
    @group.command("trace")
    def trace(trace_id: str, json: bool = False):
        """Show a recorded trace: statements, counts and timings."""
        from urllib.parse import quote

        from .. import cli

        # Quoted whole: an id can never name another path.
        cli.output(cli.request("GET", "traces/" + quote(trace_id, safe="")), json)

    @group.command("token-limits")
    def token_limits(
        token_id: int,
        set_: list[str] = typer.Option(
            [], "--set", help="name=value, repeatable; none shows the limits."
        ),
        json: bool = False,
    ):
        """Show or set a token's budget limits (owner database connection)."""
        from .. import cli, tokens

        values = {}
        for item in set_:
            name, separator, value = item.partition("=")
            if not separator or not name.strip():
                raise typer.BadParameter("--set takes name=value")
            values[name.strip().replace("-", "_")] = value.strip()
        try:
            row = (
                tokens.set_limits(token_id, **values)
                if values
                else tokens.get_limits(token_id)
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from None
        cli.output(row, json)
