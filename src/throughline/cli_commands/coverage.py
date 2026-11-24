"""`throughline lib coverage`: source coverage of the channels you may read,
over HTTP with THROUGHLINE_TOKEN."""

import typer


def register(group):
    @group.command("coverage")
    def coverage(
        channel: list[str] = typer.Option([], help="Channel (repeatable)"),
        after: str | None = typer.Option(None, help="Window start (ISO)"),
        before: str | None = typer.Option(None, help="Window end (ISO)"),
        json: bool = False,
    ):
        """Coverage per channel: status, reasons, gaps, window status."""
        from ..cli import output, request

        params = {}
        if channel:
            params["channels"] = ",".join(channel)
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        result = request("GET", "coverage", params=params)
        if json:
            output(result, True)
            return
        for row in result["channels"]:
            output(
                {
                    key: row.get(key)
                    for key in (
                        "channel",
                        "status",
                        "window_status",
                        "reasons",
                        "stale",
                    )
                }
                | {"gaps": len(row.get("gaps") or [])}
            )
        for row in result.get("plugins") or []:
            output({"plugin": row["plugin"], "status": row["status"]})
