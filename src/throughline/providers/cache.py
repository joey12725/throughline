"""tl.semantic_cache: derived values keyed by operator, version, model and input.

A cached value can be text derived from messages (a verdict, a score for a
passage). It is therefore read only with a scope: an entry that names source
messages is returned only when every one of them is visible to that scope,
checked in the database through the reader path. Entries of purged messages
are removed by a purge hook.
"""

import hashlib
import json
import logging

import psycopg
from psycopg.types.json import Jsonb

from ..db import connect

log = logging.getLogger("throughline.providers.cache")


def content_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def key(operator, operator_version, model, *parts):
    """Cache key. ``parts`` must pin the input exactly: content hashes, or a
    message id together with its version; never an id alone."""
    if not parts:
        raise ValueError("a cache key needs at least one input part")
    return hashlib.sha256(
        json.dumps(
            [operator, operator_version, model, *parts],
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _sources_visible(ids, scope):
    """True when this scope may see every source message of an entry."""
    try:
        from ..query import reader_transaction
    except ImportError:
        return scope.mode == "owner"
    try:
        with reader_transaction(scope) as conn:
            row = conn.execute(
                "SELECT api.sources_visible(%s)", ([int(i) for i in ids],)
            ).fetchone()
    except psycopg.errors.UndefinedFunction:
        # Before the asker gate exists nothing can prove visibility.
        return scope.mode == "owner"
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return value is True


def get(key, scope):
    """The cached value, or None. ``scope`` is required: there is no unscoped read."""
    if scope is None:
        raise ValueError("cache.get requires a scope")
    with connect("app") as conn:
        row = conn.execute(
            "SELECT value,source_message_ids FROM tl.semantic_cache WHERE key=%s AND (expires_at IS NULL OR expires_at>now())",
            (key,),
        ).fetchone()
    if not row:
        return None
    if row["source_message_ids"] and not _sources_visible(
        row["source_message_ids"], scope
    ):
        return None
    return row["value"]


def put(
    key,
    operator,
    operator_version,
    model,
    input_hash,
    value,
    source_message_ids=(),
    ttl_s=None,
):
    """Store or replace one entry. Returns False (and logs) when it could not
    be written: a cache failure never fails the operation that produced it."""
    try:
        with connect("app") as conn:
            conn.execute(
                "INSERT INTO tl.semantic_cache(key,operator,operator_version,model,input_hash,value,source_message_ids,expires_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,now()+make_interval(secs=>%s))"
                " ON CONFLICT(key) DO UPDATE SET operator=EXCLUDED.operator,operator_version=EXCLUDED.operator_version,model=EXCLUDED.model,input_hash=EXCLUDED.input_hash,value=EXCLUDED.value,source_message_ids=EXCLUDED.source_message_ids,created_at=now(),expires_at=EXCLUDED.expires_at",
                (
                    key,
                    operator,
                    str(operator_version),
                    model,
                    input_hash,
                    Jsonb(value),
                    sorted({int(i) for i in source_message_ids or ()}),
                    None if ttl_s is None else float(ttl_s),
                ),
            )
        return True
    except psycopg.Error as exc:
        log.warning("cache entry was not written: %s", exc.sqlstate)
        return False


def sweep():
    """Delete expired entries; returns how many."""
    with connect("app") as conn:
        return conn.execute(
            "DELETE FROM tl.semantic_cache WHERE expires_at<=now()"
        ).rowcount
