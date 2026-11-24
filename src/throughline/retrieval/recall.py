"""recall(): one retrieval interface over every record kind (QRY-13..22).

Protocol (stable):
  clamp budgets; resolve the hard scope (unresolvable -> empty with reason);
  materialize deferred maintenance once; legs = PLANNER.initial(ctx);
  while legs and round <= THROUGHLINE_RECALL_MAX_ROUNDS:
      run the legs concurrently; fuse (RRF); legs, stop = PLANNER.next_round(...)
  min_score; stages in order; default selection (top k, then the token
  budget) when no select stage ran; hydrate through tl.record_sources;
  completeness; copy ctx.manifest/dropped/extras; ctx.after_recall callbacks;
  PLANNER.after_recall; save as a result set when asked.

recall() never calls a semantic model or a controller (QRY-40): escalation to
the librarian is the route's decision, after recall returns.
"""

import hashlib
import importlib
import logging
import os
import time

from ..budget import Budget, BudgetTracker, clamp
from ..execution import Execution, Trace
from . import default_planner, executor, fusion, hydrate, legs as legs_module
from . import ranking, scope_sql
from . import stages as stages_module
from .types import RecallRequest, RecallResponse

log = logging.getLogger("throughline.retrieval")

DEFAULT_MAX_ROUNDS = 2
TRACE_IDS = 1000


def max_rounds():
    try:
        value = int(os.getenv("THROUGHLINE_RECALL_MAX_ROUNDS") or DEFAULT_MAX_ROUNDS)
    except ValueError:
        value = DEFAULT_MAX_ROUNDS
    return max(1, min(value, 10))


def planner():
    """retrieval.planner.PLANNER when that module exists, else the default."""
    try:
        module = importlib.import_module(__package__ + ".planner")
    except ModuleNotFoundError as exc:
        if exc.name != __package__ + ".planner":
            raise
        return default_planner.PLANNER
    return getattr(module, "PLANNER", default_planner.PLANNER)


def _ids(candidates):
    return [c.record_id for c in candidates][:TRACE_IDS]


def _record_candidates(ctx, key, candidates):
    if ctx.request.trace:
        ctx.stage_candidates[key] = _ids(candidates)


def _request(request):
    if isinstance(request, RecallRequest):
        return request
    if isinstance(request, dict):
        return RecallRequest.model_validate(request)
    raise TypeError("recall needs a RecallRequest")


def _passes_min_score(candidate, minimum):
    scores = candidate.leg_scores()
    return not scores or max(scores) >= minimum


def _run_stages(ctx, candidates):
    ran_select = False
    for stage in stages_module.discover():
        try:
            if not stage.enabled(ctx):
                continue
        except Exception as exc:
            ctx.execution.stage(
                stage.name, "failed", kind="stage", reason=executor.failure_reason(exc)
            )
            continue
        started = time.perf_counter()
        exact = _stage_exact(stage, ctx)
        try:
            result = stage.run(ctx, list(candidates))
        except executor.StageFailed as exc:
            ctx.execution.stage(
                stage.name,
                "failed",
                kind="stage",
                exact=exact,
                reason=exc.reason,
                **exc.info,
            )
            continue
        except Exception as exc:
            log.warning("recall stage %s failed: %s", stage.name, type(exc).__name__)
            ctx.execution.stage(
                stage.name,
                "failed",
                kind="stage",
                exact=exact,
                reason=executor.failure_reason(exc),
            )
            continue
        ms = (time.perf_counter() - started) * 1000
        if result is not None:
            candidates = list(result)
        ctx.execution.add_timing("stage:" + stage.name, ms)
        ctx.execution.stage(
            stage.name,
            "ok",
            kind="stage",
            exact=exact,
            order=stage.order,
            rows=len(candidates),
            ms=round(ms, 3),
        )
        if stage.name == "select":
            ran_select = True
        _record_candidates(ctx, "after_" + stage.name, candidates)
    return candidates, ran_select


def _stage_exact(stage, ctx):
    """A stage's exactness for this request: exact_for(ctx) when the stage
    depends on the request (rerank follows its ranker), else stage.exact."""
    exact_for = getattr(stage, "exact_for", None)
    if callable(exact_for):
        try:
            return bool(exact_for(ctx))
        except Exception:
            return False
    return getattr(stage, "exact", True)


def _excerpt(records, query, limit):
    """Long contents cut to their best match for the query, tokens recounted."""
    from ..agent_view import snippet
    from ..budget import estimate_tokens

    for record in records:
        content = record.get("content")
        if isinstance(content, str) and len(content) > limit:
            record["content"] = snippet(content, query, limit)[0]
            record["tokens"] = estimate_tokens(record["content"])


def _token_cut(ctx, records):
    """Keep records in order while they fit the token budget."""
    kept = []
    for index, record in enumerate(records):
        if ctx.tracker.spend_tokens(record["tokens"] or 0):
            kept.append(record)
            continue
        for rest in records[index:]:
            ctx.dropped.append(dict(record_id=rest["record_id"], reason="budget"))
        ctx.execution.set_truncation("token_budget", ctx.tracker.budget.tokens)
        break
    return kept


def _save(ctx, records, completeness, counts):
    try:
        from .. import resultsets
    except ImportError:
        ctx.execution.degrade("save", "result_sets_unavailable")
        return None
    request = ctx.request
    origin = dict(
        op="recall",
        query_sha256=hashlib.sha256((request.query or "").encode("utf-8")).hexdigest(),
        strategy=request.strategy,
        legs=list(ctx.ran),
        k=request.k,
    )
    try:
        return resultsets.create(
            ctx.scope,
            records,
            origin=origin,
            completeness=completeness,
            counts=counts,
            session=request.session,
            as_of=request.scope.as_of,
            known_by=request.scope.known_by,
        )
    except Exception as exc:
        log.warning("recall result set not saved: %s", type(exc).__name__)
        ctx.execution.degrade("save", executor.failure_reason(exc))
        return None


def _exactness(ctx):
    flags = [
        s.get("exact")
        for s in ctx.execution.to_dict()["stages"]
        if s.get("status") == "ok" and s.get("kind") in ("leg", "stage")
    ]
    flags = [f for f in flags if f is not None]
    if not flags:
        return "exact"
    if all(flags):
        return "exact"
    return "approximate" if not any(flags) else "mixed"


def recall(scope, request, token_row=None, trace=None):
    """Recall records for a request under a scope -> RecallResponse."""
    started = time.perf_counter()
    request = _request(request)
    # An unknown ranker is the caller's error (400), raised before any work.
    chosen_ranker, ranker_warning = ranking.choose(request)
    budget = clamp(Budget(**request.budgets.model_dump()), token_row)
    tracker = BudgetTracker(budget)
    own_trace = None
    if trace is None and request.trace:
        own_trace = trace = Trace(
            scope,
            "recall",
            dict(
                k=request.k,
                strategy=request.strategy,
                legs=request.legs,
                query_sha256=hashlib.sha256(
                    (request.query or "").encode("utf-8")
                ).hexdigest(),
            ),
        )
    execution = Execution(tracker, trace_id=trace.id if trace is not None else None)
    ctx = executor.RecallContext(
        scope,
        request,
        token_row=token_row,
        tracker=tracker,
        execution=execution,
        trace=trace,
        legs=legs_module.discover(),
    )
    ctx.ranker = chosen_ranker
    execution.set("ranker", ranking.describe(chosen_ranker))
    if ranker_warning:
        execution.warn(ranker_warning)
    active = planner()
    try:
        ctx.resolved = scope_sql.resolve(ctx, request.scope)
    except Exception as exc:
        # The deadline passed while names in the scope were being resolved:
        # nothing was searched, and the response says so instead of failing.
        if executor.failure_reason(exc) != "timeout":
            raise
        ctx.resolved = scope_sql.Resolved(
            scope=request.scope, kinds=scope_sql.message_kinds(request.scope)
        )
        execution.stage("scope", "timeout", kind="scope", reason="timeout")
        execution.completeness("truncated" if request.exhaustive else "ranked_sample")
        return _finish(ctx, [], None, started, own_trace)
    if ctx.resolved.historical:
        execution.set("as_of_honoured", ctx.as_of_honoured)
    if ctx.resolved.unresolvable:
        field = ctx.resolved.unresolvable
        execution.warn(f"unresolvable_scope:{field}")
        execution.stage(
            "scope", "skipped", kind="scope", reason=f"unresolvable_scope:{field}"
        )
        execution.completeness("truncated" if request.exhaustive else "ranked_sample")
        return _finish(ctx, [], None, started, own_trace)

    from ..derived import materialize_for_read, read_wait_s

    remaining = tracker.remaining_ms()
    wait = read_wait_s() if remaining is None else min(read_wait_s(), remaining / 1000)
    materialize_started = time.perf_counter()
    if not materialize_for_read(wait):
        execution.warn("deferred thread maintenance did not finish in time")
    execution.add_timing(
        "materialize", (time.perf_counter() - materialize_started) * 1000
    )

    planning = time.perf_counter()
    legs = active.initial(ctx)
    planned_ms = (time.perf_counter() - planning) * 1000
    results, rounds, stop_reason = {}, 0, None
    k = fusion.rrf_k(request)
    fused = []
    round_no = 1
    limit = max_rounds()
    while legs and round_no <= limit:
        ctx.round_no = round_no
        ran = executor.run_legs(ctx, legs)
        ctx.ran.extend(name for name in legs if name not in ctx.ran)
        results.update(ran)
        rounds += 1
        if request.trace:
            legs_seen = ctx.stage_candidates.setdefault("legs", {})
            for name, candidates in ran.items():
                legs_seen[name] = _ids(candidates)
        fusion_started = time.perf_counter()
        fused = fusion.rrf(results, k)
        execution.add_timing("fusion", (time.perf_counter() - fusion_started) * 1000)
        planning = time.perf_counter()
        legs, stop_reason = active.next_round(ctx, fused, round_no)
        planned_ms += (time.perf_counter() - planning) * 1000
        round_no += 1
    if legs and round_no > limit:
        stop_reason = "max_rounds"
    if not ctx.ran and stop_reason is None:
        stop_reason = "no_legs"
    execution.add_timing("planner", planned_ms)
    execution.stage(
        "planner",
        "ok",
        kind="planner",
        planner=getattr(active, "name", type(active).__name__),
        rounds=rounds,
        legs=list(ctx.ran),
        stop_reason=stop_reason,
    )
    execution.stage("fuse", "ok", kind="fusion", exact=True, k=k, rows=len(fused))
    _record_candidates(ctx, "fused", fused)

    candidates = fused
    # Seen non-message records go before any selection, so k still holds
    # (seen message records never reach a leg's limit: exclude_message_ids).
    excluded = ctx.resolved.exclude_records
    if excluded:
        candidates = [c for c in candidates if c.record_id not in excluded]
    minimum = request.scope.min_score
    if minimum is not None:
        kept = []
        for candidate in candidates:
            if _passes_min_score(candidate, minimum):
                kept.append(candidate)
            else:
                ctx.dropped.append(
                    dict(record_id=candidate.record_id, reason="min_score")
                )
        candidates = kept

    candidates, selected_by_stage = _run_stages(ctx, candidates)
    cut = False
    if not selected_by_stage:
        limit_k = executor.exhaustive_cap() if request.exhaustive else request.k
        if len(candidates) > limit_k:
            if request.exhaustive:
                cut = True
                execution.set_truncation("row_cap", limit_k)
            for rest in candidates[limit_k:]:
                ctx.dropped.append(dict(record_id=rest.record_id, reason="rank"))
            candidates = candidates[:limit_k]
    hydrate_started = time.perf_counter()
    try:
        records = hydrate.records(ctx, candidates)
        if request.include_images:
            hydrate.attach_assets(ctx.sql, records, include_nsfw=request.include_nsfw)
    except Exception as exc:
        # The deadline passed before the selected candidates could be read:
        # the recall degrades to no records and declares itself incomplete.
        if executor.failure_reason(exc) != "timeout":
            raise
        execution.stage("hydrate", "timeout", kind="hydrate", reason="timeout")
        for candidate in candidates:
            ctx.dropped.append(dict(record_id=candidate.record_id, reason="timeout"))
        records = []
        cut = cut or bool(candidates)
    execution.add_timing("hydrate", (time.perf_counter() - hydrate_started) * 1000)
    if request.excerpt_chars:
        _excerpt(records, request.query or "", request.excerpt_chars)
    before_cut = len(records)
    records = _token_cut(ctx, records)
    cut = cut or len(records) < before_cut
    if request.trace:
        ctx.stage_candidates["selected"] = [r["record_id"] for r in records][:TRACE_IDS]

    if request.exhaustive:
        incomplete = any(
            s.get("kind") == "leg" and s.get("status") != "ok"
            for s in execution.to_dict()["stages"]
        )
        complete = not (cut or incomplete or ctx.truncated_legs)
        execution.completeness("exhaustive" if complete else "truncated")
        if ctx.truncated_legs and not cut:
            execution.set_truncation("row_cap", executor.exhaustive_cap())
    else:
        execution.completeness("ranked_sample")
    return _finish(ctx, records, fused, started, own_trace, active)


def _finish(ctx, records, fused, started, own_trace, active=None):
    execution = ctx.execution
    counts = dict(
        retrieved=len(fused or []),
        mechanically_processed=len(fused or []),
        semantically_evaluated=0,
        inspected_by_controller=0,
        returned=len(records),
        per_leg={
            s["name"]: s.get("rows", 0)
            for s in execution.to_dict()["stages"]
            if s.get("kind") == "leg" and s.get("status") == "ok"
        },
    )
    execution.set("counts", counts)
    execution.set("exactness", _exactness(ctx))
    # Source coverage of the final records, on every path.
    try:
        from .. import coverage

        coverage.recall_summary(ctx, records)
    except Exception as exc:
        execution.degrade("coverage", executor.failure_reason(exc))
    if ctx.request.trace:
        execution.set("stage_candidates", ctx.stage_candidates)
    for callback in list(ctx.after_recall):
        try:
            callback(ctx, records)
        except Exception as exc:
            execution.stage(
                "after_recall",
                "failed",
                kind="callback",
                reason=executor.failure_reason(exc),
            )
    if active is not None:
        try:
            active.after_recall(ctx)
        except Exception as exc:
            execution.stage(
                "after_recall",
                "failed",
                kind="planner",
                reason=executor.failure_reason(exc),
            )
    result_set = None
    if ctx.request.save:
        result_set = _save(ctx, records, execution.completeness(), counts)
    execution.add_timing("total", (time.perf_counter() - started) * 1000)
    if own_trace is not None:
        if not own_trace.flush(execution):
            execution.trace_id = None
            execution.warn("trace was not written")
    return RecallResponse(
        records=records,
        execution=execution.to_dict(),
        result_set=result_set,
        manifest=ctx.manifest,
        dropped=list(ctx.dropped),
        extras=dict(ctx.extras),
    )
