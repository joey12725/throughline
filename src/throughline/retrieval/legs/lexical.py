"""Keyword leg: api.search_records under the full hard scope (QRY-20).

Under as_of/known_by each stored version matches on its own text and the
scope keeps the version valid at the cutoff (BRN-5), so the leg honours it.
"""

from ..scope_sql import lit
from ..types import Candidate
from . import Leg, message_kinds_ok, query_ok


class Lexical(Leg):
    name = "lexical"
    exact = True
    kinds = ("life_event", "reference", "derived")
    honours_as_of = True
    implements = "lexical_search"
    implementation = "postgres_fts"

    def available(self, ctx):
        ok, reason = query_ok(ctx)
        if not ok:
            return ok, reason
        return message_kinds_ok(ctx)

    def run(self, ctx):
        depth = ctx.depth()
        statement = (
            "SELECT * FROM api.search_records("
            + ",".join(
                [lit(ctx.request.query), lit(depth), ctx.args(), lit(ctx.message_ids)]
            )
            + ")"
        )
        result = ctx.query(statement, cap=ctx.cap(depth), leg=self.name)
        return [
            Candidate(
                record_id=f"m:{row['message_id']}",
                message_id=row["message_id"],
                score=row["score"],
                conversation=(
                    dict(
                        id=row["conversation_id"],
                        first_no=row["first_thread_no"],
                        last_no=row["last_thread_no"],
                    )
                    if row["conversation_id"] is not None
                    else None
                ),
            )
            for row in result["rows"]
        ]


LEG = Lexical()
