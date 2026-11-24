"""Dedup stage (order 30, QRY-26; record aliases).

1. Exact duplicates (the same record id) and spans contained in a span kept
   earlier in the same thread are dropped (reason 'redundant').
2. Alias pairs: one statement over the gated api.links finds active
   'same_as' links between message candidates; each connected set collapses
   into one candidate. The record whose fields.identity_quality ranks highest
   in throughline.identity_quality (THROUGHLINE_IDENTITY_QUALITY_ORDER,
   default native_id, bridge_id, fingerprint, unknown) is kept, a tie keeps the lower record id. Every
   other one goes to ctx.dropped with reason 'duplicate_alias' and its
   record id is appended to the kept candidate's meta.aliases. The kept
   candidate takes the best rank of its set and the discovery of all of it.
   Without such links nothing changes.
3. Every candidate is annotated with meta.independent_group,
   meta.independent_sources and meta.group_unsupported (selection rules).

The record facts the selection stages need (kind, thread, channel, record
time, support, and for select the text) are read here from the registered
gated record views, under the request's scope and as_of/known_by, and
cached on the context for the later stages.
"""

from ... import identity_quality
from ...budget import estimate_tokens
from .. import hydrate, selection
from ..scope_sql import lit
from . import Stage

DEFAULT_QUALITY_ORDER = identity_quality.DEFAULT_ORDER
FACTS_CAP = 2000
ALIAS_CAP = 10000
FACT_COLUMNS = (
    "record_id",
    "kind",
    "ref_id",
    "message_id",
    "version",
    "channel",
    "thread_id",
    "thread_no",
    "record_time",
    "person_id",
    "generated",
    "unsupported",
    "support",
)


def quality_order():
    return identity_quality.order()


def quality_rank(value, order=None):
    """Lower is better; an unlisted or missing quality ranks after 'unknown'."""
    return identity_quality.position(value, order)


# --- record facts (shared with select and group) -------------------------------


def _cache(ctx):
    cache = getattr(ctx, "_selection_facts", None)
    if cache is None:
        cache = {}
        ctx._selection_facts = cache
    return cache


def _statement(prefix, relation, ids, scope, content):
    columns = ",".join("r." + c for c in FACT_COLUMNS)
    if content:
        columns += ",r.content"
    if (
        prefix == "m"
        and scope is not None
        and (scope.as_of is not None or scope.known_by is not None)
    ):
        at = lit(scope.as_of) if scope.as_of is not None else "'infinity'::timestamptz"
        source = f"api.records_asof({at},{lit(scope.known_by)})"
    else:
        source = relation
    return f"SELECT {columns} FROM {source} r WHERE r.ref_id = ANY({lit(sorted(ids))})"


def fetch_rows(ctx, record_ids, content=False):
    """{record_id: facts} for record ids (one statement per prefix), cached
    on ctx. With content, facts also hold tokens, terms and lead words (the
    text itself is not kept)."""
    cache = _cache(ctx)
    wanted = {}
    for record_id in record_ids:
        known = cache.get(record_id)
        if known is not None and (not content or "tokens" in known):
            continue
        try:
            prefix, ref = record_id.split(":", 1)
            wanted.setdefault(prefix, set()).add(int(ref))
        except ValueError:
            continue
    if wanted:
        sources = hydrate.registry()
        scope = ctx.resolved.scope if ctx.resolved is not None else None
        for prefix, ids in sorted(wanted.items()):
            relation = sources.get(prefix)
            if relation is None:
                continue
            for row in ctx.sql(
                _statement(prefix, relation, ids, scope, content), cap=len(ids)
            ):
                info = {c: row.get(c) for c in FACT_COLUMNS}
                if content:
                    text = row.get("content") or ""
                    info.update(
                        tokens=estimate_tokens(text),
                        terms=sorted(selection.terms(text)),
                        lead=selection.leading_words(text),
                    )
                cache[row["record_id"]] = info
    return {r: cache[r] for r in record_ids if r in cache}


def annotate(candidate, info):
    """Copy record facts onto a candidate (fields that are still unset)."""
    if info is None:
        return candidate
    candidate.meta["facts"] = info
    if candidate.kind is None:
        candidate.kind = info.get("kind")
    if candidate.thread_id is None:
        candidate.thread_id = info.get("thread_id")
    if candidate.thread_no is None:
        candidate.thread_no = info.get("thread_no")
    if candidate.generated is None:
        candidate.generated = info.get("generated")
    if candidate.unsupported is None:
        candidate.unsupported = info.get("unsupported")
    if candidate.message_id is None and candidate.prefix == "m":
        candidate.message_id = info.get("ref_id")
    if "tokens" in info:
        candidate.tokens = info["tokens"]
    return candidate


def load_facts(ctx, candidates, content=False, cap=FACTS_CAP):
    """Annotate the first cap candidates with their record facts."""
    head = candidates[:cap]
    found = fetch_rows(ctx, [c.record_id for c in head], content=content)
    for c in head:
        annotate(c, found.get(c.record_id))
    return candidates


# --- the stage -------------------------------------------------------------------


def _contained(span, kept):
    """The kept candidate whose span contains span, else None."""
    thread, first, last = span
    if thread is None or first is None or last is None:
        return None
    return next(
        (k for t, a, b, k in kept if t == thread and a <= first and last <= b), None
    )


def drop_contained(ctx, candidates):
    """Drop repeated record ids and candidates whose span lies inside the
    explicit span (from the spans stage) of a candidate kept earlier in the
    same thread. A candidate without a span is placed at its thread_no, but
    only for current records: under as_of/known_by older versions keep stale
    thread numbers, so there only explicit spans are compared."""
    historical = ctx.resolved is not None and ctx.resolved.historical
    seen, kept_spans, result = set(), [], []
    for c in candidates:
        if c.record_id in seen:
            continue
        seen.add(c.record_id)
        span = c.span or {}
        first, last = span.get("first_no"), span.get("last_no")
        if first is None and last is None and not historical:
            first = last = c.thread_no
        item = (c.thread_id, first, last)
        container = _contained(item, kept_spans)
        if container is not None:
            # Why it was found stays visible on the span that holds it.
            for entry in c.discovery:
                if entry not in container.discovery:
                    container.discovery.append(dict(entry))
            ctx.dropped.append(
                dict(
                    record_id=c.record_id, reason="redundant", kept=container.record_id
                )
            )
            continue
        if (
            c.span is not None
            and item[0] is not None
            and first is not None
            and last is not None
        ):
            kept_spans.append(item + (c,))
        result.append(c)
    return result


def alias_links(ctx, message_ids):
    """Active same_as links between the given messages, with each end's
    identity quality, in one statement over the gated api.links."""
    ids = lit(sorted(message_ids))
    source, extra = ctx.links_source()
    return ctx.sql(
        "SELECT l.id, l.from_id, l.to_id,"
        " f.fields->>'identity_quality' AS from_quality,"
        " t.fields->>'identity_quality' AS to_quality FROM "
        + source
        + " l LEFT JOIN api.message_versions f ON f.id=l.from_id"
        " LEFT JOIN api.message_versions t ON t.id=l.to_id"
        " WHERE l.status='active' AND l.type='same_as'"
        " AND l.from_kind='message' AND l.to_kind='message'"
        " AND l.from_id = ANY("
        + ids
        + ") AND l.to_id = ANY("
        + ids
        + ")"
        + extra
        + " ORDER BY l.id",
        cap=ALIAS_CAP,
    )


def collapse_aliases(ctx, candidates, links=None):
    """Collapse candidates joined by same_as links (see the module doc)."""
    by_message = {}
    for c in candidates:
        mid = selection.message_ref(c)
        if mid is not None:
            by_message.setdefault(mid, c)
    if len(by_message) < 2:
        return candidates
    if links is None:
        links = alias_links(ctx, list(by_message)[:ALIAS_CAP])
    if not links:
        return candidates
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    quality = {}
    for link in links:
        a, b = link["from_id"], link["to_id"]
        if a not in by_message or b not in by_message or a == b:
            continue
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        quality.setdefault(a, link.get("from_quality"))
        quality.setdefault(b, link.get("to_quality"))
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    if not parent:
        return candidates
    sets = {}
    for mid in parent:
        sets.setdefault(find(mid), []).append(mid)
    order = quality_order()
    position = {c.record_id: i for i, c in enumerate(candidates)}
    set_of, kept_of, best_of = {}, {}, {}
    for index, members in enumerate(sets.values()):
        keep = min(members, key=lambda m: (quality_rank(quality.get(m), order), m))
        kept_of[index] = by_message[keep]
        ids = [by_message[m].record_id for m in members]
        best_of[index] = min(ids, key=lambda r: position[r])
        for record_id in ids:
            set_of[record_id] = index
    result = []
    for c in candidates:
        index = set_of.get(c.record_id)
        if index is None:
            result.append(c)
            continue
        kept = kept_of[index]
        if c.record_id == best_of[index]:
            # The kept record takes the best rank of its set.
            result.append(kept)
        if c is kept:
            continue
        kept.meta.setdefault("aliases", []).append(c.record_id)
        for entry in c.discovery:
            if entry not in kept.discovery:
                kept.discovery.append(dict(entry))
        if c.score is not None and (kept.score is None or c.score > kept.score):
            kept.score = c.score
        ctx.dropped.append(
            dict(record_id=c.record_id, reason="duplicate_alias", kept=kept.record_id)
        )
    return result


def annotate_groups(candidates):
    groups = selection.independent_groups(candidates)
    for c in candidates:
        info = groups.get(c.record_id)
        if info is None:
            continue
        c.meta["independent_group"] = info["group"]
        c.meta["independent_sources"] = info["independent_sources"]
        c.meta["group_unsupported"] = info["unsupported"]
    return groups


class Dedup(Stage):
    name = "dedup"
    order = 30
    exact = True

    def enabled(self, ctx):
        return True

    def run(self, ctx, candidates):
        if not candidates:
            return candidates
        load_facts(ctx, candidates)
        candidates = drop_contained(ctx, candidates)
        before = len(candidates)
        candidates = collapse_aliases(ctx, candidates)
        if len(candidates) < before:
            ctx.execution.set("aliases_collapsed", before - len(candidates))
        annotate_groups(candidates[:FACTS_CAP])
        return candidates


STAGE = Dedup()
