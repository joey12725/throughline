"""Runs of the query helper: an in-memory registry and the bounded loop.

A run belongs to the token (and asker) that started it, lives for 30
minutes and is never written to the database: what the helper saw and said
stays in process memory (the statements it ran are in its trace and in the
audit log, like any read). At most four runs of one token are active at once,
and a run is active until its worker returns (a cancelled run included); at
most MAX_BUSY runs are active in the process (429 beyond). Ended runs are
kept at most MAX_ENDED_PER_TOKEN per token and MAX_ENDED in all.
The loop runs on a worker thread; GET long-polls on an event, never a busy
loop.

The loop is bounded three ways: tool steps (THROUGHLINE_ASSISTANT_MAX_STEPS),
spend and time (THROUGHLINE_ASSISTANT_DEADLINE_S). Spend: one run may cost at
most the lower of THROUGHLINE_ASSISTANT_MAX_USD and the token's
max_model_cost, and one token at most THROUGHLINE_ASSISTANT_DAILY_USD in 24
hours (from the provider ledger). Each turn's output is capped so its worst
case (max_tokens at the output price) fits what is left, and that worst case
passes the cost gate before the call. A run that reaches a bound ends with a
final answer that says so.
"""

import asyncio
import hashlib
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .. import cost, providers
from ..budget import Budget, BudgetTracker
from ..execution import Trace
from ..librarian.controller import redact
from . import daily_usd, local, max_steps, max_usd, prompt, tiers_for
from .catalog import Catalog
from .tools import TOOLS, Belt

TTL_S = 30 * 60
MAX_ACTIVE_PER_TOKEN = 4
MAX_ENDED_PER_TOKEN = 50
MAX_ENDED = 500
MAX_WAITERS = 64
WORKERS = 8
# Active runs in the process: the workers plus a bounded queue.
MAX_BUSY = WORKERS * 3
# A turn that cannot afford this many output tokens is not started.
MIN_OUTPUT_TOKENS = 1024
DEFAULT_DEADLINE_S = 300
MAX_PAUSES = 2


class TooManyRuns(Exception):
    """The token already has MAX_ACTIVE_PER_TOKEN runs active."""

    detail = f"at most {MAX_ACTIVE_PER_TOKEN} helper runs at once"


class Busy(TooManyRuns):
    """The process already has MAX_BUSY runs active."""

    detail = "the helper is busy; retry shortly"


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _short(exc, limit=200):
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return redact(text)[:limit]


def deadline_s():
    import os

    value = (os.getenv("THROUGHLINE_ASSISTANT_DEADLINE_S") or "").strip()
    try:
        seconds = float(value) if value else DEFAULT_DEADLINE_S
    except ValueError:
        raise ValueError("THROUGHLINE_ASSISTANT_DEADLINE_S must be a number") from None
    if not 5 <= seconds <= 1800:
        raise ValueError("THROUGHLINE_ASSISTANT_DEADLINE_S must be 5..1800")
    return seconds


class Run:
    def __init__(self, token, request, provider, tier):
        self.id = "r_" + secrets.token_urlsafe(12)
        self.owner = (token["id"], token.get("asker") or None)
        self.request = request
        self.provider = provider
        self.tier = tier
        self.status = "running"
        self.events = []
        self.final = None
        self.error = None
        self.usage = dict(tokens_in=0, tokens_out=0, cost_usd=0.0, steps=0)
        self.created = time.monotonic()
        self.ended = None
        self.cancelled = threading.Event()
        self.trace_id = None
        # Called once by the worker just before the run ends (writes its
        # trace); only the worker thread ever calls it.
        self.on_finish = None
        self.worker_ident = None
        # Set when the worker returned: until then the run holds its slot.
        self.worker_done = False
        self._lock = threading.Lock()
        self._waiters = set()

    # -- events ------------------------------------------------------------------
    def event(self, kind, **fields):
        with self._lock:
            entry = dict(seq=len(self.events) + 1, at=_now(), kind=kind, **fields)
            self.events.append(entry)
            waiters = list(self._waiters)
        for loop, flag in waiters:
            try:
                loop.call_soon_threadsafe(flag.set)
            except RuntimeError:  # The waiting loop is gone.
                pass
        return entry

    def finish(self, status, final=None, error=None):
        hook = None
        with self._lock:
            if self.status != "running":
                return False
            # The trace is written by the worker only: a cancel from a
            # request thread never races the worker on it.
            if threading.get_ident() == self.worker_ident:
                hook, self.on_finish = self.on_finish, None
        if hook is not None:
            try:
                hook()
            except Exception:  # The trace is best effort.
                pass
        with self._lock:
            if self.status != "running":
                return False
            self.status = status
            self.final = final
            self.error = error
            self.ended = time.monotonic()
        self.event(
            "final" if status == "done" else status,
            **({"error": error} if error else {}),
        )
        return True

    @property
    def running(self):
        return self.status == "running"

    def view(self, after=0):
        with self._lock:
            events = [dict(e) for e in self.events if e["seq"] > after]
            return dict(
                run=self.id,
                status=self.status,
                provider=self.provider.name,
                model=self.provider.model,
                tier=self.tier,
                events=events,
                final=self.final,
                usage=dict(self.usage, cost_usd=round(self.usage["cost_usd"], 6)),
                error=self.error,
                trace_id=self.trace_id,
            )

    def has_after(self, after):
        with self._lock:
            return len(self.events) > after or self.status != "running"

    async def wait(self, after, seconds):
        """Return when an event after `after` exists, the run ends, or the
        time is up. Holds no thread and no database connection."""
        if seconds <= 0 or self.has_after(after):
            return
        loop = asyncio.get_running_loop()
        flag = asyncio.Event()
        entry = (loop, flag)
        with self._lock:
            if len(self._waiters) >= MAX_WAITERS:
                return
            self._waiters.add(entry)
        try:
            ends = loop.time() + seconds
            # Every new event wakes the waiters; one at or before `after`
            # (a client that is ahead) waits on.
            while not self.has_after(after):
                left = ends - loop.time()
                if left <= 0:
                    return
                flag.clear()
                if self.has_after(after):
                    return
                try:
                    await asyncio.wait_for(flag.wait(), left)
                except (asyncio.TimeoutError, TimeoutError):
                    return
        finally:
            with self._lock:
                self._waiters.discard(entry)


_runs = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="assistant")


def _sweep():
    """Expire runs after TTL_S (a run whose worker is still going is
    cancelled and kept until it returns) and keep at most
    MAX_ENDED_PER_TOKEN ended runs per token and MAX_ENDED in all."""
    now = time.monotonic()
    with _lock:
        for run_id, run in list(_runs.items()):
            if now - run.created > TTL_S:
                run.cancelled.set()
                if run.worker_done:
                    del _runs[run_id]
        ended = sorted(
            (r for r in _runs.values() if r.worker_done and not r.running),
            key=lambda r: r.created,
            reverse=True,
        )
        per_token = {}
        kept = 0
        for run in ended:
            count = per_token.get(run.owner[0], 0)
            if count >= MAX_ENDED_PER_TOKEN or kept >= MAX_ENDED:
                del _runs[run.id]
                continue
            per_token[run.owner[0]] = count + 1
            kept += 1


def lookup(token, run_id):
    """The run when this token (and asker) started it, else None."""
    _sweep()
    with _lock:
        run = _runs.get(run_id)
    if run is None or run.owner != (token["id"], token.get("asker") or None):
        return None
    return run


def start(token, scope, request):
    """Register a run and start its loop; raises ValueError (400) for a bad
    request or configuration, TooManyRuns (429)."""
    found = providers.get("assistant")
    if found.name == "anthropic" and not getattr(found, "key", None):
        raise ValueError(
            "assistant not configured: set THROUGHLINE_ASSISTANT_API_KEY (or ANTHROPIC_API_KEY)"
        )
    allowed, default = tiers_for(found)
    tier = request.get("tier") or default
    if tier not in allowed:
        raise ValueError(
            f"tier {tier} is not allowed for this assistant endpoint (allowed: {', '.join(allowed)})"
        )
    steps, ceiling, seconds = max_steps(), max_usd(), deadline_s()
    daily_usd()  # A bad setting is a 400 here, not a failed run.
    _sweep()
    run = Run(token, request, found, tier)
    with _lock:
        busy = [r for r in _runs.values() if not r.worker_done]
        if sum(1 for r in busy if r.owner[0] == token["id"]) >= MAX_ACTIVE_PER_TOKEN:
            raise TooManyRuns()
        if len(busy) >= MAX_BUSY:
            raise Busy()
        _runs[run.id] = run
    token_row = {k: v for k, v in token.items() if k != "audit"}
    try:
        _pool.submit(_execute, run, token_row, scope, steps, ceiling, seconds)
    except RuntimeError:  # The pool is shut down (the process is stopping).
        run.worker_done = True
        run.finish("failed", error="assistant run failed: the server is stopping")
    return run


def cancel(run):
    """Stop a run: it reads as cancelled at once; its worker returns at its
    next check and keeps the run's slot until then."""
    run.cancelled.set()
    run.finish("cancelled")
    return run


# -- the loop ------------------------------------------------------------------------


def _execute(run, token, scope, steps, ceiling, seconds):
    run.worker_ident = threading.get_ident()
    try:
        _work(run, token, scope, steps, ceiling, seconds)
    finally:
        run.worker_done = True


def _work(run, token, scope, steps, ceiling, seconds):
    trace = Trace(
        scope,
        "assistant",
        dict(
            run=run.id,
            verb=run.request["verb"],
            provider=run.provider.name,
            tier=run.tier,
        ),
    )
    deadline = time.monotonic() + seconds

    def flush():
        if trace.events and trace.flush():
            run.trace_id = trace.id

    run.on_finish = flush
    try:
        catalog = Catalog(scope)
        belt = Belt(
            token,
            scope,
            run.tier,
            run.provider,
            catalog,
            trace=trace,
            deadline=deadline,
            run_id=run.id,
        )
        if run.provider.scripted:
            _local(run, belt)
        else:
            _model(run, belt, token, scope, trace, steps, ceiling, deadline)
    except Exception as exc:  # A run never takes the process down.
        run.finish("failed", error="assistant run failed: " + _short(exc))
    finally:
        with run._lock:
            hook, run.on_finish = run.on_finish, None
        if hook is not None:
            # Ended without finish() on this thread (cancelled from outside):
            # still traced, by the worker.
            try:
                hook()
            except Exception:
                pass


def _emit(run):
    def emit(tool, arguments, outcome, **extra):
        if run.cancelled.is_set():
            return
        fields = dict(tool=tool)
        details = dict(getattr(outcome, "event", {}) or {})
        if "sql" not in details and "sql" not in extra:
            fields["input"] = details.pop("input", arguments)
        else:
            details.pop("input", None)
        fields.update(details)
        fields.update(extra)
        fields.setdefault("summary", getattr(outcome, "summary", ""))
        fields.setdefault("sent", getattr(outcome, "sent", ""))
        fields.setdefault("ms", fields.get("ms") or 0)
        if getattr(outcome, "is_error", False):
            fields["is_error"] = True
        run.event("step", **fields)
        with run._lock:
            run.usage["steps"] += 1

    return emit


def _local(run, belt):
    emit = _emit(run)
    timed = {}

    def timed_emit(tool, arguments, outcome, **extra):
        extra.setdefault(
            "ms",
            round((time.perf_counter() - timed.get("t", time.perf_counter())) * 1000),
        )
        emit(tool, arguments, outcome, **extra)
        timed["t"] = time.perf_counter()

    timed["t"] = time.perf_counter()
    found = local.answer(belt, run.request, timed_emit)
    if run.cancelled.is_set():
        return
    run.finish("done", final=_final(found))


def _final(found):
    return dict(
        answer=found.get("answer") or "",
        statement=found.get("statement"),
        spec=found.get("spec"),
        notes=list(found.get("notes") or []),
    )


def _checked_final(run, belt, arguments):
    """(final, problem): the model's final call with its statement validated
    and its spec completed by the chart rules."""
    answer = arguments.get("answer") if isinstance(arguments.get("answer"), str) else ""
    statement = (
        arguments.get("statement")
        if isinstance(arguments.get("statement"), str)
        else None
    )
    notes = [n for n in (arguments.get("notes") or []) if isinstance(n, str)][:10]
    if not statement or not statement.strip():
        statement = run.request.get("statement")
    problem = None
    if statement:
        check = belt.validate(statement)
        if not check["valid"]:
            problem = (check["error"] or "invalid") + (
                f" (SQLSTATE {check['code']})" if check["code"] else ""
            )
            if check.get("detail"):
                problem += ": " + check["detail"]
    spec = None
    given = arguments.get("spec")
    if isinstance(given, dict) and statement and problem is None:
        source = given.get("source") if isinstance(given.get("source"), dict) else {}
        spec, problems = belt.spec(
            source.get("sql")
            if isinstance(source.get("sql"), str) and source.get("sql").strip()
            else statement,
            given.get("lens"),
            given.get("encode") if isinstance(given.get("encode"), dict) else {},
            title=given.get("title") if isinstance(given.get("title"), str) else None,
            why=given.get("why") if isinstance(given.get("why"), str) else None,
            options=given.get("options")
            if isinstance(given.get("options"), dict)
            else None,
            channel=run.request.get("channel"),
        )
        if problems:
            notes.append("The proposed view was dropped: " + "; ".join(problems)[:300])
    return dict(answer=answer, statement=statement, spec=spec, notes=notes), problem


def _model(run, belt, token, scope, trace, steps, ceiling, deadline):
    provider = run.provider
    system = [prompt.static_text(), prompt.dynamic_text(belt.catalog)]
    messages = [
        {"role": "user", "text": prompt.user_text(run.request, run.tier, steps)}
    ]
    # The run's limit: the owner's ceiling and the token's own limit, which
    # holds for the whole run, not for each turn.
    limit = token.get("max_model_cost")
    run_limit = min(
        float(cost.DEFAULT_MODEL_COST if limit is None else limit), float(ceiling)
    )
    daily = daily_usd()
    tracker = BudgetTracker(Budget(model_cost=run_limit, model_calls=steps * 2 + 6))
    emit = _emit(run)
    used = 0
    nudged = False
    retried_final = False
    pauses = 0
    over_limit = 0
    last_good = None
    last_text = ""

    def stop(reason):
        statement = last_good or run.request.get("statement")
        answer = f"Stopped before finishing: {reason}."
        if last_text:
            answer += "\n\n" + last_text
        if statement:
            answer += "\n\nThe statement is the last one that ran without an error."
        run.finish(
            "done",
            final=dict(answer=answer, statement=statement, spec=None, notes=[reason]),
        )

    turn = 0
    while True:
        if run.cancelled.is_set():
            return
        if time.monotonic() >= deadline:
            return stop("the run's time limit was reached")
        turn += 1
        with run._lock:
            spent = run.usage["cost_usd"]
        try:
            today = _spent_today(token["id"])
        except Exception:
            return stop("this token's helper spend could not be read from the ledger")
        run_left = run_limit - spent
        day_left = daily - today
        left = min(run_left, day_left)
        output = _output_tokens(provider, system, messages, left)
        if output < MIN_OUTPUT_TOKENS:
            if day_left < run_left:
                return stop(
                    f"this token's helper spend limit of {daily:.2f} USD per 24 hours was reached"
                )
            return stop(
                f"the spend ceiling of {run_limit:.2f} USD for one run was reached"
                + (
                    " (this token's max_model_cost)"
                    if run_limit < ceiling
                    else ""
                )
            )
        estimate = provider.estimate(system, TOOLS, messages, output_tokens=output)
        try:
            cost.authorize(
                token,
                [estimate],
                scope=scope,
                caller_limit=left,
                request_hash=cost.request_hash(
                    dict(assistant=run.id, turn=turn), scope, [], dict(max_usd=ceiling)
                ),
            )
        except cost.ApprovalRequired as exc:
            return stop(
                f"one model turn would cost up to {exc.estimate['cost']:.4f} USD, above the "
                f"{exc.limit:.4f} USD this run may still spend (approval {exc.approval_id})"
            )
        try:
            reply, usage = providers.call(
                "assistant",
                "assistant",
                lambda: provider.turn(system, TOOLS, messages, max_tokens=output),
                estimate=estimate,
                scope=scope,
                tracker=tracker,
                trace=trace,
                origin="assistant",
                origin_ref=run.id,
            )
        except providers.ProviderUnavailable as exc:
            if "budget exhausted" in str(exc):
                return stop(
                    f"the spend ceiling of {run_limit:.2f} USD for one run was reached"
                )
            return run.finish("failed", error=_short(exc))
        # The worst case was reserved; what the turn did not spend is free
        # again (a turn that cost more was charged its extra by the call).
        tracker.release(max(0.0, float(estimate.cost or 0) - float(usage.cost or 0)), 0)
        with run._lock:
            run.usage["tokens_in"] += int(reply.get("tokens_in") or 0)
            run.usage["tokens_out"] += int(reply.get("tokens_out") or 0)
            run.usage["cost_usd"] += float(usage.cost or 0)
        if run.cancelled.is_set():
            return
        messages.append(
            dict(
                role="assistant",
                native=reply["native"],
                text=reply["text"],
                calls=reply["calls"],
            )
        )
        if reply["text"]:
            last_text = reply["text"]
            run.event("text", text=reply["text"])
        if reply["stop"] == "refusal":
            category = f" (category: {reply['detail']})" if reply.get("detail") else ""
            run.finish(
                "done",
                final=dict(
                    answer="The model declined this request"
                    + category
                    + ". Nothing was changed.",
                    statement=run.request.get("statement") or last_good,
                    spec=None,
                    notes=["refusal" + category],
                ),
            )
            return
        if reply["stop"] == "pause" and not reply["calls"]:
            pauses += 1
            if pauses > MAX_PAUSES:
                return stop("the model paused its turn too often")
            continue
        calls = reply["calls"]
        if not calls:
            if reply["stop"] == "max_tokens":
                return stop("the model's answer was cut off at its output limit")
            if not nudged:
                nudged = True
                messages.append(
                    dict(
                        role="user",
                        text="Finish now: call final with your answer and the statement.",
                    )
                )
                continue
            run.finish(
                "done",
                final=dict(
                    answer=reply["text"] or "The model ended without an answer.",
                    statement=last_good or run.request.get("statement"),
                    spec=None,
                    notes=["the model did not call final"],
                ),
            )
            return
        results = []
        finished = None
        for call in calls:
            if call["name"] == "final":
                final, problem = _checked_final(run, belt, call["input"])
                if problem and not retried_final:
                    retried_final = True
                    emit(
                        "final",
                        {},
                        _Note(
                            "final statement does not validate",
                            "the validator's message",
                        ),
                        sql=final["statement"],
                        error=problem,
                        is_error=True,
                    )
                    results.append(
                        dict(
                            id=call["id"],
                            name="final",
                            is_error=True,
                            content=f"The statement does not validate: {problem}. Fix it and call final again.",
                        )
                    )
                    continue
                if problem:
                    final["notes"].append("The statement does not validate: " + problem)
                finished = final
                results.append(
                    dict(id=call["id"], name="final", is_error=False, content="done")
                )
                break
            if used >= steps:
                over_limit += 1
                results.append(
                    dict(
                        id=call["id"],
                        name=call["name"],
                        is_error=True,
                        content="The tool step limit is reached. Call final now.",
                    )
                )
                continue
            used += 1
            began = time.perf_counter()
            outcome = belt.call(call["name"], call["input"])
            outcome.event.setdefault("ms", round((time.perf_counter() - began) * 1000))
            emit(call["name"], call["input"], outcome)
            if call["name"] == "run_sql" and not outcome.is_error:
                last_good = call["input"].get("statement")
            results.append(
                dict(
                    id=call["id"],
                    name=call["name"],
                    is_error=outcome.is_error,
                    content=outcome.text(),
                )
            )
        if finished is not None:
            run.finish("done", final=finished)
            return
        if over_limit >= 2:
            return stop(f"the limit of {steps} tool steps was reached")
        messages.append(dict(role="tool", results=results))


def _spent_today(token_id):
    """USD this token's helper runs spent in the last 24 hours (ledger)."""
    from ..db import connect

    with connect("app") as conn:
        row = conn.execute(
            "SELECT coalesce(sum(cost),0)::float8 AS usd FROM tl.model_ledger"
            " WHERE token_id=%s AND origin='assistant' AND at>now()-interval '24 hours'",
            (token_id,),
        ).fetchone()
    return float(row["usd"] or 0)


def _output_tokens(provider, system, messages, left):
    """The output cap of the next turn: the provider's max_tokens, lowered
    so that the turn's worst case (its input plus that many output tokens)
    costs at most `left` USD."""
    cap = int(getattr(provider, "max_tokens", 0) or 16000)
    price_out = float(getattr(provider, "price_out", 0) or 0)
    if price_out <= 0:
        return cap
    floor = provider.estimate(system, TOOLS, messages, output_tokens=0).cost
    affordable = int((left - floor) * 1000000 / price_out)
    return max(0, min(cap, affordable))


class _Note:
    def __init__(self, summary, sent):
        self.summary = summary
        self.sent = sent
        self.event = {}
        self.is_error = True


def digest(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
