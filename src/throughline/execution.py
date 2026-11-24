"""Execution metadata returned with every result, and the trace store.

Execution is the compact description every response carries (QRY-36, CST-9).
Trace is the detailed record written only on request or for multi-statement
operations (QRY-36, QRY-44). A trace holds statements, ids, counts and hashes,
never excerpts or prompts.
"""

import hashlib
import json
import logging
import threading
import uuid

from psycopg.types.json import Jsonb

from .db import connect


def _encode(value):
    """JSON fallback for trace values: timestamps (request bounds, coverage
    times) as ISO text, sets as lists, anything else as its text."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def _dumps(obj):
    return json.dumps(obj, default=_encode)


def _jsonb(obj):
    return Jsonb(obj, dumps=_dumps)


log = logging.getLogger("throughline.execution")

STATUSES = ("complete", "degraded", "failed", "approval_required")
# A longer value in a trace detail could be an excerpt; it is stored as a hash.
MAX_DETAIL_CHARS = 200


class Execution:
    """Thread-safe accumulator; to_dict() is the wire form."""

    def __init__(self, tracker=None, trace_id=None):
        self.tracker = tracker
        self.trace_id = trace_id
        self._lock = threading.RLock()
        self._stages = []
        self._timings = {}
        self._incomplete = []
        self._warnings = []
        self._truncation = None
        self._completeness = "unknown"
        self._failed = None
        self._approval = None
        self._extra = {}

    def stage(self, name, status="ok", **info):
        """Record one executed step. A failed or skipped step makes the result
        incomplete (degraded), it does not fail the call."""
        entry = dict(name=name, status=status, **info)
        with self._lock:
            self._stages.append(entry)
            if status in ("failed", "skipped", "timeout"):
                self._incomplete.append(
                    dict(stage=name, reason=info.get("reason") or status)
                )
        return entry

    def degrade(self, name, reason):
        """Something was left out; the rest of the result still stands."""
        with self._lock:
            self._incomplete.append(dict(stage=name, reason=reason))

    def fail(self, reason, stage=None):
        with self._lock:
            self._failed = reason
            self._incomplete.append(dict(stage=stage, reason=reason))

    def require_approval(self, approval=None):
        with self._lock:
            self._approval = approval if approval is not None else True

    def warn(self, message):
        with self._lock:
            if message not in self._warnings:
                self._warnings.append(message)

    def set_truncation(self, reason, limit=None):
        """reason: row_cap | response_bytes | row_too_large | token_budget | ..."""
        with self._lock:
            self._truncation = dict(reason=reason, limit=limit)

    def completeness(self, value=None):
        """Set (exhaustive | partial | unknown | ...) and return completeness."""
        with self._lock:
            if value is not None:
                self._completeness = value
            return self._completeness

    def add_timing(self, name, ms):
        with self._lock:
            self._timings[name] = self._timings.get(name, 0.0) + float(ms)

    def set(self, key, value):
        """Extra keys a component reports (for example as_of_honoured)."""
        with self._lock:
            self._extra[key] = value

    @property
    def status(self):
        with self._lock:
            if self._failed is not None:
                return "failed"
            if self._approval is not None:
                return "approval_required"
            return "degraded" if self._incomplete else "complete"

    def to_dict(self):
        with self._lock:
            result = dict(self._extra)
            result.update(
                status=self.status,
                truncation=dict(self._truncation) if self._truncation else None,
                completeness=self._completeness,
                stages=[dict(s) for s in self._stages],
                timings_ms={k: round(v, 3) for k, v in self._timings.items()},
                incomplete=[dict(i) for i in self._incomplete],
                budget=self.tracker.snapshot() if self.tracker is not None else None,
                warnings=list(self._warnings),
                trace_id=self.trace_id,
            )
            if self._approval not in (None, True):
                result["approval"] = self._approval
            return result


def scrub(value):
    """Keep ids, counts and hashes; replace anything long by its sha256."""
    if isinstance(value, dict):
        return {str(k)[:MAX_DETAIL_CHARS]: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [scrub(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return "sha256:" + hashlib.sha256(bytes(value)).hexdigest()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    if len(text) > MAX_DETAIL_CHARS:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text


class Trace:
    """Detailed record of one operation, kept in memory until flush()."""

    def __init__(self, scope, kind, request=None):
        self.id = str(uuid.uuid4())
        self.scope = scope
        self.kind = kind
        self.request = scrub(request) if request is not None else None
        self._events = []
        self._lock = threading.Lock()
        self._flushed = False

    def event(
        self,
        op,
        statement=None,
        status="ok",
        rows=None,
        ms=None,
        model=None,
        tokens=None,
        cost=None,
        detail=None,
    ):
        entry = dict(
            op=op,
            statement=statement,
            status=status,
            rows=rows,
            ms=None if ms is None else round(float(ms), 3),
            model=model,
            tokens=tokens,
            cost=cost,
            detail=scrub(detail) if detail is not None else None,
        )
        with self._lock:
            self._events.append(entry)
        return entry

    @property
    def events(self):
        with self._lock:
            return [dict(e) for e in self._events]

    def flush(self, execution=None):
        """Write the trace with the app role. Never raises; returns success."""
        if isinstance(execution, Execution):
            execution = execution.to_dict()
        with self._lock:
            events = [dict(e) for e in self._events]
            first = not self._flushed
        try:
            with connect("app") as conn:
                version = 0
                if conn.execute(
                    "SELECT to_regprocedure('api.policy_version()') IS NOT NULL ok"
                ).fetchone()["ok"]:
                    version = conn.execute("SELECT api.policy_version() v").fetchone()[
                        "v"
                    ]
                if first:
                    conn.execute(
                        "INSERT INTO tl.traces(id,token_id,asker,policy_version,kind,request,execution) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                        (
                            self.id,
                            self.scope.token_id,
                            self.scope.asker,
                            version or 0,
                            self.kind,
                            _jsonb(self.request) if self.request is not None else None,
                            _jsonb(execution) if execution is not None else None,
                        ),
                    )
                elif execution is not None:
                    conn.execute(
                        "UPDATE tl.traces SET execution=%s WHERE id=%s",
                        (_jsonb(execution), self.id),
                    )
                written = conn.execute(
                    "SELECT count(*) n FROM tl.trace_events WHERE trace_id=%s",
                    (self.id,),
                ).fetchone()["n"]
                for seq, e in enumerate(events[written:], start=written + 1):
                    conn.execute(
                        "INSERT INTO tl.trace_events(trace_id,seq,op,statement,status,rows,ms,model,tokens,cost,detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            self.id,
                            seq,
                            e["op"],
                            e["statement"],
                            e["status"],
                            e["rows"],
                            e["ms"],
                            e["model"],
                            e["tokens"],
                            e["cost"],
                            _jsonb(e["detail"]) if e["detail"] is not None else None,
                        ),
                    )
            with self._lock:
                self._flushed = True
            return True
        except Exception as exc:  # A trace must never fail the operation.
            log.warning("trace %s was not written: %s", self.id, exc)
            return False
