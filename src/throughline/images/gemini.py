"""The pipeline's one door to Gemini multimodal image embeddings.

embed(images) -> list of vectors, for [(bytes, mime)]; GeminiPending when the
account cannot pay or is rate limited (HTTP 402 / 429 / quota / budget):
the stage records `pending` and moves on, it never marks the asset failed.
All wiring to the providers layer is here (providers.visual.embed_images,
which goes through providers.call: budget, ledger, egress).
"""

import re

MODEL = "gemini-embedding-2"
PENDING = re.compile(
    r"(HTTP 402|HTTP 429|payment|credit|billing|quota|RESOURCE_EXHAUSTED|budget exhausted|egress)", re.I
)


class GeminiPending(RuntimeError):
    """Not now: credits, rate limit, budget or configuration. Retry later."""


class GeminiFailed(RuntimeError):
    """This input failed (bad image); other inputs may succeed."""


def status_of(error):
    status = getattr(error, "status", None) or getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    return status


def classify_error(error):
    """'pending' for anything that says "not now", else 'failed'."""
    if status_of(error) in (402, 429):
        return "pending"
    text = f"{type(error).__name__}: {error}"
    if PENDING.search(text):
        return "pending"
    if type(error).__name__ in ("ProviderUnavailable", "EgressDenied") and (
        "not configured" in text or "unavailable" in text or "key" in text.lower()
    ):
        return "pending"
    return "failed"


def _default_embed(images):
    from ..providers import visual
    from ..scope import Scope

    vectors, _usage = visual.embed_images(images, scope=Scope.local(), origin="worker")
    return vectors


def embed(images, embedder=None):
    """Vectors of [(bytes, mime)] in order; GeminiPending / GeminiFailed."""
    images = list(images)
    if not images:
        return []
    try:
        return list((embedder or _default_embed)(images))
    except Exception as error:
        if classify_error(error) == "pending":
            raise GeminiPending(str(error)[:300]) from None
        raise GeminiFailed(str(error)[:300]) from None
