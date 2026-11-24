"""Classifiers: annotations written by Jev, the configured LLM or
agent-written Python, over messages, whole threads and their participants
(migration 0068, docs/v11/classifiers.md).

    from throughline import classifiers
    classifiers.define(conn, token, {...})      # message or python kind
    classifiers.try_(conn, token, {...}, {"limit": 5})
    classifiers.tick()                          # the maintenance timer
"""

from . import bridge, check, groups, registry, sandbox, threads
from .engine import (
    KINDS,
    STATUSES,
    ClassifierError,
    backfill,
    define,
    get,
    list_classifiers,
    results,
    retract_all,
    run,
    set_status,
    tick,
    try_,
)
from .guide import guide

__all__ = [
    "KINDS",
    "STATUSES",
    "ClassifierError",
    "backfill",
    "bridge",
    "check",
    "define",
    "get",
    "groups",
    "guide",
    "list_classifiers",
    "registry",
    "results",
    "retract_all",
    "run",
    "sandbox",
    "set_status",
    "threads",
    "tick",
    "try_",
]


def _receipts():
    import hashlib as _h
    import json as _j

    from .. import writes

    def make(table, status_sql):
        def fn(conn, ident):
            row = conn.execute(f"SELECT *,{status_sql} AS _status FROM tl.{table} WHERE {'name' if table == 'annotation_attributes' else 'id'}=%s",
                               (ident,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            digest = _h.sha256(_j.dumps({k: v for k, v in row.items() if k != "_status"}, sort_keys=True, default=str).encode()).hexdigest()
            return {"status": row["_status"], "stored_hash": digest, "version": row.get("version", ident),
                    "written_at": row.get("updated_at") or row.get("created_at")}
        return fn

    writes.register_receipt("classifier", make("classifiers", "status"))
    writes.register_receipt("group_annotation", make("group_annotations", "status"))
    writes.register_receipt("annotation_attribute", make("annotation_attributes", "status"))


_receipts()
