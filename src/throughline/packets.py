"""Evidence packets (LIB-9, QRY-43, INT-12): what an agent or the librarian
delivers, built from one live read of a result set.

    packet = build(scope, handle, notes=[{generated_text, supporting_refs,
                   uncertainty}], unresolved=[...], token_budget=4000)

Records are copied by the runtime from a single resultsets.read(scope,
handle): every record field comes from the gated record views under the
caller's scope, never from a caller. Notes are orientation only: each is
flagged generated and is kept only when

  - it has at least one supporting ref {record_id, version},
  - every ref matches a record of that same read on record_id and version,
  - every record id written in its text (a prefix registered in
    tl.record_sources followed by ':digits', read at build time) is among
    the delivered records.

A dropped note is listed in truncation_and_omissions with the reason. Note
text is never placed in a record field. execution_status (how the run went)
and evidence_adequacy (what the evidence covers) are reported separately;
there is no single 'complete' flag. Over the token budget the packet carries
the records that fit, a manifest of the rest and a continuation_ref.
"""

import json
import re
import time

from . import resultsets
from .budget import estimate_tokens
from .db import connect
from .scope import Scope

FIELDS = (
    "investigation_id",
    "result_set_ref",
    "execution_status",
    "records",
    "orientation",
    "coverage",
    "evidence_adequacy",
    "unresolved_needs",
    "truncation_and_omissions",
    "cost_and_latency",
    "continuation_ref",
    "manifest",
)
RECORD_FIELDS = (
    "record_id",
    "revision_id",
    "kind",
    "original_excerpt",
    "span",
    "event_time",
    "recorded_time",
    "ingested_time",
    "applicable_time",
    "source_refs",
    "claim_metadata",
    "generated",
    "unsupported",
    "evidence_class",
    "attribution",
    "discovery_reasons",
)
MAX_NOTES = 100
MAX_NOTE_CHARS = 4000
PREFIX = re.compile(r"^[a-z]{1,8}$")


def record_prefixes():
    """Prefixes registered in tl.record_sources now (m, p, c, e, l, ...)."""
    with connect("app") as conn:
        rows = conn.execute("SELECT prefix FROM tl.record_sources").fetchall()
    return sorted({r["prefix"] for r in rows if PREFIX.fullmatch(r["prefix"] or "")})


def id_pattern(prefixes):
    """Record ids in free text: <registered prefix>:<digits>."""
    if not prefixes:
        return None
    alternatives = "|".join(
        sorted((re.escape(p) for p in prefixes), key=len, reverse=True)
    )
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + alternatives + r"):[0-9]+(?![0-9])")


def _record(record):
    """The packet form of one Record (copied, never caller-supplied)."""
    record_time = record.get("record_time")
    subject_from = record.get("subject_from")
    return dict(
        record_id=record.get("record_id"),
        revision_id=dict(
            message_id=record.get("message_id"), version=record.get("version")
        ),
        kind=record.get("kind"),
        original_excerpt=record.get("content"),
        span=record.get("span"),
        event_time=subject_from if subject_from is not None else record_time,
        recorded_time=record_time,
        ingested_time=record.get("ingested_at"),
        applicable_time=dict(
            {"from": subject_from, "to": record.get("subject_to")},
            precisions=dict(
                {
                    "from": record.get("subject_from_precision"),
                    "to": record.get("subject_to_precision"),
                }
            ),
        ),
        source_refs=record.get("support") or [],
        claim_metadata=dict(
            claim=record.get("claim"), provenance=record.get("provenance")
        )
        if record.get("claim") is not None or record.get("provenance") is not None
        else None,
        generated=record.get("generated"),
        unsupported=record.get("unsupported"),
        evidence_class=record.get("evidence_class"),
        attribution=record.get("attribution"),
        discovery_reasons=record.get("discovery") or [],
    )


def _ref(ref):
    if isinstance(ref, dict):
        version = ref.get("version")
        return str(ref.get("record_id")), (
            int(version)
            if isinstance(version, (int, float)) and not isinstance(version, bool)
            else None
        )
    return str(ref), None


def _check_note(index, note, delivered, pattern):
    """(orientation entry, None) or (None, omission)."""
    if not isinstance(note, dict):
        return None, dict(kind="note", index=index, reason="not an object")
    text = note.get("generated_text")
    if not isinstance(text, str) or not text.strip():
        return None, dict(kind="note", index=index, reason="no generated_text")
    if len(text) > MAX_NOTE_CHARS:
        return None, dict(kind="note", index=index, reason="generated_text too long")
    refs = note.get("supporting_refs") or []
    if not isinstance(refs, list) or not refs:
        return None, dict(kind="note", index=index, reason="no supporting reference")
    checked = []
    for ref in refs:
        record_id, version = _ref(ref)
        if record_id not in delivered:
            return None, dict(
                kind="note",
                index=index,
                reason="reference not among the delivered records",
                ref=record_id,
            )
        # A versionless record (claim, entity, link) is cited without a
        # version; a versioned one must name the delivered version.
        if version not in delivered[record_id]:
            return None, dict(
                kind="note",
                index=index,
                reason="reference version does not match the delivered record",
                ref=record_id,
            )
        checked.append(dict(record_id=record_id, version=version))
    if pattern is not None:
        for match in pattern.finditer(text):
            if match.group(0) not in delivered:
                return None, dict(
                    kind="note",
                    index=index,
                    reason="text names a record that is not delivered",
                    ref=match.group(0),
                )
    uncertainty = note.get("uncertainty")
    if uncertainty is not None and not isinstance(uncertainty, (str, int, float)):
        uncertainty = json.dumps(uncertainty, default=str)[:500]
    return (
        dict(
            generated_text=text,
            supporting_refs=checked,
            uncertainty=uncertainty,
            generated=True,
        ),
        None,
    )


def build(
    scope,
    selection_handle,
    *,
    notes=None,
    unresolved=None,
    token_budget=None,
    investigation=None,
):
    """EvidencePacket of one of the caller's result sets (see the module doc)."""
    if not isinstance(scope, Scope):
        raise TypeError("packets.build requires a Scope")
    started = time.perf_counter()
    notes = list(notes or [])
    if len(notes) > MAX_NOTES:
        raise ValueError(f"a packet takes at most {MAX_NOTES} notes")
    read = resultsets.read(scope, selection_handle)
    info = read["info"]
    rows = read["records"]
    delivered = {}
    for row in rows:
        delivered.setdefault(row["record_id"], set()).add(row.get("version"))
    pattern = id_pattern(record_prefixes())
    orientation, omissions = [], []
    for index, note in enumerate(notes):
        entry, omission = _check_note(index, note, delivered, pattern)
        if entry is not None:
            orientation.append(entry)
        else:
            omissions.append(omission)
    records = [_record(r) for r in rows]
    manifest = None
    continuation = None
    if token_budget is not None:
        budget = max(0, int(token_budget))
        spent = estimate_tokens(orientation) if orientation else 0
        kept = []
        for record in records:
            size = estimate_tokens(json.dumps(record, default=str, ensure_ascii=False))
            if spent + size > budget:
                break
            spent += size
            kept.append(record)
        if len(kept) < len(records):
            rest = records[len(kept) :]
            manifest = [
                dict(
                    record_id=r["record_id"],
                    version=r["revision_id"]["version"],
                    kind=r["kind"],
                )
                for r in rest
            ]
            omissions.append(
                dict(
                    kind="records",
                    reason="token_budget",
                    limit=budget,
                    omitted=len(rest),
                )
            )
            continuation = dict(
                result_set=selection_handle,
                offset=len(kept),
                investigation_id=investigation,
            )
            records = kept
    legs = sorted(
        {
            str(d.get("leg"))
            for row in rows
            for d in (row.get("discovery") or [])
            if isinstance(d, dict) and d.get("leg")
        }
    )
    origin = info.get("origin") or {}
    methods = sorted(set(legs) | ({str(origin["op"])} if origin.get("op") else set()))
    completeness = info.get("completeness")
    from . import coverage as source_coverage

    known_gaps, source_status = source_coverage.packet_coverage(
        scope, [row.get("channel") for row in rows]
    )
    coverage = dict(
        authorized_scope=dict(
            mode=scope.mode, asker=scope.asker, token_kind=scope.token_kind
        ),
        retrieval_methods=methods,
        enumerated_or_sampled={
            "exhaustive": "enumerated",
            "ranked_sample": "sampled",
            "truncated": "truncated",
        }.get(completeness, completeness),
        processing_counts=info.get("counts") or {},
        # Source coverage of the packet records' channels under the caller's
        # scope: partial channels and open gaps (source coverage).
        known_source_gaps=known_gaps,
        source_status=source_status,
    )
    adequacy = dict(
        completeness=completeness,
        records_delivered=len(records),
        records_omitted=len(rows) - len(records),
        notes_kept=len(orientation),
        notes_dropped=sum(1 for o in omissions if o["kind"] == "note"),
        unresolved=len(list(unresolved or [])),
    )
    return dict(
        investigation_id=investigation,
        result_set_ref=selection_handle,
        execution_status=read["execution"]["status"],
        records=records,
        orientation=orientation,
        coverage=coverage,
        evidence_adequacy=adequacy,
        unresolved_needs=list(unresolved or []),
        truncation_and_omissions=omissions,
        cost_and_latency=dict(
            ms=round((time.perf_counter() - started) * 1000, 3),
            model_cost=0.0,
            model_calls=0,
        ),
        continuation_ref=continuation,
        manifest=manifest,
    )
