"""`throughline lib cost ...`: approvals and model spend. Owner database connection."""

from datetime import datetime

import typer


def register(group):
    cost_group = typer.Typer(no_args_is_help=True)
    group.add_typer(cost_group, name="cost", short_help="Model cost and approvals.")

    @cost_group.command("approvals")
    def approvals(status: str | None = None, json: bool = False):
        """List cost approvals (--status pending|approved|used|rejected)."""
        from .. import cost
        from ..cli import output
        from ..db import connect

        if status is not None and status not in cost.STATUSES:
            raise typer.BadParameter("status must be " + ", ".join(cost.STATUSES))
        with connect() as conn:
            rows = cost.list_approvals(conn, own=False, status=status)
        output(rows, json)

    @cost_group.command("approve")
    def approve(approval_id: str, reject: bool = False, json: bool = False):
        """Approve (or --reject) a pending approval; it then runs once."""
        from .. import cost
        from ..cli import output
        from ..db import connect

        try:
            with connect() as conn:
                row = cost.decide(
                    conn,
                    approval_id,
                    "rejected" if reject else "approved",
                    by="owner (command line)",
                )
        except (LookupError, ValueError) as error:
            raise typer.BadParameter(str(error)) from None
        output(row, json)

    @cost_group.command("spend")
    def spend(
        since: datetime | None = None,
        until: datetime | None = None,
        json: bool = False,
    ):
        """Model and embedding spend by purpose, provider, model and origin."""
        from ..cli import output
        from ..db import connect
        from ..providers import ledger

        with connect() as conn:
            result = ledger.spend(conn, since, until)
        if json:
            output(result, True)
            return
        output(result["by"])
        output({k: result[k] for k in ("cost", "tokens", "calls")})
