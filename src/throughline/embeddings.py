"""Conversation embedding worker and query embedding, over providers.get('embedding').

The provider is remote (a named vendor: voyage, gemini, openrouter; or any
OpenAI-compatible endpoint once BASE_URL, MODEL and DIMENSIONS are
configured) or the deterministic local embedder
(THROUGHLINE_EMBEDDING_PROVIDER=local). Unconfigured, embeddings are
unavailable and nothing else is affected.

What is embedded: closed conversations and page passages of the channels
whose `embed` flag is set (tl.channels.embed, default true). Two ways:

  passive   the worker (`throughline embed run`) embeds what is pending with
            ordinary requests, a batch at a time, as records arrive;
  historic  a backlog goes to the vendor's asynchronous batch interface
            (throughline.embedding_batches, `throughline embed backfill`),
            which answers within hours at a lower price.

A stored text is embedded as a document and a search as a query when the
model distinguishes them, and with a contextual model (voyage-context-4)
each chunk is embedded together with its neighbours (embedding_context).
"""

import contextvars
import logging
import os
import time
from contextlib import contextmanager

from . import providers
from .db import connect
from .derived import materialize, materialize_for_read
from .providers import cache

log = logging.getLogger("throughline.embeddings")

# Worker batches wait up to this long for the endpoint; an interactive query
# embedding uses THROUGHLINE_EMBEDDING_QUERY_TIMEOUT_S instead.
BATCH_TIMEOUT_S = 60
_timeout = contextvars.ContextVar("throughline_embedding_timeout", default=None)
# What is being embedded: a stored text ('document') or a search ('query').
_input_type = contextvars.ContextVar(
    "throughline_embedding_input_type", default="document"
)
# The Usage of the last provider call made by embed() or embed_documents() in
# this context; None after a replacement of either ran instead.
_last_usage = contextvars.ContextVar("throughline_embedding_usage", default=None)
# Session lock of the embedding worker and of the batch commands.
WORKER_LOCK = 71824009


def settings():
    """(endpoint or 'local', model, dimensions) of the configured provider."""
    provider = providers.get("embedding")
    return (
        getattr(provider, "base", None) or provider.name,
        provider.model,
        provider.dimensions,
    )


def _provider():
    provider = providers.get("embedding")
    timeout = _timeout.get()
    if hasattr(provider, "with_timeout"):
        provider = provider.with_timeout(timeout or BATCH_TIMEOUT_S)
    return provider


def embed(texts, model=None, dimensions=None):
    """Embed texts with the configured provider: (vectors, token_count).

    Callers use this module attribute at call time, so a replacement with the
    same signature and return shape is used by the worker and by embed_search.
    """
    provider = _provider()
    if getattr(provider, "asymmetric", False):
        vectors, usage = provider.embed(
            texts, model, dimensions, input_type=_input_type.get()
        )
    else:
        vectors, usage = provider.embed(texts, model, dimensions)
    _last_usage.set(usage)
    return vectors, usage.tokens


def embed_documents(documents, model=None, dimensions=None):
    """Embed the chunks of each document in the context of that document:
    (one list of vectors per document, token_count). Only a contextual
    provider offers it; like embed, it is looked up at call time."""
    provider = _provider()
    vectors, usage = provider.embed_documents(documents, model, dimensions)
    _last_usage.set(usage)
    return vectors, usage.tokens


def spent(tokens, call):
    """Run ``call`` (which embeds) and return (its result, cost in USD): the
    cost the provider reported for it, else tokens at the configured price."""
    _last_usage.set(None)
    result = call()
    usage = _last_usage.get()
    used = tokens(result)
    if usage is not None and usage.tokens == used:
        return result, float(usage.cost or 0)
    provider = providers.get("embedding")
    return result, (
        0.0 if provider.local else used * provider.price_per_million / 1000000
    )


def query_timeout():
    value = os.getenv("THROUGHLINE_EMBEDDING_QUERY_TIMEOUT_S") or "5"
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0
    if not 0 < seconds <= BATCH_TIMEOUT_S:
        raise ValueError(
            "THROUGHLINE_EMBEDDING_QUERY_TIMEOUT_S must be above 0 and at most 60"
        )
    return seconds


QUERY_CACHE_TTL_S = 604800


def query_cache_ttl():
    """Seconds a query vector stays cached. An unusable setting falls back to
    the default: it must never fail a search whose embedding was already paid."""
    value = os.getenv("THROUGHLINE_EMBEDDING_QUERY_CACHE_TTL_S")
    try:
        seconds = float(value) if value else QUERY_CACHE_TTL_S
    except ValueError:
        seconds = QUERY_CACHE_TTL_S
    if not 0 < seconds < float("inf"):
        log.warning("THROUGHLINE_EMBEDDING_QUERY_CACHE_TTL_S is not a positive number")
        seconds = QUERY_CACHE_TTL_S
    return seconds


def vector(value):
    return "[" + ",".join(str(float(x)) for x in value) + "]"


@contextmanager
def worker_lock():
    """One embedding writer at a time. No transaction, and so no ingest or
    row lock, is open while a request is out: the session lock sits on an
    otherwise idle connection."""
    with connect("app") as guard:
        guard.execute("SELECT pg_advisory_lock(%s)", (WORKER_LOCK,))
        guard.commit()
        try:
            yield
        finally:
            try:
                guard.rollback()
                guard.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK,))
            except Exception:
                # Never return a connection that still holds the lock to the pool.
                guard.close()
                raise


def run_once(batch_size=64):
    """One worker step: answered batch jobs are stored, a backlog is handed
    to the vendor's batch interface when THROUGHLINE_EMBEDDING_HISTORIC=batch,
    then a batch of conversations and passages of page revisions (build, then
    a batch of distinct passage texts) is embedded directly. Returns the
    number of conversations embedded plus revisions split plus passage texts
    embedded plus texts stored from or handed to batch jobs, so 0 means there
    was nothing to do."""
    from . import embedding_batches

    _, model, dimensions = settings()
    materialize(wait=False)
    with worker_lock():
        built = _build_passages()
        if over_monthly_cap():
            return built
        count = built + embedding_batches.step(model, dimensions)
        count += _run_batch(model, dimensions, batch_size)
        count += _run_passages(batch_size)
        _activate(model)
        return count


def monthly_cap():
    """THROUGHLINE_EMBEDDING_MONTHLY_USD: the most the embedding worker spends
    in a calendar month, at list price; None (unset) is no cap."""
    value = os.getenv("THROUGHLINE_EMBEDDING_MONTHLY_USD")
    if not value:
        return None
    try:
        cap = float(value)
    except ValueError:
        cap = -1
    if cap < 0:
        raise ValueError("THROUGHLINE_EMBEDDING_MONTHLY_USD must be a number of at least 0")
    return cap


def month_spent(conn):
    """USD of embedding recorded this calendar month (worker, batch jobs and
    query embeddings), counted from THROUGHLINE_EMBEDDING_BUDGET_SINCE when
    that is later: a one-off load can be left out of the monthly cap. The
    ledger keeps list prices, so a vendor's free tokens make the bill lower."""
    since = os.getenv("THROUGHLINE_EMBEDDING_BUDGET_SINCE") or None
    return float(
        conn.execute(
            "SELECT coalesce(sum(cost),0) AS used FROM tl.spend WHERE purpose='embedding' "
            "AND at >= greatest(date_trunc('month', now()), coalesce(%s::timestamptz, '-infinity'))",
            (since,),
        ).fetchone()["used"]
    )


def over_monthly_cap():
    """True when this month's embedding spend reached the cap. The worker then
    embeds nothing until the next month; a pass started below the cap can go
    over it by at most that pass."""
    cap = monthly_cap()
    if cap is None:
        return False
    with connect("app") as conn:
        used = month_spent(conn)
    if used >= cap:
        log.warning(
            "embedding paused: $%.2f spent this month, cap $%.2f", used, cap
        )
        return True
    return False


# A conversation is embedded when its channel is: not a page-revision channel
# (pages are embedded as passages, EMB-2) and not switched off
# (tl.channels.embed). Such conversations are neither embedded nor counted.
_EMBEDDED_CHANNEL = "EXISTS(SELECT 1 FROM tl.threads t JOIN tl.channels ch ON ch.id=t.channel_id WHERE t.id=c.thread_id AND ch.record_kind IS DISTINCT FROM 'page_revision' AND ch.embed)"
_NOT_PAGE = _EMBEDDED_CHANNEL
# A text held by an open batch job is not embedded a second time.
_UNRESERVED = "NOT EXISTS(SELECT 1 FROM tl.embedding_batch_items i JOIN tl.embedding_batches b ON b.id=i.batch_id WHERE b.status IN ('submitting','submitted') AND b.model=%s AND i.store AND i.conversation_id=c.id AND i.text_hash=c.text_hash)"
_PENDING = (
    "FROM tl.live_conversations c LEFT JOIN tl.embeddings e ON e.conversation_id=c.id AND e.model=%s AND e.text_hash=c.text_hash WHERE c.closed AND e.conversation_id IS NULL AND "
    + _EMBEDDED_CHANNEL
    + " AND "
    + _UNRESERVED
)


def remote():
    """True when stored text handed to the embedding provider leaves this
    host. An unusable configuration counts as remote."""
    try:
        return providers.egress.leaves(providers.get("embedding"))
    except ValueError:
        return True


def egress_clause(test="tl.conversation_egress_ok(c.id)"):
    """SQL that leaves out content the egress rule holds (labels the owner
    has not allowed to leave, migration 0055). Empty for a local provider,
    which may embed everything."""
    return f" AND {test}" if remote() else ""


_STORE = "INSERT INTO tl.embeddings SELECT c.id,%s,c.text_hash,%s::vector FROM tl.conversations c WHERE c.id=%s AND c.text_hash=%s ON CONFLICT(conversation_id,model) DO UPDATE SET text_hash=EXCLUDED.text_hash,embedding=EXCLUDED.embedding"


def register(conn, model, dimensions):
    """Make the model known, with its index; a model keeps its dimensions."""
    conn.execute(
        "INSERT INTO tl.embedding_models(model,dimensions) VALUES(%s,%s) ON CONFLICT DO NOTHING",
        (model, dimensions),
    )
    if (
        conn.execute(
            "SELECT dimensions FROM tl.embedding_models WHERE model=%s", (model,)
        ).fetchone()["dimensions"]
        != dimensions
    ):
        raise ValueError("use a new model name when changing embedding dimensions")
    if index_enabled():
        conn.execute("SELECT tl.ensure_embedding_index(%s)", (model,))


def index_enabled():
    """THROUGHLINE_EMBEDDING_INDEX=off keeps ranking exact (no HNSW index)."""
    return os.getenv("THROUGHLINE_EMBEDDING_INDEX", "").strip().lower() not in (
        "0",
        "off",
        "false",
        "no",
        "none",
    )


def close_conversations(conn):
    # SKIP LOCKED: never wait for an ingest's rows while holding others.
    conn.execute(
        "UPDATE tl.conversations c SET closed=true WHERE c.id IN (SELECT c2.id FROM tl.conversations c2 JOIN tl.threads t ON t.id=c2.thread_id JOIN tl.channels ch ON ch.id=t.channel_id WHERE NOT c2.closed AND c2.ended_at+make_interval(secs=>ch.conversation_gap_s)<now() FOR UPDATE OF c2 SKIP LOCKED)"
    )


def pending_conversations(conn, model, limit, by_thread=False):
    """Closed conversations without a vector of `model`, not held by a batch
    job. Conversations of threads awaiting maintenance are stale: not
    embedded. by_thread orders them for contextual embedding."""
    # The egress test is costly (a lineage walk per row): the OFFSET 0 fence
    # keeps it off the rows the anti-joins drop, which during a large load
    # are most of the rows the ordered scan passes.
    order = " ORDER BY c.thread_id,c.chunk_no" if by_thread else " ORDER BY c.id"
    return conn.execute(
        "SELECT c.* FROM (SELECT c.* "
        + _PENDING
        + order
        + " OFFSET 0) c WHERE true"
        + egress_clause()
        + order
        + " LIMIT %s",
        (model, model, limit),
    ).fetchall()


def conversation_context(conn):
    """context(row, tokens): the closed chunks of the row's thread just
    before it, oldest first, within a token budget."""

    # Context is sent with its chunk, so held chunks are not context either.
    clause = egress_clause()

    def context(row, tokens):
        found = []
        for before in conn.execute(
            "SELECT c.id,c.text_hash,c.content,c.token_estimate FROM tl.conversations c WHERE c.thread_id=%s AND c.chunk_no<%s AND c.closed"
            + clause
            + " ORDER BY c.chunk_no DESC LIMIT 16",
            (row["thread_id"], row["chunk_no"]),
        ):
            tokens -= before["token_estimate"]
            if tokens < 0:
                break
            found.append(before)
        return found[::-1]

    return context


def _run_batch(model, dimensions, batch_size):
    from . import embedding_context

    contextual = embedding_context.active()
    with connect("app") as conn:
        close_conversations(conn)
        register(conn, model, dimensions)
        rows = pending_conversations(conn, model, batch_size, by_thread=contextual)
        windows = (
            embedding_context.windows(
                rows, conversation_context(conn), group=lambda r: r["thread_id"]
            )
            if contextual and rows
            else None
        )
    if not rows:
        return 0
    if windows is not None:
        (vectors, tokens), cost = spent(
            lambda r: r[1], lambda: embedding_context.embed(windows)
        )
    else:
        (vectors, tokens), cost = spent(
            lambda r: r[1], lambda: embed([r["content"] for r in rows])
        )
    with connect("app") as conn:
        with conn.cursor() as cur:
            # The conversation may have changed or gone while embedding.
            cur.executemany(
                _STORE,
                [
                    (model, vector(v), row["id"], row["text_hash"])
                    for row, v in zip(rows, vectors)
                ],
            )
        conn.execute(
            "INSERT INTO tl.embedding_ledger(model,tokens,cost) VALUES(%s,%s,%s)",
            (model, tokens, cost),
        )
    return len(rows)


def _build_passages():
    from . import passages

    with connect("app") as conn:
        return passages.build(conn)


def _run_passages(batch_size):
    from . import passages

    return passages.embed_once(batch_size)


def _activate(model):
    """Make `model` the serving model once conversations and passages are
    both covered by it."""
    from . import passages

    with connect("app") as conn:
        # EXISTS, not count: it stops at the first chunk still to embed, so
        # a worker pass during a large load does not scan the whole corpus.
        missing = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM (SELECT c.id FROM tl.live_conversations c WHERE c.closed AND "
            + _NOT_PAGE
            + " AND NOT EXISTS(SELECT 1 FROM tl.embeddings e WHERE e.conversation_id=c.id AND e.model=%s AND e.text_hash=c.text_hash) OFFSET 0) c WHERE true"
            + egress_clause()
            + ") n",
            (model,),
        ).fetchone()["n"]
        # live_conversations hides every conversation of a thread awaiting
        # maintenance, so "nothing missing" proves coverage only when no thread
        # is waiting. Until then the model in use stays in use.
        waiting = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM tl.dirty_threads) pending"
        ).fetchone()["pending"]
        uncovered = passages.pending(conn, model)
        if (
            not missing
            and not waiting
            and not uncovered["revisions_without_passages"]
            and not uncovered["texts_without_vector"]
        ):
            conn.execute(
                "UPDATE tl.embedding_models SET active=false WHERE active AND model<>%s",
                (model,),
            )
            conn.execute(
                "UPDATE tl.embedding_models SET active=true WHERE model=%s", (model,)
            )


def run(once=False):
    if once:
        return run_once()
    settings()  # A configuration error ends the worker; outages do not.
    while True:
        try:
            count = run_once()
        except Exception as exc:
            log.warning("embedding batch failed, retrying: %s", exc)
            count = 0
        if not count:
            time.sleep(10)


def run_until_idle(max_seconds=None):
    """Worker steps until nothing is pending (or max_seconds pass), for a
    scheduler that starts it every few minutes (the Docker maintenance
    service). No configured provider is not an error: it reports
    {status: "unavailable"} and exits cleanly, so an install without
    embeddings has a quiet job instead of a failing one."""
    from .providers.base import ProviderUnavailable

    try:
        settings()
    except ProviderUnavailable as exc:
        return dict(status="unavailable", detail=str(exc), embedded=0)
    start = time.monotonic()
    total = 0
    while True:
        count = run_once()
        total += count
        if not count:
            return dict(status="idle", embedded=total)
        if max_seconds and time.monotonic() - start >= max_seconds:
            return dict(status="time_limit", embedded=total)


def embed_search(text, with_model=False, *, scope=None, tracker=None, trace=None):
    """Embed a query with the model that serves search.

    The vector is cached per provider, serving model, dimensions and text, so
    a repeated query makes no call and, during a model handover, a vector of
    the previous serving model is never reused. A call that is made goes
    through providers.call: budget, ledger row (origin 'query') and trace.
    """
    provider = providers.get("embedding")
    materialize_for_read()
    with connect("app") as conn:
        active = conn.execute(
            "SELECT model,dimensions FROM tl.embedding_models WHERE active"
        ).fetchone()
    if not active:
        raise ValueError(
            "meaning search unavailable: run throughline embed run --once until initial model is complete"
        )
    if scope is None:
        from .scope import Scope

        scope = Scope.local()
    model, dimensions = active["model"], active["dimensions"]
    digest = cache.content_hash(text)
    key = cache.key(
        "query_embedding", provider.version, model, provider.name, dimensions, digest
    )
    hit = cache.get(key, scope=scope)
    embedding = hit.get("embedding") if isinstance(hit, dict) else None
    if not (isinstance(embedding, list) and len(embedding) == dimensions):

        def run():
            started = time.perf_counter()
            token = _timeout.set(query_timeout())
            kind = _input_type.set("query")
            try:
                # The module attribute, looked up now: see embed().
                (vectors, tokens), cost = spent(
                    lambda r: r[1], lambda: embed([text], model, dimensions)
                )
            finally:
                _input_type.reset(kind)
                _timeout.reset(token)
            return vectors[0], providers.Usage(
                model=model,
                tokens=tokens,
                cost=cost,
                calls=1,
                ms=(time.perf_counter() - started) * 1000,
            )

        estimate = provider.estimate([text], purpose="query_embedding")
        embedding, _ = providers.call(
            "embedding",
            "query_embedding",
            run,
            estimate=providers.Estimate(
                purpose=estimate.purpose,
                provider=estimate.provider,
                model=model,
                calls=estimate.calls,
                tokens=estimate.tokens,
                cost=estimate.cost,
                local=estimate.local,
            ),
            scope=scope,
            tracker=tracker,
            trace=trace,
            origin="query",
        )
        # The entry holds the vector and a hash of the query, not its text.
        cache.put(
            key,
            "query_embedding",
            provider.version,
            model,
            digest,
            {"embedding": embedding},
            ttl_s=query_cache_ttl(),
        )
    return (embedding, model) if with_model else embedding
