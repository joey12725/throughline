"""Link leg: from the starting sets (or scope.message_ids), the links that
touch them as 'l:' records, and their other message ends as hits after
api.scope_filter with the full hard scope. Only starting records that satisfy
the full hard scope (and any as_of/known_by cutoff) are expanded: a record
outside the scope is never a source of hits. Under as_of/known_by links come
from api.links_asof when it exists (otherwise only links created by the
cutoff, in their current state, and the leg reports as_of not honoured)."""

from ..scope_sql import admits, lit
from ..types import Candidate
from . import Leg

LINKS_CAP = 5000


def message_ends(link):
    for kind, ref in (
        (link["from_kind"], link["from_id"]),
        (link["to_kind"], link["to_id"]),
    ):
        if kind == "message":
            yield ref


class Links(Leg):
    name = "links"
    exact = True
    kinds = ("link", "life_event", "reference", "derived")
    implements = "link_expansion"
    implementation = "links_table"

    def honours_as_of(self, ctx):
        return ctx.capabilities()["links_asof"]

    def available(self, ctx):
        if ctx.request.raw_only:
            return False, "raw_only"
        if not ctx.message_ids:
            return False, "no_starting_records"
        if not admits(ctx.resolved.scope, "link") and ctx.resolved.kinds == []:
            return False, "kinds_excluded"
        return True, None

    def run(self, ctx):
        in_scope = ctx.filter_hits(ctx.message_ids)
        if not in_scope:
            return []
        seeds = lit(sorted(in_scope))
        source, extra = ctx.links_source()
        result = ctx.query(
            "SELECT l.id, l.type, l.from_kind, l.from_id, l.to_kind, l.to_id, l.confidence FROM "
            + source
            + " l WHERE l.status='active' AND ((l.from_kind='message' AND l.from_id = ANY("
            + seeds
            + ")) OR (l.to_kind='message' AND l.to_id = ANY("
            + seeds
            + ")))"
            + extra
            + " ORDER BY l.confidence DESC NULLS LAST, l.id",
            cap=LINKS_CAP,
            leg=self.name,
        )
        links = result["rows"]
        allowed = ctx.filter_hits([ref for link in links for ref in message_ends(link)])
        records = admits(ctx.resolved.scope, "link")
        result, seen = [], set()
        for link in links:
            via = f"l:{link['id']}"
            if records:
                result.append(
                    Candidate(record_id=via, kind="link", score=link["confidence"])
                )
            for ref in message_ends(link):
                if ref not in allowed or ref in seen:
                    continue
                seen.add(ref)
                result.append(
                    Candidate(
                        record_id=f"m:{ref}",
                        message_id=ref,
                        score=link["confidence"],
                        discovery=[
                            dict(leg=self.name, detail=dict(via=via, type=link["type"]))
                        ],
                    )
                )
        return result


LEG = Links()
