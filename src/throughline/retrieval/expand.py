"""expand(): grow a result set by a mechanical relation (QRY-32, QRY-27).

    handle = expand(scope, handle="rs_...", mode="context", before=2, after=2)

Modes: context (span-aware thread neighbours), thread (whole threads,
capped), sources (provenance sources of generated members and their support;
a raw message is its own source), links (api.follow_many: message ends as
messages, entity ends as e: records, traversed links as l: records), time
(api.time_overlap_many), place (api.near_many) and focus (approximate: the
union of context, links and sources ranked against the focus text by
retrieval.recall restricted to those messages).

Rules:
- Expansion is set-based: every mode runs a fixed number of statements, one
  per relation over all anchors at once, whatever the number of anchors.
- Every added message id passes api.scope_filter with kinds, as_of, known_by
  and include_outside, plus the caller's ``within`` scope when one is given.
  The input set's as_of/known_by are inherited unless the caller names its
  own. Statements read gated api views only, under the caller's scope, so a
  hidden row is never reached; added e:/l: records are checked through their
  gated record views as well.
- Added members have role 'context' (or 'dependency'), depends_on = the
  anchor's record id and discovery [{leg: 'expand:<mode>', detail: {anchor}}].
  expand never adds a 'hit'.
- Completeness: exact modes are exhaustive unless capped (truncated); focus is
  ranked_sample; an input that is not exhaustive caps the output at its own
  completeness.
"""

import os
import time

from .. import resultsets
from ..execution import Execution
from ..query import reader_transaction
from ..scope import Scope
from . import scope_sql
from .scope_sql import DEFAULT_KINDS, lit
from .types import HardScope

MODES = ("context", "thread", "sources", "links", "time", "place", "focus")
EXACT_MODES = MODES[:-1]
ROLES = ("context", "dependency")
DEFAULT_MAX_INPUT = 2000
DEFAULT_MAX_OUTPUT = 5000
DEFAULT_THREAD_CAP = 1000
# Upper bound of candidate rows one relation statement may return.
CANDIDATE_CAP = 100000
MAX_WINDOW = 50
MAX_DEPTH = 4
# Derived kind of each record prefix, as api.sources and tl.provenance name it.
DERIVED_KIND = dict(m="message", p="passage", l="link", e="entity")
# Non-message record kinds api.follow_many may reach.
FOLLOW_PREFIX = dict(entity="e", link="l")


class ExpandUnavailable(ValueError):
    """The request cannot be expanded as asked (a 400)."""


def _bound(name, default):
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        value = default
    return max(1, value)


def max_input():
    return _bound("THROUGHLINE_EXPAND_MAX_INPUT", DEFAULT_MAX_INPUT)


def max_output():
    return _bound("THROUGHLINE_EXPAND_MAX_OUTPUT", DEFAULT_MAX_OUTPUT)


def thread_cap():
    return _bound("THROUGHLINE_EXPAND_THREAD_CAP", DEFAULT_THREAD_CAP)


def _rows(scope, statement, params=None):
    """One statement on the reader path under the scope."""
    with reader_transaction(scope) as conn:
        return conn.execute(statement, params).fetchall()


def _worst(*values):
    rank = {"exhaustive": 0, "truncated": 1, "ranked_sample": 2}
    return max(values, key=lambda v: rank.get(v, 2))


def _record_id(item):
    if isinstance(item, int) and not isinstance(item, bool):
        return f"m:{item}"
    text = str(item).strip()
    if text.isdigit():
        return f"m:{int(text)}"
    if not scope_sql.RECORD_ID.fullmatch(text):
        raise ExpandUnavailable(f"invalid record id: {text[:40]}")
    return text


def _input(scope, handle, ids, as_of, known_by):
    """(records, info) of the input: a result set, or explicit record ids."""
    if (handle is None) == (ids is None):
        raise ExpandUnavailable("give a result set handle or ids, not both")
    if handle is not None:
        # The bound is checked before any member is read.
        count = resultsets.info(scope, handle)["member_count"]
        if count > max_input():
            raise ExpandUnavailable(
                f"expand takes at most {max_input()} input members (THROUGHLINE_EXPAND_MAX_INPUT)"
            )
        result = resultsets.read(scope, handle)
        return result["records"], result["info"]
    record_ids = list(dict.fromkeys(_record_id(i) for i in ids))
    if len(record_ids) > max_input():
        raise ExpandUnavailable(
            f"expand takes at most {max_input()} input members (THROUGHLINE_EXPAND_MAX_INPUT)"
        )
    from .hydrate import records_for_members

    records = records_for_members(
        scope, [dict(record_id=r) for r in record_ids], as_of=as_of, known_by=known_by
    )
    info = dict(
        handle=None,
        session_id=None,
        completeness="exhaustive",
        as_of=None,
        known_by=None,
        member_count=len(records),
    )
    return records, info


# --- the reduced (or caller's) scope ------------------------------------------


class _Filter:
    """api.scope_filter arguments for added message ids: kinds, as_of,
    known_by and include_outside always; the caller's whole scope when
    ``within`` is given."""

    def __init__(self, scope, within, as_of, known_by, input_kinds):
        self.scope = scope
        self.within = within
        self.unresolvable = None
        base = within if within is not None else HardScope()
        update = {}
        if as_of is not None:
            update["as_of"] = as_of
        if known_by is not None:
            update["known_by"] = known_by
        hard = base.model_copy(update=update) if update else base
        self.hard = hard
        resolved = scope_sql.Resolved(scope=hard, kinds=scope_sql.message_kinds(hard))
        if within is None:
            # Context of a reference message is reference text too: the kinds
            # of the input's own messages are admitted beside the default.
            kinds = list(DEFAULT_KINDS)
            for kind in sorted(input_kinds):
                if kind not in kinds and kind != "page_revision":
                    kinds.append(kind)
            resolved.kinds = kinds
        if hard.near is not None:
            resolved.lat, resolved.lon, resolved.meters = (
                hard.near.lat,
                hard.near.lon,
                hard.near.meters,
            )
        if hard.place is not None:
            points = {
                (float(r["lat"]), float(r["lon"]))
                for r in _rows(
                    scope,
                    "SELECT (p.metadata->>'lat')::float8 AS lat,(p.metadata->>'lon')::float8 AS lon FROM api.places p"
                    " WHERE p.name=%s AND jsonb_typeof(p.metadata->'lat')='number' AND jsonb_typeof(p.metadata->'lon')='number'",
                    (hard.place.name,),
                )
            }
            if len(points) != 1:
                self.unresolvable = "place"
            else:
                (resolved.lat, resolved.lon), resolved.meters = (
                    points.pop(),
                    hard.place.meters,
                )
        self.resolved = resolved
        self.allowed = set(hard.message_ids) if hard.message_ids is not None else None
        self.args = scope_sql.args(resolved, reduced=within is None)

    @property
    def cutoff(self):
        return self.resolved.cutoff

    def admits(self, kind):
        return self.within is None or scope_sql.admits(self.hard, kind)

    def messages(self, ids, versions=False):
        """{candidate id: eligible message id} for the candidates that pass.
        versions=True maps a candidate to the eligible version of its record
        (references such as provenance sources and link ends name one
        version); otherwise only the candidate version itself may pass."""
        ids = sorted({int(i) for i in ids})
        if not ids or self.unresolvable:
            return {}
        if versions:
            rows = _rows(
                self.scope,
                "WITH c AS (SELECT DISTINCT c.id FROM unnest(%s::bigint[]) c(id)),"
                " v AS MATERIALIZED (SELECT c.id AS cand,x.id FROM c JOIN api.message_versions o ON o.id=c.id"
                " JOIN api.message_versions x ON x.channel_id=o.channel_id AND x.external_id=o.external_id)"
                " SELECT DISTINCT ON (v.cand) v.cand,f.message_id FROM v JOIN api.scope_filter(ARRAY(SELECT DISTINCT v.id FROM v),"
                + self.args
                + ") f ON f.message_id=v.id ORDER BY v.cand,f.message_id",
                (ids,),
            )
            found = {r["cand"]: r["message_id"] for r in rows}
        else:
            rows = _rows(
                self.scope,
                "SELECT f.message_id FROM api.scope_filter(%s::bigint[],"
                + self.args
                + ") f",
                (ids,),
            )
            found = {r["message_id"]: r["message_id"] for r in rows}
        if self.allowed is not None:
            found = {k: v for k, v in found.items() if v in self.allowed}
        return found


# --- relations --------------------------------------------------------------


def _message_anchors(records):
    return [r for r in records if r["record_id"].startswith("m:")]


def _context(scope, records, before, after, whole_thread):
    """[(anchor record id, candidate message id)] in anchor order."""
    anchors = [r for r in _message_anchors(records) if r.get("thread_id") is not None]
    if not anchors:
        return [], False
    names, threads, lows, highs, mids = [], [], [], [], []
    for r in anchors:
        span = r.get("span") or {}
        first = span.get("first_no", r.get("thread_no"))
        last = span.get("last_no", r.get("thread_no"))
        if not whole_thread and (first is None or last is None):
            continue
        names.append(r["record_id"])
        threads.append(int(r["thread_id"]))
        lows.append(None if whole_thread else int(first) - before)
        highs.append(None if whole_thread else int(last) + after)
        mids.append(int(r["message_id"]))
    if not names:
        return [], False
    rows = _rows(
        scope,
        "SELECT a.anchor,m.id FROM unnest(%s::text[],%s::bigint[],%s::int[],%s::int[],%s::bigint[]) WITH ORDINALITY a(anchor,thread_id,lo,hi,mid,ord)"
        " JOIN api.message_versions m ON m.thread_id=a.thread_id"
        " AND (a.lo IS NULL OR m.thread_no>=a.lo) AND (a.hi IS NULL OR m.thread_no<=a.hi)"
        " WHERE NOT EXISTS(SELECT 1 FROM api.message_versions o WHERE o.id=a.mid AND o.channel_id=m.channel_id AND o.external_id=m.external_id)"
        " ORDER BY a.ord,m.thread_no,m.id LIMIT %s",
        (names, threads, lows, highs, mids, CANDIDATE_CAP + 1),
    )
    capped = len(rows) > CANDIDATE_CAP
    return [(r["anchor"], r["id"]) for r in rows[:CANDIDATE_CAP]], capped


def _pairs(scope, statement, anchors):
    """Run one many-anchor function over the message anchors."""
    by_message = {}
    for r in anchors:
        by_message.setdefault(int(r["message_id"]), r["record_id"])
    if not by_message:
        return [], False
    rows = _rows(
        scope, statement + " LIMIT %s", (sorted(by_message), CANDIDATE_CAP + 1)
    )
    capped = len(rows) > CANDIDATE_CAP
    order = {
        rid: n for n, rid in enumerate(dict.fromkeys(r["record_id"] for r in anchors))
    }
    pairs = [
        (by_message[r["anchor_id"]], r["message_id"]) for r in rows[:CANDIDATE_CAP]
    ]
    pairs.sort(key=lambda p: order.get(p[0], 0))
    return pairs, capped


def _sources(scope, records):
    """(message pairs, capped): sources of generated members and support."""
    pairs = []
    lateral = []
    for r in records:
        prefix, ref = r["record_id"].split(":", 1)
        if prefix == "m" and not r.get("generated"):
            # A raw message is its own source.
            pairs.append((r["record_id"], int(r["message_id"])))
            continue
        kind = DERIVED_KIND.get(prefix)
        if kind is not None:
            lateral.append((r["record_id"], kind, int(ref)))
        for item in r.get("support") or []:
            if isinstance(item, dict) and item.get("message_id") is not None:
                if item.get("relation") == "self":
                    continue
                pairs.append((r["record_id"], int(item["message_id"])))
    if lateral:
        rows = _rows(
            scope,
            "SELECT a.anchor,s.id FROM unnest(%s::text[],%s::text[],%s::bigint[]) WITH ORDINALITY a(anchor,kind,ref,ord)"
            " CROSS JOIN LATERAL api.sources(a.kind,a.ref) s ORDER BY a.ord,s.id LIMIT %s",
            (
                [x[0] for x in lateral],
                [x[1] for x in lateral],
                [x[2] for x in lateral],
                CANDIDATE_CAP + 1,
            ),
        )
        pairs.extend((r["anchor"], r["id"]) for r in rows[:CANDIDATE_CAP])
        if len(rows) > CANDIDATE_CAP:
            return pairs, True
    order = {rid: n for n, rid in enumerate(r["record_id"] for r in records)}
    pairs.sort(key=lambda p: order.get(p[0], 0))
    return pairs, False


def _links(scope, records, link_type, depth, at):
    """(message pairs, other pairs, capped) from api.follow_many."""
    starts = {"message": [], "entity": [], "link": []}
    for r in records:
        prefix, ref = r["record_id"].split(":", 1)
        if prefix == "m":
            starts["message"].append((r["record_id"], int(r["message_id"])))
        elif prefix == "e":
            starts["entity"].append((r["record_id"], int(ref)))
        elif prefix == "l":
            starts["link"].append((r["record_id"], int(ref)))
    messages, others, capped = [], [], False
    order = {rid: n for n, rid in enumerate(r["record_id"] for r in records)}
    for kind, items in starts.items():
        if not items:
            continue
        anchor_of = {}
        for record_id, ref in items:
            anchor_of.setdefault(ref, record_id)
        rows = _rows(
            scope,
            "SELECT * FROM api.follow_many(%s,%s::bigint[],%s,%s,%s::timestamptz) LIMIT %s",
            (kind, sorted(anchor_of), link_type, depth, at, CANDIDATE_CAP + 1),
        )
        capped = capped or len(rows) > CANDIDATE_CAP
        for row in rows[:CANDIDATE_CAP]:
            anchor = anchor_of.get(row["anchor_id"])
            if anchor is None:
                continue
            if row["kind"] == "message":
                messages.append((anchor, row["id"], row["depth"]))
            elif row["kind"] in FOLLOW_PREFIX:
                others.append(
                    (anchor, f"{FOLLOW_PREFIX[row['kind']]}:{row['id']}", row["depth"])
                )
    messages.sort(key=lambda p: (order.get(p[0], 0), p[2]))
    others.sort(key=lambda p: (order.get(p[0], 0), p[2]))
    return (
        [(a, m) for a, m, _ in messages],
        [(a, o) for a, o, _ in others],
        capped,
    )


def _visible_records(scope, record_ids):
    """The non-message record ids the scope can see, through their views."""
    if not record_ids:
        return set()
    return set(resultsets.resolve(scope, sorted(set(record_ids))))


# --- assembly ---------------------------------------------------------------


def _member(record):
    """A member dict for resultsets.create from a Record (input members)."""
    return dict(
        record_id=record["record_id"],
        version=record.get("version"),
        message_id=record.get("message_id"),
        span=record.get("span"),
        score=record.get("score"),
        role=record.get("role") or "hit",
        depends_on=record.get("depends_on"),
        groups=record.get("groups") or [],
        discovery=record.get("discovery") or [],
        meta=record.get("meta") or {},
    )


def _added(record_id, anchor, mode, role, score=None, extra=None):
    detail = dict(anchor=anchor, **(extra or {}))
    return dict(
        record_id=record_id,
        role=role,
        depends_on=anchor,
        score=score,
        discovery=[dict(leg="expand:" + mode, detail=detail)],
    )


def _focus(scope, records, flt, focus, k, before, after, link_type, depth):
    """Ranked members for mode focus: [(record id, anchor, score)]."""
    from .recall import recall
    from .types import RecallRequest

    pairs, _ = _context(scope, records, before, after, False)
    message_pairs, other_pairs, _ = _links(scope, records, link_type, depth, flt.cutoff)
    source_pairs, _ = _sources(scope, records)
    anchor_of = {}
    passed = flt.messages([m for _, m in pairs])
    for anchor, mid in pairs:
        if mid in passed:
            anchor_of.setdefault(f"m:{passed[mid]}", anchor)
    referenced = flt.messages(
        [m for _, m in message_pairs] + [m for _, m in source_pairs], versions=True
    )
    for anchor, mid in message_pairs + source_pairs:
        if mid in referenced:
            anchor_of.setdefault(f"m:{referenced[mid]}", anchor)
    visible = _visible_records(scope, [o for _, o in other_pairs])
    for anchor, other in other_pairs:
        if other in visible and flt.admits("entity" if other[0] == "e" else "link"):
            anchor_of.setdefault(other, anchor)
    ids = sorted(int(r.split(":", 1)[1]) for r in anchor_of if r.startswith("m:"))
    if not ids:
        return [], len(anchor_of)
    hard = HardScope(
        message_ids=ids,
        as_of=flt.hard.as_of,
        known_by=flt.hard.known_by,
        include_outside=flt.hard.include_outside,
        kinds=flt.resolved.kinds,
    )
    response = recall(
        scope,
        RecallRequest(query=focus, k=k, scope=hard, expand=False, strategy="keyword"),
    )
    ranked = []
    for record in response.records:
        anchor = anchor_of.get(record["record_id"])
        if anchor is not None and record.get("role") == "hit":
            ranked.append((record["record_id"], anchor, record.get("score")))
    return ranked[:k], len(anchor_of)


def expand(
    scope,
    *,
    handle=None,
    ids=None,
    mode,
    before=3,
    after=3,
    link_type=None,
    depth=1,
    as_of=None,
    known_by=None,
    window_s=0,
    meters=100,
    focus=None,
    k=20,
    within: HardScope | None = None,
    token_budget=None,
    tracker=None,
    execution=None,
    keep_input=True,
    role="context",
    session=None,
):
    """Expand a result set (or explicit record ids) -> the new set's handle."""
    if not isinstance(scope, Scope):
        raise TypeError("expand requires a Scope")
    if mode not in MODES:
        raise ExpandUnavailable("mode must be " + ", ".join(MODES))
    if role not in ROLES:
        raise ExpandUnavailable("role must be context or dependency")
    if isinstance(within, dict):
        within = HardScope.model_validate(within)
    if within is not None and not isinstance(within, HardScope):
        raise ExpandUnavailable("within must be a hard scope")
    for name, value in (("before", before), ("after", after)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ExpandUnavailable(f"{name} must be a non-negative integer")
    before, after = min(before, MAX_WINDOW), min(after, MAX_WINDOW)
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ExpandUnavailable("depth must be a positive integer")
    depth = min(depth, MAX_DEPTH)
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 1000:
        raise ExpandUnavailable("k must be 1..1000")
    if mode == "focus" and not (isinstance(focus, str) and focus.strip()):
        raise ExpandUnavailable("mode focus needs focus text")
    if mode == "place" and not (
        isinstance(meters, (int, float)) and 0 < meters <= 20_000_000
    ):
        raise ExpandUnavailable("meters must be in (0, 20000000]")
    if mode == "time" and (
        isinstance(window_s, bool) or not isinstance(window_s, int) or window_s < 0
    ):
        raise ExpandUnavailable("window_s must be a non-negative integer")
    as_of = resultsets._time(as_of, "as_of")
    known_by = resultsets._time(known_by, "known_by")
    started = time.perf_counter()
    execution = execution if execution is not None else Execution(tracker)
    records, info = _input(scope, handle, ids, as_of, known_by)
    if len(records) > max_input():
        raise ExpandUnavailable(
            f"expand takes at most {max_input()} input members (THROUGHLINE_EXPAND_MAX_INPUT)"
        )
    # The input set's cutoffs are inherited unless the caller names its own.
    if within is not None:
        as_of = as_of or within.as_of
        known_by = known_by or within.known_by
    as_of = as_of or info.get("as_of")
    known_by = known_by or info.get("known_by")
    input_kinds = {
        r["kind"] for r in records if r["record_id"].startswith("m:") and r.get("kind")
    }
    flt = _Filter(scope, within, as_of, known_by, input_kinds)
    if flt.unresolvable:
        execution.warn(f"unresolvable_scope:{flt.unresolvable}")
    present = {r["record_id"] for r in records}
    # Input members are never added again; without keep_input an input
    # member may come back as another member's context (or as its own source).
    added, seen = [], set(present) if keep_input else set()
    capped = False
    retrieved = 0
    expired = tracker is not None and tracker.expired()

    def add(record_id, anchor, score=None, extra=None):
        if record_id in seen:
            return
        seen.add(record_id)
        added.append(_added(record_id, anchor, mode, role, score, extra))

    if expired:
        capped = True
        execution.degrade("expand", "deadline")
    elif mode in ("context", "thread"):
        pairs, capped = _context(scope, records, before, after, mode == "thread")
        retrieved = len(pairs)
        passed = flt.messages([m for _, m in pairs])
        for anchor, mid in pairs:
            if mid in passed:
                add(f"m:{passed[mid]}", anchor)
    elif mode == "sources":
        pairs, capped = _sources(scope, records)
        retrieved = len(pairs)
        passed = flt.messages([m for _, m in pairs], versions=True)
        for anchor, mid in pairs:
            if mid not in passed:
                continue
            add(f"m:{passed[mid]}", anchor)
    elif mode == "links":
        message_pairs, other_pairs, capped = _links(
            scope, records, link_type, depth, flt.cutoff
        )
        retrieved = len(message_pairs) + len(other_pairs)
        passed = flt.messages([m for _, m in message_pairs], versions=True)
        visible = _visible_records(scope, [o for _, o in other_pairs])
        for anchor, mid in message_pairs:
            if mid in passed:
                add(f"m:{passed[mid]}", anchor)
        for anchor, other in other_pairs:
            kind = "entity" if other.startswith("e:") else "link"
            if other in visible and flt.admits(kind):
                add(other, anchor)
    elif mode == "time":
        pairs, capped = _pairs(
            scope,
            "SELECT * FROM api.time_overlap_many(%s::bigint[],"
            + lit(int(window_s))
            + ")",
            _message_anchors(records),
        )
        retrieved = len(pairs)
        passed = flt.messages([m for _, m in pairs])
        for anchor, mid in pairs:
            if mid in passed:
                add(f"m:{passed[mid]}", anchor)
    elif mode == "place":
        pairs, capped = _pairs(
            scope,
            "SELECT * FROM api.near_many(%s::bigint[]," + lit(float(meters)) + ")",
            _message_anchors(records),
        )
        retrieved = len(pairs)
        passed = flt.messages([m for _, m in pairs])
        for anchor, mid in pairs:
            if mid in passed:
                add(f"m:{passed[mid]}", anchor)
    else:  # focus
        ranked, retrieved = _focus(
            scope, records, flt, focus.strip(), k, before, after, link_type, depth
        )
        for rank, (record_id, anchor, score) in enumerate(ranked, start=1):
            add(record_id, anchor, score, dict(rank=rank))
    limit = thread_cap() if mode == "thread" else max_output()
    if len(added) > limit:
        added = added[:limit]
        capped = True
    if mode == "focus":
        own = "ranked_sample"
    else:
        own = "truncated" if capped else "exhaustive"
    completeness = _worst(info.get("completeness") or "exhaustive", own)
    if capped and mode != "focus":
        execution.set_truncation("row_cap", limit)
    members = [_member(r) for r in records] if keep_input else []
    members += added
    counts = dict(
        retrieved=retrieved,
        mechanically_processed=retrieved,
        semantically_evaluated=0,
        inspected_by_controller=0,
        anchors=len(records),
        added=len(added),
    )
    origin = dict(
        op="expand",
        mode=mode,
        params=dict(
            before=before,
            after=after,
            depth=depth,
            link_type=link_type,
            window_s=window_s,
            meters=meters,
            k=k,
            keep_input=keep_input,
            role=role,
            within=within is not None,
        ),
        parents=[handle] if handle else [],
    )
    if focus:
        import hashlib

        origin["focus_sha256"] = hashlib.sha256(focus.encode("utf-8")).hexdigest()
    result = resultsets.create(
        scope,
        members,
        origin=origin,
        completeness=completeness,
        counts=counts,
        session=session if session is not None else info.get("session_id"),
        as_of=flt.hard.as_of,
        known_by=flt.hard.known_by,
    )
    ms = (time.perf_counter() - started) * 1000
    execution.add_timing("expand", ms)
    execution.stage(
        "expand",
        "ok",
        kind="operator",
        exact=mode in EXACT_MODES,
        mode=mode,
        anchors=len(records),
        rows=len(added),
        truncated=capped,
        ms=round(ms, 3),
    )
    execution.completeness(completeness)
    execution.set("counts", dict(counts, returned=len(members)))
    if token_budget is not None:
        execution.set("token_budget", int(token_budget))
    return result
