"""Shared model calls: lenses that read the same item with the same model ask
their questions in one call, so the item's text is sent (and paid for) once.

Two lenses are *siblings* when they use the same worker (llm or jev), the same
endpoint and model, and the same channels and grain. Whether they really read
the same input is decided per item: the item part of the request (the rendered
messages and date calendar for an LLM lens, the candidate and item context for
a Jev lens) must be identical (workers.share_fingerprint).

During a scheduler tick (scheduler.tick opens a Share for each dependency
layer):

1. When lens A is about to call its model for an item, the Share looks at A's
   siblings. A sibling B joins the call when that same item is one B's own next
   run would read (same window, same fingerprint).
2. The call is made once (workers.run_shared); A gets its rows straight away and
   B's answer is kept for B.
3. When B runs, an item with a kept answer costs nothing.

Schedules: a sibling that is not due this tick may *ride along* (the default):
it joins A's call, then runs right after A on the answers it was given, and
stops at the first item that has none (it reads that at its own scheduled run).
A rider joins only items whose earlier items it also has answers for, so every
answer it is given is used. worker_config.coalesce:

  true (default)  share calls and ride along
  "due"           share calls only when this lens is due anyway
  false           never share

Nothing changes for runs outside a tick (run_lens by hand, backfills, rule
runs): they call alone.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .workers import (
    JEV_ENDPOINT,
    ShareTooLarge,
    WorkerResult,
    _resolve_llm_endpoint,
    execute_worker,
    run_shared,
    share_fingerprint,
)

log = logging.getLogger(__name__)

_local = threading.local()


def mode_of(tdef: dict[str, Any]) -> str:
    """'ride', 'due' or 'off' for a lens's worker_config.coalesce."""
    value = (tdef.get("worker_config") or {}).get("coalesce", True)
    if value is False or value in ("false", "off", "never"):
        return "off"
    if value == "due":
        return "due"
    return "ride"


def group_key(tdef: dict[str, Any]) -> tuple | None:
    """Lenses with the same key may share calls (None: never shares)."""
    worker = tdef.get("worker")
    cfg = tdef.get("worker_config") or {}
    if worker not in ("llm", "jev") or cfg.get("provider") == "local" or mode_of(tdef) == "off":
        return None
    if worker == "llm":
        base, model, _, _ = _resolve_llm_endpoint(cfg)
    else:
        base = str(cfg.get("endpoint") or JEV_ENDPOINT)
        model = str(cfg.get("model") or "")
    return (worker, base, model, tuple(sorted(tdef.get("channels") or [])), tdef.get("grain"))


@dataclass
class Member:
    tdef: dict[str, Any]
    due: bool  # runs this tick anyway
    ride: bool  # not due, but may join calls and run on their answers
    plan: list[dict[str, Any]] | None = None
    ctx: list[dict[str, Any]] | None = None
    finished: bool = False
    answers: dict[tuple[str, str], WorkerResult] = field(default_factory=dict)
    order: dict[str, int] = field(default_factory=dict)


class Share:
    def __init__(self, members: dict[str, Member]):
        self.members = members
        self.keys = {n: group_key(m.tdef) for n, m in members.items()}
        self.stats = {"shared_calls": 0, "answers_given": 0, "answers_used": 0, "saved_tokens": 0}

    # -- membership -------------------------------------------------------------
    def has(self, name: str) -> bool:
        return name in self.members and self.keys.get(name) is not None

    def riding(self, name: str) -> bool:
        m = self.members.get(name)
        return bool(m and m.ride and not m.due)

    def pending_answers(self, name: str) -> int:
        m = self.members.get(name)
        return len(m.answers) if m else 0

    def finished(self, name: str) -> None:
        m = self.members.get(name)
        if m is not None:
            m.finished = True
            if m.answers:
                log.info("coalesce: %d shared answers for %s were not used", len(m.answers), name)
            m.answers.clear()

    # -- planning ---------------------------------------------------------------
    def _plan(self, conn, m: Member) -> list[dict[str, Any]]:
        if m.plan is None:
            from .runner import _filter_egress_held, _plan_items, get_lens

            try:
                tdef = get_lens(m.tdef["name"], conn=conn)
                cur = tdef.get("cursor") or {"last_message_id": 0, "done_ids": [], "processed_windows": {}}
                items, _ = _plan_items(conn, tdef, cur, max_items=int(tdef["max_items_per_run"]))
                m.plan = [_filter_egress_held(conn, tdef, it) for it in items]
                m.order = {it["window_key"]: i for i, it in enumerate(m.plan)}
            except Exception as exc:  # a sibling we cannot plan simply calls alone
                log.warning("coalesce: cannot plan %s: %s", m.tdef["name"], exc)
                m.plan, m.order = [], {}
        return m.plan

    def _ctx(self, m: Member) -> list[dict[str, Any]]:
        if m.ctx is None:
            from .runner import _run_context_query

            m.ctx = _run_context_query(m.tdef)
        return m.ctx

    def _joins(self, conn, name: str, item: dict[str, Any], fp: str) -> list[tuple]:
        """Siblings that take part in the call for this item: (tdef, item, ctx, corrections)."""
        from .runner import _load_owner_corrections

        key = self.keys.get(name)
        out = []
        for other, m in self.members.items():
            if other == name or m.finished or self.keys.get(other) != key:
                continue
            if not (m.due or m.ride):
                continue
            plan = self._plan(conn, m)
            idx = m.order.get(item["window_key"])
            if idx is None or not plan[idx]["messages"]:
                continue
            if (item["window_key"], fp) in m.answers:
                continue
            if not m.due:
                # A rider stops at its first item without an answer, so it
                # only joins when every earlier item of its run is answered.
                earlier = {wk for wk, _ in m.answers}
                if any(p["window_key"] not in earlier and p["messages"] for p in plan[:idx]):
                    continue
            try:
                if share_fingerprint(conn, m.tdef, plan[idx]) != fp:
                    continue
            except Exception as exc:
                log.warning("coalesce: fingerprint for %s failed: %s", other, exc)
                continue
            out.append((m.tdef, plan[idx], self._ctx(m), _load_owner_corrections(conn, int(m.tdef["id"]))))
        return out

    # -- the call ---------------------------------------------------------------
    def work(self, conn, tdef, item, ctx_rows, corrections, *, sql_runner) -> WorkerResult | None:
        """The WorkerResult for this lens and item: an answer kept from a
        shared call, a new shared call, or a call alone. None when the lens
        is riding along and has no answer for this item (stop the run)."""
        name = tdef["name"]
        m = self.members.get(name)
        if m is None or self.keys.get(name) is None:
            return execute_worker(conn, tdef, item, ctx_rows, corrections, sql_runner=sql_runner)
        try:
            fp = share_fingerprint(conn, tdef, item)
        except Exception as exc:
            log.warning("coalesce: fingerprint for %s failed: %s", name, exc)
            fp = None
        if fp is not None:
            kept = m.answers.pop((item["window_key"], fp), None)
            if kept is not None:
                self.stats["answers_used"] += 1
                return kept
        if self.riding(name):
            return None
        joins = self._joins(conn, name, item, fp) if fp is not None else []
        if not joins:
            return execute_worker(conn, tdef, item, ctx_rows, corrections, sql_runner=sql_runner)
        try:
            out = run_shared(conn, [(tdef, item, ctx_rows, corrections), *joins])
        except ShareTooLarge:
            return execute_worker(conn, tdef, item, ctx_rows, corrections, sql_runner=sql_runner)
        self.stats["shared_calls"] += 1
        for jdef, jitem, _, _ in joins:
            sib = self.members[jdef["name"]]
            sib.answers[(jitem["window_key"], fp)] = out[jdef["name"]]
            self.stats["answers_given"] += 1
        self.stats["saved_tokens"] += sum(r.saved_tokens for r in out.values())
        return out[name]


def current(name: str) -> Share | None:
    share = getattr(_local, "share", None)
    if share is not None and share.has(name):
        return share
    return None


@contextmanager
def sharing(members: dict[str, Member]):
    """Make calls of these lenses shareable on this thread until exit."""
    previous = getattr(_local, "share", None)
    share = Share(members)
    _local.share = share
    try:
        yield share
    finally:
        _local.share = previous
