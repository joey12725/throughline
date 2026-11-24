"""Passages leg: page passages inside recall (BRN-6, BRN-5, QRY-20).

Pages enter recall only through this leg, as 'p:' records of kind passage,
hydrated through api.passage_records like every other kind. Revision
eligibility (the latest revision the asker may read, the revision valid at
as_of, known_by) is decided in SQL by api.search_passages and
api.vector_passages, so the leg honours as_of.

Scope: channels, as_of, known_by, after/before with time_basis ('effective'
is read as 'record': revisions carry no overlay), thread_ids, message_ids,
must_terms and exclusions are arguments of the passage functions. Persons and
place reach passages through their provenance sources: the in-scope source
messages are enumerated first with api.list_records (at most
THROUGHLINE_SCOPE_SOURCE_CAP, 5,000) and passed as source_ids; a larger set
skips the leg with reason 'scope_too_broad_for_passages', never an unscoped
search.

Unavailable with raw_only, without a query, or when the caller's kinds
exclude pages (kinds given without passage or page_revision). The vector
part runs only when embeddings are available and no as_of/known_by is set
(vectors exist for current revisions only).
"""

import os
import time

from ... import embeddings, providers
from ...db import connect
from .. import scope_sql
from ..executor import LegSkipped
from ..scope_sql import lit
from ..types import Candidate
from . import Leg, query_ok

DEFAULT_SOURCE_CAP = 5000
RRF_K = 60


def source_cap():
    try:
        value = int(os.getenv("THROUGHLINE_SCOPE_SOURCE_CAP") or DEFAULT_SOURCE_CAP)
    except ValueError:
        value = DEFAULT_SOURCE_CAP
    return max(1, min(value, 100000))


def admits_pages(scope):
    """Decided on the caller's kinds, never on the rendered SQL list."""
    return scope_sql.admits(scope, "passage") or (
        scope.kinds is not None and "page_revision" in scope.kinds
    )


class Passages(Leg):
    name = "passages"
    exact = True
    kinds = ("passage",)
    honours_as_of = True
    auto = True
    implements = "passage_search"
    implementation = "passage_functions"

    def available(self, ctx):
        if ctx.request.raw_only:
            return False, "raw_only"
        ok, reason = query_ok(ctx)
        if not ok:
            return ok, reason
        if not admits_pages(ctx.resolved.scope):
            return False, "kinds_excluded"
        return True, None

    def _vector_ready(self, ctx):
        if ctx.resolved.historical:
            return False
        try:
            providers.get("embedding")
        except (providers.ProviderUnavailable, ValueError):
            return False
        with connect("app") as conn:
            return bool(
                conn.execute(
                    "SELECT 1 FROM tl.embedding_models WHERE active"
                ).fetchone()
            )

    def _sources(self, ctx):
        """In-scope source message ids for person/place scopes, None when the
        scope has none."""
        if not scope_sql.needs_sources(ctx.resolved):
            return None
        cap = source_cap()
        rows = ctx.query(
            "SELECT * FROM api.list_records("
            + ",".join(
                [
                    scope_sql.render(scope_sql.source_values(ctx.resolved)),
                    lit(None),
                    lit(cap + 1),
                ]
            )
            + ")",
            cap=cap + 1,
            leg=self.name,
        )["rows"]
        if len(rows) > cap:
            raise LegSkipped("scope_too_broad_for_passages")
        return sorted({r["message_id"] for r in rows})

    def _arguments(self, ctx, source_ids):
        scope, resolved = ctx.resolved.scope, ctx.resolved
        return [
            lit(scope.channels, "text[]"),
            lit(scope.as_of),
            lit(True),
            lit(scope.after),
            lit(scope.before),
            lit(scope_sql.passage_time_basis(scope)),
            lit(scope.thread_ids, "bigint[]"),
            lit(ctx.message_ids, "bigint[]"),
            lit(scope.must_terms, "text[]"),
            lit(source_ids, "bigint[]"),
            lit(resolved.exclude_message_ids or None, "bigint[]"),
            lit(scope.known_by),
        ]

    def run(self, ctx):
        source_ids = self._sources(ctx)
        if source_ids is not None and not source_ids:
            return []
        depth = ctx.depth()
        cap = ctx.cap(depth)
        rest = self._arguments(ctx, source_ids)
        keyword = ctx.query(
            "SELECT * FROM api.search_passages("
            + ",".join([lit(ctx.request.query), lit(depth)] + rest)
            + ")",
            cap=cap,
            leg=self.name,
        )["rows"]
        meaning = []
        if self._vector_ready(ctx):
            started = time.perf_counter()
            try:
                embedding, model = embeddings.embed_search(
                    ctx.request.query,
                    with_model=True,
                    scope=ctx.scope,
                    tracker=ctx.tracker,
                    trace=ctx.trace,
                )
            finally:
                ctx.timing("embedding", (time.perf_counter() - started) * 1000)
            meaning = ctx.query(
                "SELECT * FROM api.vector_passages("
                + ",".join(
                    [lit(embeddings.vector(embedding)), lit(depth)]
                    + rest
                    + [lit(model)]
                )
                + ")",
                cap=cap,
                leg=self.name,
            )["rows"]
        return combine(keyword, meaning, depth)


def combine(keyword, meaning, depth=None):
    """One ranked list from the keyword and vector rows: reciprocal rank
    fusion inside the leg, ties by passage id. The candidate's score is its
    keyword score when it has one, else its similarity; both ranks and
    scores are kept in meta for the discovery entry."""
    found = {}
    for source, rows in (("keyword", keyword), ("vector", meaning)):
        for rank, row in enumerate(rows, start=1):
            entry = found.setdefault(
                row["passage_id"],
                dict(message_id=row["message_id"], fused=0.0, detail={}),
            )
            entry["fused"] += 1.0 / (RRF_K + rank)
            entry["detail"][source + "_rank"] = rank
            entry["detail"][source + "_score"] = row["score"]
    ordered = sorted(found.items(), key=lambda item: (-item[1]["fused"], item[0]))
    if depth is not None:
        ordered = ordered[:depth]
    result = []
    for passage_id, entry in ordered:
        detail = entry["detail"]
        score = detail.get("keyword_score")
        if score is None:
            score = detail.get("vector_score")
        result.append(
            Candidate(
                record_id=f"p:{passage_id}",
                kind="passage",
                message_id=entry["message_id"],
                score=score,
                discovery=[dict(leg="passages", detail=dict(detail))],
            )
        )
    return result


LEG = Passages()
