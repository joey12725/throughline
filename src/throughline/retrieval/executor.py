"""Recall context and concurrent leg execution (QRY-14, QRY-22, CST-9).

Every statement a leg or stage runs goes through RecallContext.sql: one
validated SELECT through query_ex under the request's scope, on its own
reader transaction, bounded by the remaining deadline (and the leg timeout).
Legs of one round run on a bounded thread pool, so each has its own reader
connection; a failed, timed-out or unavailable leg is reported and the others
continue.
"""

import contextvars
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from .. import query as query_module
from ..db import connect
from ..execution import Execution
from . import scope_sql
from .scope_sql import lit

DEFAULT_CONCURRENCY = 4
EXHAUSTIVE_CAP = 10000
# Leg statements of one recall never run longer than this.
DEFAULT_LEG_TIMEOUT_MS = 30000

log = logging.getLogger("throughline.retrieval")

_leg_deadline = contextvars.ContextVar("throughline_leg_deadline", default=None)


def _int_env(name, default, low=1, high=None):
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        value = default
    value = max(low, value)
    return min(value, high) if high is not None else value


def concurrency():
    return _int_env("THROUGHLINE_RETRIEVAL_CONCURRENCY", DEFAULT_CONCURRENCY, 1, 32)


def leg_timeout_ms():
    return _int_env("THROUGHLINE_LEG_TIMEOUT_MS", DEFAULT_LEG_TIMEOUT_MS, 1, 30000)


def exhaustive_cap():
    return _int_env("THROUGHLINE_RECALL_EXHAUSTIVE_CAP", EXHAUSTIVE_CAP, 1, 100000)


class LegTimeout(Exception):
    """The deadline passed before a statement could start."""


class LegSkipped(Exception):
    """Raised by a leg's run() when it finds, while running, that it cannot
    answer under the scope (for example scope_too_broad_for_passages). The
    leg is reported as skipped with the reason; the others continue."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class StageFailed(Exception):
    """Raised by a stage that keeps its input unchanged and wants its own
    reason disclosed (provider_unavailable, timeout, approval_required).
    info is added to the stage entry (ids and numbers only)."""

    def __init__(self, reason, **info):
        super().__init__(reason)
        self.reason = reason
        self.info = info


def failure_reason(exc):
    """A short reason for execution metadata: never statement text or data."""
    log.debug("recall operation failed", exc_info=exc)
    code = getattr(exc, "sqlstate", None)
    if code == "57014" or isinstance(exc, (LegTimeout, FutureTimeout)):
        return "timeout"
    if code:
        return f"database_error:{code}"
    return f"exception:{type(exc).__name__}"


class RecallContext:
    """Everything a planner, leg or stage of one recall may use.

    Stable attributes (later stages rely on them): scope, token_row, request,
    tracker, execution, trace, resolved, legs, manifest, dropped, extras,
    after_recall, plus sql(), query(), literal(), args(). ranker is the
    ranking.Ranker chosen for the request (set by recall()).
    """

    def __init__(
        self,
        scope,
        request,
        token_row=None,
        tracker=None,
        execution=None,
        trace=None,
        legs=None,
    ):
        self.scope = scope
        self.token_row = token_row
        self.request = request
        self.tracker = tracker
        self.execution = execution if execution is not None else Execution(tracker)
        self.trace = trace
        self.resolved = None
        self.legs = legs if legs is not None else {}
        self.manifest = None
        self.dropped = []
        self.extras = {}
        self.after_recall = []
        self.ranker = None
        self.round_no = 0
        self.ran = []
        self.as_of_honoured = {}
        self.truncated_legs = set()
        self.stage_candidates = {}
        self._lock = threading.Lock()
        self._capabilities = None
        self._unrestricted = None

    # --- scope -----------------------------------------------------------
    @property
    def message_ids(self):
        return self.resolved.message_ids if self.resolved is not None else None

    def literal(self, value):
        return lit(value)

    def args(self, reduced=False):
        """The scope arguments S as SQL literals (reduced: kinds, as_of,
        known_by, include_outside only)."""
        return scope_sql.args(self.resolved, reduced=reduced)

    def depth(self):
        """How many candidates a leg fetches: None (all) when exhaustive."""
        if self.request.exhaustive:
            return None
        k = self.request.k
        return min(1000, max(100, 5 * k))

    def cap(self, depth):
        return depth if depth is not None else exhaustive_cap()

    # --- statements --------------------------------------------------------
    def timing(self, name, ms):
        self.execution.add_timing(name, ms)

    def _deadline(self):
        remaining = self.tracker.remaining_ms() if self.tracker is not None else None
        leg = _leg_deadline.get()
        if leg is not None:
            left = max(0.0, (leg - time.perf_counter()) * 1000)
            remaining = left if remaining is None else min(remaining, left)
        return remaining

    def query(self, statement, cap=1000, leg=None):
        """One validated SELECT under the scope; query_ex's dict. A truncated
        result marks the leg as truncated."""
        remaining = self._deadline()
        if remaining is not None and remaining <= 0:
            raise LegTimeout()
        local = Execution()
        started = time.perf_counter()
        status, rows = "ok", None
        try:
            result = query_module.query_ex(
                statement,
                cap,
                scope=self.scope,
                deadline_ms=remaining,
                execution=local,
                materialize=False,
            )
            rows = result["row_count"]
        except Exception as exc:
            status = failure_reason(exc)
            raise
        finally:
            ms = (time.perf_counter() - started) * 1000
            self.execution.add_timing(
                "database", local.to_dict()["timings_ms"].get("database", ms)
            )
            if self.trace is not None:
                self.trace.event(
                    "recall.sql",
                    statement,
                    "ok" if status == "ok" else "failed",
                    rows=rows,
                    ms=ms,
                    detail=dict(leg=leg, reason=None if status == "ok" else status),
                )
        if result["truncated"] and leg:
            with self._lock:
                self.truncated_legs.add(leg)
        return result

    def sql(self, statement, cap=1000):
        return self.query(statement, cap)["rows"]

    def filter_hits(self, message_ids):
        """The ids among message_ids that satisfy the full hard scope
        (including scope.message_ids), in one api.scope_filter statement."""
        ids = sorted({int(i) for i in message_ids})
        if self.message_ids is not None:
            allowed = set(self.message_ids)
            ids = [i for i in ids if i in allowed]
        if not ids:
            return set()
        rows = self.sql(
            "SELECT * FROM api.scope_filter(" + lit(ids) + "," + self.args() + ")",
            cap=len(ids),
        )
        return {r["message_id"] for r in rows}

    def filter_context(self, message_ids):
        """The ids that may appear as dependency or context rows (reduced
        scope: kinds, as_of, known_by, include_outside)."""
        ids = sorted({int(i) for i in message_ids})
        if not ids:
            return set()
        rows = self.sql(
            "SELECT * FROM api.scope_filter("
            + lit(ids)
            + ","
            + self.args(reduced=True)
            + ")",
            cap=len(ids),
        )
        return {r["message_id"] for r in rows}

    # --- environment -------------------------------------------------------
    def unrestricted(self):
        """Whether the asker sees everything (decided by the database)."""
        if self._unrestricted is None:
            with query_module.reader_transaction(self.scope) as conn:
                self._unrestricted = (
                    conn.execute("SELECT api.asker_unrestricted() AS ok").fetchone()[
                        "ok"
                    ]
                    is True
                )
        return self._unrestricted

    def capabilities(self):
        """Optional database features: link history (api.links_asof) and link
        validity columns. Catalogue metadata only, no record data."""
        if self._capabilities is None:
            with connect("app") as conn:
                row = conn.execute(
                    "SELECT to_regprocedure('api.links_asof(timestamptz)') IS NOT NULL AS links_asof,"
                    " EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid='tl.links'::regclass AND attname='valid_from' AND NOT attisdropped) AS link_validity"
                ).fetchone()
            self._capabilities = dict(
                links_asof=bool(row["links_asof"])
                and "links_asof" in query_module.FUNCTIONS,
                link_validity=bool(row["link_validity"]),
            )
        return self._capabilities

    def links_source(self):
        """(FROM item, extra WHERE) of the links a leg may use: current links,
        or the link state at the cutoff under as_of/known_by."""
        cutoff = self.resolved.cutoff
        if cutoff is None:
            return "api.links", ""
        caps = self.capabilities()
        if caps["links_asof"]:
            extra = ""
            if self.resolved.scope.as_of is not None and caps["link_validity"]:
                extra = (
                    " AND api.applies_at(l.valid_from, l.valid_to, "
                    + lit(self.resolved.scope.as_of)
                    + ")"
                )
            return "api.links_asof(" + lit(cutoff) + ")", extra
        return "api.links", " AND l.created_at <= " + lit(cutoff)


def _run_one(ctx, leg, deadline):
    token = _leg_deadline.set(deadline)
    started = time.perf_counter()
    try:
        return leg.run(ctx) or [], (time.perf_counter() - started) * 1000
    finally:
        _leg_deadline.reset(token)


def _stamp(leg, candidates):
    """Record each candidate's rank (and score) in this leg's discovery entry."""
    for rank, candidate in enumerate(candidates, start=1):
        entry = next((d for d in candidate.discovery if d.get("leg") == leg), None)
        if entry is None:
            entry = dict(leg=leg, detail=None)
            candidate.discovery.append(entry)
        entry["rank"] = rank
        entry.setdefault("score", candidate.score)
        entry.setdefault("detail", None)
    return candidates


def _honoured(ctx, leg, value):
    if ctx.resolved.historical:
        with ctx._lock:
            ctx.as_of_honoured[leg] = bool(value)


def run_legs(ctx, names):
    """Run the named legs of one round concurrently: {leg: [Candidate]} for
    the legs that succeeded. Every other leg is reported in execution."""
    execution = ctx.execution
    runnable = []
    for name in names:
        leg = ctx.legs.get(name)
        if leg is None:
            execution.stage(name, "skipped", kind="leg", reason="unknown_leg")
            continue
        try:
            ok, reason = leg.available(ctx)
        except Exception as exc:
            ok, reason = False, failure_reason(exc)
        if not ok:
            execution.stage(
                name,
                "skipped",
                kind="leg",
                exact=leg.exact,
                reason=reason or "unavailable",
            )
            _honoured(ctx, name, False)
            continue
        runnable.append(leg)
    results = {}
    if not runnable:
        return results
    started = time.perf_counter()
    budget = leg_timeout_ms()
    remaining = ctx.tracker.remaining_ms() if ctx.tracker is not None else None
    if remaining is not None:
        budget = min(budget, remaining)
    deadline = started + budget / 1000
    pool = ThreadPoolExecutor(
        max_workers=min(concurrency(), len(runnable)),
        thread_name_prefix="throughline-leg",
    )
    try:
        futures = [
            (
                leg,
                pool.submit(
                    contextvars.copy_context().run, _run_one, ctx, leg, deadline
                ),
            )
            for leg in runnable
        ]
        for leg, future in futures:
            try:
                candidates, ms = future.result(
                    timeout=max(0.0, deadline - time.perf_counter()) + 0.05
                )
            except LegSkipped as exc:
                execution.stage(
                    leg.name,
                    "skipped",
                    kind="leg",
                    exact=leg.exact,
                    reason=exc.reason,
                    round=ctx.round_no,
                )
                _honoured(ctx, leg.name, False)
                continue
            except Exception as exc:
                future.cancel()
                reason = failure_reason(exc)
                execution.stage(
                    leg.name,
                    "timeout" if reason == "timeout" else "failed",
                    kind="leg",
                    exact=leg.exact,
                    reason=reason,
                    round=ctx.round_no,
                )
                _honoured(ctx, leg.name, False)
                continue
            _stamp(leg.name, candidates)
            results[leg.name] = candidates
            execution.add_timing("leg:" + leg.name, ms)
            info = dict(
                kind="leg",
                exact=leg.exact,
                rows=len(candidates),
                ms=round(ms, 3),
                round=ctx.round_no,
            )
            if leg.name in ctx.truncated_legs:
                info["truncated"] = True
            execution.stage(leg.name, "ok", **info)
            try:
                _honoured(ctx, leg.name, leg.honours(ctx))
            except Exception:
                _honoured(ctx, leg.name, False)
    finally:
        # A leg still running after its deadline is abandoned, not awaited:
        # its statement is cancelled by its own statement timeout.
        pool.shutdown(wait=False, cancel_futures=True)
    execution.add_timing("retrieval", (time.perf_counter() - started) * 1000)
    return results
