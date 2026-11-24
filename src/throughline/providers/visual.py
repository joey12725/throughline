"""Image-space embeddings: Gemini multimodal and the local SigLIP2 of Immich ML.

Text records keep their own model (tl.embedding_models, Voyage); images live
in a second vector space with models of their own, stored in tl.asset_vectors:

  gemini-embedding-2   (1536)  images and queries through Gemini's
                               batchEmbedContents with inline_data parts
  siglip2-so400m-384   (1152)  query text through the Immich ML /predict
                               endpoint (ViT-SO400M-16-SigLIP2-384__webli);
                               image vectors are copied from Immich

Configuration (all optional):
  THROUGHLINE_VISUAL_EMBEDDING_PROVIDER  gemini (default) | openrouter | none
  THROUGHLINE_VISUAL_EMBEDDING_MODEL     gemini-embedding-2 (the stored model name)
  THROUGHLINE_VISUAL_EMBEDDING_DIMENSIONS 1536
  THROUGHLINE_VISUAL_EMBEDDING_KEY       else GEMINI_API_KEY, GOOGLE_API_KEY
                                         (openrouter: else OPENROUTER_API_KEY)
  THROUGHLINE_VISUAL_EMBEDDING_BASE_URL  Gemini v1beta (openrouter: openrouter.ai/api/v1)
  THROUGHLINE_VISUAL_EMBEDDING_ROUTE     openrouter model id, google/<model>
  THROUGHLINE_VISUAL_EMBEDDING_PRICE_PER_MILLION 0.20
  IMMICH_ML_URL                          http://127.0.0.1:3003
  IMMICH_CLIP_MODEL               ViT-SO400M-16-SigLIP2-384__webli

The openrouter provider reaches the same Google model through OpenRouter's
OpenAI-style /embeddings endpoint (images as image_url content parts), so its
vectors share the gemini-embedding-2 space and stored model name.

Gemini's (or OpenRouter's) host must be in THROUGHLINE_EGRESS_ALLOW (providers.egress); the
Immich ML server is on loopback. Every call goes through providers.call, so
the budget, the ledger (tl.model_ledger) and the trace apply; query vectors
are cached in tl.semantic_cache under the operator 'visual_query_embedding'.
Text is never embedded here for text search.
"""

import base64
import json
import math
import os
import threading
import time
from contextlib import contextmanager

from . import cache, http
from . import call as provider_call
from .base import Embedder, Estimate, ProviderUnavailable, Usage, text_tokens

GEMINI = "gemini-embedding-2"
SIGLIP = "siglip2-so400m-384"
SIGLIP_IMMICH_NAME = "ViT-SO400M-16-SigLIP2-384__webli"
SIGLIP_DIMENSIONS = 1152
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_PRICE = 0.20
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# OpenRouter bills gemini-embedding-2 images at $0.45 per million tokens.
OPENROUTER_PRICE = 0.45
# Gemini counts an image as 258 tokens (one tile); used before the answer
# reports its own count.
IMAGE_TOKENS = 258
QUERY_CACHE_TTL_S = 30 * 86400
QUERY_TIMEOUT_S = 8
# A batch of up to REQUEST_IMAGES images takes far longer than a query.
IMAGE_TIMEOUT_S = 60
# Images in one batchEmbedContents request and inline bytes in one request.
REQUEST_IMAGES = 16
REQUEST_BYTES = 16 * 1024 * 1024

_overrides = {}
_lock = threading.Lock()


def _env(name, default=None):
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _finite(vector, dimensions=None):
    try:
        values = [float(x) for x in vector]
    except (TypeError, ValueError):
        raise ProviderUnavailable("invalid embedding response") from None
    if not values or not all(math.isfinite(x) for x in values):
        raise ProviderUnavailable("invalid embedding response")
    if dimensions is not None and len(values) != dimensions:
        raise ProviderUnavailable("invalid embedding response")
    return values


class GeminiVisualEmbedder(http.HttpProvider, Embedder):
    """POST {base}/models/{model}:batchEmbedContents, key in x-goog-api-key.

    embed_images([(bytes, mime)]) -> (vectors, Usage); embed([text],
    input_type='query') embeds query text into the same space."""

    name = "gemini"
    version = "gemini-visual-v1"
    asymmetric = True

    def __init__(self, base, model, dimensions, **kwargs):
        super().__init__(base, model, **kwargs)
        self.dimensions = int(dimensions)

    def image_request(self, data, mime):
        """One EmbedContentRequest for an image (also usable in a batch job)."""
        return {
            "model": "models/" + self.model,
            "outputDimensionality": self.dimensions,
            "content": {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": mime or "image/jpeg",
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    }
                ]
            },
        }

    def text_request(self, text):
        return {
            "model": "models/" + self.model,
            "outputDimensionality": self.dimensions,
            "content": {"parts": [{"text": "task: search result | query: " + text}]},
        }

    def _post(self, requests):
        data = http.request(
            "POST",
            f"{self.base}/models/{self.model}:batchEmbedContents",
            headers={"x-goog-api-key": self.key} if self.key else None,
            payload={"requests": requests},
            timeout=self.timeout,
            what="embedding",
        )
        try:
            vectors = [
                http.unit(_finite(e["values"])[: self.dimensions])
                for e in data["embeddings"]
            ]
        except (KeyError, TypeError):
            raise ProviderUnavailable("invalid embedding response") from None
        return http.checked_vectors(vectors, len(requests), self.dimensions), data

    def _runs(self, images):
        run, size = [], 0
        for item in images:
            length = len(item[0])
            if run and (len(run) >= REQUEST_IMAGES or size + length > REQUEST_BYTES):
                yield run
                run, size = [], 0
            run.append(item)
            size += length
        if run:
            yield run

    def embed_images(self, images):
        started = time.perf_counter()
        images = list(images)
        vectors, tokens, calls = [], 0, 0
        for run in self._runs(images):
            found, data = self._post([self.image_request(b, m) for b, m in run])
            vectors += found
            usage = data.get("usageMetadata") if isinstance(data, dict) else None
            reported = usage.get("promptTokenCount") if isinstance(usage, dict) else None
            tokens += reported if isinstance(reported, int) else IMAGE_TOKENS * len(run)
            calls += 1
        return vectors, self._usage(
            tokens, calls=calls or 1, ms=(time.perf_counter() - started) * 1000
        )

    def embed(self, texts, model=None, dimensions=None, input_type="query"):
        started = time.perf_counter()
        texts = list(texts)
        found, data = self._post([self.text_request(t) for t in texts])
        return found, self._usage(
            text_tokens(*texts), ms=(time.perf_counter() - started) * 1000
        )

    def estimate_images(self, count, purpose="visual_embedding"):
        return self._estimate(purpose, IMAGE_TOKENS * int(count), calls=1)


class OpenRouterVisualEmbedder(GeminiVisualEmbedder):
    """gemini-embedding-2 through OpenRouter: POST {base}/embeddings with a
    bearer key; an image is {"content": [{"type": "image_url", ...}]}.
    self.model stays the stored name; route is OpenRouter's model id."""

    name = "openrouter"
    version = "openrouter-gemini-visual-v1"

    def __init__(self, base, model, dimensions, route=None, **kwargs):
        super().__init__(base, model, dimensions, **kwargs)
        self.route = route or ("google/" + model)

    def image_request(self, data, mime):
        url = "data:%s;base64,%s" % (
            mime or "image/jpeg",
            base64.b64encode(data).decode("ascii"),
        )
        return {"content": [{"type": "image_url", "image_url": {"url": url}}]}

    def text_request(self, text):
        return "task: search result | query: " + text

    def _post(self, requests):
        data = http.request(
            "POST",
            self.base + "/embeddings",
            headers={"Authorization": "Bearer " + self.key} if self.key else None,
            payload={"model": self.route, "input": requests, "dimensions": self.dimensions},
            timeout=self.timeout,
            what="embedding",
        )
        try:
            rows = sorted(data["data"], key=lambda e: e.get("index", 0))
            vectors = [http.unit(_finite(e["embedding"])[: self.dimensions]) for e in rows]
        except (KeyError, TypeError, AttributeError):
            raise ProviderUnavailable("invalid embedding response") from None
        usage = data.get("usage") if isinstance(data, dict) else None
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        if isinstance(tokens, int):
            data = dict(data, usageMetadata={"promptTokenCount": tokens})
        return http.checked_vectors(vectors, len(requests), self.dimensions), data


class SiglipTextEmbedder(Embedder):
    """Query text into the SigLIP2 space through Immich ML (local, free):
    POST {IMMICH_ML_URL}/predict, multipart entries + text."""

    name = "immich-ml"
    version = "siglip2-v1"
    local = True

    def __init__(self, base, immich_model=SIGLIP_IMMICH_NAME, timeout=QUERY_TIMEOUT_S):
        self.base = base.rstrip("/")
        self.model = SIGLIP
        self.immich_model = immich_model
        self.dimensions = SIGLIP_DIMENSIONS
        self.timeout = timeout

    def embed(self, texts, model=None, dimensions=None, input_type="query"):
        started = time.perf_counter()
        entries = json.dumps({"clip": {"textual": {"modelName": self.immich_model}}})
        vectors = []
        for text in texts:
            data = http.request(
                "POST",
                self.base + "/predict",
                files={"entries": (None, entries), "text": (None, text)},
                timeout=self.timeout,
                what="image model",
            )
            value = data.get("clip")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    raise ProviderUnavailable("invalid embedding response") from None
            vectors.append(http.unit(_finite(value)))
        return vectors, self._usage(
            0, calls=len(vectors) or 1, ms=(time.perf_counter() - started) * 1000
        )


def gemini_model():
    """The configured Gemini image model name (the tl.asset_vectors model)."""
    return _env("THROUGHLINE_VISUAL_EMBEDDING_MODEL", GEMINI)


def gemini():
    """The Gemini image embedder, or ProviderUnavailable."""
    with _lock:
        stack = _overrides.get("gemini")
        if stack:
            if stack[-1] is None:
                raise ProviderUnavailable("visual embeddings unavailable: disabled")
            return stack[-1]
    provider = (_env("THROUGHLINE_VISUAL_EMBEDDING_PROVIDER", "gemini") or "").lower()
    if provider == "none":
        raise ProviderUnavailable("visual embeddings unavailable: disabled")
    if provider not in ("gemini", "openrouter"):
        raise ValueError(
            "THROUGHLINE_VISUAL_EMBEDDING_PROVIDER must be gemini, openrouter or none"
        )
    routed = provider == "openrouter"
    fallback = ("OPENROUTER_API_KEY",) if routed else ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    key = _env("THROUGHLINE_VISUAL_EMBEDDING_KEY")
    for name in fallback:
        key = key or _env(name)
    if not key:
        raise ProviderUnavailable(
            "visual embeddings unavailable: set %s or THROUGHLINE_VISUAL_EMBEDDING_KEY"
            % " or ".join(fallback)
        )
    try:
        price = float(
            _env(
                "THROUGHLINE_VISUAL_EMBEDDING_PRICE_PER_MILLION",
                OPENROUTER_PRICE if routed else GEMINI_PRICE,
            )
        )
        dimensions = int(_env("THROUGHLINE_VISUAL_EMBEDDING_DIMENSIONS", 1536))
    except ValueError:
        raise ValueError(
            "THROUGHLINE_VISUAL_EMBEDDING_PRICE_PER_MILLION and _DIMENSIONS must be numbers"
        ) from None
    if routed:
        return OpenRouterVisualEmbedder(
            _env("THROUGHLINE_VISUAL_EMBEDDING_BASE_URL", OPENROUTER_BASE),
            gemini_model(),
            dimensions,
            route=_env("THROUGHLINE_VISUAL_EMBEDDING_ROUTE"),
            key=key,
            price_per_million=price,
            timeout=QUERY_TIMEOUT_S,
        )
    return GeminiVisualEmbedder(
        _env("THROUGHLINE_VISUAL_EMBEDDING_BASE_URL", GEMINI_BASE),
        gemini_model(),
        dimensions,
        key=key,
        price_per_million=price,
        timeout=QUERY_TIMEOUT_S,
    )


def siglip():
    """The local SigLIP2 query embedder (Immich ML), or ProviderUnavailable."""
    with _lock:
        stack = _overrides.get("siglip")
        if stack:
            if stack[-1] is None:
                raise ProviderUnavailable("image model unavailable: disabled")
            return stack[-1]
    base = _env("IMMICH_ML_URL", "http://127.0.0.1:3003")
    return SiglipTextEmbedder(base, _env("IMMICH_CLIP_MODEL", SIGLIP_IMMICH_NAME))


@contextmanager
def override(which, provider):
    """Use provider (or None: unavailable) for 'gemini' or 'siglip' until the
    block ends (tests). Process-wide."""
    if which not in ("gemini", "siglip"):
        raise ValueError("which must be gemini or siglip")
    with _lock:
        _overrides.setdefault(which, []).append(provider)
    try:
        yield provider
    finally:
        with _lock:
            stack = _overrides[which]
            for index in range(len(stack) - 1, -1, -1):
                if stack[index] is provider:
                    del stack[index]
                    break


def _estimate(provider, purpose, tokens):
    return Estimate(
        purpose=purpose,
        provider=provider.name,
        model=provider.model,
        calls=1,
        tokens=int(tokens),
        cost=0.0 if provider.local else tokens * provider.price_per_million / 1e6,
        local=bool(provider.local),
    )


# A provider that refused a request (no credit: HTTP 402; key: 401/403) or
# could not be reached is not asked again for a while, so every search does
# not pay a failing round trip first. which -> monotonic time it may retry.
DOWN_S = 600
_down = {}


def _mark_down(which, exc):
    text = str(exc)
    if "(HTTP 40" in text or "unavailable or returned an invalid response" in text:
        with _lock:
            _down[which] = time.monotonic() + DOWN_S


def embed_query(text, which="gemini", *, scope, tracker=None, trace=None):
    """(vector, model) of query text in an image space ('gemini' or
    'siglip'). Cached per provider version, model, dimensions and text; a
    call is made through providers.call (origin 'query'). After a refused or
    failed call the provider is skipped for DOWN_S seconds."""
    provider = gemini() if which == "gemini" else siglip()
    if scope is None:
        raise ValueError("embed_query requires a scope")
    digest = cache.content_hash(text)
    key = cache.key(
        "visual_query_embedding",
        provider.version,
        provider.model,
        provider.name,
        provider.dimensions,
        digest,
    )
    hit = cache.get(key, scope=scope)
    vector = hit.get("embedding") if isinstance(hit, dict) else None
    if isinstance(vector, list) and len(vector) == provider.dimensions:
        return vector, provider.model

    def run():
        vectors, usage = provider.embed([text], input_type="query")
        return vectors[0], usage

    with _lock:
        until = _down.get(which)
    if until is not None and time.monotonic() < until and not _overrides.get(which):
        raise ProviderUnavailable(f"{which} image embeddings recently unavailable")
    try:
        vector, _ = provider_call(
            "embedding",
            "visual_query_embedding",
            run,
            estimate=_estimate(provider, "visual_query_embedding", text_tokens(text)),
            scope=scope,
            tracker=tracker,
            trace=trace,
            origin="query",
        )
    except ProviderUnavailable as exc:
        _mark_down(which, exc)
        raise
    with _lock:
        _down.pop(which, None)
    cache.put(
        key,
        "visual_query_embedding",
        provider.version,
        provider.model,
        digest,
        {"embedding": vector},
        ttl_s=QUERY_CACHE_TTL_S,
    )
    return vector, provider.model


def embed_images(images, *, scope, tracker=None, trace=None, origin="worker", origin_ref=None):
    """Gemini vectors of [(bytes, mime)], in order, through providers.call
    (budget and ledger, purpose 'visual_embedding'). Returns (vectors, Usage)."""
    provider = gemini()
    if isinstance(provider, http.HttpProvider):
        provider = provider.with_timeout(max(provider.timeout, IMAGE_TIMEOUT_S))
    images = list(images)
    if not images:
        return [], Usage(model=provider.model, calls=0)
    return provider_call(
        "embedding",
        "visual_embedding",
        lambda: provider.embed_images(images),
        estimate=_estimate(provider, "visual_embedding", IMAGE_TOKENS * len(images)),
        scope=scope,
        tracker=tracker,
        trace=trace,
        origin=origin,
        origin_ref=origin_ref,
    )
