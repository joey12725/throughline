"""match(): three-valued semantic matching of a result set (QRY-33, CST-5,
CST-6, CST-10).

    handle = match(scope, token_row, handle="rs_...", condition="...", max_rows=200)

Every member of the set is judged against the condition by the semantic
provider (providers.get('semantic'), operation 'match'). The output set keeps
the input order; each member's meta carries {verdict, evidence, provider,
model, operator_version, cached} and its group is the verdict.

Analytics invariants, each enforced here:
- Bound first: members <= max_rows <= THROUGHLINE_MATCH_MAX_ROWS (500) is
  checked before anything is read or sent.
- Frozen population: the text of every member comes from resultsets.read under
  the caller's scope, so the set's pinned versions are judged, never the
  current text, and only text the caller may read is ever sent.
- Content + rubric + model: a verdict is keyed by (record prefix, ref_id,
  version, sha256(text), 'match', sha256(normalised condition), model,
  operator version). Stored verdicts (tl.verdicts) are reused only for
  members of the caller's read whose source messages are all visible to the
  caller, and they survive the expiry of the set that produced them.
- Estimate, then approve: the members without a verdict are estimated and
  passed through cost.authorize before any call; ApprovalRequired is raised
  before any call.
- NULL, not zero: a failed batch (provider error, timeout, invalid output)
  gives its members meta.verdict null with meta.error. It is never
  'uncertain' or 'nonmatch', never stored, counted in counts.failed, and the
  execution is marked partial. 'uncertain' means only that the model said so,
  or that the member has no text (meta.reason 'not_evaluable').
- Episode grain: message members are grouped with api.episodes_of and each
  episode is judged once on its members' texts in time order. Only the anchor
  member carries the verdict; the others are context members without one.
"""

import hashlib
import os
import time
import unicodedata
from dataclasses import dataclass, field

from psycopg.types.json import Jsonb

from .. import cost, providers, resultsets
from ..budget import estimate_tokens
from ..db import connect
from ..execution import Execution
from ..providers.base import MATCH_OUTPUTS
from ..query import reader_transaction
from ..scope import Scope

OPERATION = "match"
OPERATOR_VERSION = "match-1"
GRAINS = ("record", "episode")
DEFAULT_MAX_ROWS = 500
DEFAULT_GROUP_MAX_TOKENS = 4000
MAX_CONDITION_CHARS = 2000
MAX_EVIDENCE = 20
SEPARATOR = "\n\n"
OUTPUT_SCHEMA = {"type": "string", "enum": list(MATCH_OUTPUTS)}
# Keys of an input member's meta that a match replaces: a member never keeps
# a verdict from an earlier operator next to (or instead of) this one.
VERDICT_KEYS = (
    "verdict",
    "evidence",
    "evidence_clamped",
    "provider",
    "model",
    "operator_version",
    "cached",
    "error",
    "reason",
    "grain",
    "episode_id",
    "members",
    "truncated",
)


class MatchUnavailable(ValueError):
    """The request cannot be matched as asked (a 400)."""


def _bound(name, default):
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        value = default
    return max(1, value)


def max_rows_limit():
    return _bound("THROUGHLINE_MATCH_MAX_ROWS", DEFAULT_MAX_ROWS)


def group_max_tokens():
    return _bound("THROUGHLINE_MATCH_GROUP_MAX_TOKENS", DEFAULT_GROUP_MAX_TOKENS)


def normalise(condition):
    """The rubric as keyed: NFC, whitespace collapsed, trimmed."""
    return " ".join(unicodedata.normalize("NFC", condition or "").split())


def sha256(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class Unit:
    """One judgement: a record, or an episode of message members."""

    prefix: str
    ref_id: int
    version: int
    text: str
    sources: list
    members: list  # Records judged together (one for record grain)
    episode_id: str | None = None
    segments: list = field(default_factory=list)  # [(record_id, start, end)]
    group: list = field(default_factory=list)  # every member of an episode
    truncated: bool = False
    verdict: str | None = None
    evidence: list = field(default_factory=list)
    clamped: bool = False
    cached: bool = False
    error: str | None = None
    provider: str | None = None
    model: str | None = None

    @property
    def text_hash(self):
        return sha256(self.text)


@dataclass
class Plan:
    records: list
    info: dict
    units: list
    not_evaluable: list
    context_of: dict  # record ordinal -> anchor record (episode grain)
    condition: str
    condition_hash: str
    provider: object
    model: str
    operator_version: str


def _text(record):
    text = record.get("content")
    return text if isinstance(text, str) and text.strip() else None


def _sources(record):
    ids = []
    if record.get("message_id") is not None:
        ids.append(int(record["message_id"]))
    for item in record.get("support") or []:
        if isinstance(item, dict) and item.get("message_id") is not None:
            ids.append(int(item["message_id"]))
    return sorted(set(ids))


def _record_unit(record):
    prefix, ref = record["record_id"].split(":", 1)
    return Unit(
        prefix=prefix,
        ref_id=int(ref),
        version=int(record.get("version") or 0),
        text=_text(record),
        sources=_sources(record),
        members=[record],
    )


def _episodes(scope, records):
    """{message id: episode row} through api.episodes_of under the scope."""
    ids = sorted(
        {int(r["message_id"]) for r in records if r["record_id"].startswith("m:")}
    )
    if not ids:
        return {}
    with reader_transaction(scope) as conn:
        present = conn.execute(
            "SELECT to_regprocedure('api.episodes_of(bigint[])') IS NOT NULL AS ok"
        ).fetchone()["ok"]
        if not present:
            raise MatchUnavailable(
                "grain episode needs api.episodes_of (migration 0040)"
            )
        rows = conn.execute(
            "SELECT * FROM api.episodes_of(%s::bigint[])", (ids,)
        ).fetchall()
    return {r["message_id"]: r for r in rows}


def _episode_units(scope, records):
    """(units, context_of): one unit per episode and per non-message member."""
    found = _episodes(scope, records)
    groups, units, context_of = {}, [], {}
    for record in records:
        row = (
            found.get(int(record["message_id"]))
            if record["record_id"].startswith("m:")
            else None
        )
        if row is None:
            units.append(_record_unit(record))
            continue
        groups.setdefault(row["episode_id"], (row, []))[1].append(record)
    limit = group_max_tokens() * 4
    for episode_id, (row, members) in groups.items():
        members = sorted(
            members,
            key=lambda r: (
                str(r.get("record_time") or ""),
                r.get("thread_no") if r.get("thread_no") is not None else 0,
                r["ref_id"],
            ),
        )
        parts, segments, length, truncated = [], [], 0, False
        for record in members:
            text = _text(record)
            if text is None:
                continue
            if length + (len(SEPARATOR) if parts else 0) >= limit:
                truncated = True
                break
            if parts:
                length += len(SEPARATOR)
            room = limit - length
            if len(text) > room:
                text, truncated = text[:room], True
            segments.append((record["record_id"], length, length + len(text)))
            parts.append(text)
            length += len(text)
            if truncated:
                break
        text = SEPARATOR.join(parts) if parts else None
        anchor = members[0]
        for record in members[1:]:
            context_of[record["ordinal"]] = (anchor, episode_id)
        units.append(
            Unit(
                prefix="ep",
                ref_id=int(row["first_message_id"]),
                version=0,
                text=text,
                sources=sorted({int(r["message_id"]) for r in members}),
                members=[anchor],
                episode_id=episode_id,
                segments=segments,
                truncated=truncated,
                group=members,
            )
        )
    return units, context_of


def _check(condition, max_rows, grain):
    if not isinstance(condition, str) or not condition.strip():
        raise MatchUnavailable("condition must be non-empty text")
    if len(condition) > MAX_CONDITION_CHARS:
        raise MatchUnavailable(
            f"condition is limited to {MAX_CONDITION_CHARS} characters"
        )
    if grain not in GRAINS:
        raise MatchUnavailable("grain must be record or episode")
    limit = max_rows_limit()
    if (
        isinstance(max_rows, bool)
        or not isinstance(max_rows, int)
        or not 1 <= max_rows <= limit
    ):
        raise MatchUnavailable(
            f"max_rows must be 1..{limit} (THROUGHLINE_MATCH_MAX_ROWS)"
        )


def _plan(scope, handle, condition, max_rows, grain):
    """Everything a match needs before any model call: bound, provider, the
    frozen population, units and the cached verdicts that may be served."""
    if not isinstance(scope, Scope):
        raise TypeError("match requires a Scope")
    _check(condition, max_rows, grain)
    # The bound is checked before any text is read or sent.
    count = resultsets.info(scope, handle)["member_count"]
    if count > max_rows:
        raise MatchUnavailable(
            f"the set has {count} members, more than max_rows {max_rows}"
        )
    try:
        provider = providers.get("semantic")
    except providers.ProviderUnavailable as error:
        raise MatchUnavailable(str(error)) from None
    result = resultsets.read(scope, handle)
    records = result["records"]
    if len(records) > max_rows:
        raise MatchUnavailable(
            f"the set has {len(records)} members, more than max_rows {max_rows}"
        )
    if grain == "episode":
        units, context_of = _episode_units(scope, records)
    else:
        units, context_of = [_record_unit(r) for r in records], {}
    not_evaluable = [u for u in units if u.text is None]
    model = str(provider.model or provider.name)
    plan = Plan(
        records=records,
        info=result["info"],
        units=units,
        not_evaluable=not_evaluable,
        context_of=context_of,
        condition=condition,
        condition_hash=sha256(normalise(condition)),
        provider=provider,
        model=model,
        operator_version=f"{OPERATOR_VERSION}/{provider.version}",
    )
    _serve_cached(scope, plan)
    # A unit with a source under a label the owner has not allowed to leave
    # is not sent to a provider off this host: it stays NULL (migration 0055).
    waiting = _pending(plan)
    held = providers.egress.held_for(provider, [i for u in waiting for i in u.sources])
    for unit in waiting:
        if held.intersection(unit.sources):
            unit.error = EGRESS_HELD
    return plan


def _serve_cached(scope, plan):
    """Mark units whose stored verdict may be served to this scope."""
    wanted = [u for u in plan.units if u.text is not None]
    if not wanted:
        return
    with connect("app") as conn:
        rows = conn.execute(
            "SELECT v.record_prefix,v.ref_id,v.version,v.text_hash,v.verdict,v.evidence,v.source_message_ids,v.provider"
            " FROM tl.verdicts v JOIN unnest(%s::text[],%s::bigint[],%s::int[],%s::text[]) k(prefix,ref_id,version,text_hash)"
            " ON v.record_prefix=k.prefix AND v.ref_id=k.ref_id AND v.version=k.version AND v.text_hash=k.text_hash"
            " WHERE v.operation=%s AND v.condition_hash=%s AND v.model=%s AND v.operator_version=%s",
            (
                [u.prefix for u in wanted],
                [u.ref_id for u in wanted],
                [u.version for u in wanted],
                [u.text_hash for u in wanted],
                OPERATION,
                plan.condition_hash,
                plan.model,
                plan.operator_version,
            ),
        ).fetchall()
    if not rows:
        return
    stored = {
        (r["record_prefix"], r["ref_id"], r["version"], r["text_hash"]): r for r in rows
    }
    # A stored verdict is served only when every message it was derived from
    # is visible to this scope now (CST-6), checked in the database.
    ids = sorted({i for r in rows for i in r["source_message_ids"] or ()})
    with reader_transaction(scope) as conn:
        unrestricted = conn.execute("SELECT api.asker_unrestricted() AS ok").fetchone()[
            "ok"
        ]
        visible = {
            r["i"]
            for r in conn.execute(
                "SELECT x.i FROM unnest(%s::bigint[]) x(i) WHERE api.message_visible(x.i)",
                (ids,),
            ).fetchall()
        }
    for unit in wanted:
        row = stored.get((unit.prefix, unit.ref_id, unit.version, unit.text_hash))
        if row is None:
            continue
        sources = list(row["source_message_ids"] or ())
        if not unrestricted and (not sources or not set(sources) <= visible):
            continue
        unit.verdict = row["verdict"]
        unit.evidence = list(row["evidence"] or [])
        unit.cached = True
        unit.provider = row["provider"]
        unit.model = plan.model


EGRESS_HELD = "egress_held"


def _pending(plan):
    return [
        u
        for u in plan.units
        if u.text is not None and not u.cached and u.error != EGRESS_HELD
    ]


def _items(units, offset=0):
    return [dict(id=str(offset + n), text=u.text) for n, u in enumerate(units)]


def _estimate(plan):
    pending = _pending(plan)
    if not pending:
        return []
    return [plan.provider.estimate(OPERATION, plan.condition, _items(pending))]


def _request_hash(plan, scope, handle, grain, max_rows, tracker):
    budgets = tracker.budget.to_dict() if tracker is not None else None
    return cost.request_hash(
        dict(
            op=OPERATION,
            condition=plan.condition,
            grain=grain,
            max_rows=max_rows,
            operator_version=plan.operator_version,
        ),
        scope,
        [(handle, len(plan.records))],
        budgets,
    )


def _counts(plan):
    units = plan.units
    applied = sum(
        1
        for u in units
        if u.verdict is not None and not u.cached and u.text is not None
    )
    cached = sum(1 for u in units if u.cached)
    failed = sum(1 for u in units if u.error is not None)
    counts = dict(
        retrieved=len(plan.records),
        mechanically_processed=len(plan.records),
        semantically_evaluated=applied + cached,
        inspected_by_controller=0,
        applied=applied,
        cached=cached,
        not_evaluable=len(plan.not_evaluable),
        failed=failed,
        units=len(units),
        match=sum(1 for u in units if u.verdict == "match"),
        nonmatch=sum(1 for u in units if u.verdict == "nonmatch"),
        uncertain=sum(1 for u in units if u.verdict == "uncertain"),
    )
    episodes = sum(1 for u in units if u.episode_id is not None)
    if episodes:
        counts["episodes"] = episodes
    return counts


def estimate(scope, *, handle, condition, max_rows, grain="record"):
    """What a match would cost, without any model call (dry run).

    Returns {estimates: [Estimate], summary, counts{members, cached, pending,
    not_evaluable}, operator_version, model}.
    """
    plan = _plan(scope, handle, condition, max_rows, grain)
    estimates = _estimate(plan)
    return dict(
        estimates=estimates,
        summary=cost.summarize(estimates),
        counts=dict(
            members=len(plan.records),
            units=len(plan.units),
            cached=sum(1 for u in plan.units if u.cached),
            pending=len(_pending(plan)),
            not_evaluable=len(plan.not_evaluable),
        ),
        model=plan.model,
        operator_version=plan.operator_version,
    )


# --- applying ---------------------------------------------------------------


def _offsets(evidence, length):
    """Clamped [[start, end]] offsets into a text of `length` characters and
    whether anything was out of range (or malformed)."""
    clamped = False
    result = []
    if not isinstance(evidence, (list, tuple)):
        return [], evidence is not None
    for item in evidence:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in item)
        ):
            clamped = True
            continue
        start, end = item
        fixed = (min(max(start, 0), length), min(max(end, 0), length))
        if fixed != (start, end) or fixed[1] < fixed[0]:
            clamped = True
        if fixed[1] < fixed[0]:
            fixed = (fixed[1], fixed[0])
        result.append([fixed[0], fixed[1]])
        if len(result) >= MAX_EVIDENCE:
            clamped = clamped or len(evidence) > MAX_EVIDENCE
            break
    return result, clamped


def _valid(results, items):
    if not isinstance(results, (list, tuple)) or len(results) != len(items):
        return False
    for result, item in zip(results, items):
        if not isinstance(result, dict):
            return False
        if str(result.get("id")) != item["id"]:
            return False
        if result.get("output") not in MATCH_OUTPUTS:
            return False
    return True


def _failure(error):
    if isinstance(error, providers.ProviderUnavailable):
        return "budget_exhausted" if "budget" in str(error) else "provider_unavailable"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "provider_error"


def _apply(plan, scope, tracker, trace, execution):
    """Judge the pending units in batches; failures stay NULL."""
    pending = _pending(plan)
    if any(u.error == EGRESS_HELD for u in plan.units):
        execution.degrade("match", EGRESS_HELD)
    if not pending:
        return
    provider = plan.provider
    size = max(1, int(getattr(provider, "batch_size", 20) or 20))
    for start in range(0, len(pending), size):
        batch = pending[start : start + size]
        items = _items(batch, start)
        reason = None
        results = None
        if tracker is not None and tracker.expired():
            reason = "timeout"
        else:
            try:
                results, _usage = providers.call(
                    "semantic",
                    OPERATION,
                    lambda: provider.apply(
                        OPERATION, plan.condition, items, OUTPUT_SCHEMA
                    ),
                    estimate=provider.estimate(OPERATION, plan.condition, items),
                    scope=scope,
                    tracker=tracker,
                    trace=trace,
                )
            except Exception as error:  # Any failure is NULL, never a verdict.
                reason = _failure(error)
            if reason is None and not _valid(results, items):
                reason = "invalid_output"
        if reason is not None:
            for unit in batch:
                unit.error = reason
            execution.degrade("match", reason)
            continue
        for unit, result in zip(batch, results):
            unit.verdict = result["output"]
            unit.evidence, unit.clamped = _offsets(
                result.get("evidence"), len(unit.text)
            )
            unit.provider = provider.name
            unit.model = plan.model


def _store(plan):
    """Keep new verdicts (never failures) with their source messages."""
    new = [
        u
        for u in plan.units
        if u.verdict is not None and not u.cached and u.text is not None
    ]
    if not new:
        return
    with connect("app") as conn, conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO tl.verdicts(record_prefix,ref_id,version,text_hash,operation,condition_hash,condition,provider,model,operator_version,verdict,evidence,source_message_ids)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            [
                (
                    u.prefix,
                    u.ref_id,
                    u.version,
                    u.text_hash,
                    OPERATION,
                    plan.condition_hash,
                    plan.condition,
                    u.provider or plan.provider.name,
                    plan.model,
                    plan.operator_version,
                    u.verdict,
                    Jsonb(_episode_evidence(u) if u.episode_id else u.evidence),
                    u.sources,
                )
                for u in new
            ],
        )


def _episode_evidence(unit):
    """Offsets into the concatenation mapped onto members: [{record_id, offsets}]."""
    if unit.cached:
        return unit.evidence
    by_record = {}
    for start, end in unit.evidence:
        for record_id, lo, hi in unit.segments:
            if lo <= start < hi or (start == end == hi):
                by_record.setdefault(record_id, []).append(
                    [start - lo, min(end, hi) - lo]
                )
                break
    return [
        dict(record_id=record_id, offsets=offsets)
        for record_id, offsets in by_record.items()
    ]


def _meta(base, **values):
    meta = {k: v for k, v in (base or {}).items() if k not in VERDICT_KEYS}
    meta.update({k: v for k, v in values.items() if v is not None or k == "verdict"})
    return meta


def _groups(record, verdict):
    groups = [
        g for g in record.get("groups") or [] if g.get("grp") not in MATCH_OUTPUTS
    ]
    if verdict is not None:
        groups.append(dict(grp=verdict, certain=True))
    return groups


def _member(record, role=None, depends_on=None, meta=None, groups=None):
    return dict(
        record_id=record["record_id"],
        version=record.get("version"),
        message_id=record.get("message_id"),
        span=record.get("span"),
        score=record.get("score"),
        role=role or record.get("role") or "hit",
        depends_on=depends_on if depends_on is not None else record.get("depends_on"),
        groups=groups if groups is not None else record.get("groups") or [],
        discovery=record.get("discovery") or [],
        meta=meta if meta is not None else record.get("meta") or {},
    )


def _output(plan):
    """Members in input order with their verdicts in meta."""
    by_ordinal = {}
    for unit in plan.units:
        common = dict(
            provider=unit.provider,
            model=unit.model,
            operator_version=plan.operator_version,
            cached=unit.cached,
        )
        if unit.text is None:
            values = dict(verdict="uncertain", reason="not_evaluable", cached=False)
        elif unit.error is not None:
            values = dict(verdict=None, error=unit.error, cached=False)
        else:
            values = dict(verdict=unit.verdict, **common)
        if unit.episode_id is not None:
            anchor = unit.members[0]
            evidence = _episode_evidence(unit) if unit.verdict is not None else []
            meta = _meta(
                anchor.get("meta"),
                grain="episode",
                episode_id=unit.episode_id,
                evidence=evidence,
                members=len(unit.group or unit.members),
                truncated=unit.truncated or None,
                **values,
            )
            by_ordinal[anchor["ordinal"]] = _member(
                anchor,
                role="hit",
                depends_on=None,
                meta=meta,
                groups=_groups(anchor, values.get("verdict")),
            )
            continue
        for record in unit.members:
            meta = _meta(
                record.get("meta"),
                evidence=unit.evidence if unit.verdict is not None else None,
                evidence_clamped=True if unit.clamped else None,
                **values,
            )
            by_ordinal[record["ordinal"]] = _member(
                record, meta=meta, groups=_groups(record, values.get("verdict"))
            )
    members = []
    for record in plan.records:
        if record["ordinal"] in by_ordinal:
            members.append(by_ordinal[record["ordinal"]])
            continue
        anchor, episode_id = plan.context_of.get(record["ordinal"], (None, None))
        members.append(
            _member(
                record,
                role="context",
                depends_on=anchor["record_id"] if anchor is not None else None,
                meta=_meta(record.get("meta"), grain="episode", episode_id=episode_id)
                if episode_id
                else _meta(record.get("meta")),
                groups=[
                    g
                    for g in record.get("groups") or []
                    if g.get("grp") not in MATCH_OUTPUTS
                ],
            )
        )
    return members


def _ordinals(records):
    for n, record in enumerate(records):
        record.setdefault("ordinal", n)
    return records


def match(
    scope,
    token_row,
    *,
    handle,
    condition,
    max_rows,
    grain="record",
    tracker=None,
    execution=None,
    approval_id=None,
    trace=None,
    session=None,
    authorize=True,
):
    """Judge every member of a result set; returns the new set's handle.

    Raises MatchUnavailable (a 400) for a bad request or a set over max_rows,
    and cost.ApprovalRequired before any model call when the estimate is over
    the cost limit. authorize=False is for a caller that has already passed
    this match's estimate through cost.authorize (the semantic SQL runtime
    and plans authorize a whole statement at once).
    """
    started = time.perf_counter()
    execution = execution if execution is not None else Execution(tracker)
    plan = _plan(scope, handle, condition, max_rows, grain)
    _ordinals(plan.records)
    estimates = _estimate(plan)
    if estimates and authorize:
        cost.authorize(
            token_row,
            estimates,
            scope=scope,
            tracker=tracker,
            approval_id=approval_id,
            request_hash=_request_hash(plan, scope, handle, grain, max_rows, tracker),
        )
    _apply(plan, scope, tracker, trace, execution)
    _store(plan)
    counts = _counts(plan)
    completeness = plan.info.get("completeness") or "exhaustive"
    if counts["failed"]:
        execution.set("partial", True)
        execution.warn(f"match: {counts['failed']} evaluation(s) failed and are null")
    handle_out = resultsets.create(
        scope,
        _output(plan),
        origin=dict(
            op=OPERATION,
            condition_sha256=plan.condition_hash,
            grain=grain,
            max_rows=max_rows,
            model=plan.model,
            operator_version=plan.operator_version,
            parents=[handle],
            partial=bool(counts["failed"]),
        ),
        completeness=completeness,
        counts=counts,
        session=session if session is not None else plan.info.get("session_id"),
        as_of=plan.info.get("as_of"),
        known_by=plan.info.get("known_by"),
    )
    ms = (time.perf_counter() - started) * 1000
    execution.add_timing("match", ms)
    execution.stage(
        "match",
        "failed" if counts["failed"] else "ok",
        kind="operator",
        exact=False,
        grain=grain,
        rows=len(plan.records),
        applied=counts["applied"],
        cached=counts["cached"],
        failed=counts["failed"],
        ms=round(ms, 3),
        **({"reason": "evaluation failed"} if counts["failed"] else {}),
    )
    execution.completeness(completeness)
    execution.set("counts", dict(counts, returned=len(plan.records)))
    execution.set(
        "match",
        dict(
            model=plan.model,
            operator_version=plan.operator_version,
            provider=plan.provider.name,
            tokens_sent=sum(
                estimate_tokens(u.text or "")
                for u in plan.units
                if u.text is not None and not u.cached
            ),
        ),
    )
    return handle_out
