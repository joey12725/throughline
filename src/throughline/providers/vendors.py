"""Named vendors: Voyage AI, Google Gemini and OpenRouter.

Each is selected by name (THROUGHLINE_<KIND>_PROVIDER=voyage | gemini |
openrouter) and speaks the vendor's own interface, so what the generic
OpenAI-compatible provider cannot express is used: query and document input
types, contextualized chunk embeddings (voyage-context-4), the Gemini task
prompts, OpenRouter's data policy, and the asynchronous batch interfaces of
Voyage and Gemini for historic loads (providers.batch).

Requests go through providers.http.request, so the egress allow-list applies:
the vendor's host must be listed in THROUGHLINE_EGRESS_ALLOW.

None of this is exercised against the live services by the tests (they need
credentials); the request and response shapes follow the vendors' published
references and are tested against recorded responses on loopback.
"""

import json
import os
import time

from . import http
from .base import ProviderUnavailable, text_tokens

# Known models: (default dimensions, USD per million tokens). A model that is
# not listed works too: set THROUGHLINE_EMBEDDING_DIMENSIONS and
# _PRICE_PER_MILLION for it.
VOYAGE_MODELS = {
    "voyage-4-large": (1024, 0.12),
    "voyage-4": (1024, 0.06),
    "voyage-4-lite": (1024, 0.02),
    "voyage-context-4": (1024, 0.12),
    "voyage-context-3": (1024, 0.18),
    "voyage-3-large": (1024, 0.18),
    "voyage-3.5": (1024, 0.06),
    "voyage-3.5-lite": (1024, 0.02),
    "voyage-code-3": (1024, 0.18),
}
GEMINI_MODELS = {
    "gemini-embedding-2": (1536, 0.20),
    "gemini-embedding-001": (1536, 0.15),
}
OPENROUTER_MODELS = {
    "openai/text-embedding-3-small": (1536, 0.02),
    "openai/text-embedding-3-large": (3072, 0.13),
    "google/gemini-embedding-001": (1536, 0.15),
    "google/gemini-embedding-2": (1536, 0.20),
    "mongodb/voyage-4-large": (1024, 0.12),
    "mongodb/voyage-4": (1024, 0.06),
    "mongodb/voyage-4-lite": (1024, 0.02),
    "baai/bge-m3": (1024, 0.01),
    "mistralai/mistral-embed-2312": (1024, 0.10),
}
RERANK_PRICES = {
    "rerank-2.5": 0.05,
    "rerank-2.5-lite": 0.02,
}

# name -> per kind: default base URL, default model, conventional key variables.
VENDORS = {
    "voyage": dict(
        base="https://api.voyageai.com/v1",
        keys=("VOYAGE_API_KEY",),
        models=dict(embedding="voyage-4-large", rerank="rerank-2.5"),
        known=VOYAGE_MODELS,
    ),
    "gemini": dict(
        base="https://generativelanguage.googleapis.com/v1beta",
        # Chat goes through Gemini's OpenAI-compatible surface.
        chat_base="https://generativelanguage.googleapis.com/v1beta/openai",
        keys=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        models=dict(embedding="gemini-embedding-2"),
        known=GEMINI_MODELS,
    ),
    "openrouter": dict(
        base="https://openrouter.ai/api/v1",
        keys=("OPENROUTER_API_KEY",),
        models=dict(rerank="cohere/rerank-v3.5"),
        known=OPENROUTER_MODELS,
    ),
}
# Which kinds a vendor serves.
KINDS = {
    "voyage": ("embedding", "rerank"),
    "gemini": ("embedding", "semantic", "controller"),
    "openrouter": ("embedding", "rerank", "semantic", "controller"),
}

# Estimated tokens (characters / 4) sent in one request. The estimate can be
# well under a vendor's real count, so these stay far below the vendors'
# limits (Voyage: 120K per request for its large models, 32K per contextual
# document).
REQUEST_TOKENS = 60000
DOCUMENT_TOKENS = 16000
# One contextual request: Voyage's reference gives both 120K and 32K as the
# total across its documents, so the lower one is respected.
CONTEXT_REQUEST_TOKENS = 24000


def slices(items, max_items, max_tokens, size=lambda item: text_tokens(item)):
    """Consecutive runs of items within both limits (an item above the token
    limit goes alone: the vendor truncates it)."""
    run, used = [], 0
    for item in items:
        tokens = size(item)
        if run and (len(run) >= max_items or used + tokens > max_tokens):
            yield run
            run, used = [], 0
        run.append(item)
        used += tokens
    if run:
        yield run


def fit(vector, dimensions):
    """A unit vector of `dimensions` numbers. A longer vector is cut: the
    models offered here are Matryoshka-trained, so a prefix is the smaller
    embedding once it is normalised again."""
    if len(vector) > dimensions:
        vector = vector[:dimensions]
    return http.unit(vector)


def openrouter_routing():
    """OpenRouter provider preferences. Default: no upstream may keep or
    train on what is sent. THROUGHLINE_OPENROUTER_PROVIDER replaces it with
    any JSON object of OpenRouter's `provider` field ('{}' sends none)."""
    value = os.getenv("THROUGHLINE_OPENROUTER_PROVIDER", "").strip()
    if not value:
        return {"data_collection": "deny"}
    try:
        routing = json.loads(value)
    except ValueError:
        routing = None
    if not isinstance(routing, dict):
        raise ValueError("THROUGHLINE_OPENROUTER_PROVIDER must be a JSON object")
    return routing


class VendorEmbedder(http.HttpProvider):
    asymmetric = True
    retries = 3

    def __init__(self, base, model, dimensions, **kwargs):
        super().__init__(base, model, **kwargs)
        self.dimensions = int(dimensions)

    def with_timeout(self, seconds):
        other = super().with_timeout(seconds)
        # An interactive query does not wait out a rate limit.
        if seconds < 30:
            other.retries = 0
        return other

    def _request(self, path, payload, headers=None, key=True):
        return http.request(
            "POST",
            self.base + path,
            key=self.key if key else None,
            headers=headers,
            payload=payload,
            timeout=self.timeout,
            what="embedding",
            retries=self.retries,
        )


class VoyageEmbedder(VendorEmbedder, http.Embedder):
    """POST {base}/embeddings and, for voyage-context-*, /contextualizedembeddings."""

    name = "voyage"
    version = "voyage-v1"
    # Asynchronous batch requests cost two thirds of the price.
    batch_discount = 0.67

    @property
    def contextual(self):
        return self.model.startswith("voyage-context")

    def embed(self, texts, model=None, dimensions=None, input_type="document"):
        started = time.perf_counter()
        model, dimensions = model or self.model, dimensions or self.dimensions
        texts = list(texts)
        if model.startswith("voyage-context"):
            # No shared context: each text is a document of one chunk. This is
            # how a query is embedded for a contextual model.
            documents, usage = self.embed_documents(
                [[t] for t in texts], model, dimensions, input_type
            )
            return [d[0] for d in documents], usage
        vectors, tokens, calls = [], 0, 0
        for run in slices(texts, 128, REQUEST_TOKENS):
            data = self._request(
                "/embeddings",
                {
                    "input": run,
                    "model": model,
                    "input_type": input_type,
                    "output_dimension": dimensions,
                    "truncation": True,
                },
            )
            vectors += http.checked_vectors(
                _indexed(data.get("data")), len(run), dimensions
            )
            tokens += http.reported_tokens(data, text_tokens(*run))
            calls += 1
        return vectors, self._usage(
            tokens,
            calls=calls or 1,
            ms=(time.perf_counter() - started) * 1000,
            model=model,
        )

    def embed_documents(
        self, documents, model=None, dimensions=None, input_type="document"
    ):
        """One vector per chunk, each computed with the other chunks of its
        document in view. documents: lists of chunk texts, in reading order."""
        started = time.perf_counter()
        model, dimensions = model or self.model, dimensions or self.dimensions
        documents = [list(d) for d in documents]
        result, tokens, calls = [], 0, 0
        for run in slices(
            documents, 200, CONTEXT_REQUEST_TOKENS, size=lambda d: text_tokens(*d)
        ):
            data = self._request(
                "/contextualizedembeddings",
                {
                    "inputs": run,
                    "model": model,
                    "input_type": input_type,
                    "output_dimension": dimensions,
                },
            )
            result += contextual_vectors(data, run, dimensions)
            tokens += http.reported_tokens(data, sum(text_tokens(*d) for d in run))
            calls += 1
        return result, self._usage(
            tokens,
            calls=calls or 1,
            ms=(time.perf_counter() - started) * 1000,
            model=model,
        )

    def batch_client(self):
        from . import batch

        return batch.VoyageBatch(self)


def _indexed(rows):
    """The embeddings of an OpenAI-shaped data list, in index order."""
    try:
        return [r["embedding"] for r in sorted(rows, key=lambda r: r["index"])]
    except (KeyError, TypeError):
        raise ProviderUnavailable("invalid embedding response") from None


def contextual_vectors(data, documents, dimensions):
    """Vectors of a /contextualizedembeddings answer, one list per document."""
    try:
        groups = sorted(data["data"], key=lambda g: g["index"])
        if len(groups) != len(documents):
            raise KeyError
        return [
            http.checked_vectors(_indexed(group["data"]), len(document), dimensions)
            for group, document in zip(groups, documents)
        ]
    except (KeyError, TypeError):
        raise ProviderUnavailable("invalid embedding response") from None


class GeminiEmbedder(VendorEmbedder, http.Embedder):
    """POST {base}/models/{model}:batchEmbedContents, key in x-goog-api-key.

    gemini-embedding-001 takes the task as taskType. Later models take it as
    a prefix of the text (the documented retrieval formats), so a query and a
    document are embedded for retrieval with every model.
    """

    name = "gemini"
    version = "gemini-v1"
    # Asynchronous batch requests cost half the price.
    batch_discount = 0.5

    def request_for(self, text, model, dimensions, input_type):
        """One EmbedContentRequest."""
        request = {"model": "models/" + model, "outputDimensionality": dimensions}
        if model == "gemini-embedding-001":
            request["taskType"] = (
                "RETRIEVAL_QUERY" if input_type == "query" else "RETRIEVAL_DOCUMENT"
            )
        elif input_type == "query":
            text = "task: search result | query: " + text
        else:
            text = "title: none | text: " + text
        request["content"] = {"parts": [{"text": text}]}
        return request

    def embed(self, texts, model=None, dimensions=None, input_type="document"):
        started = time.perf_counter()
        model, dimensions = model or self.model, dimensions or self.dimensions
        texts = list(texts)
        vectors, tokens, calls = [], 0, 0
        for run in slices(texts, 100, REQUEST_TOKENS):
            data = http.request(
                "POST",
                f"{self.base}/models/{model}:batchEmbedContents",
                headers={"x-goog-api-key": self.key} if self.key else None,
                payload={
                    "requests": [
                        self.request_for(t, model, dimensions, input_type) for t in run
                    ]
                },
                timeout=self.timeout,
                what="embedding",
                retries=self.retries,
            )
            try:
                found = [fit(e["values"], dimensions) for e in data["embeddings"]]
            except (KeyError, TypeError):
                raise ProviderUnavailable("invalid embedding response") from None
            vectors += http.checked_vectors(found, len(run), dimensions)
            tokens += gemini_tokens(data, text_tokens(*run))
            calls += 1
        return vectors, self._usage(
            tokens,
            calls=calls or 1,
            ms=(time.perf_counter() - started) * 1000,
            model=model,
        )

    def batch_client(self):
        from . import batch

        return batch.GeminiBatch(self)


def gemini_tokens(data, fallback):
    usage = data.get("usageMetadata") if isinstance(data, dict) else None
    tokens = usage.get("promptTokenCount") if isinstance(usage, dict) else None
    return (
        tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else fallback
    )


class OpenRouterEmbedder(VendorEmbedder, http.Embedder):
    """POST {base}/embeddings: OpenAI's shape plus input_type and the routing
    preferences. The cost OpenRouter reports for a request is what the ledger
    records. No batch client: OpenRouter's batch interface gives no discount
    and takes no input_type, so a historic load uses large ordinary requests.
    """

    name = "openrouter"
    version = "openrouter-v1"

    def embed(self, texts, model=None, dimensions=None, input_type="document"):
        started = time.perf_counter()
        model, dimensions = model or self.model, dimensions or self.dimensions
        texts = list(texts)
        routing = openrouter_routing()
        vectors, tokens, calls, cost = [], 0, 0, 0.0
        for run in slices(texts, 128, REQUEST_TOKENS):
            payload = {
                "model": model,
                "input": run,
                "dimensions": dimensions,
                "input_type": "search_query"
                if input_type == "query"
                else "search_document",
            }
            if routing:
                payload["provider"] = routing
            data = self._request("/embeddings", payload)
            vectors += http.checked_vectors(
                [fit(v, dimensions) for v in _indexed(data.get("data"))],
                len(run),
                dimensions,
            )
            used = http.reported_tokens(data, text_tokens(*run))
            tokens += used
            calls += 1
            reported = (data.get("usage") or {}).get("cost")
            if isinstance(reported, (int, float)) and not isinstance(reported, bool):
                cost += float(reported)
            else:
                cost += used * self.price_per_million / 1000000
        usage = self._usage(
            tokens,
            calls=calls or 1,
            ms=(time.perf_counter() - started) * 1000,
            model=model,
        )
        if calls:
            usage = type(usage)(
                model=usage.model,
                tokens=usage.tokens,
                cost=cost,
                calls=usage.calls,
                ms=usage.ms,
            )
        return vectors, usage


class OpenRouterReranker(http.HttpReranker):
    name = "openrouter"
    version = "openrouter-v1"

    @property
    def extra(self):
        routing = openrouter_routing()
        return {"provider": routing} if routing else {}


class VoyageReranker(http.HttpReranker):
    name = "voyage"
    version = "voyage-v1"
    extra = {"truncation": True}


class OpenRouterSemanticModel(http.HttpSemanticModel):
    name = "openrouter"

    @property
    def extra(self):
        routing = openrouter_routing()
        return {"provider": routing} if routing else {}


class OpenRouterController(http.HttpController):
    name = "openrouter"

    @property
    def extra(self):
        routing = openrouter_routing()
        return {"provider": routing} if routing else {}


class GeminiSemanticModel(http.HttpSemanticModel):
    name = "gemini"


class GeminiController(http.HttpController):
    name = "gemini"


CLASSES = {
    ("voyage", "embedding"): VoyageEmbedder,
    ("voyage", "rerank"): VoyageReranker,
    ("gemini", "embedding"): GeminiEmbedder,
    ("gemini", "semantic"): GeminiSemanticModel,
    ("gemini", "controller"): GeminiController,
    ("openrouter", "embedding"): OpenRouterEmbedder,
    ("openrouter", "rerank"): OpenRouterReranker,
    ("openrouter", "semantic"): OpenRouterSemanticModel,
    ("openrouter", "controller"): OpenRouterController,
}


def build(vendor, kind, setting, price):
    """The provider of a vendor for a kind, from THROUGHLINE_<KIND>_* with the
    vendor's defaults. setting(name) reads one of the kind's variables;
    price(default) reads its price."""
    spec = VENDORS[vendor]
    label = f"THROUGHLINE_{kind.upper()}"
    if kind not in KINDS[vendor]:
        raise ValueError(
            f"{label}_PROVIDER={vendor}: {vendor} offers " + ", ".join(KINDS[vendor])
        )
    chat = kind in ("semantic", "controller")
    base = (
        os.getenv(f"{label}_BASE_URL")
        or (spec.get("chat_base") if chat else None)
        or spec["base"]
    )
    # The kind's own key, else the vendor's conventional variable. The shared
    # THROUGHLINE_MODEL_KEY belongs to THROUGHLINE_MODEL_BASE_URL and is never
    # sent to a vendor.
    from .. import secrets

    key = os.getenv(f"{label}_KEY") or next(
        (os.getenv(name) for name in spec["keys"] if os.getenv(name)), None
    )
    if key and secrets.is_ref(key):
        try:
            key = secrets.resolve(key)
        except secrets.InfisicalError as exc:
            raise ProviderUnavailable(str(exc)) from None
    unavailable = "embeddings" if kind == "embedding" else f"{kind} provider"
    if not key:
        raise ProviderUnavailable(
            f"{unavailable} unavailable: set {label}_KEY or {spec['keys'][0]}"
        )
    model = setting("MODEL") or spec["models"].get(kind)
    if not model:
        raise ProviderUnavailable(
            f"{unavailable} unavailable: set {label}_MODEL for {vendor}"
        )
    cls = CLASSES[(vendor, kind)]
    if kind != "embedding":
        return cls(
            base, model, key=key, price_per_million=price(RERANK_PRICES.get(model, 0.0))
        )
    known = spec["known"].get(model)
    dimensions = os.getenv(f"{label}_DIMENSIONS") or (known[0] if known else None)
    if not dimensions:
        raise ProviderUnavailable(
            f"embeddings unavailable: set {label}_DIMENSIONS for model {model}"
        )
    dimensions = int(dimensions)
    if not 1 <= dimensions <= 16000:
        raise ValueError("embedding dimensions must be 1..16000")
    return cls(
        base,
        model,
        dimensions,
        key=key,
        price_per_million=price(known[1] if known else 0.0),
    )
