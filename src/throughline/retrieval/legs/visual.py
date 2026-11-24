"""Image legs: messages found through the images attached to them.

visual        the query embedded into an image space and matched against
              tl.asset_vectors with api.visual_records: Gemini
              (gemini-embedding-2) first; when Gemini is unavailable (no key,
              HTTP 402 when credits run out, egress) the local SigLIP2 of
              Immich ML (siglip2-so400m-384). The model used is in each
              candidate's discovery detail; with neither, the leg is skipped
              ('visual_embeddings_unavailable').
image_lexical keywords over the images' search text (kind, tags, caption,
              place, people, OCR, file name) with api.image_search_records.

Both join strategy 'auto'; without images in the scope they return no
candidates (visual makes no embedding call then). nsfw
images are left out unless the request sets include_nsfw. Image vectors and
text exist for current assets only: under as_of/known_by both are
unavailable ('images_current_only').
"""

import time

from ... import embeddings
from ...db import connect
from ...providers import ProviderUnavailable
from ...providers import visual as visual_models
from ..executor import LegSkipped
from ..scope_sql import lit
from ..types import Candidate
from . import Leg, message_kinds_ok, query_ok

def _images_ok(ctx):
    ok, reason = query_ok(ctx)
    if not ok:
        return ok, reason
    ok, reason = message_kinds_ok(ctx)
    if not ok:
        return ok, reason
    if ctx.resolved.historical:
        return False, "images_current_only"
    return True, None


def _models_present(ctx):
    """Which image spaces have vectors of images in the scope's channels
    (configuration-level facts, no record data): ['gemini', 'siglip']."""
    channels = ctx.resolved.scope.channels
    statement = (
        "SELECT 1 FROM tl.asset_vectors v JOIN tl.message_assets ma ON ma.asset_id=v.asset_id"
    )
    if channels:
        statement += " JOIN tl.channels c ON c.id=ma.channel_id AND c.name=ANY(%(channels)s)"
    statement += " WHERE v.model=%(model)s LIMIT 1"
    found = []
    with connect("app") as conn:
        for which, model in (
            ("gemini", visual_models.gemini_model()),
            ("siglip", visual_models.SIGLIP),
        ):
            params = dict(model=model, channels=list(channels or ()))
            if conn.execute(statement, params).fetchone():
                found.append(which)
    return found


def _include_nsfw(ctx):
    return bool(getattr(ctx.request, "include_nsfw", False))


def _candidates(rows, leg, detail):
    out = []
    for row in rows:
        candidate = Candidate(
            record_id=f"m:{row['message_id']}",
            message_id=row["message_id"],
            score=row["score"],
        )
        candidate.discovery.append(
            dict(leg=leg, detail=dict(detail, asset_id=row["asset_id"]))
        )
        out.append(candidate)
    return out


class Visual(Leg):
    name = "visual"
    exact = False
    cost_class = "embedding"
    kinds = ("life_event", "reference", "derived")
    honours_as_of = False
    auto = True
    implements = "image_meaning_search"
    implementation = "cosine_hnsw_image_space"

    def available(self, ctx):
        return _images_ok(ctx)

    def run(self, ctx):
        started = time.perf_counter()
        vector = model = None
        reasons = []
        present = _models_present(ctx)
        if not present:
            # No image of the scope has a vector: nothing to embed for.
            return []
        try:
            for which in present:
                try:
                    vector, model = visual_models.embed_query(
                        ctx.request.query,
                        which,
                        scope=ctx.scope,
                        tracker=ctx.tracker,
                        trace=ctx.trace,
                    )
                    break
                except ProviderUnavailable as exc:
                    reasons.append(f"{which}: {exc}"[:160])
        finally:
            ctx.timing("visual_embedding", (time.perf_counter() - started) * 1000)
        if vector is None:
            raise LegSkipped("visual_embeddings_unavailable")
        if reasons:
            ctx.execution.warn("visual leg fell back to " + model + " (" + "; ".join(reasons) + ")")
        depth = ctx.depth()
        statement = (
            "SELECT * FROM api.visual_records("
            + ",".join(
                [
                    lit(embeddings.vector(vector)),
                    lit(model),
                    lit(depth),
                    ctx.args(),
                    lit(ctx.message_ids),
                    lit(_include_nsfw(ctx)),
                ]
            )
            + ")"
        )
        result = ctx.query(statement, cap=ctx.cap(depth), leg=self.name)
        return _candidates(result["rows"], self.name, dict(model=model))


LEG = Visual()
