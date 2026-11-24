"""Enumeration leg: api.list_records, every record of the hard scope by
record time (QRY-30). With exhaustive=true it has no limit, so the result is
the complete population (or reported as truncated)."""

from ..scope_sql import lit
from ..types import Candidate
from . import Leg, message_kinds_ok


class Structured(Leg):
    name = "structured"
    exact = True
    kinds = ("life_event", "reference", "derived")
    honours_as_of = True
    implements = "enumerate_scope"
    implementation = "list_records"

    def available(self, ctx):
        return message_kinds_ok(ctx)

    def run(self, ctx):
        depth = ctx.depth()
        statement = (
            "SELECT * FROM api.list_records("
            + ",".join([ctx.args(), lit(ctx.message_ids), lit(depth)])
            + ")"
        )
        result = ctx.query(statement, cap=ctx.cap(depth), leg=self.name)
        return [
            Candidate(
                record_id=f"m:{row['message_id']}",
                message_id=row["message_id"],
                thread_id=row["thread_id"],
                thread_no=row["thread_no"],
            )
            for row in result["rows"]
        ]


LEG = Structured()
