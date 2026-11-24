"""Feeds: named cursors over arrivals (JOB-2, JOB-7 consumer
side, SCN-consumer_exactly_once). Feeds were called consumers; the storage
(tl.consumers, tl.consumer_settings, api.consumer_horizon) keeps that name and
throughline.consumers is a deprecated alias of this module.

A feed is the caller's own working state: a name under its token and
asker, a scope (channels, kinds, thread_ids, persons, include_ignored,
include_versions) and a cursor (seen_to_ingested_at, seen_to_message_id).
Restricted askers may use feeds; what they read passes the database gate.

Arrival-keyed: the cursor is (ingested_at, message_id), never message time,
so a late backfill of old messages is delivered once. api.news delivers only
rows with ingested_at at or before api.consumer_horizon() = now() - settle_s;
the database refuses any transaction that inserts messages and runs longer
than settle_s - margin_s (migration 0046), so a cursor that stops at the
horizon never skips a late commit.

Mark after work: read news, do the work, then mark the last row's cursor.
Delivery is at least once (a crash before mark redelivers the same rows);
marking is exactly once: mark is one transaction with compare-and-set on the
previous position, a `to` at or before the current position is a no-op that
returns the same answer, and a `to` beyond the horizon is refused (409).

Errors: ValueError (400), LookupError (404, the same for a foreign and an
unknown name), ConflictError (409), PermissionError (403).
"""

from datetime import datetime, timezone

from psycopg import sql
from psycopg.types.json import Jsonb

from .db import connect
from .query import query_ex
from .scope import Scope

SCOPE_KEYS = (
    "channels",
    "kinds",
    "thread_ids",
    "persons",
    "include_ignored",
    "include_versions",
)
KINDS = ("life_event", "reference", "page_revision", "derived")
MAX_RESULTS = 5000
DEFAULT_RESULTS = 200


class ConflictError(Exception):
    """The cursor moved (compare-and-set failed) or `to` is not settled."""


def _scope(scope):
    if not isinstance(scope, Scope):
        raise TypeError("feeds require a Scope")
    if scope.mode == "deny":
        raise PermissionError("this scope may not use feeds")
    return scope


def _owner(scope):
    return scope.token_id, scope.asker


def _name(name):
    if not isinstance(name, str) or not 1 <= len(name) <= 100:
        raise ValueError("a feed name has 1 to 100 characters")
    return name


def _time(value, name):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text[-1:] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{name} must be an ISO timestamp") from None
    else:
        raise ValueError(f"{name} must be an ISO timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _list(value, key, kind):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 10000:
        raise ValueError(f"scope.{key} must be a list")
    if kind is int:
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            raise ValueError(f"scope.{key} must hold integers")
    elif not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"scope.{key} must hold names")
    return list(value)


def normalize_scope(value):
    """Validate a feed scope; unknown keys are refused."""
    value = dict(value or {})
    unknown = set(value) - set(SCOPE_KEYS)
    if unknown:
        raise ValueError("unknown feed scope keys: " + ", ".join(sorted(unknown)))
    out = {}
    for key, kind in (
        ("channels", str),
        ("kinds", str),
        ("thread_ids", int),
        ("persons", int),
    ):
        items = _list(value.get(key), key, kind)
        if items is not None:
            out[key] = items
    if "kinds" in out:
        bad = [k for k in out["kinds"] if k not in KINDS]
        if bad:
            raise ValueError("unknown record kinds: " + ", ".join(bad))
    for key, default in (("include_ignored", False), ("include_versions", True)):
        if key in value and value[key] is not None:
            if not isinstance(value[key], bool):
                raise ValueError(f"scope.{key} must be a boolean")
            out[key] = value[key]
        else:
            out[key] = default
    return out


def _position(row):
    if row["seen_to_ingested_at"] is None:
        return None
    return dict(
        ingested_at=row["seen_to_ingested_at"], message_id=row["seen_to_message_id"]
    )


def _public(row):
    return dict(
        name=row["name"],
        scope=row["scope"],
        seen_to=_position(row),
        marks=row["marks"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row(conn, scope, name, lock=False):
    token_id, asker = _owner(scope)
    row = conn.execute(
        "SELECT * FROM tl.consumers WHERE name=%s AND token_id IS NOT DISTINCT FROM %s AND asker IS NOT DISTINCT FROM %s"
        + (" FOR UPDATE" if lock else ""),
        (_name(name), token_id, asker),
    ).fetchone()
    if not row:
        raise LookupError("feed not found")
    return row


def create(scope, name, consumer_scope=None, start="now"):
    """Create a feed. start: 'now' (arrivals after the current horizon),
    'beginning' (every arrival) or an ISO timestamp (arrivals after it)."""
    scope = _scope(scope)
    name = _name(name)
    cleaned = normalize_scope(consumer_scope)
    token_id, asker = _owner(scope)
    with connect("app") as conn:
        if start in (None, "now"):
            at = conn.execute("SELECT api.consumer_horizon() AS h").fetchone()["h"]
        elif start == "beginning":
            at = None
        else:
            at = _time(start, "start")
        row = conn.execute(
            "INSERT INTO tl.consumers(name,token_id,asker,scope,seen_to_ingested_at,seen_to_message_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *",
            (name, token_id, asker, Jsonb(cleaned), at, None if at is None else 0),
        ).fetchone()
        if row is None:
            raise ConflictError("a feed with this name exists")
        return _public(row)


def list_feeds(scope):
    scope = _scope(scope)
    token_id, asker = _owner(scope)
    with connect("app") as conn:
        rows = conn.execute(
            "SELECT * FROM tl.consumers WHERE token_id IS NOT DISTINCT FROM %s AND asker IS NOT DISTINCT FROM %s ORDER BY name",
            (token_id, asker),
        ).fetchall()
    return [_public(r) for r in rows]


def get(scope, name):
    scope = _scope(scope)
    with connect("app") as conn:
        return _public(_row(conn, scope, name))


def drop(scope, name):
    scope = _scope(scope)
    with connect("app") as conn:
        row = _row(conn, scope, name, lock=True)
        conn.execute("DELETE FROM tl.consumers WHERE id=%s", (row["id"],))
    return dict(name=row["name"], dropped=True)


def _limit(max_results):
    if max_results is None:
        return DEFAULT_RESULTS
    try:
        value = int(max_results)
    except (TypeError, ValueError):
        raise ValueError("max must be an integer") from None
    return max(1, min(value, MAX_RESULTS))


def news(scope, name, max_results=None):
    """A page of arrivals for the feed (api.news through the reader
    path) -> {rows, feed, consumer (same name, kept for compatibility),
    next_cursor, row_count, execution}."""
    scope = _scope(scope)
    get(scope, name)  # 404 before reading
    limit = _limit(max_results)
    statement = (
        sql.SQL("SELECT * FROM api.news({},{})")
        .format(sql.Literal(name), sql.Literal(limit))
        .as_string()
    )
    result = query_ex(statement, limit, scope=scope)
    rows = result["rows"]
    return dict(
        feed=name,
        consumer=name,
        rows=rows,
        row_count=len(rows),
        truncated=result["truncated"],
        next_cursor=rows[-1]["cursor"] if rows else None,
        execution=result["execution"],
    )


def pending(scope, name):
    scope = _scope(scope)
    get(scope, name)
    statement = (
        sql.SQL("SELECT * FROM api.pending({})").format(sql.Literal(name)).as_string()
    )
    rows = query_ex(statement, 1, scope=scope)["rows"]
    if not rows:
        raise LookupError("feed not found")
    row = dict(rows[0])
    row.setdefault("feed", row.get("consumer"))
    return row


def _pending_count(scope, name):
    try:
        return pending(scope, name)["pending"]
    except Exception:
        return None


def _cursor(value, name):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be {{ingested_at, message_id}}")
    at = _time(value.get("ingested_at"), f"{name}.ingested_at")
    message_id = value.get("message_id")
    if message_id is None:
        message_id = 0
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id < 0
    ):
        raise ValueError(f"{name}.message_id must be a non-negative integer")
    return at, message_id


def mark(scope, name, to, expected=None, receipt=None):
    """Move the cursor to `to` {ingested_at, message_id}.

    One transaction: the row is locked; a `to` at or before the current
    position is a no-op returning the current position; otherwise `expected`
    (when given) must equal the current position (compare-and-set) and `to`
    must be at or before api.consumer_horizon(). Returns {seen_to, marked,
    pending_after}; marked is the feed's count of advancing marks, so a
    repeated mark returns the same answer. `receipt`, when given, is called
    as receipt(conn, consumer_id) inside the same transaction, after the
    compare-and-set, and its answer is returned under the key 'receipt': the
    cursor it describes is the one this call left, never a later mark's.
    """
    scope = _scope(scope)
    target = _cursor(to, "to")
    if target is None:
        raise ValueError("to is required")
    wanted = _cursor(expected, "expected")
    with connect("app") as conn:
        row = _row(conn, scope, name, lock=True)
        current = None
        if row["seen_to_ingested_at"] is not None:
            current = (row["seen_to_ingested_at"], row["seen_to_message_id"] or 0)
        if current is not None and target <= current:
            pass  # already marked: same answer
        else:
            if expected is not None and wanted != current:
                raise ConflictError("the cursor moved; read news again")
            horizon = conn.execute("SELECT api.consumer_horizon() AS h").fetchone()["h"]
            if target[0] > horizon:
                raise ConflictError("to is beyond the settle horizon")
            updated = conn.execute(
                """UPDATE tl.consumers SET seen_to_ingested_at=%s,seen_to_message_id=%s,marks=marks+1,updated_at=now()
                   WHERE id=%s AND seen_to_ingested_at IS NOT DISTINCT FROM %s AND seen_to_message_id IS NOT DISTINCT FROM %s
                   RETURNING *""",
                (
                    target[0],
                    target[1],
                    row["id"],
                    row["seen_to_ingested_at"],
                    row["seen_to_message_id"],
                ),
            ).fetchone()
            if updated is None:
                raise ConflictError("the cursor moved; read news again")
            row = updated
        stored = receipt(conn, row["id"]) if receipt is not None else None
    result = dict(
        seen_to=_position(row),
        marked=row["marks"],
        pending_after=_pending_count(scope, name),
    )
    if receipt is not None:
        result["receipt"] = stored
    return result


def read_settings(conn=None):
    def read(c):
        row = c.execute(
            "SELECT settle_s,margin_s FROM tl.consumer_settings WHERE one"
        ).fetchone()
        return dict(row) if row else dict(settle_s=120, margin_s=15)

    if conn is not None:
        return read(conn)
    with connect("app") as c:
        return read(c)


def settings(conn, owner, settle_s, margin_s):
    """Change the settle window (owner only). The table's constraints
    (margin_s >= 0, settle_s > margin_s, settle_s >= 2) are the rule; a
    violation is a ValueError."""
    if not owner:
        raise PermissionError("only the owner changes feed settings")
    for key, value in (("settle_s", settle_s), ("margin_s", margin_s)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{key} must be an integer")
    import psycopg

    try:
        with conn.transaction():
            row = conn.execute(
                "UPDATE tl.consumer_settings SET settle_s=%s,margin_s=%s WHERE one RETURNING settle_s,margin_s",
                (settle_s, margin_s),
            ).fetchone()
    except psycopg.errors.CheckViolation:
        raise ValueError(
            "settle_s must be at least 2 and above margin_s, and margin_s at least 0"
        ) from None
    if row is None:
        raise LookupError("feed settings are missing; run tl.reseed()")
    return dict(row)


list_consumers = list_feeds  # deprecated name
