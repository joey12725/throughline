"""Span selection inside conversations (order 10, QRY-25, QRY-20, CST-5).

The vector leg finds conversations and names one representative message per
conversation (legacy behaviour of api.vector_records, QRY-10). This stage
replaces such a candidate by up to THROUGHLINE_SPANS_PER_CONVERSATION (3)
non-overlapping minimal spans:

1. The messages of every such conversation are loaded in one statement
   (gated api.conversations and api.messages).
2. Anchors: only messages that pass api.scope_filter with the full hard
   scope. Other messages may appear in a window only as context, and only if
   they pass the reduced filter (kinds, as_of, known_by, include_outside);
   any other message breaks the window.
3. Windows of 1..THROUGHLINE_SPAN_MAX_MESSAGES (3) consecutive messages that
   contain an anchor and share a query term are scored with
   ranking.score_cached (cached; only misses reach the rerank provider).
4. Windows scoring within THROUGHLINE_SPAN_MARGIN (0.2, relative) of the best
   are taken best first, shorter first on equal scores (minimal), never
   overlapping a span already taken.
5. Each span becomes a candidate anchored on its best in-scope message, with
   span {first_no, last_no} and the conversation set; it keeps the fused
   score and discovery of the conversation hit.

No window shares a query term with the query -> the single representative is
kept unchanged. request.expand false or hints.single_span -> the stage does
not run (legacy one row per conversation). When the rerank provider cannot
score (unavailable, approval required for a non-local provider, deadline)
the representatives are kept and the stage is reported failed with that
reason. No per-message vector is stored
or computed: windows are scored as text at query time.
"""

import copy
import os

from ...providers import local as local_text
from ..scope_sql import lit
from . import Stage

DEFAULT_MAX_MESSAGES = 3
DEFAULT_PER_CONVERSATION = 3
DEFAULT_MARGIN = 0.2
MIN_CANDIDATES = 20
ROW_CAP = 20000


def _int_env(name, default, low, high):
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        value = default
    return max(low, min(value, high))


def max_messages():
    return _int_env("THROUGHLINE_SPAN_MAX_MESSAGES", DEFAULT_MAX_MESSAGES, 1, 20)


def per_conversation():
    return _int_env(
        "THROUGHLINE_SPANS_PER_CONVERSATION", DEFAULT_PER_CONVERSATION, 1, 20
    )


def margin():
    try:
        value = float(os.getenv("THROUGHLINE_SPAN_MARGIN") or DEFAULT_MARGIN)
    except ValueError:
        value = DEFAULT_MARGIN
    return max(0.0, min(value, 1.0))


def query_terms(query):
    words = local_text.tokens(query)
    content = [w for w in words if w not in local_text.STOPWORDS]
    return set(content or words)


def _is_target(candidate):
    conversation = candidate.conversation or {}
    return (
        candidate.prefix == "m"
        and conversation.get("id") is not None
        and any(d.get("leg") == "vector" for d in candidate.discovery)
    )


def windows(messages, anchors, allowed, terms, longest):
    """Windows (start, end) over messages (in thread order) that hold only
    allowed messages, at least one anchor and at least one query term."""
    found = []
    for start in range(len(messages)):
        for end in range(start, min(len(messages), start + longest)):
            if messages[end]["id"] not in allowed:
                break
            part = messages[start : end + 1]
            if not any(m["id"] in anchors for m in part):
                continue
            if not any(m["terms"] & terms for m in part):
                continue
            found.append((start, end))
    return found


def choose(scored, per, relative_margin):
    """scored: [((start, end), score)]. Best-first non-overlapping spans
    within the margin of the best; equal scores prefer the shorter span."""
    positive = [(w, s) for w, s in scored if s is not None and s > 0]
    if not positive:
        return []
    best = max(s for _, s in positive)
    floor = best * (1.0 - relative_margin)
    ordered = sorted(
        (item for item in positive if item[1] >= floor - 1e-12),
        key=lambda item: (-item[1], item[0][1] - item[0][0], item[0][0]),
    )
    taken = []
    for (start, end), score in ordered:
        if any(not (end < a or start > b) for (a, b), _ in taken):
            continue
        taken.append(((start, end), score))
        if len(taken) >= per:
            break
    return taken


def _merge(into, other):
    for entry in other.discovery:
        if entry not in into.discovery:
            into.discovery.append(dict(entry))
    for entry in other.support:
        if entry not in into.support:
            into.support.append(dict(entry))
    if into.span is None and other.span is not None:
        into.span = dict(other.span)
    if into.conversation is None and other.conversation is not None:
        into.conversation = dict(other.conversation)
    if other.role == "hit":
        into.role, into.depends_on = "hit", None


class Spans(Stage):
    name = "spans"
    order = 10
    exact = True

    def enabled(self, ctx):
        request = ctx.request
        if request.expand is False or (request.hints or {}).get("single_span"):
            return False
        return bool((request.query or "").strip())

    def _load(self, ctx, conversation_ids):
        rows = ctx.sql(
            "SELECT c.id AS conversation_id, m.id, m.version, m.thread_id, m.thread_no, m.content"
            " FROM api.conversations c JOIN api.messages m ON m.thread_id=c.thread_id"
            " AND m.thread_no BETWEEN c.first_thread_no AND c.last_thread_no"
            " WHERE c.id = ANY(" + lit(sorted(conversation_ids)) + ")"
            " ORDER BY c.id, m.thread_no, m.id",
            cap=ROW_CAP,
        )
        by_conversation = {}
        for row in rows:
            row = dict(row)
            row["terms"] = set(local_text.tokens(row["content"] or ""))
            by_conversation.setdefault(row["conversation_id"], []).append(row)
        return by_conversation

    def _plan(self, ctx, targets):
        """{record_id of target: [(messages, (start, end), anchor row)]}."""
        terms = query_terms(ctx.request.query)
        if not terms:
            return {}
        loaded = self._load(ctx, {c.conversation["id"] for c in targets})
        every = {m["id"] for rows in loaded.values() for m in rows}
        if not every:
            return {}
        anchors = ctx.filter_hits(every)
        context = ctx.filter_context(every - anchors) if every - anchors else set()
        allowed = anchors | context
        longest = max_messages()
        jobs = []  # (target, messages, window)
        for target in targets:
            messages = loaded.get(target.conversation["id"], [])
            # Single in-scope messages sharing a term are windows too: their
            # scores pick each span's anchor.
            for window in windows(messages, anchors, allowed, terms, longest):
                jobs.append((target, messages, window))
        if not jobs:
            return {}
        items = []
        for _, messages, (start, end) in jobs:
            part = messages[start : end + 1]
            items.append(
                dict(
                    text="\n".join(m["content"] or "" for m in part),
                    message_ids=[m["id"] for m in part],
                )
            )
        from .. import ranking
        from ..executor import StageFailed

        if ctx.tracker is not None and ctx.tracker.expired():
            raise StageFailed("timeout")
        try:
            scores, _ = ranking.score_cached(
                ctx, ctx.request.query, items, operator="spans", purpose="spans"
            )
        except Exception as exc:
            # The representative rows stay (legacy one row per conversation);
            # the reason is disclosed and the recall is degraded.
            failure = ranking.stage_failure(exc)
            if failure is None:
                raise
            raise failure from None
        per_target = {}
        for (target, messages, window), score in zip(jobs, scores):
            per_target.setdefault(target.record_id, (messages, {}))[1][window] = score
        plans = {}
        per, relative = per_conversation(), margin()
        for target in targets:
            if target.record_id not in per_target:
                continue
            messages, scored = per_target[target.record_id]
            chosen = choose(list(scored.items()), per, relative)
            spans = []
            for (start, end), score in chosen:
                part = messages[start : end + 1]
                position = max(
                    (i for i in range(start, end + 1) if messages[i]["id"] in anchors),
                    key=lambda i: (
                        scored.get((i, i)) or 0.0,
                        messages[i]["id"] == target.message_id,
                        -i,
                    ),
                )
                spans.append((part, score, messages[position]))
            if spans:
                plans[target.record_id] = spans
        return plans

    def run(self, ctx, candidates):
        limit = max(MIN_CANDIDATES, 2 * ctx.request.k)
        targets = [c for c in candidates[:limit] if _is_target(c)]
        if not targets:
            return candidates
        plans = self._plan(ctx, targets)
        if not plans:
            return candidates
        out, index = [], {}

        def emit(candidate):
            existing = index.get(candidate.record_id)
            if existing is not None:
                _merge(existing, candidate)
                return
            index[candidate.record_id] = candidate
            out.append(candidate)

        for candidate in candidates:
            spans = plans.get(candidate.record_id) if _is_target(candidate) else None
            if not spans:
                emit(candidate)
                continue
            for part, score, anchor in spans:
                made = copy.deepcopy(candidate)
                made.record_id = f"m:{anchor['id']}"
                made.message_id = anchor["id"]
                made.version = anchor["version"]
                made.thread_id = anchor["thread_id"]
                made.thread_no = anchor["thread_no"]
                made.span = dict(
                    first_no=part[0]["thread_no"], last_no=part[-1]["thread_no"]
                )
                made.meta = dict(made.meta)
                made.meta["span"] = dict(
                    score=score, message_ids=[m["id"] for m in part]
                )
                emit(made)
        return out


STAGE = Spans()
