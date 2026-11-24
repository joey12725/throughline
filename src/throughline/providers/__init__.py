"""Model providers: selection, the call wrapper and the egress description.

    provider = providers.get("rerank")
    scores, usage = providers.call(
        "rerank", "rerank", lambda: provider.score(query, passages),
        estimate=provider.estimate(query, passages), scope=scope, tracker=tracker)

Nothing here needs credentials or a network: rerank, semantic, controller and
assistant default to deterministic local implementations and embedding stays unavailable
until it is configured (or THROUGHLINE_EMBEDDING_PROVIDER=local). A kind's
provider is local, http (any OpenAI-compatible endpoint), none, or a named
vendor with its own interface: voyage, gemini or openrouter (vendors.py).

Semantic operations are a registry (base.register_operation): 'match' is
built in, and a package registers its own operation (outputs, a local
implementation, the remote prompt) at import, without editing these files:

    providers.register_operation("classify_media_noise",
        ("media", "conversation", "uncertain"), local=rule, prompt=RULE,
        fields={"score": "unit", "matched_record_id": "string"})
"""

import json
import logging
import os
import threading
import time
from contextlib import contextmanager

import httpx

from . import cache, egress, ledger
from .base import (
    KINDS,
    Assistant,
    MATCH_OUTPUTS,
    OPERATIONS,
    RESERVED_OPERATIONS,
    Controller,
    EgressDenied,
    Embedder,
    Estimate,
    Operation,
    ProviderUnavailable,
    Reranker,
    SemanticModel,
    Usage,
    check_operation,
    get_operation,
    register_operation,
    registered_operations,
)
from .local import (
    LocalAssistant,
    LocalHashEmbedder,
    LocalLexicalReranker,
    LocalPlanController,
    LocalRuleModel,
)

__all__ = [
    "KINDS",
    "Assistant",
    "MATCH_OUTPUTS",
    "OPERATIONS",
    "RESERVED_OPERATIONS",
    "Controller",
    "EgressDenied",
    "Embedder",
    "Estimate",
    "Operation",
    "ProviderUnavailable",
    "Reranker",
    "SemanticModel",
    "Usage",
    "cache",
    "call",
    "check_operation",
    "describe",
    "egress",
    "get",
    "get_operation",
    "ledger",
    "override",
    "register_operation",
    "registered_operations",
    "selected",
    "sync_state",
]

log = logging.getLogger("throughline.providers")

EMBEDDING_UNAVAILABLE = "embeddings unavailable: configure THROUGHLINE_EMBEDDING_BASE_URL, MODEL and DIMENSIONS"
_LOCAL = {
    "embedding": LocalHashEmbedder,
    "rerank": LocalLexicalReranker,
    "semantic": LocalRuleModel,
    "controller": LocalPlanController,
    "assistant": LocalAssistant,
}
# The query helper's remote implementations (providers/assistant.py); http is
# an alias of openai (any OpenAI-compatible /chat/completions endpoint).
ASSISTANT_REMOTE = ("anthropic", "openai", "http")
_overrides = {}
_override_lock = threading.Lock()


def _kind(kind):
    if kind not in KINDS:
        raise ValueError("provider kind must be one of " + ", ".join(KINDS))
    return kind


def vendor_names(kind):
    """The named vendors that serve a kind (voyage, gemini, openrouter)."""
    from . import vendors

    return tuple(name for name, kinds in vendors.KINDS.items() if kind in kinds)


def selected(kind):
    """The configured implementation name of a kind: local | http | none, or
    a vendor that serves it (voyage | gemini | openrouter)."""
    kind = _kind(kind)
    value = os.getenv(f"THROUGHLINE_{kind.upper()}_PROVIDER", "").strip().lower()
    if not value:
        # Embedding keeps its original rule: remote once it is configured,
        # otherwise unavailable. The other kinds work offline by default.
        return "http" if kind == "embedding" else "local"
    names = ("local", "http", "none") + (
        ASSISTANT_REMOTE[:2] if kind == "assistant" else vendor_names(kind)
    )
    if value not in names:
        raise ValueError(
            f"THROUGHLINE_{kind.upper()}_PROVIDER must be one of " + ", ".join(names)
        )
    return value


def _setting(kind, name):
    """THROUGHLINE_<KIND>_<NAME>; semantic and controller share THROUGHLINE_MODEL_*."""
    value = os.getenv(f"THROUGHLINE_{kind.upper()}_{name}")
    if not value and kind in ("semantic", "controller"):
        value = os.getenv(f"THROUGHLINE_MODEL_{name}")
    return value or None


def _price(kind, default=0.0):
    value = _setting(kind, "PRICE_PER_MILLION")
    try:
        price = float(value) if value else default
    except ValueError:
        raise ValueError(
            f"THROUGHLINE_{kind.upper()}_PRICE_PER_MILLION must be a number"
        ) from None
    if not 0 <= price < float("inf"):
        raise ValueError(
            f"THROUGHLINE_{kind.upper()}_PRICE_PER_MILLION must be a number"
        )
    return price


def _http(kind):
    from .. import secrets
    from . import http

    base, model = _setting(kind, "BASE_URL"), _setting(kind, "MODEL")
    key = _setting(kind, "KEY")
    own_key = os.getenv(f"THROUGHLINE_{kind.upper()}_KEY")
    if (
        kind in ("semantic", "controller")
        and not own_key
        and os.getenv(f"THROUGHLINE_{kind.upper()}_BASE_URL")
        and (base or "").rstrip("/")
        != (os.getenv("THROUGHLINE_MODEL_BASE_URL") or "").rstrip("/")
    ):
        # The shared THROUGHLINE_MODEL_KEY belongs to THROUGHLINE_MODEL_BASE_URL;
        # it is never sent to a different endpoint configured for one kind.
        key = None
    if key and secrets.is_ref(key):
        try:
            key = secrets.resolve(key)
        except secrets.InfisicalError as exc:
            raise ProviderUnavailable(str(exc)) from None
    if kind == "embedding":
        dimensions = os.getenv("THROUGHLINE_EMBEDDING_DIMENSIONS")
        if not all((base, model, dimensions)):
            raise ProviderUnavailable(EMBEDDING_UNAVAILABLE)
        dimensions = int(dimensions)
        if not 1 <= dimensions <= 16000:
            raise ValueError("embedding dimensions must be 1..16000")
        # 0.02 is the default the embedding ledger has always used.
        return http.HttpEmbedder(
            base,
            model,
            dimensions,
            key=key,
            price_per_million=_price(kind, 0.02),
        )
    if not (base and model):
        raise ProviderUnavailable(
            f"{kind} provider unavailable: configure THROUGHLINE_{kind.upper()}_BASE_URL and _MODEL"
        )
    cls = {
        "rerank": http.HttpReranker,
        "semantic": http.HttpSemanticModel,
        "controller": http.HttpController,
    }[kind]
    return cls(base, model, key=key, price_per_million=_price(kind))


def get(kind):
    """The active provider of a kind, or ProviderUnavailable."""
    kind = _kind(kind)
    with _override_lock:
        stack = _overrides.get(kind)
        if stack:
            provider = stack[-1]
            if provider is None:
                raise ProviderUnavailable(f"{kind} provider unavailable: disabled")
            return provider
    name = selected(kind)
    if name == "local":
        return _LOCAL[kind]()
    if name == "none":
        raise ProviderUnavailable(
            EMBEDDING_UNAVAILABLE
            if kind == "embedding"
            else f"{kind} provider unavailable: disabled by THROUGHLINE_{kind.upper()}_PROVIDER"
        )
    if kind == "assistant":
        from . import assistant

        return assistant.build(name)
    if name != "http":
        from . import vendors

        return vendors.build(
            name,
            kind,
            lambda setting: _setting(kind, setting),
            lambda default: _price(kind, default),
        )
    return _http(kind)


@contextmanager
def override(kind, provider):
    """Use ``provider`` (an instance, or None for 'unavailable') for a kind
    until the block ends. Process-wide, so worker threads see it too."""
    kind = _kind(kind)
    with _override_lock:
        _overrides.setdefault(kind, []).append(provider)
    try:
        yield provider
    finally:
        with _override_lock:
            stack = _overrides[kind]
            for index in range(len(stack) - 1, -1, -1):
                if stack[index] is provider:
                    del stack[index]
                    break


def describe():
    """One row per kind for tl.provider_state: no host names and no keys."""
    rows = []
    for kind in KINDS:
        try:
            provider = get(kind)
            rows.append(
                dict(
                    kind=kind,
                    provider=provider.name,
                    model=provider.model,
                    local=bool(provider.local),
                    excerpts_leave_host=bool(
                        getattr(provider, "excerpts_leave_host", not provider.local)
                    ),
                )
            )
        except ValueError:
            rows.append(
                dict(
                    kind=kind,
                    provider="none",
                    model=None,
                    local=True,
                    excerpts_leave_host=False,
                )
            )
    return rows


def sync_state(conn):
    """Upsert tl.provider_state from the current configuration (app start)."""
    import psycopg

    rows = describe()
    for row in rows:
        try:
            # One savepoint per kind: a database whose migrations predate a
            # kind (assistant: 0061) still records every other kind.
            with conn.transaction():
                _upsert_state(conn, row)
        except psycopg.errors.CheckViolation:
            log.warning("provider kind %s needs a newer migration", row["kind"])
    return rows


def _upsert_state(conn, row):
    conn.execute(
        "INSERT INTO tl.provider_state(kind,provider,model,local,excerpts_leave_host) VALUES (%s,%s,%s,%s,%s)"
        " ON CONFLICT(kind) DO UPDATE SET provider=EXCLUDED.provider,model=EXCLUDED.model,local=EXCLUDED.local,excerpts_leave_host=EXCLUDED.excerpts_leave_host,updated_at=now()"
        " WHERE (tl.provider_state.provider,tl.provider_state.model,tl.provider_state.local,tl.provider_state.excerpts_leave_host) IS DISTINCT FROM (EXCLUDED.provider,EXCLUDED.model,EXCLUDED.local,EXCLUDED.excerpts_leave_host)",
        (
            row["kind"],
            row["provider"],
            row["model"],
            row["local"],
            row["excerpts_leave_host"],
        ),
    )


def call(
    kind,
    purpose,
    fn,
    *,
    estimate,
    scope,
    tracker=None,
    trace=None,
    origin="query",
    origin_ref=None,
):
    """Run one provider call ``fn() -> (result, Usage)`` under the budget.

    Reserves the estimate on the tracker (ProviderUnavailable('budget
    exhausted') when it does not fit, before anything is sent), runs the call,
    writes one tl.model_ledger row and one trace event, and turns transport
    and decoding errors into ProviderUnavailable. Returns (result, Usage).
    """
    _kind(kind)
    if scope is None:
        raise ValueError("providers.call requires a scope")
    if origin not in ledger.ORIGINS:
        raise ValueError("ledger origin must be one of " + ", ".join(ledger.ORIGINS))
    reserved_cost = 0.0 if estimate.local else float(estimate.cost or 0)
    reserved_calls = int(estimate.calls or 0)
    op = "model." + kind

    def event(status, ms, usage=None, **detail):
        if trace is None:
            return
        trace.event(
            op,
            statement=None,
            status=status,
            rows=None,
            ms=ms,
            model=usage.model if usage else estimate.model,
            tokens=usage.tokens if usage else None,
            cost=usage.cost if usage else None,
            detail=dict(
                purpose=purpose,
                provider=estimate.provider,
                origin=origin,
                calls=usage.calls if usage else 0,
                **detail,
            ),
        )

    if tracker is not None and not tracker.reserve(reserved_cost, reserved_calls):
        event("refused", 0, reason="budget exhausted")
        raise ProviderUnavailable("budget exhausted")
    started = time.perf_counter()
    try:
        result, usage = fn()
    except ProviderUnavailable as exc:
        if tracker is not None and isinstance(exc, EgressDenied):
            # Refused before any request: nothing was spent.
            tracker.release(reserved_cost, reserved_calls)
        event(
            "failed",
            (time.perf_counter() - started) * 1000,
            reason="egress denied" if isinstance(exc, EgressDenied) else "unavailable",
        )
        raise
    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        event("failed", (time.perf_counter() - started) * 1000, reason="transport")
        # The cause may carry request or response text.
        raise ProviderUnavailable(
            f"{kind} provider unavailable or returned an invalid response"
        ) from None
    except Exception:
        event("failed", (time.perf_counter() - started) * 1000, reason="error")
        raise
    ms = (time.perf_counter() - started) * 1000
    over_budget = False
    if tracker is not None and not estimate.local:
        # A remote call may cost more than its estimate. The extra is charged
        # to the request's budget so that later calls see what was really
        # spent; when it no longer fits, the trace says so.
        extra = float(usage.cost or 0) - reserved_cost
        if extra > 1e-12 and not tracker.reserve(extra, 0):
            over_budget = True
    try:
        ledger.record(
            purpose=purpose,
            provider=estimate.provider,
            model=usage.model or estimate.model,
            tokens=usage.tokens,
            cost=usage.cost,
            calls=usage.calls,
            token_id=getattr(scope, "token_id", None),
            trace_id=getattr(trace, "id", None),
            origin=origin,
            origin_ref=origin_ref
            if origin_ref is not None
            else getattr(scope, "investigation_id", None),
        )
    except Exception as exc:
        # The call already happened; losing its result would not undo the
        # spend. The failure is logged and shown in the trace.
        log.error("model ledger row was not written: %s", type(exc).__name__)
        event("ledger_failed", ms, usage)
    if over_budget:
        event("ok", ms, usage, over_budget=True)
    else:
        event("ok", ms, usage)
    return result, usage
