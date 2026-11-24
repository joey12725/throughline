"""The only record hydrator in the project (MSG-7).

Candidates and result-set members are turned into Records by reading, per
record-id prefix, the gated record view registered in tl.record_sources
(m -> api.record_versions, l -> api.link_records, e -> api.entity_records,
p, c ...). One statement per prefix, under the caller's scope, so the asker
gate decides what comes back; a record the asker cannot see simply does not
hydrate. Under as_of/known_by message records are read through
api.records_asof, so nothing after the cutoff can appear.
"""

import logging
import re
import threading
import warnings

from ..budget import estimate_tokens
from ..db import connect
from ..query import query_ex
from .scope_sql import lit
from .types import ATTRIBUTION_KEYS, RECORD_CONTRACT, Candidate

RELATION = re.compile(r"^api\.([a-z_]+)$")
PREFIX = re.compile(r"^[a-z]{1,8}$")

log = logging.getLogger("throughline.retrieval")


# --- evidence class ---------------------------------------------
# One pure function of the RECORD CONTRACT fields, applied to every hydrated
# record (recall, result sets, expand, match, packets), so every surface
# carries the identical value. Classes, strongest first:
#   primary     the source's own words: life_event and reference messages
#   normalized  source-stated structure: page revisions, derived records and
#               passages with support, entities, links not generated
#   derived     mechanically derived: observation claims with sources, links
#               made by a rule (and generated links that expose no method)
#   inference   a model's or person's inference: inference, hypothesis and
#               judgment claims, links made by a model, and any generated
#               record without support (unsupported)
# Later kinds add one entry to EVIDENCE_RULES (the commitment ledger: 'commitment').


def _link_method(record):
    for name in ("provenance", "claim"):
        value = record.get(name)
        if isinstance(value, dict) and isinstance(value.get("method"), str):
            return value["method"]
    return None


def _link_class(record):
    if not record.get("generated"):
        return "normalized"
    method = _link_method(record)
    if method == "model":
        return "inference"
    # 'rule', or a generated link that exposes no method.
    return "derived"


def _source_words(record):
    # The source's own words only when nothing generated them.
    return "derived" if record.get("generated") else "primary"


def _source_structure(record):
    # Entities are source-stated structure only when not generated.
    return "derived" if record.get("generated") else "normalized"


EVIDENCE_RULES = {
    "life_event": _source_words,
    "reference": _source_words,
    "page_revision": lambda r: "normalized",
    "derived": lambda r: "normalized",
    "passage": lambda r: "normalized",
    "entity": _source_structure,
    "link": _link_class,
}

_warned = set()
_warned_lock = threading.Lock()


def _unknown_kind(kind):
    with _warned_lock:
        if kind in _warned:
            return
        _warned.add(kind)
    message = f"no evidence_class rule for record kind {kind!r}; using the default"
    log.warning(message)
    warnings.warn(message, RuntimeWarning, stacklevel=3)


def evidence_class(record):
    """primary | normalized | derived | inference, from contract fields only."""
    if record.get("generated") and record.get("unsupported"):
        return "inference"
    kind = record.get("kind")
    rule = EVIDENCE_RULES.get(kind)
    if rule is None:
        _unknown_kind(kind)
        return "derived" if record.get("generated") else "primary"
    return rule(record)


def registry():
    """{prefix: relation} from tl.record_sources (configuration, not data)."""
    with connect("app") as conn:
        rows = conn.execute(
            "SELECT prefix, relation FROM tl.record_sources ORDER BY prefix"
        ).fetchall()
    return {
        r["prefix"]: r["relation"]
        for r in rows
        if PREFIX.fullmatch(r["prefix"]) and RELATION.fullmatch(r["relation"])
    }


def _statement(prefix, relation, ids, as_of=None, known_by=None):
    ids = lit(sorted(ids))
    if prefix == "m" and (as_of is not None or known_by is not None):
        at = lit(as_of) if as_of is not None else "'infinity'::timestamptz"
        source = f"api.records_asof({at},{lit(known_by)})"
    else:
        source = relation
    return f"SELECT * FROM {source} r WHERE r.ref_id = ANY({ids})"


def _merge_support(row_support, extra):
    support = [dict(s) for s in (row_support or [])]
    for entry in extra or []:
        if entry not in support:
            support.append(dict(entry))
    return support


def build(row, candidate):
    """A Record: the row's RECORD CONTRACT columns plus the retrieval keys."""
    record = {name: row.get(name) for name in RECORD_CONTRACT}
    support = _merge_support(record["support"], candidate.support)
    if support and not record["support"] and record["unsupported"]:
        record["unsupported"] = False
    record["support"] = support
    span = candidate.span
    if span is None and record["thread_no"] is not None:
        span = dict(first_no=record["thread_no"], last_no=record["thread_no"])
    record.update(
        span=span,
        conversation=candidate.conversation,
        score=candidate.score,
        discovery=[dict(d) for d in candidate.discovery],
        role=candidate.role,
        depends_on=candidate.depends_on,
        groups=list(candidate.groups),
        tokens=estimate_tokens(record["content"] or ""),
    )
    record["evidence_class"] = evidence_class(record)
    # Filled by attach_attribution for message records (None otherwise).
    record["attribution"] = None
    # Filled by attach_assets for message records with images.
    record["images"] = None
    return record


def _message_id(record):
    record_id = record.get("record_id") or ""
    if not record_id.startswith("m:"):
        return None
    value = record.get("message_id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(record_id.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def attach_attribution(run, records):
    """One api.attribution statement for the message records ('m:'):
    record['attribution'] = {is_me, direction, attribution_status,
    identity_quality, time_precision}; None for other kinds and for message
    records without a row (reference and page records). A failure leaves
    every attribution None and never fails the hydration."""
    ids = sorted({i for i in (_message_id(r) for r in records) if i is not None})
    if not ids:
        return records
    try:
        rows = run(
            "SELECT * FROM api.attribution(" + lit(ids, "bigint[]") + ")", len(ids)
        )
    except Exception as exc:
        log.warning("attribution unavailable: %s", type(exc).__name__)
        return records
    found = {
        row["message_id"]: {key: row.get(key) for key in ATTRIBUTION_KEYS}
        for row in rows
    }
    for record in records:
        message_id = _message_id(record)
        if message_id is not None and message_id in found:
            record["attribution"] = dict(found[message_id])
    return records


def attach_assets(run, records, include_nsfw=False):
    """One api.assets_of statement for the message records: record['images']
    = the message's images (retrieval.images.image shape); None for other
    kinds and messages without images. A failure leaves every value None."""
    from . import images

    ids = [i for i in (_message_id(r) for r in records) if i is not None]
    found = images.of(run, ids, include_nsfw=include_nsfw)
    for record in records:
        message_id = _message_id(record)
        if message_id is not None and found.get(message_id):
            record["images"] = [dict(i) for i in found[message_id]]
    return records


def _hydrate(run, candidates, as_of=None, known_by=None, sources=None):
    """run(statement, cap) -> rows. Returns Records in candidate order; a
    candidate whose record is not visible (or not registered) is left out."""
    sources = sources if sources is not None else registry()
    wanted = {}
    for candidate in candidates:
        try:
            wanted.setdefault(candidate.prefix, set()).add(candidate.ref_id)
        except (IndexError, ValueError):
            continue
    rows = {}
    for prefix, ids in sorted(wanted.items()):
        relation = sources.get(prefix)
        if relation is None or not ids:
            continue
        for row in run(_statement(prefix, relation, ids, as_of, known_by), len(ids)):
            rows[row["record_id"]] = row
    records = []
    for candidate in candidates:
        row = rows.get(candidate.record_id)
        if row is not None:
            records.append(build(row, candidate))
    return attach_attribution(run, records)


def records(ctx, candidates):
    """Hydrate recall candidates under ctx's scope (and as_of/known_by)."""
    scope = ctx.resolved.scope if ctx.resolved is not None else None
    return _hydrate(
        ctx.sql,
        candidates,
        as_of=scope.as_of if scope is not None else None,
        known_by=scope.known_by if scope is not None else None,
    )


def _member_candidate(member):
    record_id = member.get("record_id")
    prefix = member.get("prefix")
    if prefix == "m" and member.get("message_id") is not None:
        record_id = f"m:{member['message_id']}"
    elif not record_id and prefix and member.get("ref_id") is not None:
        record_id = f"{prefix}:{member['ref_id']}"
    span = member.get("span")
    if span is None and (
        member.get("span_first") is not None or member.get("span_last") is not None
    ):
        span = dict(first_no=member.get("span_first"), last_no=member.get("span_last"))
    meta = member.get("meta") or {}
    groups = member.get("groups") or []
    return Candidate(
        record_id=record_id,
        message_id=member.get("message_id"),
        version=member.get("version"),
        span=span,
        conversation=member.get("conversation") or meta.get("conversation"),
        score=member.get("score"),
        discovery=list(member.get("discovery") or []),
        role=member.get("role") or "hit",
        depends_on=member.get("depends_on"),
        groups=list(groups),
    )


def records_for_members(scope, members, *, as_of=None, known_by=None):
    """Records for result-set members (or record-shaped dicts) under a scope:
    the same function recall uses, so the keys are identical."""
    candidates = [_member_candidate(m) for m in members]
    candidates = [c for c in candidates if c.record_id]

    def run(statement, cap):
        return query_ex(statement, cap, scope=scope, materialize=False)["rows"]

    return attach_assets(
        run, _hydrate(run, candidates, as_of=as_of, known_by=known_by)
    )
