"""Select stage (order 40, QRY-24, QRY-27, QRY-28, CST-8, scenario
newest_relevant).

Enabled when the request gives budgets.tokens, gives expand=true
explicitly, sets hints.select, or asks hints.order='newest'; never for an
exhaustive request (an enumeration is not a selection). Otherwise recall's
default selection (top k, then the token budget) applies unchanged.

Selection (retrieval.selection.select) is greedy by marginal gain within the
remaining token budget (tokens are counted exactly as recall counts returned
records) and k hits. Context dependencies come from heuristic triggers only;
neighbours are fetched through the logical operation 'context.fetch', which
has two exact implementations registered here ('context_many': one
api.context_many statement; 'context_loop': api.context per anchor), chosen
with operators.choose from tl.operator_profiles. A dependency row is added
only if it passes the reduced filter (kinds, as_of, known_by,
include_outside through api.scope_filter with the other arguments NULL); it
gets role 'dependency' and depends_on, never role 'hit'. expand=false: no
dependencies; hints.diversity=false: no diversity penalties;
hints.selection_weights overrides weights.

Dropped candidates go to ctx.dropped (budget, redundant, duplicate_group,
rank); a budget drop sets execution truncation 'token_budget' and
execution.selection carries the counts. hints.order='newest' orders the
selected hits newest first among the query-relevant ones (execution.order =
'newest_relevant').
"""

import time

from ... import operators
from .. import selection
from ..scope_sql import lit
from ..types import Candidate
from . import Stage
from .dedup import fetch_rows, load_facts

POOL_CAP = 500
CONTEXT_BEFORE = 1
CONTEXT_AFTER = 0
CONTEXT_OPERATION = "context.fetch"
LINK_CAP = 5000

CONTEXT_MANY = operators.OperatorSpec(
    name="context.fetch.context_many",
    implements=CONTEXT_OPERATION,
    implementation="context_many",
    kind="stage",
    inputs=("message_ids",),
    outputs=("context_rows",),
    exact=True,
)
CONTEXT_LOOP = operators.OperatorSpec(
    name="context.fetch.context_loop",
    implements=CONTEXT_OPERATION,
    implementation="context_loop",
    kind="stage",
    inputs=("message_ids",),
    outputs=("context_rows",),
    exact=True,
)
# Registration order makes context_many the reference implementation.
for _spec in (CONTEXT_MANY, CONTEXT_LOOP):
    operators.register(_spec, replace=True)


def _context_many(ctx, anchors, before, after):
    rows = ctx.sql(
        "SELECT c.anchor_id, c.message_id, c.thread_id, c.thread_no FROM api.context_many("
        + lit(sorted(anchors))
        + ","
        + lit(before)
        + ","
        + lit(after)
        + ") c",
        cap=len(anchors) * (before + after + 1) + 1,
    )
    return [dict(r) for r in rows]


def _context_loop(ctx, anchors, before, after):
    rows = []
    for anchor in sorted(anchors):
        for row in ctx.sql(
            "SELECT "
            + lit(int(anchor))
            + "::bigint AS anchor_id, c.id AS message_id, c.thread_id, c.thread_no FROM api.context("
            + lit(int(anchor))
            + ","
            + lit(before)
            + ","
            + lit(after)
            + ") c",
            cap=before + after + 2,
        ):
            rows.append(dict(row))
    return rows


IMPLEMENTATIONS = {
    CONTEXT_MANY.name: _context_many,
    CONTEXT_LOOP.name: _context_loop,
}


def _profiles(ctx):
    from .. import planner

    return planner.profiles(ctx)


def choose_context(ctx):
    allow = bool((ctx.request.hints or {}).get("allow_approximate"))
    spec = operators.choose(CONTEXT_OPERATION, _profiles(ctx), allow_approximate=allow)
    if spec is None or spec.name not in IMPLEMENTATIONS:
        spec = CONTEXT_MANY
    return spec


def fetch_context(ctx, anchors, before=CONTEXT_BEFORE, after=CONTEXT_AFTER):
    """Neighbour rows {anchor_id, message_id, thread_id, thread_no} through
    the chosen 'context.fetch' implementation, sorted (anchor, thread_no)."""
    if not anchors:
        return []
    spec = choose_context(ctx)
    started = time.perf_counter()
    rows = IMPLEMENTATIONS[spec.name](ctx, anchors, before, after)
    ms = (time.perf_counter() - started) * 1000
    ctx._context_fetch = (spec, ms)
    ctx.execution.add_timing("context.fetch", ms)
    ctx.execution.set("context_fetch", spec.implementation)
    rows.sort(key=lambda r: (r["anchor_id"], r["thread_no"] or 0, r["message_id"]))
    return rows


def reply_links(ctx, message_ids):
    if not message_ids:
        return []
    source, extra = ctx.links_source()
    types = lit(list(selection.REPLY_LINK_TYPES))
    return [
        dict(r)
        for r in ctx.sql(
            "SELECT l.id, l.type, l.from_id, l.to_id FROM "
            + source
            + " l WHERE l.status='active' AND l.type = ANY("
            + types
            + ") AND l.from_kind='message' AND l.to_kind='message' AND l.from_id = ANY("
            + lit(sorted(message_ids))
            + ")"
            + extra
            + " ORDER BY l.id",
            cap=LINK_CAP,
        )
    ]


def _hints(ctx):
    return ctx.request.hints or {}


def dependency_candidates(ctx, pool):
    """{record_id: [dependency Candidate]} for the pool (QRY-27)."""
    anchors, triggered = [], []
    for c in pool:
        found = selection.dependency_triggers(c)
        mid = selection.message_ref(c)
        if found:
            triggered.append(c)
        if mid is not None and ("short" in found or "anaphora" in found):
            anchors.append(mid)
    messages = [m for m in (selection.message_ref(c) for c in pool) if m is not None]
    links = reply_links(ctx, messages)
    if links:
        linked = {link["from_id"] for link in links}
        triggered += [
            c for c in pool if selection.message_ref(c) in linked and c not in triggered
        ]
    if not triggered:
        return {}
    neighbours = fetch_context(ctx, sorted(set(anchors)))
    by_anchor = {}
    for row in neighbours:
        by_anchor.setdefault(row["anchor_id"], []).append(row)
    wanted = {}
    for c in triggered:
        mid = selection.message_ref(c)
        found = selection.dependencies(c, by_anchor.get(mid, []), links)
        if found:
            wanted[c.record_id] = found
    ids = sorted({d["message_id"] for items in wanted.values() for d in items})
    if not ids:
        return {}
    # Dependency rows satisfy the reduced scope only (kinds, as_of, known_by,
    # include_outside); they are never hits.
    allowed = ctx.filter_context(ids)
    rows = fetch_rows(ctx, [f"m:{i}" for i in sorted(allowed)], content=True)
    result = {}
    for record_id, items in wanted.items():
        deps = []
        for item in items:
            info = rows.get(f"m:{item['message_id']}")
            if item["message_id"] not in allowed or info is None:
                continue
            dep = Candidate(
                record_id=f"m:{item['message_id']}",
                message_id=item["message_id"],
                role="dependency",
                depends_on=record_id,
                discovery=[
                    dict(
                        leg="select",
                        rank=None,
                        score=None,
                        detail=dict(dependency=item["reason"], of=record_id),
                    )
                ],
                meta=dict(facts=info),
            )
            dep.kind = info.get("kind")
            dep.thread_id, dep.thread_no = info.get("thread_id"), info.get("thread_no")
            dep.tokens = info.get("tokens")
            deps.append(dep)
        if deps:
            result[record_id] = deps
    return result


class Select(Stage):
    name = "select"
    order = 40
    exact = True

    def enabled(self, ctx):
        request = ctx.request
        if request.exhaustive:
            return False
        hints = request.hints or {}
        return bool(
            request.budgets.tokens is not None
            or ("expand" in request.model_fields_set and request.expand is not False)
            or hints.get("select")
            or hints.get("order") == "newest"
        )

    def run(self, ctx, candidates):
        request = ctx.request
        hints = _hints(ctx)
        pool, rest = candidates[:POOL_CAP], candidates[POOL_CAP:]
        load_facts(ctx, pool, content=True)
        for c in pool:
            c.role, c.depends_on = "hit", None
        deps = {}
        if request.expand is not False:
            deps = dependency_candidates(ctx, pool)
        budget = ctx.tracker.remaining_tokens() if ctx.tracker is not None else None
        weights = hints.get("selection_weights")
        if not isinstance(weights, dict):
            weights = None
        else:
            weights = {
                key: float(value)
                for key, value in weights.items()
                if key in selection.DEFAULT_WEIGHTS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            }
        selected, dropped = selection.select(
            pool,
            budget,
            request.k,
            weights,
            query=request.query or "",
            deps=deps,
            diversity=hints.get("diversity") is not False,
        )
        dropped += [dict(record_id=c.record_id, reason="rank") for c in rest]
        ctx.dropped.extend(dropped)
        reasons = {}
        for entry in dropped:
            reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
        spent = sum(selection._tokens(c) for c in selected)
        if reasons.get("budget"):
            ctx.execution.set_truncation("token_budget", ctx.tracker.budget.tokens)
        ctx.execution.set(
            "selection",
            dict(
                considered=len(pool),
                selected=sum(1 for c in selected if c.role == "hit"),
                dependencies=sum(1 for c in selected if c.role == "dependency"),
                dropped=reasons,
                budget_tokens=budget,
                spent_tokens=spent,
            ),
        )
        if hints.get("order") == "newest":
            selected = selection.newest_relevant(selected, request.query or "")
            ctx.execution.set("order", "newest_relevant")
        return selected


STAGE = Select()
