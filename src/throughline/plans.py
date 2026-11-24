"""Plans: validated operation DAGs run under one budget (QRY-41, QRY-42, LIB-5).

    plan = {"nodes": [
        {"id": "hits", "op": "recall", "args": {"query": "orchard", "k": 20}},
        {"id": "ctx", "op": "expand", "args": {"mode": "context"},
         "inputs": {"input": "hits"}},
        {"id": "both", "op": "transform", "args": {"op": "union"},
         "inputs": ["hits", "ctx"]},
        {"id": "look", "op": "inspect", "args": {"view": "by_channel"},
         "inputs": ["both"]},
        {"id": "n", "op": "sql", "args": {"statement":
         "SELECT message_id FROM api.result_set({{both}}) WHERE kind = 'life_event'",
         "max_rows": 500}},
    ]}
    execute(scope, token_row, plan, budgets, dry_run, approval_id)
        -> {handles, previews, execution}

Ops: sql, transform and inspect here; recall, expand and match from
throughline.semantic_ops when installed. 'librarian' is never accepted (a
plan never escalates), and recall arguments lose hints.escalate.

validate() checks the whole plan before anything runs: unique ids, known
ops, operator arguments, inputs that name earlier nodes, no cycle, at most
THROUGHLINE_MAX_PLAN_NODES (24) nodes, and every sql statement validated with
its {{id}} placeholders replaced. execute() estimates, passes the cost gate
once (approval_required returns before any call), waits for deferred
maintenance once, then dispatches ready nodes concurrently. Before dispatch
each ready node gets remaining model cost / ready nodes, reserved from the one
tracker. A failed node is isolated: its dependants are skipped, the rest of
the plan stands (status degraded). Nothing is dispatched after the deadline.
The result is assembled in plan order.
"""

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import cost, resultsets, semantic, semantic_ops
from .budget import Budget, BudgetTracker, clamp
from .cost import ApprovalRequired
from .execution import Execution, Trace
from .scope import Scope

DEFAULT_MAX_NODES = 24
BUILTIN_OPS = ("sql", "transform", "inspect")
SEMANTIC_PLAN_OPS = ("recall", "expand", "match")
REFUSED_OPS = ("librarian",)
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]{0,31})\s*\}\}")
DEFAULT_SQL_ROWS = 1000
PREVIEW_IDS = 5


def max_nodes():
    try:
        value = int(os.getenv("THROUGHLINE_MAX_PLAN_NODES") or DEFAULT_MAX_NODES)
    except ValueError:
        value = DEFAULT_MAX_NODES
    return max(1, min(value, 200))


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    op: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, str] | list[str] = Field(default_factory=dict)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[Node] = Field(min_length=1)


class Validated:
    """A checked plan: nodes in plan order, dependencies and topological order."""

    def __init__(self, plan, deps, order, ops):
        self.plan = plan
        self.nodes = {n.id: n for n in plan.nodes}
        self.deps = deps
        self.order = order
        self.ops = ops

    def to_dict(self):
        return dict(
            nodes=[
                dict(id=n.id, op=n.op, depends_on=sorted(self.deps[n.id]))
                for n in self.plan.nodes
            ],
            order=list(self.order),
        )


def _plan(plan):
    if isinstance(plan, Plan):
        return plan
    if isinstance(plan, dict):
        try:
            return Plan.model_validate(plan)
        except Exception as exc:
            raise ValueError(f"invalid plan: {_short(exc)}") from None
    raise ValueError("a plan is an object {nodes: [...]}")


def _short(exc):
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text[:300]


def _strip_escalation(args):
    args = dict(args)
    options = args.get("options")
    if isinstance(options, dict):
        options = dict(options)
        hints = dict(options.get("hints") or {})
        hints.pop("escalate", None)
        options["hints"] = hints
        args["options"] = options
    hints = args.get("hints")
    if isinstance(hints, dict):
        hints = dict(hints)
        hints.pop("escalate", None)
        args["hints"] = hints
    return args


def _sql_statement(node, handles):
    statement = node.args.get("statement")

    def replace(match):
        name = match.group(1)
        if name not in handles:
            raise ValueError(f"node {node.id}: {{{{{name}}}}} names no earlier node")
        return "'" + handles[name] + "'"

    return PLACEHOLDER.sub(replace, statement)


def _max_rows(node):
    value = node.args.get("max_rows", DEFAULT_SQL_ROWS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
        raise ValueError(f"node {node.id}: max_rows must be an integer 1..10000")
    return value


def validate(plan):
    """Check a plan before anything runs -> Validated (ValueError if not)."""
    plan = _plan(plan)
    if len(plan.nodes) > max_nodes():
        raise ValueError(f"a plan has at most {max_nodes()} nodes")
    ids = [n.id for n in plan.nodes]
    if len(set(ids)) != len(ids):
        raise ValueError("node ids must be unique")
    available = semantic_ops.discover()
    ops = {}
    deps = {}
    for node in plan.nodes:
        if node.op in REFUSED_OPS:
            raise ValueError(f"node {node.id}: op {node.op} is not accepted in a plan")
        if node.op in BUILTIN_OPS:
            pass
        elif node.op in SEMANTIC_PLAN_OPS and node.op in available:
            ops[node.id] = available[node.op]
        else:
            raise ValueError(f"node {node.id}: unknown op {node.op}")
        needed = set()
        if isinstance(node.inputs, dict):
            needed |= set(node.inputs.values())
        else:
            needed |= set(node.inputs)
        if node.op == "sql":
            statement = node.args.get("statement")
            if not isinstance(statement, str) or not statement.strip():
                raise ValueError(f"node {node.id}: sql needs args.statement")
            if set(node.args) - {"statement", "max_rows"}:
                raise ValueError(f"node {node.id}: sql takes statement and max_rows")
            _max_rows(node)
            needed |= set(PLACEHOLDER.findall(statement))
        unknown = needed - set(ids)
        if unknown:
            raise ValueError(
                f"node {node.id}: inputs name unknown nodes: {', '.join(sorted(unknown))}"
            )
        if node.id in needed:
            raise ValueError(f"node {node.id}: a node cannot be its own input")
        deps[node.id] = needed
    # Kahn's algorithm in plan order: deterministic, and a cycle is refused.
    order, remaining = [], {n: set(d) for n, d in deps.items()}
    while remaining:
        ready = [i for i in ids if i in remaining and not remaining[i]]
        if not ready:
            raise ValueError(
                "the plan has a cycle: " + ", ".join(i for i in ids if i in remaining)
            )
        for i in ready:
            order.append(i)
            del remaining[i]
        for d in remaining.values():
            d.difference_update(ready)
    checked = Validated(plan, deps, order, ops)
    for node in plan.nodes:
        _check_node(checked, node)
    return checked


def _check_node(checked, node):
    from .query import validate_sql

    if node.op == "sql":
        statement = _sql_statement(
            node, {i: semantic.PLACEHOLDER for i in checked.nodes}
        )
        if semantic.find_calls(statement):
            raise ValueError(
                f"node {node.id}: semantic calls are not allowed in a sql node; use a plan node"
            )
        try:
            validate_sql(statement)
        except ValueError as exc:
            raise ValueError(f"node {node.id}: {exc}") from None
        return
    if node.op == "transform":
        op = node.args.get("op")
        if op not in resultsets.OPS:
            raise ValueError(
                f"node {node.id}: transform op must be " + ", ".join(resultsets.OPS)
            )
        if set(node.args) - {"op", "params"}:
            raise ValueError(f"node {node.id}: transform takes op and params")
        if not isinstance(node.args.get("params", {}), dict):
            raise ValueError(f"node {node.id}: params must be an object")
        if not isinstance(node.inputs, list) or not node.inputs:
            raise ValueError(f"node {node.id}: transform needs a list of input nodes")
        return
    if node.op == "inspect":
        view = node.args.get("view", "summary")
        if view not in resultsets.VIEWS:
            raise ValueError(
                f"node {node.id}: inspect view must be " + ", ".join(resultsets.VIEWS)
            )
        if set(node.args) - {"view", "selector", "token_budget"}:
            raise ValueError(
                f"node {node.id}: inspect takes view, selector, token_budget"
            )
        if not isinstance(node.inputs, list) or len(node.inputs) != 1:
            raise ValueError(f"node {node.id}: inspect needs exactly one input node")
        return
    op = checked.ops[node.id]
    if not isinstance(node.inputs, dict):
        raise ValueError(f"node {node.id}: inputs of {node.op} are {{param: node}}")
    for param in node.inputs:
        if param not in op.input_params:
            raise ValueError(f"node {node.id}: {param} is not an input of {node.op}")
    args = _strip_escalation(node.args) if node.op == "recall" else dict(node.args)
    for param in node.inputs:
        args.pop(param, None)
    try:
        bound = semantic_ops.bind(op, [], args)
    except ValueError as exc:
        raise ValueError(f"node {node.id}: {exc}") from None
    for param in op.input_params:
        if param in node.inputs:
            continue
        value = bound.get(param)
        if value is None and param in op.required:
            raise ValueError(f"node {node.id}: missing input {param}")
        if value is not None and not semantic_ops.HANDLE.fullmatch(str(value)):
            raise ValueError(f"node {node.id}: {param} must be a result set handle")


def _args(checked, node, handles):
    args = _strip_escalation(node.args) if node.op == "recall" else dict(node.args)
    op = checked.ops[node.id]
    for param in node.inputs:
        args.pop(param, None)
    bound = semantic_ops.bind(op, [], args)
    for param, source in node.inputs.items():
        bound[param] = handles.get(source)
    return bound


def _input_handles(checked, node, handles):
    sources = (
        node.inputs if isinstance(node.inputs, list) else list(node.inputs.values())
    )
    return [handles[s] for s in sources]


def estimate(scope, token_row, plan, budgets=None):
    """Estimates of every operator whose inputs are known before the run."""
    checked = plan if isinstance(plan, Validated) else validate(plan)
    tracker = BudgetTracker(clamp(_budget(budgets), token_row))
    estimates = []
    per_node = {}
    for node_id in checked.order:
        node = checked.nodes[node_id]
        if node_id not in checked.ops:
            per_node[node_id] = []
            continue
        op = checked.ops[node_id]
        ctx = semantic_ops.OpContext(
            scope=scope, token_row=token_row, tracker=tracker, execution=Execution()
        )
        args = _args(checked, node, {})
        if any(args.get(p) is None for p in node.inputs):
            # Inputs come from earlier nodes; the operator gates itself
            # (cost.authorize) when its input is known.
            per_node[node_id] = None
            continue
        try:
            found = op.estimates(ctx, args)
        except Exception:
            found = None
        per_node[node_id] = None if found is None else [e.to_dict() for e in found]
        estimates += found or []
    return estimates, per_node


def _budget(budgets):
    if budgets is None:
        return Budget()
    if isinstance(budgets, Budget):
        return budgets
    if hasattr(budgets, "model_dump"):
        budgets = budgets.model_dump()
    values = {k: v for k, v in dict(budgets).items() if v is not None}
    unknown = set(values) - set(Budget.__dataclass_fields__)
    if unknown:
        raise ValueError("unknown budget: " + ", ".join(sorted(unknown)))
    return Budget(**values)


def _preview(scope, handle):
    info = resultsets.info(scope, handle)
    members = resultsets.members(scope, handle)
    return dict(
        handle=handle,
        completeness=info["completeness"],
        member_count=info["member_count"],
        sample=[m["record_id"] for m in members[:PREVIEW_IDS]],
    )


def execute(
    scope,
    token_row,
    plan,
    budgets=None,
    dry_run=False,
    approval_id=None,
    trace=None,
):
    """Run a plan -> {handles, previews, execution} (see the module doc)."""
    if not isinstance(scope, Scope):
        raise TypeError("plans.execute requires a Scope")
    started = time.perf_counter()
    raw = plan.model_dump() if isinstance(plan, Plan) else plan
    checked = validate(plan)
    requested = _budget(budgets)
    tracker = BudgetTracker(clamp(requested, token_row))
    execution = Execution(tracker)
    own_trace = None
    if trace is None:
        own_trace = trace = Trace(
            scope,
            "plan",
            dict(
                nodes=[dict(id=n.id, op=n.op) for n in checked.plan.nodes],
                plan_sha256=hashlib.sha256(
                    json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
            ),
        )
    execution.trace_id = trace.id
    estimates, per_node = estimate(scope, token_row, checked, requested)
    summary = cost.summarize(estimates)
    execution.set("plan", checked.to_dict())
    execution.set("estimate", dict(summary, per_node=per_node))

    def finish(handles, previews):
        execution.add_timing("total", (time.perf_counter() - started) * 1000)
        if own_trace is not None and not own_trace.flush(execution):
            execution.trace_id = None
            execution.warn("trace was not written")
        return dict(handles=handles, previews=previews, execution=execution.to_dict())

    if dry_run:
        execution.set("dry_run", True)
        execution.stage("plan", "ok", dry_run=True, nodes=len(checked.order))
        return finish({}, {})
    digest = cost.request_hash(dict(plan=raw), scope, [], requested.to_dict())
    try:
        cost.authorize(
            token_row,
            estimates,
            scope=scope,
            tracker=tracker,
            caller_limit=requested.model_cost,
            approval_id=approval_id,
            request_hash=digest,
        )
    except ApprovalRequired as exc:
        execution.require_approval(exc.to_dict())
        execution.stage("cost_gate", "ok", approval_required=True)
        trace.event("cost_gate", None, "approval_required", detail=exc.to_dict())
        return finish({}, {})

    from .derived import materialize_for_read, read_wait_s

    remaining = tracker.remaining_ms()
    wait_s = (
        read_wait_s() if remaining is None else min(read_wait_s(), remaining / 1000)
    )
    if not materialize_for_read(wait_s):
        execution.warn("deferred thread maintenance did not finish in time")

    handles, previews, outcome = {}, {}, {}
    lock = threading.Lock()
    in_investigation = bool(getattr(scope, "investigation_id", None))

    def run_node(node, sub):
        if node.op == "sql":
            statement = _sql_statement(node, handles)
            handle, completeness, count = semantic.input_set(
                scope,
                statement,
                _max_rows(node),
                deadline_ms=sub.remaining_ms(),
                origin=dict(plan_node=node.id),
            )
            trace.event("plan.sql", statement, rows=count, detail=dict(node=node.id))
            return handle, None
        if node.op == "transform":
            params = dict(node.args.get("params") or {})
            return (
                resultsets.transform(
                    scope,
                    node.args["op"],
                    _input_handles(checked, node, handles),
                    **params,
                ),
                None,
            )
        if node.op == "inspect":
            (handle,) = _input_handles(checked, node, handles)
            return None, resultsets.inspect(
                scope,
                handle,
                node.args.get("view", "summary"),
                node.args.get("selector"),
                node.args.get("token_budget", 1000),
            )
        op = checked.ops[node.id]
        ctx = semantic_ops.OpContext(
            scope=scope,
            token_row=token_row,
            tracker=sub,
            execution=execution,
            trace=trace,
            in_investigation=in_investigation,
        )
        if per_node.get(node.id) is not None:
            # This node's estimate passed the plan's one cost gate (with the
            # plan's approval): the operator must not gate it a second time.
            ctx.extras["authorized"] = True
        handle = op.run(ctx, _args(checked, node, handles))
        if not isinstance(handle, str) or not semantic.HANDLE.fullmatch(handle):
            raise RuntimeError(f"{node.op} returned no result set")
        return handle, None

    def timed(node, sub):
        begun = time.perf_counter()
        result = run_node(node, sub)
        return result, (time.perf_counter() - begun) * 1000

    pending = list(checked.order)
    running = {}
    pool = ThreadPoolExecutor(
        max_workers=max(1, min(8, len(pending))), thread_name_prefix="plan"
    )
    try:
        while pending or running:
            blocked = [
                n
                for n in pending
                if any(
                    outcome.get(d, ("",))[0] in ("failed", "skipped")
                    for d in checked.deps[n]
                )
            ]
            for node_id in blocked:
                pending.remove(node_id)
                failed_inputs = sorted(
                    d
                    for d in checked.deps[node_id]
                    if outcome.get(d, ("",))[0] in ("failed", "skipped")
                )
                outcome[node_id] = (
                    "skipped",
                    "input failed: " + ", ".join(failed_inputs),
                )
                execution.stage(
                    "node:" + node_id,
                    "skipped",
                    op=checked.nodes[node_id].op,
                    reason=outcome[node_id][1],
                )
            ready = [
                n
                for n in pending
                if all(outcome.get(d, ("",))[0] == "ok" for d in checked.deps[n])
            ]
            if ready and tracker.expired():
                for node_id in list(pending):
                    pending.remove(node_id)
                    outcome[node_id] = ("skipped", "deadline")
                    execution.stage(
                        "node:" + node_id,
                        "skipped",
                        op=checked.nodes[node_id].op,
                        reason="deadline",
                    )
                ready = []
            if ready:
                # Reserve each ready branch's share before dispatch.
                snapshot = tracker.snapshot()
                limits = tracker.budget
                spent = snapshot["spent"]
                share_cost = None
                if limits.model_cost is not None:
                    share_cost = max(
                        0.0, limits.model_cost - spent["model_cost"]
                    ) / len(ready)
                share_calls = None
                if limits.model_calls is not None:
                    share_calls = max(
                        0, limits.model_calls - spent["model_calls"]
                    ) // len(ready)
                # Tokens are counted, not reserved, by the tracker: each ready
                # branch gets its share of what running branches do not hold.
                share_tokens = None
                left_tokens = tracker.remaining_tokens()
                if left_tokens is not None:
                    held = sum(entry[4] or 0 for entry in running.values())
                    share_tokens = max(0, left_tokens - held) // len(ready)
                for node_id in ready:
                    pending.remove(node_id)
                    tracker.reserve(share_cost or 0.0, share_calls or 0)
                    remaining = tracker.remaining_ms()
                    sub = BudgetTracker(
                        Budget(
                            deadline_ms=None if remaining is None else int(remaining),
                            tokens=share_tokens,
                            model_cost=share_cost,
                            model_calls=share_calls,
                            controller_turns=limits.controller_turns,
                        )
                    )
                    future = pool.submit(timed, checked.nodes[node_id], sub)
                    running[future] = (
                        node_id,
                        sub,
                        share_cost or 0.0,
                        share_calls or 0,
                        share_tokens,
                    )
            if not running:
                continue
            remaining = tracker.remaining_ms()
            done, _ = wait(
                list(running),
                timeout=None if remaining is None else max(0.001, remaining / 1000),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                # Deadline: whatever still runs is abandoned and reported.
                for future, (node_id, *_) in list(running.items()):
                    future.cancel()
                    outcome[node_id] = ("failed", "deadline exceeded")
                    execution.stage(
                        "node:" + node_id,
                        "timeout",
                        op=checked.nodes[node_id].op,
                        reason="deadline exceeded",
                    )
                    del running[future]
                continue
            for future in done:
                node_id, sub, share, calls, _ = running.pop(future)
                node = checked.nodes[node_id]
                spent = sub.snapshot()["spent"]
                tracker.release(share, calls)
                tracker.reserve(spent["model_cost"], spent["model_calls"])
                tracker.spend_tokens(spent["tokens"])
                try:
                    (handle, preview), ms = future.result()
                except Exception as exc:
                    reason = _short(exc)
                    outcome[node_id] = ("failed", reason)
                    execution.stage(
                        "node:" + node_id, "failed", op=node.op, reason=reason
                    )
                    trace.event(
                        "plan." + node.op, None, "failed", detail=dict(node=node_id)
                    )
                    continue
                with lock:
                    if handle is not None:
                        handles[node_id] = handle
                    if preview is not None:
                        previews[node_id] = preview
                outcome[node_id] = ("ok", None)
                execution.stage(
                    "node:" + node_id,
                    "ok",
                    op=node.op,
                    handle=handle,
                    ms=round(ms, 3),
                )
                trace.event(
                    "plan." + node.op,
                    None,
                    "ok",
                    ms=ms,
                    detail=dict(node=node_id, handle=handle),
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # Deterministic assembly in plan order.
    ordered_handles, ordered_previews = {}, {}
    for node in checked.plan.nodes:
        if node.id in handles:
            ordered_handles[node.id] = handles[node.id]
            try:
                ordered_previews[node.id] = _preview(scope, handles[node.id])
            except Exception as exc:
                ordered_previews[node.id] = dict(
                    handle=handles[node.id], error=_short(exc)
                )
        if node.id in previews:
            ordered_previews[node.id] = previews[node.id]
    execution.set(
        "nodes",
        [
            dict(
                id=n.id,
                op=n.op,
                status=outcome.get(n.id, ("skipped",))[0],
                reason=outcome.get(n.id, (None, None))[1],
                handle=handles.get(n.id),
            )
            for n in checked.plan.nodes
        ],
    )
    return finish(ordered_handles, ordered_previews)
