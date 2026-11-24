"""Entity leg: entity records for a name, and the messages linked to them.

api.entity_search lists entity and person candidates (alternatives, never
merged). Each entity becomes an 'e:' record whose support is the evidence
messages of the links used. Messages at the other end of those links become
hits only after api.scope_filter with the full hard scope, before any limit;
their discovery names the entity they were reached through. Links come from
api.links, or under as_of/known_by from api.links_asof (with validity at
as_of) when that history function exists; an entity is then returned only
when a link at the cutoff touches it (entities have no timestamp of their
own).

Entities have no visible-source proof, so their views are unrestricted-only:
for a restricted asker the leg is unavailable ('not_visible_for_asker').
"""

from ..scope_sql import admits, lit
from ..types import Candidate
from . import Leg, query_ok

ENTITIES = 10
LINKS_CAP = 5000


def evidence_ids(evidence):
    ids = evidence.get("message_ids") if isinstance(evidence, dict) else None
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]


class Entity(Leg):
    name = "entity"
    exact = False
    kinds = ("entity", "life_event", "reference", "derived")
    implements = "entity_search"
    implementation = "trigram_links"

    def honours_as_of(self, ctx):
        return ctx.capabilities()["links_asof"]

    def available(self, ctx):
        ok, reason = query_ok(ctx)
        if not ok:
            return ok, reason
        if ctx.request.raw_only:
            return False, "raw_only"
        if not admits(ctx.resolved.scope, "entity") and ctx.resolved.kinds == []:
            return False, "kinds_excluded"
        if not ctx.unrestricted():
            return False, "not_visible_for_asker"
        return True, None

    def _links(self, ctx, found):
        source, extra = ctx.links_source()
        conditions = []
        for end_kind in ("entity", "person"):
            ids = sorted(
                {
                    r["id"]
                    for r in found
                    if (r["kind"] == "person") == (end_kind == "person")
                }
            )
            if not ids:
                continue
            conditions.append(
                f"(l.from_kind='{end_kind}' AND l.from_id = ANY({lit(ids)}) AND l.to_kind='message')"
            )
            conditions.append(
                f"(l.to_kind='{end_kind}' AND l.to_id = ANY({lit(ids)}) AND l.from_kind='message')"
            )
        return ctx.query(
            "SELECT l.id, l.from_kind, l.from_id, l.to_kind, l.to_id, l.evidence, l.confidence FROM "
            + source
            + " l WHERE l.status='active' AND ("
            + " OR ".join(conditions)
            + ")"
            + extra
            + " ORDER BY l.id",
            cap=LINKS_CAP,
            leg=self.name,
        )["rows"]

    def run(self, ctx):
        found = ctx.sql(
            "SELECT * FROM api.entity_search("
            + lit(ctx.request.query)
            + ","
            + lit(ENTITIES)
            + ")",
            cap=ENTITIES,
        )
        if not found:
            return []
        links = self._links(ctx, found)
        by_end = {}
        for link in links:
            if link["from_kind"] == "message":
                end, message = (link["to_kind"], link["to_id"]), link["from_id"]
            else:
                end, message = (link["from_kind"], link["from_id"]), link["to_id"]
            by_end.setdefault(end, []).append((link, message))
        allowed = ctx.filter_hits([m for pairs in by_end.values() for _, m in pairs])
        cited = sorted(
            {i for link in links for i in evidence_ids(link.get("evidence"))}
        )
        versions = {}
        if cited:
            versions = {
                r["id"]: r["version"]
                for r in ctx.sql(
                    "SELECT m.id, m.version FROM api.message_versions m WHERE m.id = ANY("
                    + lit(cited)
                    + ")",
                    cap=len(cited),
                )
            }
        records = admits(ctx.resolved.scope, "entity")
        historical = ctx.resolved.historical
        result, seen = [], set()
        for row in found:
            is_person = row["kind"] == "person"
            end = ("person" if is_person else "entity", row["id"])
            pairs = by_end.get(end, [])
            via = ("person:" if is_person else "e:") + str(row["id"])
            # Entities carry no timestamp: under as_of/known_by only an entity
            # with a link in the link state at the cutoff is known to have
            # existed then, so no later entity reaches any stage.
            if records and not is_person and (pairs or not historical):
                support = []
                for link, _ in pairs:
                    for i in evidence_ids(link.get("evidence")):
                        if i in versions:
                            entry = dict(
                                message_id=i, version=versions[i], relation="evidence"
                            )
                            if entry not in support:
                                support.append(entry)
                result.append(
                    Candidate(
                        record_id=via,
                        kind="entity",
                        score=row["score"],
                        support=support,
                        meta=dict(links=[link["id"] for link, _ in pairs]),
                    )
                )
            ordered = sorted(pairs, key=lambda p: (-(p[0]["confidence"] or 0), p[1]))
            for link, message in ordered:
                if message not in allowed or message in seen:
                    continue
                seen.add(message)
                result.append(
                    Candidate(
                        record_id=f"m:{message}",
                        message_id=message,
                        score=row["score"],
                        discovery=[
                            dict(leg=self.name, detail=dict(via=via, link=link["id"]))
                        ],
                    )
                )
        return result


LEG = Entity()
