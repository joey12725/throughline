"""Pluggable rankers (QRY-23) and cached scoring (CST-5).

A Ranker orders the top candidates of a recall after fusion (stage 'rerank',
order 20). Interface:

  name, version   version changes whenever the order for one input may change
  exact           whether the ranker keeps an exact result exact
  cost_class      free | embedding | model
  needs_text      whether rank() needs the records' text (the stage hydrates
                  the top candidates through the gated record views first)
  estimate(ctx, items)          -> [providers.Estimate] (empty when free)
  rank(ctx, query, items)       -> [(item, score)] best first

items are RankItem(candidate, text, message_ids). Built in: 'fused' (keeps the
fusion order; exact; the default), 'lexical' and 'vector' (order by the scores
the keyword or meaning legs already computed; exact, free), 'cross_encoder'
(the rerank provider; approximate; provider name and version are part of its
version) and 'learned' (a placeholder that is always unavailable). Choice:
request.ranker, else THROUGHLINE_DEFAULT_RANKER, else 'fused'.

score_cached() is the only way a ranker or the spans stage asks the rerank
provider for scores. Every score is cached under (operator, provider version,
model, sha256(query), sha256(text)) with the messages behind the text as its
sources, and read with cache.get(key, ctx.scope): an entry whose source the
asker may not see is a miss. Only misses reach the provider, in one call;
a non-local provider passes cost.authorize first.
"""

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field

from .. import cost, operators, providers
from ..db import connect
from ..providers import cache

log = logging.getLogger("throughline.retrieval")

DEFAULT = "fused"
KEYWORD_LEGS = ("lexical",)
MEANING_LEGS = ("vector",)

_registry = {}
_lock = threading.Lock()


@dataclass
class RankItem:
    """One candidate as a ranker sees it. text is the hydrated content (None
    for rankers that do not need text); message_ids are the messages behind
    that text (the record itself or its support)."""

    candidate: object
    text: str | None = None
    message_ids: list = field(default_factory=list)


class Ranker:
    name = ""
    version = "1"
    exact = True
    cost_class = "free"
    needs_text = False
    implementation = None

    def estimate(self, ctx, items):
        return []

    def rank(self, ctx, query, items):
        return [(item, item.candidate.score) for item in items]

    def describe(self):
        return dict(
            name=self.name,
            version=self.version,
            exact=self.exact,
            cost_class=self.cost_class,
        )

    def spec(self):
        return operators.OperatorSpec(
            name="ranker." + self.name,
            implements="rank",
            implementation=self.implementation or self.name,
            kind="ranker",
            inputs=("query", "candidates"),
            outputs=("candidates",),
            exact=self.exact,
            cost_class=self.cost_class,
            cache_key="ranker, provider version, model, sha256(query), sha256(text)"
            if self.needs_text
            else None,
            version=str(self.version),
        )


def register(ranker, replace=True):
    """Add a ranker (and its OperatorSpec with implements='rank')."""
    if not isinstance(ranker, Ranker) or not ranker.name:
        raise TypeError("a named Ranker is required")
    with _lock:
        _registry[ranker.name] = ranker
    try:
        operators.register(ranker.spec(), replace=replace)
    except Exception:  # A bad spec never stops ranking.
        log.warning("ranker %s has no operator spec", ranker.name)
    return ranker


def unregister(name):
    with _lock:
        ranker = _registry.pop(name, None)
    if ranker is not None:
        operators.unregister("ranker." + name)
    return ranker


def get(name):
    with _lock:
        return _registry.get(name)


def names():
    with _lock:
        return sorted(_registry)


def choose(request):
    """(Ranker, warning) for a request. An unknown request.ranker is the
    caller's error (ValueError, a 400); an unknown THROUGHLINE_DEFAULT_RANKER
    is a configuration error that falls back to 'fused' with a warning."""
    wanted = getattr(request, "ranker", None)
    if wanted:
        ranker = get(wanted)
        if ranker is None:
            raise ValueError(
                f"unknown ranker '{wanted}'; rankers: " + ", ".join(names())
            )
        return ranker, None
    configured = (os.getenv("THROUGHLINE_DEFAULT_RANKER") or "").strip()
    if configured:
        ranker = get(configured)
        if ranker is not None:
            return ranker, None
        return get(DEFAULT), "unknown THROUGHLINE_DEFAULT_RANKER; fused order kept"
    return get(DEFAULT), None


def describe(ranker):
    """What execution reports about the ranker of a request."""
    return ranker.describe() if ranker is not None else None


# --- cached scoring ----------------------------------------------------------


def sha256(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _present(keys):
    """Which keys have a live cache entry (keys only, no values: the value
    is read through cache.get with the caller's scope)."""
    if not keys:
        return set()
    try:
        with connect("app") as conn:
            rows = conn.execute(
                "SELECT key FROM tl.semantic_cache WHERE key = ANY(%s) AND (expires_at IS NULL OR expires_at>now())",
                (list(keys),),
            ).fetchall()
    except Exception as exc:
        log.warning("score cache lookup failed: %s", type(exc).__name__)
        return set()
    return {r["key"] for r in rows}


def _number(value):
    if isinstance(value, dict):
        value = value.get("score")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _budgets(ctx):
    budgets = getattr(ctx.request, "budgets", None)
    return budgets.model_dump() if budgets is not None else None


def score_cached(ctx, query, items, operator="rerank", purpose=None):
    """Scores for items [{text, message_ids}] (or RankItem), in order, and
    stats {hits, misses, provider, version, model}.

    Cache hits are read with cache.get(key, ctx.scope); only misses reach the
    rerank provider, in one call through providers.call. A non-local
    provider passes cost.authorize (ApprovalRequired propagates, nothing is
    sent). Raises ProviderUnavailable when no provider can score.
    """
    purpose = purpose or operator
    provider = providers.get("rerank")
    entries = []
    for item in items:
        if isinstance(item, RankItem):
            text, ids = item.text or "", item.message_ids
        else:
            text, ids = item.get("text") or "", item.get("message_ids") or ()
        entries.append((text, sorted({int(i) for i in ids or ()})))
    query_hash = sha256(query)
    keys = [
        cache.key(
            operator,
            provider.version,
            provider.model,
            query_hash,
            cache.content_hash(t),
        )
        for t, _ in entries
    ]
    scores = [None] * len(entries)
    present = _present(set(keys))
    for index, key in enumerate(keys):
        if key in present:
            scores[index] = _number(cache.get(key, scope=ctx.scope))
    # Identical texts with the same sources are scored once.
    wanted = {}
    for index, score in enumerate(scores):
        if score is None:
            wanted.setdefault((keys[index], tuple(entries[index][1])), []).append(index)
    # Text under a label the owner has not allowed to leave is never sent to
    # a provider off this host: those items keep no score (migration 0055).
    held = providers.egress.held_for(
        provider, [i for _, sources in wanted for i in sources]
    )
    withheld = 0
    if held:
        for entry in [e for e in wanted if held.intersection(e[1])]:
            withheld += len(wanted.pop(entry))
    stats = dict(
        hits=len(entries) - withheld - sum(len(v) for v in wanted.values()),
        misses=len(wanted),
        egress_held=withheld,
        provider=provider.name,
        version=provider.version,
        model=provider.model,
    )
    if not wanted:
        return scores, stats
    order = list(wanted)
    texts = [entries[wanted[k][0]][0] for k in order]
    estimate = provider.estimate(query, texts, purpose=purpose)
    if not estimate.local:
        cost.authorize(
            ctx.token_row,
            [estimate],
            scope=ctx.scope,
            tracker=ctx.tracker,
            caller_limit=getattr(
                getattr(ctx.request, "budgets", None), "model_cost", None
            ),
            request_hash=cost.request_hash(
                dict(
                    op=purpose,
                    operator=operator,
                    query_sha256=query_hash,
                    texts=[cache.content_hash(t) for t in texts],
                ),
                ctx.scope,
                (),
                _budgets(ctx),
            ),
        )
    if ctx.tracker is not None and ctx.tracker.expired():
        raise TimeoutError("deadline passed before scoring")
    result, _ = providers.call(
        "rerank",
        purpose,
        lambda: provider.score(query, texts),
        estimate=estimate,
        scope=ctx.scope,
        tracker=ctx.tracker,
        trace=ctx.trace,
    )
    result = list(result or [])
    if len(result) != len(texts):
        raise providers.ProviderUnavailable("rerank provider returned a wrong count")
    for (key, sources), value in zip(order, result):
        value = float(value)
        for index in wanted[(key, sources)]:
            scores[index] = value
        cache.put(
            key,
            operator,
            provider.version,
            provider.model,
            cache.content_hash(entries[wanted[(key, sources)][0]][0]),
            dict(score=value),
            source_message_ids=sources,
        )
    return scores, stats


def stage_failure(exc, **info):
    """The StageFailed a scoring stage (rerank, spans) raises for an error of
    score_cached or a ranker, or None when exc is not one the stage absorbs.
    The recall then keeps the stage's input order and reports the reason:
    approval_required (with approval_id), provider_unavailable or timeout."""
    from .executor import StageFailed

    if isinstance(exc, cost.ApprovalRequired):
        return StageFailed(
            "approval_required",
            approval_id=exc.approval_id,
            estimate=exc.estimate.get("cost")
            if isinstance(exc.estimate, dict)
            else None,
            limit=exc.limit,
            **info,
        )
    if isinstance(exc, providers.ProviderUnavailable):
        return StageFailed("provider_unavailable", **info)
    if isinstance(exc, TimeoutError):
        return StageFailed("timeout", **info)
    return None


# --- built-in rankers ----------------------------------------------------------


def _ordered(items, key):
    """Stable sort best first; items without a score keep their order after
    the scored ones."""
    scored = [(i, item, key(item)) for i, item in enumerate(items)]
    scored.sort(key=lambda t: (t[2] is None, -(t[2] or 0.0), t[0]))
    return [(item, value) for _, item, value in scored]


def _leg_score(candidate, legs, detail_key=None):
    best = None
    for entry in candidate.discovery:
        value = None
        if entry.get("leg") in legs:
            value = entry.get("score")
        elif detail_key and entry.get("leg") == "passages":
            value = (entry.get("detail") or {}).get(detail_key)
        if isinstance(value, (int, float)) and (best is None or value > best):
            best = float(value)
    return best


class Fused(Ranker):
    name = "fused"
    implementation = "rrf_order"


class Lexical(Ranker):
    """Order by the keyword score the lexical (or passages keyword) leg gave."""

    name = "lexical"
    implementation = "keyword_score"

    def rank(self, ctx, query, items):
        return _ordered(
            items, lambda it: _leg_score(it.candidate, KEYWORD_LEGS, "keyword_score")
        )


class Vector(Ranker):
    """Order by the similarity the vector (or passages vector) leg gave."""

    name = "vector"
    implementation = "vector_score"

    def rank(self, ctx, query, items):
        return _ordered(
            items, lambda it: _leg_score(it.candidate, MEANING_LEGS, "vector_score")
        )


class CrossEncoder(Ranker):
    """The rerank provider scores (query, text) pairs. Approximate. Its version
    names the provider and the provider's version, so a cache entry or a
    reported order always says which model made it."""

    name = "cross_encoder"
    base_version = "1"
    exact = False
    cost_class = "model"
    needs_text = True
    implementation = "rerank_provider"

    @property
    def version(self):
        try:
            provider = providers.get("rerank")
        except (providers.ProviderUnavailable, ValueError):
            return self.base_version + "+unavailable"
        return f"{self.base_version}+{provider.name}:{provider.version}"

    def describe(self):
        result = super().describe()
        try:
            provider = providers.get("rerank")
            result.update(
                provider=provider.name,
                provider_version=provider.version,
                model=provider.model,
                local=bool(provider.local),
            )
        except (providers.ProviderUnavailable, ValueError):
            result.update(provider=None, provider_version=None)
        return result

    def estimate(self, ctx, items):
        provider = providers.get("rerank")
        return [
            provider.estimate(
                ctx.request.query or "", [it.text or "" for it in items], "rerank"
            )
        ]

    def rank(self, ctx, query, items):
        scores, _ = score_cached(
            ctx, query, items, operator=self.name, purpose="rerank"
        )
        lookup = {id(item): score for item, score in zip(items, scores)}
        return _ordered(items, lambda it: lookup.get(id(it)))


class Learned(Ranker):
    """Placeholder for a learned ranker (deferred research): always
    unavailable, so the stage keeps the fused order and says why."""

    name = "learned"
    exact = False
    cost_class = "model"
    implementation = "placeholder"

    def rank(self, ctx, query, items):
        raise providers.ProviderUnavailable("learned ranker is not available")


for _ranker in (Fused(), Lexical(), Vector(), CrossEncoder(), Learned()):
    register(_ranker)
