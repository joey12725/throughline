"""Meaning leg: api.vector_records with the query embedded by the serving
model. Embeddings exist for current text only, so under as_of/known_by the
leg is unavailable ('embeddings_current_only') and disclosed. A scope made
only of channels that are not embedded (tl.channels.embed) has no vectors:
'channels_not_embedded'. Ranking is exact for a small eligible set and goes
through the model's HNSW index for a large one (tl.nearest_conversations)."""

import time

from ... import embeddings, providers
from ...db import connect
from ..scope_sql import lit
from ..types import Candidate
from . import Leg, message_kinds_ok, query_ok


class Vector(Leg):
    name = "vector"
    exact = False
    cost_class = "embedding"
    kinds = ("life_event", "reference", "derived")
    honours_as_of = False
    implements = "meaning_search"
    implementation = "cosine_exact_or_hnsw"

    def available(self, ctx):
        ok, reason = query_ok(ctx)
        if not ok:
            return ok, reason
        ok, reason = message_kinds_ok(ctx)
        if not ok:
            return ok, reason
        if ctx.resolved.historical:
            return False, "embeddings_current_only"
        try:
            providers.get("embedding")
        except providers.ProviderUnavailable:
            return False, "embeddings_unavailable"
        # Which model serves search, and which channels have vectors, is
        # configuration, not record data.
        channels = ctx.resolved.scope.channels
        with connect("app") as conn:
            active = conn.execute(
                "SELECT 1 FROM tl.embedding_models WHERE active"
            ).fetchone()
            embedded = (
                not channels
                or conn.execute(
                    "SELECT 1 FROM tl.channels WHERE name=ANY(%s) AND embed LIMIT 1",
                    (list(channels),),
                ).fetchone()
            )
        if not active:
            return False, "embeddings_unavailable"
        if not embedded:
            # Every channel of the scope is one the owner chose not to embed.
            return False, "channels_not_embedded"
        return True, None

    def run(self, ctx):
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
        depth = ctx.depth()
        statement = (
            "SELECT * FROM api.vector_records("
            + ",".join(
                [
                    lit(embeddings.vector(embedding)),
                    lit(depth),
                    ctx.args(),
                    lit(ctx.message_ids),
                    lit(ctx.request.query),
                    lit(model),
                ]
            )
            + ")"
        )
        result = ctx.query(statement, cap=ctx.cap(depth), leg=self.name)
        return [
            Candidate(
                record_id=f"m:{row['message_id']}",
                message_id=row["message_id"],
                score=row["score"],
                conversation=dict(
                    id=row["conversation_id"],
                    first_no=row["first_thread_no"],
                    last_no=row["last_thread_no"],
                ),
            )
            for row in result["rows"]
        ]


LEG = Vector()
