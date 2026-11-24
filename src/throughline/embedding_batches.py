"""Historic embedding through a vendor's asynchronous batch interface.

A backlog (a first load, a new model, a channel switched on) is handed to the
vendor as jobs that are answered within hours at a lower price (Voyage: two
thirds, Gemini: half) instead of being embedded request by request.

    submit()   pending conversations and passage texts become jobs
               (tl.embedding_batches) whose texts are reserved
               (tl.embedding_batch_items): the ordinary worker skips them;
    collect()  answered jobs are stored as vectors, with a ledger row at the
               batch price.

A job that fails, is cancelled or answers only part of its texts loses
nothing: its reservation ends and whatever has no vector is pending again
for the ordinary worker. The worker calls step() on every pass: it always
collects, and submits by itself only with THROUGHLINE_EMBEDDING_HISTORIC=batch
and at least THROUGHLINE_EMBEDDING_BATCH_MIN (500) pending texts.
"""

import logging
import math
import os

from . import embedding_context, embeddings, passages, providers
from .db import connect
from .providers import vendors

log = logging.getLogger("throughline.embeddings")

# Texts in one job. A job is read and written in one piece.
JOB_TEXTS = 20000
OPEN = ("submitting", "submitted")


def mode():
    """'batch' when the worker hands a backlog to the batch interface itself
    (THROUGHLINE_EMBEDDING_HISTORIC=batch), else 'sync'."""
    value = os.getenv("THROUGHLINE_EMBEDDING_HISTORIC", "").strip().lower()
    if value in ("", "sync", "off"):
        return "sync"
    if value != "batch":
        raise ValueError("THROUGHLINE_EMBEDDING_HISTORIC must be sync or batch")
    return "batch"


def _number(name, default, low):
    value = os.getenv(name)
    try:
        number = int(value) if value else default
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    return max(low, number)


def minimum():
    return _number("THROUGHLINE_EMBEDDING_BATCH_MIN", 500, 1)


def poll_seconds():
    return _number("THROUGHLINE_EMBEDDING_BATCH_POLL_S", 60, 0)


def client():
    """(provider, its batch client or None)."""
    provider = providers.get("embedding")
    make = getattr(provider, "batch_client", None)
    return provider, make() if make else None


def step(model, dimensions):
    """The worker's part: store answered jobs and, in batch mode, hand a
    backlog over. An unreachable vendor never stops the ordinary work."""
    try:
        provider, batch = client()
        if batch is None:
            return 0
        count = collect()["stored"]
        if mode() == "batch":
            count += submit(least=minimum())["texts"]
        return count
    except providers.ProviderUnavailable as error:
        log.warning("embedding batch step skipped: %s", error)
        return 0


# --- submit -------------------------------------------------------------------


def _requests(rows, windows, batch, conversation):
    """[(custom_id, texts, items)] for one job; an item is
    (position, conversation_id, text_hash, store)."""

    def item(position, row, store):
        return (position, row["id"] if conversation else None, row["text_hash"], store)

    requests = []
    if windows is not None:
        for number, window in enumerate(windows):
            members = [(c, False) for c in window["context"]] + [
                (row, True) for _, row in window["rows"]
            ]
            requests.append(
                (
                    f"w{number}",
                    [row["content"] for row, _ in members],
                    [item(i, row, store) for i, (row, store) in enumerate(members)],
                )
            )
        return requests
    for number, run in enumerate(
        vendors.slices(
            rows,
            batch.request_items,
            batch.request_tokens,
            size=lambda r: r["token_estimate"],
        )
    ):
        requests.append(
            (
                f"r{number}",
                [row["content"] for row in run],
                [item(i, row, True) for i, row in enumerate(run)],
            )
        )
    return requests


def _submit_one(kind, provider, batch, least):
    model, dimensions = provider.model, provider.dimensions
    contextual = bool(getattr(provider, "contextual", False))
    conversation = kind == "conversation"
    limit = min(JOB_TEXTS, batch.max_requests * batch.request_items)
    with connect("app") as conn:
        embeddings.register(conn, model, dimensions)
        if conversation:
            embeddings.close_conversations(conn)
            rows = embeddings.pending_conversations(
                conn, model, limit, by_thread=contextual
            )
            context = embeddings.conversation_context(conn)
            group = lambda r: r["thread_id"]  # noqa: E731
        else:
            passages.build(conn)
            rows = passages.pending_texts(conn, model, limit, by_page=contextual)
            context = passages.passage_context(conn)
            group = lambda r: r["message_id"]  # noqa: E731
        if not rows or len(rows) < least:
            return None
        windows = (
            embedding_context.windows(rows, context, group) if contextual else None
        )
        requests = _requests(rows, windows, batch, conversation)[: batch.max_requests]
        # Reserved before the job exists at the vendor: a job is never paid
        # for without a row here that knows its texts.
        job = conn.execute(
            "INSERT INTO tl.embedding_batches(provider,model,dimensions,kind,contextual,items) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                provider.name,
                model,
                dimensions,
                kind,
                contextual,
                sum(1 for _, _, items in requests for i in items if i[3]),
            ),
        ).fetchone()["id"]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tl.embedding_batch_items(batch_id,custom_id,position,conversation_id,text_hash,store) VALUES(%s,%s,%s,%s,%s,%s)",
                [
                    (job, custom_id, *entry)
                    for custom_id, _, items in requests
                    for entry in items
                ],
            )
    texts = sum(1 for _, _, items in requests for i in items if i[3])
    try:
        remote_id = batch.submit(
            [(custom_id, texts_) for custom_id, texts_, _ in requests],
            dimensions=dimensions,
            contextual=contextual,
        )
    except Exception:
        with connect("app") as conn:
            conn.execute("DELETE FROM tl.embedding_batches WHERE id=%s", (job,))
        raise
    with connect("app") as conn:
        conn.execute(
            "UPDATE tl.embedding_batches SET remote_id=%s,status='submitted' WHERE id=%s",
            (remote_id, job),
        )
    return dict(id=job, kind=kind, texts=texts, requests=len(requests))


def submit(least=1):
    """Hand every pending conversation and passage text to the batch
    interface, a job per JOB_TEXTS texts. With `least`, a kind with fewer
    pending texts is left to the ordinary worker. Returns
    {jobs: [...], texts: n}."""
    provider, batch = client()
    if batch is None:
        raise ValueError(
            f"the embedding provider '{provider.name}' has no batch interface"
        )
    jobs = []
    for kind in ("conversation", "passage"):
        floor = least
        while True:
            job = _submit_one(kind, provider, batch, floor)
            if job is None:
                break
            jobs.append(job)
            # The rest of a backlog that was worth a job goes along with it.
            floor = 1
    return dict(jobs=jobs, texts=sum(j["texts"] for j in jobs))


# --- collect ------------------------------------------------------------------


def _valid(vectors, count, dimensions):
    try:
        return len(vectors) == count and all(
            len(v) == dimensions and all(math.isfinite(x) for x in v) for v in vectors
        )
    except TypeError:
        return False


def _finish(job, status, detail=None, stored=0, tokens=0, cost=0.0):
    with connect("app") as conn:
        conn.execute(
            "UPDATE tl.embedding_batches SET status=%s,detail=%s,stored=%s,tokens=%s,cost=%s,finished_at=now(),checked_at=now() WHERE id=%s",
            (status, detail, stored, tokens, cost, job["id"]),
        )
        # The reservation ends: what has no vector is pending again.
        conn.execute(
            "DELETE FROM tl.embedding_batch_items WHERE batch_id=%s", (job["id"],)
        )


def _store(job, provider, results):
    with connect("app") as conn:
        members = {}
        for row in conn.execute(
            "SELECT custom_id,conversation_id,text_hash,store FROM tl.embedding_batch_items WHERE batch_id=%s ORDER BY custom_id,position",
            (job["id"],),
        ):
            members.setdefault(row["custom_id"], []).append(row)
    conversation = job["kind"] == "conversation"
    values, tokens = [], 0
    for custom_id, vectors, used in results:
        rows = members.pop(custom_id, None)
        if not rows or not _valid(vectors, len(rows), job["dimensions"]):
            continue
        tokens += int(used or 0)
        for row, v in zip(rows, vectors):
            if not row["store"]:
                continue
            text = embeddings.vector(v)
            values.append(
                (job["model"], text, row["conversation_id"], row["text_hash"])
                if conversation
                else (row["text_hash"], job["model"], text, row["text_hash"])
            )
    cost = tokens * provider.price_per_million * provider.batch_discount / 1000000
    with connect("app") as conn:
        with conn.cursor() as cur:
            if values:
                cur.executemany(
                    embeddings._STORE if conversation else passages.STORE, values
                )
        conn.execute(
            "INSERT INTO tl.embedding_ledger(model,tokens,cost) VALUES(%s,%s,%s)",
            (job["model"], tokens, cost),
        )
    _finish(job, "completed", None, len(values), tokens, cost)
    return len(values)


def collect(force=False):
    """Ask the vendor about every open job of the configured provider and
    store the answered ones. A job is asked at most once per
    THROUGHLINE_EMBEDDING_BATCH_POLL_S (60) unless force. Returns
    {completed, failed, pending, other_provider, stored}."""
    result = dict(completed=0, failed=0, pending=0, other_provider=0, stored=0)
    with connect("app") as conn:
        jobs = conn.execute(
            "SELECT *,now()-submitted_at>interval '1 hour' AS abandoned,"
            "checked_at IS NOT NULL AND now()-checked_at<make_interval(secs=>%s) AS fresh "
            "FROM tl.embedding_batches WHERE status=ANY(%s) ORDER BY id",
            (poll_seconds(), list(OPEN)),
        ).fetchall()
    if not jobs:
        return result
    provider, batch = client()
    for job in jobs:
        if job["status"] == "submitting":
            # Reserved, but the vendor never confirmed the job.
            if job["abandoned"]:
                _finish(job, "failed", "never submitted")
                result["failed"] += 1
            else:
                result["pending"] += 1
            continue
        if batch is None or (job["provider"], job["model"]) != (
            provider.name,
            provider.model,
        ):
            result["other_provider"] += 1
            continue
        if job["fresh"] and not force:
            result["pending"] += 1
            continue
        state = batch.poll(job["remote_id"])
        if state["status"] == "pending":
            with connect("app") as conn:
                conn.execute(
                    "UPDATE tl.embedding_batches SET checked_at=now(),detail=%s WHERE id=%s",
                    (state.get("detail"), job["id"]),
                )
            result["pending"] += 1
        elif state["status"] == "failed":
            _finish(job, "failed", state.get("detail"))
            result["failed"] += 1
        else:
            result["stored"] += _store(
                job, provider, batch.results(job["remote_id"], state)
            )
            result["completed"] += 1
    return result


def cancel(job_id):
    """Cancel an open job at the vendor and end its reservation."""
    with connect("app") as conn:
        job = conn.execute(
            "SELECT * FROM tl.embedding_batches WHERE id=%s AND status=ANY(%s)",
            (job_id, list(OPEN)),
        ).fetchone()
    if job is None:
        raise ValueError("no open embedding batch with this id")
    if job["remote_id"]:
        provider, batch = client()
        if batch is not None and job["provider"] == provider.name:
            batch.cancel(job["remote_id"])
    _finish(job, "cancelled")
    return dict(id=job_id, status="cancelled")


def status(conn, limit=20):
    """Open jobs and the most recent finished ones."""
    return conn.execute(
        "SELECT id,provider,model,kind,contextual,status,items,stored,tokens,cost,detail,submitted_at,checked_at,finished_at "
        "FROM tl.embedding_batches ORDER BY status=ANY(%s) DESC,id DESC LIMIT %s",
        (list(OPEN), limit),
    ).fetchall()
