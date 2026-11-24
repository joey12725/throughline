"""`throughline lib sources`: is every data source up, and did it catch up.

Uses the owner database connection (THROUGHLINE_DATABASE_URL), like `lib
backup`, so it answers even when the API server is down.

    throughline lib sources              the status board (same as `status`)
    throughline lib sources check        exit 2 when a source is down; with
                                         THROUGHLINE_ALERT_CMD set, alert once
                                         per outage and once on recovery
    throughline lib sources outages      outage history and catch-up results
    throughline lib sources cadence      per channel: is the silence since the
                                         newest item unusual for its pattern?
    throughline lib sources pause NAME   a source or a channel switched off on
                                         purpose: shown, never alerted
    throughline lib sources resume NAME
"""

import typer

MARK = {
    "ok": "ok",
    "failing": "FAILING",
    "stalled": "STALLED",
    "down": "DOWN",
    "never": "NEVER",
    "paused": "paused",
}


def _when(value):
    return "-" if value is None else value.astimezone().strftime("%Y-%m-%d %H:%M")


def board(result):
    """The status board as lines of text."""
    from .. import health

    entries = result["sources"]
    lines = []
    width = max([len(e["name"]) for e in entries] + [6])
    lines.append(
        f"{'SOURCE'.ljust(width)}  {'STATE':8} {'LAST SUCCESS':16}  {'AGO':>7}  NOTE"
    )
    order = {state: i for i, state in enumerate(health.SEVERITY)}
    for entry in sorted(entries, key=lambda e: (order[e["state"]], e["name"])):
        lines.append(
            f"{entry['name'].ljust(width)}  {MARK[entry['state']]:8} "
            f"{_when(entry['last_success_at']):16}  {health.duration(entry['age_s']):>7}  {entry['note']}"
        )
    counts = result["summary"]["counts"]
    parts = [f"{counts[s]} {s}" for s in health.SEVERITY if counts[s]]
    lines.append("")
    lines.append(", ".join(parts) if parts else "no sources registered")
    states = {e["state"] for e in entries}
    if "down" in states:
        lines.append(
            "Down sources catch up on their own once they run again: they resume from "
            "their stored cursor. Restart the importer (or its timer), then run "
            "`throughline lib sources` to confirm."
        )
    if "stalled" in states:
        lines.append(
            "Stalled sources run but bring nothing new: check the source's login or "
            "feed. `throughline lib sources cadence NAME` shows each channel; "
            "`lib sources pause CHANNEL` silences a channel that has really ended."
        )
    return lines


def register(group):
    sources = typer.Typer(invoke_without_command=True)
    group.add_typer(
        sources, name="sources", short_help="Data source health: outages and catch-up."
    )

    @sources.callback()
    def default(ctx: typer.Context, json: bool = False):
        """Show every data source: up, failing or down, and since when."""
        if ctx.invoked_subcommand is None:
            status(json=json)

    @sources.command("status")
    def status(json: bool = False):
        """Show every data source: up, failing or down, and since when."""
        from .. import health
        from ..cli import output
        from ..db import connect

        with connect() as conn:
            entries = health.sources(conn)
            result = dict(summary=health.summary(entries), sources=entries)
        if json:
            output(result, True)
            return
        for line in board(result):
            typer.echo(line)

    @sources.command("check")
    def check(
        notify: bool = typer.Option(
            None,
            "--notify/--no-notify",
            help="Run THROUGHLINE_ALERT_CMD on transitions (default: when it is set)",
        ),
        quiet: bool = typer.Option(False, "--quiet", help="Print only alerts"),
        json: bool = False,
    ):
        """Exit 0 when every source is up, 2 when one is down or never ran."""
        from .. import health
        from ..cli import output
        from ..db import connect

        with connect() as conn:
            result = health.check(conn, notify=notify)
        if json:
            output(result, True)
        else:
            if not quiet:
                for line in board(result):
                    typer.echo(line)
            for alert in result["alerts"]:
                state = "sent" if alert.get("sent") else "not sent"
                extra = f" ({alert['error']})" if alert.get("error") else ""
                typer.echo(f"alert {state}: {alert['title']}{extra}")
        if not result["summary"]["ok"]:
            raise typer.Exit(2)

    @sources.command("outages")
    def outages(
        name: str = typer.Argument(None, help="Only this source"),
        limit: int = 20,
        json: bool = False,
    ):
        """Outage history: how long each lasted and what the catch-up delivered."""
        from .. import health
        from ..cli import output
        from ..db import connect

        with connect() as conn:
            health.sweep(conn)
            rows = health.outages(conn, name, limit)
        if json:
            output(rows, True)
            return
        if not rows:
            typer.echo("no outages recorded")
            return
        for row in rows:
            if row["restored_at"] is None:
                end = "still down"
            else:
                end = f"restored {_when(row['restored_at'])}, caught up {row['caught_up_messages'] or 0} messages"
                if row["catch_up"] == "partial":
                    end += " (partial: " + ", ".join(row["catch_up_reasons"]) + ")"
            start = _when(row["last_good_at"]) if row["last_good_at"] else "never good"
            error = f"; error: {row['last_error'][:120]}" if row["last_error"] else ""
            typer.echo(
                f"{row['plugin']}: {row['kind']} from {start}, {health.duration(row['duration_s'])}; {end}{error}"
            )

    @sources.command("cadence")
    def cadence_command(
        name: str = typer.Argument(None, help="Only this source"),
        json: bool = False,
    ):
        """Per channel: when items usually arrive and how unusual the silence is."""
        from .. import cadence, health
        from ..cli import output
        from ..db import connect

        with connect() as conn:
            rows = cadence.channels(conn, [name] if name else None)
        if json:
            output(rows, True)
            return
        if not rows:
            typer.echo("no channels")
            return
        width = max(len(r["channel"]) for r in rows)
        for row in rows:
            typer.echo(
                f"{row['channel'].ljust(width)}  {row['verdict']:8}  {health.channel_note(row)}"
            )

    @sources.command("pause")
    def pause(
        name: str = typer.Argument(..., help="A source (plugin) or a channel"),
        reason: str = typer.Option(None, help="Why it is off"),
    ):
        """Mark a source or channel as switched off on purpose (no alerts until resumed)."""
        from .. import health
        from ..db import connect

        try:
            with connect() as conn:
                row = health.pause(conn, name, reason)
        except LookupError as error:
            raise typer.BadParameter(str(error)) from None
        typer.echo(f"{row['kind']} {name} paused")

    @sources.command("resume")
    def resume(name: str = typer.Argument(..., help="A source (plugin) or a channel")):
        """Watch a paused source or channel again."""
        from .. import health
        from ..db import connect

        try:
            with connect() as conn:
                row = health.resume(conn, name)
        except LookupError as error:
            raise typer.BadParameter(str(error)) from None
        typer.echo(f"{row['kind']} {name} resumed")
