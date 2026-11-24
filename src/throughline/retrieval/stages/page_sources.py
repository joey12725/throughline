"""Page sources stage (order 5, BEN-11, BEN-12, QRY-20, QRY-26).

For every passage ('p:') and claim ('c:') hit, the raw messages it rests on
(its support in the registered gated record view: passage-level sources,
else the page revision's; a claim's supporting sources) become hits of their
own, right after the record that led to them. Every such message passes
api.scope_filter with the full hard scope first (one statement); a source
outside the scope (person, time, place, channels, kinds, as_of ...) is never
added. An added message has role 'hit', the score of the record it came
from, and discovery {leg: 'page_sources', detail: {via}}; a message that is
already a candidate only gains that discovery entry.

The selection stages then see the claim or passage and its sources as one
independent group (they rest on the same messages, QRY-26).

Disabled by hints.page_sources = false and by raw_only: raw legs never
depend on pages (BEN-12), and a wrong page can only add raw messages that
satisfy the scope, never remove one a raw leg found. When the request names
its legs explicitly, the stage runs only with hints.page_sources = true.
"""

from .. import selection
from . import Stage

SOURCES_CAP = 2000


class PageSources(Stage):
    name = "page_sources"
    order = 5
    exact = True

    def enabled(self, ctx):
        request = ctx.request
        if request.raw_only:
            return False
        wanted = (request.hints or {}).get("page_sources")
        if wanted is False:
            return False
        if request.legs:
            # Explicit legs are an override: the caller gets what those legs
            # find, plus sources only when it asks (hints.page_sources true).
            return wanted is True
        return True

    def run(self, ctx, candidates):
        from .dedup import fetch_rows

        generated = [
            c for c in candidates if c.prefix == "p" and c.role == "hit"
        ]
        if not generated:
            return candidates
        facts = fetch_rows(ctx, [c.record_id for c in generated])
        wanted = {}
        for candidate in generated:
            info = facts.get(candidate.record_id)
            if info is None:
                continue
            ids = []
            for entry in info.get("support") or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("relation") in ("context", "contradicts", "self"):
                    continue
                mid = entry.get("message_id")
                if (
                    isinstance(mid, int)
                    and not isinstance(mid, bool)
                    and mid not in ids
                ):
                    ids.append(mid)
            own = selection.message_ref(candidate)
            wanted[candidate.record_id] = [i for i in ids if i != own]
        every = sorted({i for ids in wanted.values() for i in ids})[:SOURCES_CAP]
        if not every:
            return candidates
        allowed = ctx.filter_hits(every)
        if not allowed:
            return candidates
        index = {c.record_id: c for c in candidates}
        out = []
        added = set()
        for candidate in candidates:
            if candidate.record_id in added:
                continue
            out.append(candidate)
            for mid in wanted.get(candidate.record_id, []):
                if mid not in allowed:
                    continue
                record_id = f"m:{mid}"
                entry = dict(
                    leg="page_sources",
                    score=candidate.score,
                    detail=dict(via=candidate.record_id),
                )
                existing = index.get(record_id)
                if existing is not None:
                    if entry not in existing.discovery:
                        existing.discovery.append(entry)
                    if existing.role != "hit":
                        existing.role, existing.depends_on = "hit", None
                    continue
                made = selection_candidate(record_id, mid, candidate.score, entry)
                index[record_id] = made
                added.add(record_id)
                out.append(made)
        return out


def selection_candidate(record_id, message_id, score, entry):
    from ..types import Candidate

    return Candidate(
        record_id=record_id,
        message_id=message_id,
        score=score,
        discovery=[entry],
    )


STAGE = PageSources()
