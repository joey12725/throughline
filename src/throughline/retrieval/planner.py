"""Adaptive planner (QRY-15, QRY-21, CST-8); replaces default_planner by
module presence (recall.planner()).

initial(ctx)
  The legs the request names (request.legs, strategy or exhaustive keep the
  default planner's choice exactly). For strategy 'auto' only available legs
  are planned (an unavailable leg is disclosed as skipped with its reason, as
  the executor would); vector is pruned for a query without content tokens,
  lexical for a query whose tsquery is empty, and a leg whose p95 (measured
  profile in tl.operator_profiles, else DEFAULT_P95_MS by cost class) exceeds
  the remaining deadline is pruned. Pruned legs appear in execution with
  status 'pruned' (a planner decision, not a degradation). links runs when
  starting sets or message references exist.
  Each leg's logical operation goes through operators.choose(): another
  registered leg implementing the same operation replaces it only when its
  recorded quality is not worse and its measured p95 is lower; an
  approximate implementation needs request.hints.allow_approximate.

next_round(ctx, candidates, round_no) -> (legs or None, stop_reason)
  A second round runs only for strategy 'auto' when fewer than k candidates
  were found or a soft hints.must_terms term is not covered, and only with
  legs not yet run (fallback legs: registered legs outside the default set;
  structured only when the query has no content tokens). The hard scope is
  never relaxed. Stop reasons: single_round, deadline, budget,
  no_new_candidates, enough_candidates, low_expected_gain (expected gain
  below THROUGHLINE_MIN_ROUND_GAIN, 0.1), no_more_legs. The planner never
  reports that evidence is sufficient.

after_recall(ctx)
  Appends measured leg and 'context.fetch' timings to tl.operator_profiles
  (source 'runtime', quality carried over from the latest recorded quality)
  at most once per minute per implementation.
"""

import os
import threading
import time

from .. import operators
from ..db import connect
from . import default_planner, executor, selection

DEFAULT_P95_MS = dict(free=250.0, embedding=1500.0, model=10000.0)
DEFAULT_MIN_ROUND_GAIN = 0.1
PROFILE_INTERVAL_S = 60.0
SAMPLES = 50
SOFT_TERMS_TOP = 50
BASE_LEGS = ("lexical", "vector", "entity", "links", "structured")


def min_round_gain():
    try:
        return float(os.getenv("THROUGHLINE_MIN_ROUND_GAIN") or DEFAULT_MIN_ROUND_GAIN)
    except ValueError:
        return DEFAULT_MIN_ROUND_GAIN


def runtime_profiles_enabled():
    return (os.getenv("THROUGHLINE_RUNTIME_PROFILES") or "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def profiles(ctx):
    """Latest profile rows (cached on the context for this recall)."""
    rows = getattr(ctx, "_operator_profiles", None)
    if rows is None:
        try:
            with connect("app") as conn:
                rows = [dict(r) for r in operators.load_profiles(conn)]
        except Exception as exc:
            ctx.execution.degrade("planner", executor.failure_reason(exc))
            rows = []
        ctx._operator_profiles = rows
    return rows


def profile_for(rows, name):
    """The latest row of one operator (by measured_at)."""
    found = None
    for row in rows or ():
        if row.get("operator") != name:
            continue
        if found is None or (row.get("measured_at") or 0) >= (
            found.get("measured_at") or 0
        ):
            found = row
    return found


def p95(ctx, leg):
    row = profile_for(profiles(ctx), leg.spec().name)
    if row is not None and row.get("p95_ms") is not None:
        return float(row["p95_ms"])
    return DEFAULT_P95_MS.get(leg.cost_class, DEFAULT_P95_MS["free"])


def allow_approximate(ctx):
    return bool((ctx.request.hints or {}).get("allow_approximate"))


def choose_leg(ctx, name):
    """The leg that runs for name's logical operation (operators.choose)."""
    leg = ctx.legs.get(name)
    if leg is None:
        return name
    spec = leg.spec()
    chosen = operators.choose(
        spec.implements, profiles(ctx), allow_approximate=allow_approximate(ctx)
    )
    if chosen is None or chosen.name == spec.name:
        return name
    for other, candidate in ctx.legs.items():
        try:
            if candidate.spec().name == chosen.name:
                return other
        except Exception:
            continue
    return name


def _state(ctx):
    state = getattr(ctx, "_planner_state", None)
    if state is None:
        state = dict(considered=set(), counts=[], choices={})
        ctx._planner_state = state
    return state


def _skip(ctx, name, leg, reason):
    ctx.execution.stage(name, "skipped", kind="leg", exact=leg.exact, reason=reason)
    executor._honoured(ctx, name, False)


def _prune(ctx, name, leg, reason):
    ctx.execution.stage(name, "pruned", kind="leg", exact=leg.exact, reason=reason)


def _available(ctx, leg):
    try:
        return leg.available(ctx)
    except Exception as exc:
        return False, executor.failure_reason(exc)


def _remaining_ms(ctx):
    return ctx.tracker.remaining_ms() if ctx.tracker is not None else None


def _explicit(request):
    return bool(request.legs) or request.exhaustive or request.strategy != "auto"


class AdaptivePlanner:
    name = "adaptive"

    def __init__(self):
        self._lock = threading.Lock()
        self._samples = {}  # operator -> [ms]
        self._written = {}  # operator -> monotonic time of the last write

    # --- round 1 -------------------------------------------------------------
    def initial(self, ctx):
        request = ctx.request
        planned = default_planner.PLANNER.initial(ctx)
        state = _state(ctx)
        state["considered"].update(planned)
        if _explicit(request):
            return planned
        query = request.query or ""
        content = selection.has_content_tokens(query)
        remaining = _remaining_ms(ctx)
        legs, slow = [], []
        for name in planned:
            leg = ctx.legs.get(name)
            if leg is None:
                legs.append(name)  # The executor reports an unknown leg.
                continue
            ok, reason = _available(ctx, leg)
            if not ok:
                _skip(ctx, name, leg, reason or "unavailable")
                continue
            if name in ("vector", "visual") and not content:
                _prune(ctx, name, leg, "no_content_tokens")
                continue
            if name in ("lexical", "image_lexical") and not content:
                _prune(ctx, name, leg, "empty_tsquery")
                continue
            cost = p95(ctx, leg)
            if remaining is not None and cost > remaining:
                slow.append((cost, name, leg))
                continue
            legs.append(name)
        if slow and not [n for n in legs if n in ctx.legs]:
            # Never plan nothing because of the deadline: keep the fastest.
            slow.sort(key=lambda item: (item[0], item[1]))
            legs.append(slow.pop(0)[1])
        for _, name, leg in slow:
            _prune(ctx, name, leg, "p95_exceeds_deadline")
        chosen = []
        for name in legs:
            other = choose_leg(ctx, name)
            if other != name:
                state["choices"][name] = other
                state["considered"].add(other)
            if other not in chosen:
                chosen.append(other)
        if state["choices"]:
            ctx.execution.set("implementation_choices", dict(state["choices"]))
        return chosen

    # --- later rounds ------------------------------------------------------------
    def _fallback(self, ctx):
        request = ctx.request
        content = selection.has_content_tokens(request.query or "")
        state = _state(ctx)
        remaining = _remaining_ms(ctx)
        result = []
        for name, leg in sorted(ctx.legs.items()):
            if name in ctx.ran or name in state["considered"]:
                continue
            if name in BASE_LEGS and name != "structured":
                continue
            if name == "structured" and content:
                continue
            if getattr(leg, "auto", False):
                continue
            if request.raw_only and name not in default_planner.MESSAGE_LEGS:
                continue
            ok, _ = _available(ctx, leg)
            if not ok:
                continue
            if remaining is not None and p95(ctx, leg) > remaining:
                continue
            result.append(name)
        return result

    def _uncovered_terms(self, ctx, candidates):
        wanted = (ctx.request.hints or {}).get("must_terms") or []
        if not isinstance(wanted, list):
            return []
        wanted = [str(t) for t in wanted if str(t).strip()]
        if not wanted or not candidates:
            return wanted
        from .stages import dedup

        top = candidates[:SOFT_TERMS_TOP]
        found = dedup.fetch_rows(ctx, [c.record_id for c in top], content=True)
        covered = set()
        for info in found.values():
            covered |= set(info.get("terms") or ())
        return [t for t in wanted if not (selection.terms(t) <= covered)]

    def next_round(self, ctx, candidates, round_no):
        request = ctx.request
        state = _state(ctx)
        state["counts"].append(len(candidates))
        if _explicit(request):
            return None, "single_round"
        tracker = ctx.tracker
        if tracker is not None and tracker.expired():
            return None, "deadline"
        if tracker is not None:
            spent = tracker.snapshot()
            limits = spent["limits"]
            if (
                limits.get("model_calls") is not None
                and spent["spent"]["model_calls"] >= limits["model_calls"]
            ) or (
                limits.get("model_cost") is not None
                and spent["spent"]["model_cost"] >= limits["model_cost"] > 0
            ):
                return None, "budget"
        counts = state["counts"]
        if round_no >= 2 and len(counts) >= 2 and counts[-1] <= counts[-2]:
            return None, "no_new_candidates"
        try:
            uncovered = self._uncovered_terms(ctx, candidates)
        except Exception as exc:
            ctx.execution.degrade("planner", executor.failure_reason(exc))
            uncovered = []
        k = request.k
        if len(candidates) >= k and not uncovered:
            return None, "enough_candidates"
        expected = max(0.0, (k - len(candidates)) / k)
        if uncovered:
            expected = 1.0
        if expected < min_round_gain():
            return None, "low_expected_gain"
        legs = self._fallback(ctx)
        if not legs:
            return None, "no_more_legs"
        state["considered"].update(legs)
        return legs, None

    # --- profiles ---------------------------------------------------------------
    def _measurements(self, ctx):
        found = []
        for entry in ctx.execution.to_dict()["stages"]:
            if entry.get("kind") != "leg" or entry.get("status") != "ok":
                continue
            leg = ctx.legs.get(entry["name"])
            if leg is None or entry.get("ms") is None:
                continue
            try:
                found.append((leg.spec(), float(entry["ms"])))
            except Exception:
                continue
        fetched = getattr(ctx, "_context_fetch", None)
        if fetched:
            spec, ms = fetched
            found.append((spec, float(ms)))
        return found

    def reset_profile_clock(self):
        with self._lock:
            self._written.clear()
            self._samples.clear()

    def after_recall(self, ctx):
        if not runtime_profiles_enabled():
            return None
        measured = self._measurements(ctx)
        if not measured:
            return None
        now = time.monotonic()
        due = []
        with self._lock:
            for spec, ms in measured:
                samples = self._samples.setdefault(spec.name, [])
                samples.append(ms)
                del samples[:-SAMPLES]
                last = self._written.get(spec.name)
                if last is not None and now - last < PROFILE_INTERVAL_S:
                    continue
                self._written[spec.name] = now
                ordered = sorted(samples)
                due.append(
                    (
                        spec,
                        ordered[len(ordered) // 2],
                        ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
                    )
                )
                samples.clear()
        if not due:
            return None
        rows = profiles(ctx)
        with connect("app") as conn:
            for spec, p50, p95_ms in due:
                quality = None
                for row in sorted(
                    (r for r in rows if r.get("operator") == spec.name),
                    key=lambda r: r.get("measured_at") or 0,
                    reverse=True,
                ):
                    if row.get("quality") is not None:
                        quality = row["quality"]
                        break
                if quality is None:
                    found = conn.execute(
                        "SELECT quality FROM tl.operator_profiles WHERE operator=%s AND version=%s AND quality IS NOT NULL ORDER BY measured_at DESC LIMIT 1",
                        (spec.name, spec.version),
                    ).fetchone()
                    quality = found["quality"] if found else None
                recent = conn.execute(
                    "SELECT 1 FROM tl.operator_profiles WHERE operator=%s AND version=%s AND source='runtime' AND measured_at > clock_timestamp() - make_interval(secs=>%s)",
                    (spec.name, spec.version, PROFILE_INTERVAL_S),
                ).fetchone()
                if recent:
                    continue
                operators.record_profile(
                    conn,
                    spec,
                    round(p50, 3),
                    round(p95_ms, 3),
                    quality=quality,
                    source="runtime",
                )
        return None


PLANNER = AdaptivePlanner()
