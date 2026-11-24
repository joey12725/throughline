"""Result sets: asker-scoped working sets of record ids (QRY-34, QRY-35).

A result set is the caller's own temporary working state. It stores record
ids only (prefix, ref_id, the pinned message id and version, span, score,
role, groups, discovery and short operator output in meta) and never record
text. Every read joins the members live to the gated record views registered
in tl.record_sources (api.result_set in migration 0039), so content and
visibility are always current: a member hidden from the asker now is dropped
from members, reads, counts and inspect views alike (ACL-9).

A set belongs to the token and asker that created it and is valid only under
the access policy version it was written with and until it expires (ACL-10).
Another caller's handle raises ResultSetNotFound, exactly like an unknown one;
a set from an earlier policy version raises ResultSetStale.

The application role writes the four tables; content is read only through
query.reader_transaction(scope). Every public function takes the scope as its
first argument. No transaction is held between calls.
"""

import json
import os
import random
import re
import secrets
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .budget import estimate_tokens
from .db import connect
from .execution import Execution, scrub
from .query import reader_transaction
from .scope import Scope

HANDLE = re.compile(r"^rs_[0-9a-f]{32}$")
RECORD_ID = re.compile(r"^([a-z]{1,8}):([0-9]{1,19})$")
RELATION = re.compile(r"^api\.([a-z_]+)$")
COMPLETENESS = ("exhaustive", "ranked_sample", "truncated")
# Worse completeness wins when inputs are combined.
_RANK = {"exhaustive": 0, "truncated": 1, "ranked_sample": 2}
COUNT_KEYS = (
    "retrieved",
    "mechanically_processed",
    "semantically_evaluated",
    "inspected_by_controller",
    "returned",
)
CONTRACT = (
    "record_id",
    "kind",
    "ref_id",
    "message_id",
    "version",
    "is_current",
    "channel",
    "external_id",
    "thread_id",
    "thread_no",
    "content",
    "record_time",
    "ended_at",
    "ingested_at",
    "subject_from",
    "subject_to",
    "subject_from_precision",
    "subject_to_precision",
    "person_id",
    "author_handle",
    "lat",
    "lon",
    "loc_reason",
    "is_outside",
    "heading_path",
    "char_start",
    "char_end",
    "claim",
    "generated",
    "unsupported",
    "provenance",
    "support",
    "expansion",
)
SET_FIELDS = ("score", "role", "depends_on", "groups", "discovery", "meta")
# Record text a member dict never carries.
_TEXT_COLUMNS = ("content", "claim")
DEFAULT_TTL_S = 86400
MAX_TTL_S = 7 * 86400
MAX_META_CHARS = 500
MAX_MEMBERS = 100000
OPS = (
    "union",
    "intersect",
    "subtract",
    "filter",
    "order",
    "head",
    "sample",
    "dedup",
    "group",
    "ungroup",
)
GROUP_BY = (
    "conversation",
    "thread",
    "channel",
    "day",
    "month",
    "year",
    "person",
    "support",
    "explicit",
    "episode",
)
VIEWS = (
    "summary",
    "sample",
    "by_time",
    "by_channel",
    "by_person",
    "groups",
    "missing_link",
    "origin",
    "history",
)
EXCERPT_CHARS = 200


class ResultSetNotFound(LookupError):
    """Unknown, expired or someone else's set (or session, or record)."""


class ResultSetStale(RuntimeError):
    """The access policy changed since the set (or session) was written."""


# --- helpers ---------------------------------------------------------------


def _scope(scope):
    if not isinstance(scope, Scope):
        raise TypeError("result sets require a Scope")
    return scope


def default_ttl_s():
    try:
        value = int(os.getenv("THROUGHLINE_RESULT_SET_TTL_S", DEFAULT_TTL_S))
    except ValueError:
        value = DEFAULT_TTL_S
    return max(1, min(value, MAX_TTL_S))


def _ttl(ttl_s):
    if ttl_s is None:
        return default_ttl_s()
    ttl_s = int(ttl_s)
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    return min(ttl_s, MAX_TTL_S)


def _get(item, key, default=None):
    if isinstance(item, dict):
        value = item.get(key, default)
    else:
        value = getattr(item, key, default)
    return default if value is None else value


def _plain(value):
    """JSON-safe copy (datetimes become ISO strings)."""
    return json.loads(json.dumps(value, default=_json_default, ensure_ascii=False))


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def _short(value, limit=MAX_META_CHARS):
    """Whether every string and key in value is at most limit characters."""
    if isinstance(value, str):
        return len(value) <= limit
    if isinstance(value, dict):
        return all(len(str(k)) <= limit and _short(v, limit) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(_short(v, limit) for v in value)
    return True


def _time(value, name):
    if value is None or isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO timestamp") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_record_id(record_id):
    match = RECORD_ID.match(record_id) if isinstance(record_id, str) else None
    if not match:
        raise ValueError(f"invalid record id: {str(record_id)[:40]}")
    return match.group(1), int(match.group(2))


def _owner_key(scope):
    return scope.token_id, scope.asker or None


def _load_set(conn, scope, handle, allow_stale=False):
    if not isinstance(handle, str) or not HANDLE.match(handle):
        raise ResultSetNotFound("result set not found")
    row = conn.execute(
        "SELECT s.*,coalesce(t.revoked_at IS NOT NULL,false) AS revoked,s.expires_at<=now() AS expired,api.policy_version() AS current_policy FROM tl.result_sets s LEFT JOIN tl.tokens t ON t.id=s.token_id WHERE s.handle=%s",
        (handle,),
    ).fetchone()
    if (
        not row
        or (row["token_id"], row["asker"]) != _owner_key(scope)
        or row["revoked"]
        or row["expired"]
    ):
        raise ResultSetNotFound("result set not found")
    if row["policy_version"] != row["current_policy"] and not allow_stale:
        raise ResultSetStale("access policy changed since this result set")
    return row


def _load_session(conn, scope, session_id):
    try:
        session_id = str(uuid.UUID(str(session_id)))
    except ValueError:
        raise ResultSetNotFound("session not found") from None
    row = conn.execute(
        "SELECT s.*,coalesce(t.revoked_at IS NOT NULL,false) AS revoked,s.expires_at<=now() AS expired,api.policy_version() AS current_policy FROM tl.sessions s LEFT JOIN tl.tokens t ON t.id=s.token_id WHERE s.id=%s",
        (session_id,),
    ).fetchone()
    if (
        not row
        or (row["token_id"], row["asker"]) != _owner_key(scope)
        or row["revoked"]
        or row["expired"]
    ):
        raise ResultSetNotFound("session not found")
    if row["policy_version"] != row["current_policy"]:
        raise ResultSetStale("access policy changed since this session")
    return row


def _registry():
    """{prefix: (schema, view)} of the registered record views."""
    with connect("app") as conn:
        rows = conn.execute(
            "SELECT prefix,relation FROM tl.record_sources WHERE to_regclass(relation) IS NOT NULL ORDER BY prefix"
        ).fetchall()
    found = {}
    for row in rows:
        match = RELATION.match(row["relation"])
        if match and re.fullmatch(r"[a-z]{1,8}", row["prefix"]):
            found[row["prefix"]] = ("api", match.group(1))
    return found


def _info(row, visible):
    counts = dict(row["counts"] or {})
    counts["returned"] = visible
    return dict(
        handle=row["handle"],
        session_id=str(row["session_id"]) if row["session_id"] else None,
        completeness=row["completeness"],
        counts=counts,
        origin=row["origin"],
        as_of=row["as_of"],
        known_by=row["known_by"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        member_count=visible,
    )


def _rows(scope, handle):
    """Visible members with content, through the gated api.result_set."""
    with reader_transaction(scope) as conn:
        return conn.execute(
            "SELECT * FROM api.result_set(%s) ORDER BY ordinal", (handle,)
        ).fetchall()


def _member(row):
    member = {k: v for k, v in row.items() if k not in _TEXT_COLUMNS}
    member.pop("set_completeness", None)
    member["prefix"] = row["record_id"].split(":", 1)[0]
    member["span"] = (
        dict(first_no=row["span_first"], last_no=row["span_last"])
        if row["span_first"] is not None or row["span_last"] is not None
        else None
    )
    return member


def _record(row):
    record = {k: row[k] for k in CONTRACT}
    record.update(
        span=dict(first_no=row["span_first"], last_no=row["span_last"])
        if row["span_first"] is not None or row["span_last"] is not None
        else None,
        conversation=None,
        score=row["score"],
        discovery=row["discovery"],
        role=row["role"],
        depends_on=row["depends_on"],
        groups=row["groups"],
        meta=row["meta"],
        ordinal=row["ordinal"],
        tokens=estimate_tokens(row["content"] or ""),
    )
    return record


def _as_dict(value):
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    import dataclasses

    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return dict(vars(value))


def expire():
    """Delete expired sets and sessions; returns how many of each."""
    with connect("app") as conn:
        sets = conn.execute(
            "DELETE FROM tl.result_sets WHERE expires_at<=now()"
        ).rowcount
        sessions = conn.execute(
            "DELETE FROM tl.sessions WHERE expires_at<=now()"
        ).rowcount
    return dict(sets=sets, sessions=sessions)


# --- sessions --------------------------------------------------------------


def open_session(scope, note=None, ttl_s=None):
    """Start an exploration session; its sets form the session's history."""
    _scope(scope)
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        raise ValueError("note must be text of at most 500 characters")
    ttl = _ttl(ttl_s)
    expire()
    with connect("app") as conn:
        row = conn.execute(
            "INSERT INTO tl.sessions(id,token_id,asker,policy_version,note,expires_at) VALUES (%s,%s,%s,api.policy_version(),%s,now()+make_interval(secs=>%s)) RETURNING id,note,unresolved,created_at,expires_at",
            (str(uuid.uuid4()), scope.token_id, scope.asker or None, note, ttl),
        ).fetchone()
    return dict(row, id=str(row["id"]), sets=[])


def session(scope, session_id):
    """The session with its sets in creation order and its unresolved needs."""
    _scope(scope)
    with connect("app") as conn:
        row = _load_session(conn, scope, session_id)
        sets = conn.execute(
            "SELECT handle,origin,completeness,counts,as_of,known_by,created_at,expires_at FROM tl.result_sets WHERE session_id=%s AND expires_at>now() ORDER BY created_at,handle",
            (row["id"],),
        ).fetchall()
    # Counts are those of the members visible now, as info() reports them.
    visible = {}
    if sets:
        with reader_transaction(scope) as conn:
            visible = {
                r["handle"]: r
                for r in conn.execute(
                    "SELECT i.handle,i.counts FROM unnest(%s::text[]) h CROSS JOIN LATERAL api.result_set_info(h) i",
                    ([item["handle"] for item in sets],),
                ).fetchall()
            }
    for item in sets:
        item["parents"] = list((item["origin"] or {}).get("parents") or [])
        if item["handle"] in visible:
            item["counts"] = visible[item["handle"]]["counts"]
    return dict(
        id=str(row["id"]),
        note=row["note"],
        unresolved=row["unresolved"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        sets=sets,
    )


def set_unresolved(scope, session_id, items):
    """Replace the session's list of unresolved needs (short items only)."""
    _scope(scope)
    if not isinstance(items, (list, tuple)):
        raise ValueError("unresolved must be a list")
    items = _plain(list(items))
    if not _short(items):
        raise ValueError(f"unresolved items are limited to {MAX_META_CHARS} characters")
    with connect("app") as conn:
        row = _load_session(conn, scope, session_id)
        conn.execute(
            "UPDATE tl.sessions SET unresolved=%s WHERE id=%s",
            (Jsonb(items), row["id"]),
        )
    return session(scope, session_id)


# --- creation --------------------------------------------------------------


def _groups(value):
    if value is not None and not isinstance(value, (list, tuple)):
        raise ValueError("groups is a list of names or {grp, certain}")
    found = {}
    for item in value or ():
        if isinstance(item, str):
            name, certain = item, True
        elif isinstance(item, dict):
            name = item.get("grp", item.get("name"))
            certain = bool(item.get("certain", True))
        else:
            raise ValueError("a group is a name or {grp, certain}")
        if not isinstance(name, str) or not 1 <= len(name) <= 200:
            raise ValueError("group names have 1 to 200 characters")
        # A member is in a group once; certain wins over tentative.
        found[name] = found.get(name, False) or certain
    return found


def _span(item):
    span = _get(item, "span")
    first = last = None
    if isinstance(span, dict):
        first, last = span.get("first_no"), span.get("last_no")
    elif span is not None:
        first, last = _get(span, "first_no"), _get(span, "last_no")
    first = _get(item, "span_first", first)
    last = _get(item, "span_last", last)
    return (
        None if first is None else int(first),
        None if last is None else int(last),
    )


def _prepare(item, ordinal):
    prefix, ref_id = _parse_record_id(_get(item, "record_id"))
    version = _get(item, "version")
    message_id = ref_id if prefix == "m" else _get(item, "message_id")
    meta = _get(item, "meta", {})
    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")
    meta = _plain(meta)
    if not _short(meta):
        raise ValueError(
            f"meta values are limited to {MAX_META_CHARS} characters (no record text)"
        )
    expansion = _get(item, "expansion")
    if expansion is not None and "expansion" not in meta:
        # Expansion refs are ids; anything long is kept as a hash only.
        meta["expansion"] = scrub(_plain(expansion))
    discovery = _get(item, "discovery", [])
    if not isinstance(discovery, (list, tuple)):
        discovery = [discovery]
    depends_on = _get(item, "depends_on")
    if depends_on is not None:
        _parse_record_id(depends_on)
    score = _get(item, "score")
    first, last = _span(item)
    return dict(
        ordinal=ordinal,
        prefix=prefix,
        ref_id=ref_id,
        record_id=f"{prefix}:{ref_id}",
        message_id=None if message_id is None else int(message_id),
        version=None if version is None else int(version),
        span_first=first,
        span_last=last,
        score=None if score is None else float(score),
        role=str(_get(item, "role", "hit")),
        depends_on=depends_on,
        discovery=scrub(_plain(list(discovery))),
        meta=meta,
        groups=_groups(_get(item, "groups", [])),
    )


def resolve(scope, record_ids):
    """Visible record rows for caller-supplied ids, through the gated views.

    Returns {record_id: {record_id, ref_id, message_id, version}} with an
    entry only for records the scope can see now; an unknown prefix, a missing
    record and a hidden record are all simply absent.
    """
    _scope(scope)
    wanted = {}
    for record_id in record_ids:
        prefix, ref_id = _parse_record_id(record_id)
        wanted.setdefault(prefix, set()).add(ref_id)
    registry = _registry() if wanted else {}
    found = {}
    if not any(prefix in registry for prefix in wanted):
        return found
    with reader_transaction(scope) as conn:
        for prefix, ids in sorted(wanted.items()):
            if prefix not in registry:
                continue
            rows = conn.execute(
                sql.SQL(
                    "SELECT record_id::text AS record_id,ref_id::bigint AS ref_id,message_id::bigint AS message_id,version::int AS version FROM {} WHERE ref_id=ANY(%s)"
                ).format(sql.Identifier(*registry[prefix])),
                (sorted(ids),),
            ).fetchall()
            for row in rows:
                found[row["record_id"]] = row
    return found


def create(
    scope,
    records,
    *,
    origin=None,
    completeness,
    counts=None,
    session=None,
    as_of=None,
    known_by=None,
    ttl_s=None,
):
    """Store a new set of the given records (dicts or objects); returns its handle.

    Each record needs a record_id ('m:12'); version, message_id, span
    ({first_no, last_no}), score, role, depends_on, groups (names or
    {grp, certain}), discovery, meta and expansion are taken when present. A
    message record without a version is pinned to the version the scope sees
    now (ResultSetNotFound when it sees none). Records are not otherwise
    checked here: whatever the scope cannot see is dropped on every read.
    counts keeps retrieved, mechanically_processed, semantically_evaluated,
    inspected_by_controller and returned apart (plus optional numbers or
    objects of numbers such as per_leg); returned is always the member count.
    """
    _scope(scope)
    if completeness not in COMPLETENESS:
        raise ValueError("completeness must be " + ", ".join(COMPLETENESS))
    records = list(records or ())
    if len(records) > MAX_MEMBERS:
        raise ValueError(f"a result set holds at most {MAX_MEMBERS} members")
    members = [_prepare(item, n) for n, item in enumerate(records)]
    # A message member must be pinned to a version; other kinds get their
    # message id (for purge) and version from the view when it shows them.
    unpinned = [m for m in members if m["version"] is None or m["message_id"] is None]
    if unpinned:
        seen = resolve(scope, [m["record_id"] for m in unpinned])
        for m in unpinned:
            row = seen.get(m["record_id"])
            if row is None:
                if m["prefix"] == "m" and m["version"] is None:
                    raise ResultSetNotFound("record not found")
                continue
            if m["version"] is None:
                m["version"] = row["version"]
            if m["message_id"] is None:
                m["message_id"] = row["message_id"]
    origin = _plain(dict(origin or {"op": "create"}))
    if not _short(origin):
        raise ValueError(
            f"origin values are limited to {MAX_META_CHARS} characters (no record text)"
        )
    total = dict.fromkeys(COUNT_KEYS, 0)
    total["retrieved"] = len(members)

    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    for key, value in dict(counts or {}).items():
        # A count, or one level of named counts (for example per leg).
        if not (
            number(value)
            or (
                isinstance(value, dict)
                and all(number(v) and len(str(k)) <= 100 for k, v in value.items())
            )
        ):
            raise ValueError("counts are numbers or objects of numbers")
        if len(str(key)) > 100:
            raise ValueError("count names have at most 100 characters")
        total[str(key)] = value
    total["returned"] = len(members)
    ttl = _ttl(ttl_s)
    handle = "rs_" + secrets.token_hex(16)
    expire()
    try:
        with connect("app") as conn:
            session_id = None
            limit = None
            if session is not None:
                row = _load_session(conn, scope, session)
                session_id, limit = row["id"], row["expires_at"]
            conn.execute(
                "INSERT INTO tl.result_sets(handle,session_id,token_id,asker,policy_version,origin,completeness,counts,as_of,known_by,expires_at) VALUES (%s,%s,%s,%s,api.policy_version(),%s,%s,%s,%s,%s,least(now()+make_interval(secs=>%s),coalesce(%s,'infinity'::timestamptz)))",
                (
                    handle,
                    session_id,
                    scope.token_id,
                    scope.asker or None,
                    Jsonb(origin),
                    completeness,
                    Jsonb(total),
                    _time(as_of, "as_of"),
                    _time(known_by, "known_by"),
                    ttl,
                    limit,
                ),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO tl.result_set_members(handle,ordinal,prefix,ref_id,record_id,message_id,version,span_first,span_last,score,role,depends_on,discovery,meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            handle,
                            m["ordinal"],
                            m["prefix"],
                            m["ref_id"],
                            m["record_id"],
                            m["message_id"],
                            m["version"],
                            m["span_first"],
                            m["span_last"],
                            m["score"],
                            m["role"],
                            m["depends_on"],
                            Jsonb(m["discovery"]),
                            Jsonb(m["meta"]),
                        )
                        for m in members
                    ],
                )
                groups = [
                    (handle, m["ordinal"], name, certain)
                    for m in members
                    for name, certain in sorted(m["groups"].items())
                ]
                if groups:
                    cursor.executemany(
                        "INSERT INTO tl.result_set_groups(handle,ordinal,grp,certain) VALUES (%s,%s,%s,%s)",
                        groups,
                    )
    except psycopg.errors.ForeignKeyViolation:
        # A pinned message that does not exist reads like a hidden one.
        raise ResultSetNotFound("record not found") from None
    except psycopg.errors.CheckViolation as error:
        raise ValueError(
            "result set rejected: " + (error.diag.constraint_name or "check failed")
        ) from None
    return handle


def drop(scope, handle):
    """Delete one of the caller's sets (also a stale one)."""
    _scope(scope)
    with connect("app") as conn:
        _load_set(conn, scope, handle, allow_stale=True)
        conn.execute("DELETE FROM tl.result_sets WHERE handle=%s", (handle,))
    return dict(handle=handle, dropped=True)


# --- reading ---------------------------------------------------------------


def _checked_rows(scope, handle):
    with connect("app") as conn:
        row = _load_set(conn, scope, handle)
    return row, _rows(scope, handle)


def members(scope, handle):
    """Visible members without record text, in ordinal order."""
    _scope(scope)
    _, rows = _checked_rows(scope, handle)
    return [_member(r) for r in rows]


def info(scope, handle):
    """Completeness, counts, origin, as_of and lifetime of a set; counts and
    member_count include only the members visible now."""
    _scope(scope)
    row, rows = _checked_rows(scope, handle)
    return _info(row, len(rows))


def _hydrate(scope, rows):
    try:
        from .retrieval.hydrate import records_for_members
    except ImportError:
        return [_record(r) for r in rows]
    # The hydrator returns records in member order and leaves out any it
    # cannot show, so its output is a subsequence of rows: pair them in order
    # (a set may hold the same record twice, at different ordinals).
    hydrated = iter(
        _as_dict(v)
        for v in records_for_members(scope, [_member(r) for r in rows]) or ()
    )
    records = []
    record = next(hydrated, None)
    for row in rows:
        if record is None:
            break
        if (record.get("record_id"), record.get("version")) != (
            row["record_id"],
            row["version"],
        ):
            continue
        current, record = record, next(hydrated, None)
        for key in SET_FIELDS:
            if current.get(key) in (None, [], {}):
                current[key] = row[key]
        current.setdefault("span", _member(row)["span"])
        current["ordinal"] = row["ordinal"]
        records.append(current)
    return records


def read(scope, handle, limit=None, offset=0, token_budget=None):
    """Full records of a set (the shared hydrator when present), its info
    and execution metadata. limit/offset page over visible members;
    token_budget stops before the record that would exceed it."""
    _scope(scope)
    execution = Execution()
    row, rows = _checked_rows(scope, handle)
    offset = max(0, int(offset or 0))
    page = (
        rows[offset:] if limit is None else rows[offset : offset + max(0, int(limit))]
    )
    records = _hydrate(scope, page)
    if token_budget is not None:
        kept, spent = [], 0
        for record in records:
            spent += estimate_tokens(
                json.dumps(record, default=str, ensure_ascii=False)
            )
            if spent > token_budget:
                execution.set_truncation("token_budget", token_budget)
                break
            kept.append(record)
        records = kept
    following = offset + len(records)
    execution.completeness(row["completeness"])
    execution.stage("result_set", rows=len(records), members=len(rows))
    return dict(
        records=records,
        info=_info(row, len(rows)),
        next_offset=following if following < len(rows) else None,
        execution=execution.to_dict(),
    )


# --- transforms ------------------------------------------------------------


def _identity(member):
    return member["record_id"], member["version"]


def _worst(values):
    return max(values, key=lambda v: _RANK[v]) if values else "exhaustive"


def _check_params(op, params, allowed):
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise ValueError(f"{op}: unknown parameter " + ", ".join(unknown))


# Filter keys evaluated through one api.attribution statement over the
# message members; each takes a list of values.
ATTRIBUTION_FILTERS = ("identity_quality", "time_precision", "attribution_status")


def _attribution(scope, items):
    """{message_id: api.attribution row} for the message members."""
    ids = sorted(
        {
            m["message_id"]
            for m in items
            if m.get("prefix") == "m" and m.get("message_id") is not None
        }
    )
    if not ids:
        return {}
    with reader_transaction(scope) as conn:
        rows = conn.execute(
            "SELECT * FROM api.attribution(%s::bigint[])", (ids,)
        ).fetchall()
    return {r["message_id"]: r for r in rows}


def _filter(items, params, scope=None):
    _check_params(
        "filter",
        params,
        ("kinds", "channels", "after", "before", "min_score", "role", "grp", "meta")
        + ATTRIBUTION_FILTERS,
    )
    kinds = set(params["kinds"]) if params.get("kinds") is not None else None
    channels = set(params["channels"]) if params.get("channels") is not None else None
    after = _time(params.get("after"), "after")
    before = _time(params.get("before"), "before")
    min_score = params.get("min_score")
    meta = params.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("filter: meta is an object of key: value")
    wanted = {}
    for key in ATTRIBUTION_FILTERS:
        value = params.get(key)
        if value is None:
            continue
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(v, str) for v in value
        ):
            raise ValueError(f"filter: {key} must be a list of names")
        wanted[key] = set(value)
    attribution = _attribution(scope, items) if wanted else {}

    def attributed(m):
        # Non-message members are kept only when no attribution key is given.
        if not wanted:
            return True
        row = attribution.get(m.get("message_id")) if m.get("prefix") == "m" else None
        if row is None:
            return False
        return all(row.get(key) in values for key, values in wanted.items())

    def keep(m):
        if kinds is not None and m["kind"] not in kinds:
            return False
        if channels is not None and m["channel"] not in channels:
            return False
        if after is not None and (m["record_time"] is None or m["record_time"] < after):
            return False
        if before is not None and (
            m["record_time"] is None or m["record_time"] >= before
        ):
            return False
        if min_score is not None and (m["score"] is None or m["score"] < min_score):
            return False
        if params.get("role") is not None and m["role"] != params["role"]:
            return False
        if params.get("grp") is not None and params["grp"] not in {
            g["grp"] for g in m["groups"] or ()
        }:
            return False
        if not attributed(m):
            return False
        return all((m["meta"] or {}).get(k) == v for k, v in meta.items())

    return [m for m in items if keep(m)]


def _order(items, params):
    _check_params("order", params, ("by", "desc"))
    by = params.get("by", "score")
    if by not in ("score", "record_time", "ordinal", "record_id"):
        raise ValueError("order: by is score, record_time, ordinal or record_id")
    desc = bool(params.get("desc", by == "score"))
    present = [m for m in items if m[by] is not None]
    missing = [m for m in items if m[by] is None]
    present.sort(key=lambda m: m[by], reverse=desc)
    return present + missing


def _overlaps(a, b):
    if a["thread_id"] is None or a["thread_id"] != b["thread_id"]:
        return False
    a1 = a["span_first"] if a["span_first"] is not None else a["thread_no"]
    a2 = a["span_last"] if a["span_last"] is not None else a["thread_no"]
    b1 = b["span_first"] if b["span_first"] is not None else b["thread_no"]
    b2 = b["span_last"] if b["span_last"] is not None else b["thread_no"]
    if None in (a1, a2, b1, b2):
        return False
    return a1 <= b2 and b1 <= a2


def _support_key(m):
    support = m.get("support") or []
    keys = frozenset(
        (s.get("message_id"), s.get("version"))
        for s in support
        if isinstance(s, dict) and s.get("message_id") is not None
    )
    return keys or None


def _dedup(items, params):
    _check_params("dedup", params, ("by",))
    by = params.get("by", "record_id")
    kept = []
    if by == "record_id":
        seen = set()
        for m in items:
            if _identity(m) not in seen:
                seen.add(_identity(m))
                kept.append(m)
    elif by == "span":
        for m in items:
            if not any(_overlaps(m, k) for k in kept):
                kept.append(m)
    elif by == "support":
        seen = set()
        for m in items:
            key = _support_key(m)
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            kept.append(m)
    else:
        raise ValueError("dedup: by is record_id, span or support")
    return kept


_PRECISION = {
    "exact": 0,
    "day": 1,
    "month": 2,
    "year": 3,
    "approximate": 4,
    "unknown": 5,
}
_UNIT = {"day": 1, "month": 2, "year": 3}


def _bucket(when, unit):
    when = when.astimezone(timezone.utc)
    if unit == "day":
        return when.strftime("%Y-%m-%d")
    if unit == "month":
        return when.strftime("%Y-%m")
    return when.strftime("%Y")


def _buckets(start, end, unit, cap=24):
    """Bucket labels from start to end inclusive (at most cap)."""
    labels = []
    current = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    while len(labels) < cap:
        label = _bucket(current, unit)
        if label not in labels:
            labels.append(label)
        if label == _bucket(end, unit):
            break
        if unit == "day":
            current = current.fromordinal(current.toordinal() + 1).replace(
                tzinfo=timezone.utc
            )
        elif unit == "month":
            year, month = divmod(current.month, 12)
            current = current.replace(year=current.year + year, month=month + 1, day=1)
        else:
            current = current.replace(year=current.year + 1, month=1, day=1)
    return labels


def _conversations(scope, items):
    """{(thread_id, thread_no): conversation id} through the gated view."""
    threads = sorted({m["thread_id"] for m in items if m["thread_id"] is not None})
    if not threads:
        return []
    with reader_transaction(scope) as conn:
        return conn.execute(
            "SELECT id,thread_id,first_thread_no,last_thread_no FROM api.conversations WHERE thread_id=ANY(%s)",
            (threads,),
        ).fetchall()


def _episodes(scope, items):
    """{ordinal: {episode id: certain}} for group by 'episode': one
    api.episodes_of statement over the message members gives the stable
    episode id; any other member is a singleton group keyed by its record id.
    Without api.episodes_of, messages of one thread whose record times are at
    most 1,800 s apart form one tentative (certain false) episode."""
    messages = {
        m["ordinal"]: m["message_id"]
        for m in items
        if m.get("prefix") == "m" and m.get("message_id") is not None
    }
    result = {m["ordinal"]: {m["record_id"]: True} for m in items}
    if not messages:
        return result
    ids = sorted(set(messages.values()))
    if len(ids) > 10000:
        raise ValueError("group: episode grouping takes at most 10000 messages")
    with reader_transaction(scope) as conn:
        present = conn.execute(
            "SELECT to_regprocedure('api.episodes_of(bigint[])') IS NOT NULL AS ok"
        ).fetchone()["ok"]
        rows = (
            conn.execute(
                "SELECT message_id,episode_id FROM api.episodes_of(%s)", (ids,)
            ).fetchall()
            if present
            else None
        )
    if rows is not None:
        found = {r["message_id"]: r["episode_id"] for r in rows}
        for ordinal, mid in messages.items():
            if found.get(mid):
                result[ordinal] = {found[mid]: True}
        return result
    by_thread = {}
    for m in items:
        if m["ordinal"] not in messages:
            continue
        if m.get("thread_id") is None:
            result[m["ordinal"]] = {f"ep:m:{m['message_id']}": False}
            continue
        by_thread.setdefault(m["thread_id"], []).append(m)
    for thread, members in by_thread.items():
        members.sort(
            key=lambda m: (
                m["record_time"] is None,
                m["record_time"] or datetime.min.replace(tzinfo=timezone.utc),
                m["message_id"],
            )
        )
        current, last = None, None
        for m in members:
            when = m["record_time"]
            if (
                current is None
                or when is None
                or last is None
                or (when - last).total_seconds() > 1800
            ):
                current = f"ep:{thread}:{m['message_id']}"
            last = when
            result[m["ordinal"]] = {current: False}
    return result


def _group_assignments(scope, items, params):
    by = params.get("by")
    if by not in GROUP_BY:
        raise ValueError("group: by is " + ", ".join(GROUP_BY))
    assignments = {}
    if by == "episode":
        return _episodes(scope, items)
    if by == "explicit":
        given = params.get("assignments") or {}
        if not isinstance(given, dict):
            raise ValueError("group: assignments maps record_id to groups")
        for m in items:
            assignments[m["ordinal"]] = _groups(given.get(m["record_id"], []))
        return assignments
    conversations = _conversations(scope, items) if by == "conversation" else []
    basis = params.get("basis", "record")
    if basis not in ("record", "subject"):
        raise ValueError("group: basis is record or subject")
    for m in items:
        found = {}
        if by == "conversation":
            match = next(
                (
                    c
                    for c in conversations
                    if c["thread_id"] == m["thread_id"]
                    and m["thread_no"] is not None
                    and c["first_thread_no"] <= m["thread_no"] <= c["last_thread_no"]
                ),
                None,
            )
            if match:
                found[f"conversation:{match['id']}"] = True
            elif m["thread_id"] is not None:
                # Not yet grouped into a conversation: the thread is a guess.
                found[f"thread:{m['thread_id']}"] = False
        elif by == "thread":
            if m["thread_id"] is not None:
                found[f"thread:{m['thread_id']}"] = True
        elif by == "channel":
            if m["channel"] is not None:
                found[f"channel:{m['channel']}"] = True
        elif by in _UNIT:
            if basis == "subject" and m.get("subject_from") is not None:
                start = m["subject_from"]
                end = m.get("subject_to") or start
                labels = _buckets(start, end, by)
                precision = max(
                    _PRECISION.get(m.get("subject_from_precision") or "exact", 5),
                    _PRECISION.get(
                        m.get("subject_to_precision")
                        or m.get("subject_from_precision")
                        or "exact",
                        5,
                    ),
                )
                certain = len(labels) == 1 and precision <= _UNIT[by]
                for label in labels:
                    found[f"{by}:{label}"] = certain
            elif m["record_time"] is not None:
                found[f"{by}:{_bucket(m['record_time'], by)}"] = basis == "record"
        elif by == "person":
            if m["person_id"] is not None:
                found[f"person:{m['person_id']}"] = True
            else:
                found["person:unknown"] = False
        elif by == "support":
            for s in m.get("support") or []:
                if isinstance(s, dict) and s.get("message_id") is not None:
                    found[f"support:m:{s['message_id']}"] = True
        assignments[m["ordinal"]] = found
    return assignments


def _completeness(op, inputs, before, after):
    worst = _worst(inputs)
    dropped = after < before
    if op == "sample" and dropped:
        return "ranked_sample"
    if op in ("head", "dedup_lossy") and dropped:
        return worst if worst != "exhaustive" else "truncated"
    return worst


def transform(scope, op, handles, **params):
    """Apply one operation to the caller's sets; returns the new handle.

    The new set's origin is {op, params, parents}. Completeness is exhaustive
    only when every input is and the operation keeps every member it is meant
    to keep; head and lossy dedup of an exhaustive set give truncated, sample
    gives ranked_sample, otherwise the worst input completeness is carried.
    """
    _scope(scope)
    if op not in OPS:
        raise ValueError("op must be " + ", ".join(OPS))
    if isinstance(handles, str):
        handles = [handles]
    handles = list(handles or ())
    if not handles:
        raise ValueError("transform needs at least one handle")
    if op in ("intersect", "subtract") and len(handles) < 2:
        raise ValueError(f"{op} needs at least two handles")
    if op not in ("union", "intersect", "subtract") and len(handles) != 1:
        raise ValueError(f"{op} takes exactly one handle")
    session = params.pop("session", None)
    ttl_s = params.pop("ttl_s", None)
    # Parameters come from callers as JSON: wrong types are a 400, not a crash.
    n = params.get("n")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int)):
        raise ValueError(f"{op}: n must be an integer")
    seed = params.get("seed")
    if seed is not None and (
        isinstance(seed, bool) or not isinstance(seed, (int, str))
    ):
        raise ValueError(f"{op}: seed must be an integer or text")
    if params.get("min_score") is not None and (
        isinstance(params["min_score"], bool)
        or not isinstance(params["min_score"], (int, float))
    ):
        raise ValueError(f"{op}: min_score must be a number")
    for key in ("kinds", "channels"):
        value = params.get(key)
        if value is not None and (
            not isinstance(value, (list, tuple))
            or not all(isinstance(v, str) for v in value)
        ):
            raise ValueError(f"{op}: {key} must be a list of names")
    for key in ("role", "grp", "prefix", "by", "basis"):
        if params.get(key) is not None and not isinstance(params[key], str):
            raise ValueError(f"{op}: {key} must be text")
    parents, inputs = [], []
    for handle in handles:
        row, rows = _checked_rows(scope, handle)
        parents.append(row)
        inputs.append([_member(r) for r in rows])
    first = inputs[0]
    before = len(first)
    lossy = op
    if op == "union":
        _check_params(op, params, ())
        seen, out = set(), []
        for items in inputs:
            for m in items:
                if _identity(m) not in seen:
                    seen.add(_identity(m))
                    out.append(m)
        before = len(out)
    elif op == "intersect":
        _check_params(op, params, ())
        others = [{_identity(m) for m in items} for items in inputs[1:]]
        out = [m for m in first if all(_identity(m) in o for o in others)]
        before = len(out)
    elif op == "subtract":
        _check_params(op, params, ())
        others = set().union(*({_identity(m) for m in items} for items in inputs[1:]))
        out = [m for m in first if _identity(m) not in others]
        before = len(out)
    elif op == "filter":
        out = _filter(first, params, scope)
        before = len(out)
    elif op == "order":
        out = _order(first, params)
    elif op == "head":
        _check_params(op, params, ("n",))
        n = int(params.get("n", 10))
        if n < 0:
            raise ValueError("head: n cannot be negative")
        out = first[:n]
    elif op == "sample":
        _check_params(op, params, ("n", "seed"))
        n = int(params.get("n", 10))
        if n < 0:
            raise ValueError("sample: n cannot be negative")
        rng = random.Random(params.get("seed", 0))
        chosen = set(rng.sample(range(len(first)), n)) if n < len(first) else None
        out = [m for i, m in enumerate(first) if chosen is None or i in chosen]
    elif op == "dedup":
        out = _dedup(first, params)
        if params.get("by", "record_id") == "record_id":
            before = len(out)
        else:
            lossy = "dedup_lossy"
    elif op == "group":
        _check_params(op, params, ("by", "basis", "replace", "assignments"))
        assignments = _group_assignments(scope, first, params)
        out = []
        for m in first:
            groups = (
                {}
                if params.get("replace")
                else {g["grp"]: g["certain"] for g in m["groups"] or ()}
            )
            for name, certain in assignments.get(m["ordinal"], {}).items():
                groups[name] = groups.get(name, False) or certain
            out.append(
                dict(m, groups=[dict(grp=k, certain=v) for k, v in groups.items()])
            )
    else:  # ungroup
        _check_params(op, params, ("prefix",))
        prefix = params.get("prefix")
        out = [
            dict(
                m,
                groups=[
                    g
                    for g in m["groups"] or ()
                    if prefix is not None and not g["grp"].startswith(prefix)
                ],
            )
            for m in first
        ]
    completeness = _completeness(
        lossy, [p["completeness"] for p in parents], before, len(out)
    )
    read_total = sum(len(items) for items in inputs)
    counts = dict(
        retrieved=read_total,
        mechanically_processed=read_total,
        semantically_evaluated=0,
        inspected_by_controller=0,
    )
    as_of = {p["as_of"] for p in parents}
    known_by = {p["known_by"] for p in parents}
    return create(
        scope,
        out,
        origin=dict(op=op, params=_plain(params), parents=handles),
        completeness=completeness,
        counts=counts,
        session=session if session is not None else parents[0]["session_id"],
        as_of=as_of.pop() if len(as_of) == 1 else None,
        known_by=known_by.pop() if len(known_by) == 1 else None,
        ttl_s=ttl_s,
    )


# --- inspect ---------------------------------------------------------------


def _fit(result, items, token_budget):
    """Put as many items into result as the token budget allows."""
    result["items_total"] = len(items)
    result["truncated"] = False
    result["items"] = list(items)
    while result["items"] and estimate_tokens(result) > token_budget:
        result["truncated"] = True
        keep = len(result["items"]) // 2
        result["items"] = result["items"][:keep]
    # Still too large: drop optional keys, largest first, keeping the
    # identity of the answer.
    essential = (
        "view",
        "handle",
        "completeness",
        "member_count",
        "items",
        "items_total",
        "truncated",
        "omitted",
    )
    while estimate_tokens(result) > token_budget:
        optional = [k for k in result if k not in essential]
        if not optional:
            break
        largest = max(optional, key=lambda k: estimate_tokens(result[k]))
        result.pop(largest)
        result["truncated"] = True
        result.setdefault("omitted", []).append(largest)
    return result


def _counted(values):
    found = {}
    for value in values:
        found[value] = found.get(value, 0) + 1
    return sorted(found.items(), key=lambda kv: (-kv[1], str(kv[0])))


def _origin_chain(scope, handle, limit=50):
    chain, queue, seen = [], [handle], set()
    with connect("app") as conn:
        while queue and len(chain) < limit:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            try:
                row = _load_set(conn, scope, current, allow_stale=True)
            except ResultSetNotFound:
                chain.append(dict(handle=current, available=False))
                continue
            origin = row["origin"] or {}
            chain.append(
                dict(
                    handle=current,
                    available=True,
                    op=origin.get("op"),
                    params=origin.get("params"),
                    parents=origin.get("parents") or [],
                    completeness=row["completeness"],
                    created_at=row["created_at"],
                )
            )
            queue.extend(origin.get("parents") or [])
    return chain


def inspect(scope, handle, view="summary", selector=None, token_budget=1000):
    """A bounded look at a set (LIB-5): summary, sample, by_time,
    by_channel, by_person, groups (the manifest), missing_link, origin or
    history. Only visible members are described; at most token_budget tokens
    (estimate) are returned and `truncated` says whether items were cut."""
    _scope(scope)
    if view not in VIEWS:
        raise ValueError("view must be " + ", ".join(VIEWS))
    token_budget = max(50, int(token_budget or 1000))
    selector = selector if selector is not None else {}
    if isinstance(selector, str):
        selector = {"value": selector}
    if not isinstance(selector, dict):
        raise ValueError("selector must be an object or a string")
    row, rows = _checked_rows(scope, handle)
    result = dict(
        view=view,
        handle=handle,
        completeness=row["completeness"],
        member_count=len(rows),
    )
    items = []
    if view == "summary":
        times = [r["record_time"] for r in rows if r["record_time"] is not None]
        scores = [r["score"] for r in rows if r["score"] is not None]
        result.update(
            counts=_info(row, len(rows))["counts"],
            kinds=dict(_counted(r["kind"] for r in rows)),
            roles=dict(_counted(r["role"] for r in rows)),
            first_time=min(times) if times else None,
            last_time=max(times) if times else None,
            min_score=min(scores) if scores else None,
            max_score=max(scores) if scores else None,
            groups=len({g["grp"] for r in rows for g in r["groups"] or ()}),
        )
        items = [dict(channel=k, n=v) for k, v in _counted(r["channel"] for r in rows)]
    elif view == "sample":
        n = max(0, int(selector.get("n", 5)))
        start = max(0, int(selector.get("offset", 0)))
        chosen = rows
        if selector.get("grp") is not None:
            chosen = [
                r
                for r in rows
                if selector["grp"] in {g["grp"] for g in r["groups"] or ()}
            ]
        for r in chosen[start : start + n]:
            content = r["content"] or ""
            items.append(
                dict(
                    ordinal=r["ordinal"],
                    record_id=r["record_id"],
                    version=r["version"],
                    kind=r["kind"],
                    channel=r["channel"],
                    record_time=r["record_time"],
                    score=r["score"],
                    role=r["role"],
                    excerpt=content[:EXCERPT_CHARS],
                    excerpt_truncated=len(content) > EXCERPT_CHARS,
                )
            )
    elif view == "by_time":
        unit = selector.get("granularity", selector.get("value", "month"))
        if unit not in _UNIT:
            raise ValueError("by_time: granularity is day, month or year")
        result["granularity"] = unit
        buckets = _counted(
            _bucket(r["record_time"], unit)
            for r in rows
            if r["record_time"] is not None
        )
        items = sorted(
            (dict(bucket=k, n=v) for k, v in buckets), key=lambda b: b["bucket"]
        )
        result["undated"] = sum(1 for r in rows if r["record_time"] is None)
    elif view == "by_channel":
        items = [dict(channel=k, n=v) for k, v in _counted(r["channel"] for r in rows)]
    elif view == "by_person":
        items = [
            dict(person_id=k, n=v) for k, v in _counted(r["person_id"] for r in rows)
        ]
    elif view == "groups":
        wanted = selector.get("grp", selector.get("value"))
        if wanted is not None:
            result["grp"] = wanted
            items = [
                dict(
                    ordinal=r["ordinal"], record_id=r["record_id"], certain=g["certain"]
                )
                for r in rows
                for g in r["groups"] or ()
                if g["grp"] == wanted
            ]
        else:
            manifest = {}
            for r in rows:
                for g in r["groups"] or ():
                    entry = manifest.setdefault(
                        g["grp"],
                        dict(
                            grp=g["grp"],
                            members=0,
                            certain=0,
                            uncertain=0,
                            channels=set(),
                            times=[],
                        ),
                    )
                    entry["members"] += 1
                    entry["certain" if g["certain"] else "uncertain"] += 1
                    if r["channel"] is not None:
                        entry["channels"].add(r["channel"])
                    if r["record_time"] is not None:
                        entry["times"].append(r["record_time"])
            for entry in sorted(
                manifest.values(), key=lambda e: (-e["members"], e["grp"])
            ):
                times = entry.pop("times")
                entry["channels"] = sorted(entry["channels"])
                entry["first_time"] = min(times) if times else None
                entry["last_time"] = max(times) if times else None
                items.append(entry)
            result["ungrouped"] = sum(1 for r in rows if not r["groups"])
            result["multi_group_members"] = sum(
                1 for r in rows if len(r["groups"] or ()) > 1
            )
    elif view == "missing_link":
        present = {r["record_id"] for r in rows}
        missing = {}
        for r in rows:
            refs = []
            if r["depends_on"]:
                refs.append((r["depends_on"], "depends_on"))
            for s in r["support"] or []:
                if (
                    isinstance(s, dict)
                    and s.get("message_id") is not None
                    and s.get("relation") != "self"
                ):
                    refs.append((f"m:{s['message_id']}", "support"))
            for ref, reason in refs:
                if ref in present:
                    continue
                entry = missing.setdefault(
                    ref, dict(record_id=ref, reasons=set(), referenced_by=[])
                )
                entry["reasons"].add(reason)
                entry["referenced_by"].append(r["record_id"])
        for entry in sorted(missing.values(), key=lambda e: e["record_id"]):
            entry["reasons"] = sorted(entry["reasons"])
            items.append(entry)
    elif view == "origin":
        items = _origin_chain(scope, handle)
    else:  # history
        if row["session_id"] is None:
            items = [
                dict(
                    handle=handle,
                    op=(row["origin"] or {}).get("op"),
                    parents=(row["origin"] or {}).get("parents") or [],
                    completeness=row["completeness"],
                    created_at=row["created_at"],
                )
            ]
        else:
            current = session(scope, row["session_id"])
            result["session_id"] = current["id"]
            result["unresolved"] = current["unresolved"]
            items = [
                dict(
                    handle=s["handle"],
                    op=(s["origin"] or {}).get("op"),
                    parents=s["parents"],
                    completeness=s["completeness"],
                    created_at=s["created_at"],
                )
                for s in current["sets"]
            ]
    result = _plain(result)
    return _fit(result, _plain(items), token_budget)
