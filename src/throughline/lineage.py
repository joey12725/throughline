"""Lineage: validity intervals, provenance writers and link history.

Provenance says which source messages a derived object (a page revision or
derived message, a passage, a claim, a link, an entity) rests on, with the
source version pinned at write time. Origins 'source' (a plugin citing the
sources of its own record), 'owner' and 'job' are source-grade: they are the
record's support and decide its visibility (migration 0036, api.row_visible).
Origin 'agent' is stored apart and never counts for either.

Functions here take an app-role connection; callers authorize first.
"""

import re
from datetime import date, datetime, timezone

PRECISIONS = ("exact", "day", "month", "year", "approximate", "unknown")
BOUNDS = ("valid_from", "valid_to")
VALIDITY = ("valid_from", "valid_to", "valid_from_precision", "valid_to_precision")
DERIVED_KINDS = ("message", "passage", "link", "entity", "annotation", "table_row")
ORIGINS = ("source", "owner", "job", "agent")
SOURCE_GRADE = ("source", "owner", "job")
MAX_SOURCES = 1000

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_time(value, name="timestamp"):
    """A bound as an aware datetime. A bare date (YYYY-MM-DD) is that day at
    00:00 UTC, which is how a day-precision bound is sent."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if _DATE.fullmatch(text):
            day = date.fromisoformat(text)
            return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{name} is not an ISO timestamp") from None
    else:
        raise ValueError(f"{name} is not an ISO timestamp")
    if parsed.utcoffset() is None:
        raise ValueError(f"{name} must include UTC offset")
    return parsed


def validity(data, base=None):
    """Validated validity fields present in `data` (a subset of VALIDITY).

    `base` is the stored row the fields are applied to (PATCH), so the
    combination is checked: a precision on a missing bound must be 'unknown'
    and valid_to may not precede valid_from.
    """
    out = {}
    for key in VALIDITY:
        if key not in data:
            continue
        value = data[key]
        if key in BOUNDS:
            out[key] = parse_time(value, key)
        elif value is not None and value not in PRECISIONS:
            raise ValueError(f"{key} must be one of " + ", ".join(PRECISIONS))
        else:
            out[key] = value
    merged = {key: (base or {}).get(key) for key in VALIDITY} | out
    for bound in BOUNDS:
        if merged[bound] is None and merged[bound + "_precision"] not in (
            None,
            "unknown",
        ):
            raise ValueError(
                f"{bound}_precision without {bound} must be 'unknown' or null"
            )
    if (
        merged["valid_from"] is not None
        and merged["valid_to"] is not None
        and merged["valid_to"] < merged["valid_from"]
    ):
        raise ValueError("valid_to precedes valid_from")
    return out


def add_provenance(
    conn,
    token_or_job,
    derived_kind,
    derived_id,
    sources,
    relation="derived_from",
    origin="source",
    job=None,
    job_version=None,
):
    """Record message ids `sources` as sources of (derived_kind, derived_id).

    Each source version is pinned as stored now. `token_or_job` is the
    authenticated token (its id and asker are recorded) or a job name.
    Existing rows are kept unchanged. Returns the number of rows written.
    """
    if derived_kind not in DERIVED_KINDS:
        raise ValueError("derived_kind must be one of " + ", ".join(DERIVED_KINDS))
    if origin not in ORIGINS:
        raise ValueError("origin must be one of " + ", ".join(ORIGINS))
    if not isinstance(relation, str) or not 1 <= len(relation) <= 100:
        raise ValueError("relation must be a short name")
    token_id = asker = None
    if isinstance(token_or_job, dict):
        token_id, asker = token_or_job.get("id"), token_or_job.get("asker")
    elif isinstance(token_or_job, str) and token_or_job:
        job = job or token_or_job
    if origin == "job" and not job:
        raise ValueError("job provenance requires a job")
    if isinstance(derived_id, bool) or not isinstance(derived_id, int):
        raise ValueError("derived_id must be an id")
    ids = list(dict.fromkeys(sources))
    if any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
        raise ValueError("sources must be message ids")
    if len(ids) > MAX_SOURCES:
        raise ValueError(f"at most {MAX_SOURCES} sources per call")
    if derived_kind == "message":
        if derived_id in ids:
            raise ValueError("a record cannot be its own source")
        if not conn.execute(
            "SELECT 1 FROM tl.messages WHERE id=%s", (derived_id,)
        ).fetchone():
            raise ValueError("derived message not found")
    if not ids:
        return 0
    found = conn.execute(
        "SELECT count(*) AS n FROM tl.messages WHERE id=ANY(%s)", (ids,)
    ).fetchone()["n"]
    if found != len(ids):
        raise ValueError("source message not found")
    return len(
        conn.execute(
            """INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,relation,origin,job,job_version,token_id,asker)
            SELECT %s,%s,m.id,m.version,%s,%s,%s,%s,%s,%s FROM tl.messages m WHERE m.id=ANY(%s) ORDER BY m.id
            ON CONFLICT DO NOTHING RETURNING 1""",
            (
                derived_kind,
                derived_id,
                relation,
                origin,
                job,
                job_version,
                token_id,
                asker,
                ids,
            ),
        ).fetchall()
    )


def sources_of(conn, derived_kind, derived_id, include_agent=False):
    """Stored provenance rows of one derived object, source-grade first."""
    return conn.execute(
        "SELECT * FROM tl.provenance WHERE derived_kind=%s AND derived_id=%s AND (%s OR origin<>'agent') ORDER BY origin='agent',source_message_id",
        (derived_kind, derived_id, include_agent),
    ).fetchall()


def link_history(conn, link_id):
    """Images of a link before each update and delete, oldest first."""
    return conn.execute(
        "SELECT history_id,link_id,op,old,changed_at,token_id FROM tl.link_history WHERE link_id=%s ORDER BY changed_at,history_id",
        (link_id,),
    ).fetchall()
