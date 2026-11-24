"""Lenses CRUD, cursor-isolated job runner, crash-safe verify-then-advance,
single-pass batching (JOB-4), dependency ordering & lazy backfill (JOB-5),
late-data re-processing (JOB-7), and hand corrections (TBL-4).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from .. import lineage, query
from . import coalesce
from ..agent_view import owner_zone
from ..db import connect
from ..providers import egress
from ..providers.base import ProviderUnavailable
from ..scope import Scope
from .schema import (
    LensSpecError,
    compute_natural_key,
    validate_row_data,
    validate_lens_name,
    validate_lens_spec,
)
from .workers import (
    check_monthly_cap,
    estimate_item_cost,
    execute_worker,
    find_duplicate_groups,
    run_merged_llm_batch,
)

log = logging.getLogger("throughline.lenses")


# --- Definition CRUD (TBL-1, ACL-6) ----------------------------------------------


def define_lens(
    spec: dict[str, Any],
    *,
    token: dict[str, Any] | None = None,
    conn=None,
) -> dict[str, Any]:
    """Create or update an lens definition and refresh its gated SQL views."""
    clean = validate_lens_spec(spec)
    token_id = token.get("id") if isinstance(token, dict) else None

    def _apply(c):
        existing = c.execute(
            "SELECT * FROM tl.table_defs WHERE name=%s", (clean["name"],)
        ).fetchone()
        if existing:
            # Bump version whenever schema, prompt, query, channels, grain, worker or config changes.
            changed = any(
                [
                    existing["columns"] != clean["columns"],
                    existing["prompt"] != clean["prompt"],
                    existing["context_query"] != clean["context_query"],
                    list(existing["channels"]) != clean["channels"],
                    existing["grain"] != clean["grain"],
                    existing["worker"] != clean["worker"],
                    existing["worker_config"] != clean["worker_config"],
                    list(existing["natural_key"]) != clean["natural_key"],
                    existing.get("output", "table") != clean["output"],
                ]
            )
            new_version = int(existing["version"]) + (1 if changed else 0)
            row = c.execute(
                """UPDATE tl.table_defs SET
                    description=%s, columns=%s, prompt=%s, context_query=%s,
                    channels=%s, grain=%s, worker=%s, worker_config=%s,
                    schedule=%s, depends_on=%s, natural_key=%s,
                    version=%s, enabled=%s, lazy_backfill=%s,
                    max_items_per_run=%s, max_cost_per_run=%s, output=%s,
                    updated_at=clock_timestamp()
                WHERE id=%s RETURNING *""",
                (
                    clean["description"],
                    Jsonb(clean["columns"]),
                    clean["prompt"],
                    clean["context_query"],
                    clean["channels"],
                    clean["grain"],
                    clean["worker"],
                    Jsonb(clean["worker_config"]),
                    clean["schedule"],
                    clean["depends_on"],
                    clean["natural_key"],
                    new_version,
                    clean["enabled"],
                    clean["lazy_backfill"],
                    clean["max_items_per_run"],
                    clean["max_cost_per_run"],
                    clean["output"],
                    existing["id"],
                ),
            ).fetchone()
        else:
            row = c.execute(
                """INSERT INTO tl.table_defs(
                    name, description, columns, prompt, context_query,
                    channels, grain, worker, worker_config,
                    schedule, depends_on, natural_key,
                    enabled, lazy_backfill, max_items_per_run, max_cost_per_run,
                    created_by_token_id, output
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *""",
                (
                    clean["name"],
                    clean["description"],
                    Jsonb(clean["columns"]),
                    clean["prompt"],
                    clean["context_query"],
                    clean["channels"],
                    clean["grain"],
                    clean["worker"],
                    Jsonb(clean["worker_config"]),
                    clean["schedule"],
                    clean["depends_on"],
                    clean["natural_key"],
                    clean["enabled"],
                    clean["lazy_backfill"],
                    clean["max_items_per_run"],
                    clean["max_cost_per_run"],
                    token_id,
                    clean["output"],
                ),
            ).fetchone()
            c.execute(
                "INSERT INTO tl.table_cursors(table_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (row["id"],),
            )
        if clean["output"] == "pages":
            # No table, so no lens views: the result is pages (channel 'pages').
            c.execute("SELECT tl.drop_lens_views(%s)", (clean["name"],))
            return dict(row, pages="api.messages WHERE channel='pages'")
        c.execute("SELECT tl.refresh_lens(%s)", (clean["name"],))
        return dict(row, view=f"api.lens_{clean['name']}", versions_view=f"api.lens_{clean['name']}_versions")

    if conn is not None:
        return _apply(conn)
    with connect("app") as c:
        return _apply(c)


def get_lens(name: str, *, conn=None) -> dict[str, Any]:
    name = validate_lens_name(name)

    def _read(c):
        row = c.execute("SELECT * FROM tl.table_defs WHERE name=%s", (name,)).fetchone()
        if not row:
            raise LookupError(f"lens '{name}' not found")
        cur = c.execute("SELECT * FROM tl.table_cursors WHERE table_id=%s", (row["id"],)).fetchone()
        counts = c.execute(
            """SELECT
                count(*) FILTER (WHERE superseded_by IS NULL AND status='active') AS active_rows,
                count(*) FILTER (WHERE superseded_by IS NULL AND status='active' AND owner_locked) AS locked_rows
            FROM tl.table_rows WHERE table_id=%s""",
            (row["id"],),
        ).fetchone()
        return dict(
            row,
            view=f"api.lens_{name}",
            versions_view=f"api.lens_{name}_versions",
            cursor=dict(cur) if cur else None,
            active_rows=int(counts["active_rows"]) if counts else 0,
            locked_rows=int(counts["locked_rows"]) if counts else 0,
        )

    if conn is not None:
        return _read(conn)
    with connect("app") as c:
        return _read(c)


def list_lenses(*, conn=None) -> list[dict[str, Any]]:
    def _read(c):
        rows = c.execute(
            """SELECT d.*,
                (SELECT count(*) FROM tl.table_rows r WHERE r.table_id=d.id AND r.superseded_by IS NULL AND r.status='active') AS active_rows,
                cur.last_message_id, cur.last_ingested_at
            FROM tl.table_defs d
            LEFT JOIN tl.table_cursors cur ON cur.table_id=d.id
            ORDER BY d.name"""
        ).fetchall()
        return [dict(r, view=f"api.lens_{r['name']}") for r in rows]

    if conn is not None:
        return _read(conn)
    with connect("app") as c:
        return _read(c)


def delete_lens(name: str, *, conn=None) -> dict[str, Any]:
    name = validate_lens_name(name)

    def _drop(c):
        # Provenance is append-only for the app role; the definer function
        # removes it together with the views, rows and definition.
        row = c.execute("SELECT tl.delete_lens(%s) AS deleted", (name,)).fetchone()
        if not row or not row["deleted"]:
            raise LookupError(f"lens '{name}' not found")
        return {"deleted": name}

    if conn is not None:
        return _drop(conn)
    with connect("app") as c:
        return _drop(c)


# --- Work-item selection (JOB-2 channel isolation, JOB-7 late data) --------------


def _fetch_messages(
    conn,
    channels: list[str],
    *,
    after_message_id: int = 0,
    done_ids: list[int] | None = None,
    message_ids: list[int] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
    filter_sql: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Read ONLY messages from the table's declared channels (JOB-2), excluding rows annotations mark ignored."""
    if not channels and not message_ids:
        return []
    clauses = ["m.superseded_by IS NULL"]
    params: list[Any] = []
    if channels:
        clauses.append("c.name = ANY(%s)")
        params.append(channels)
    if message_ids is not None:
        if not message_ids:
            return []
        clauses.append("m.id = ANY(%s)")
        params.append(list(message_ids))
    else:
        clauses.append("m.id > %s")
        params.append(int(after_message_id))
        if done_ids:
            clauses.append("NOT (m.id = ANY(%s))")
            params.append(list(done_ids))
    if since:
        clauses.append("m.happened_at >= %s::timestamptz")
        params.append(since)
    if until:
        clauses.append("m.happened_at <= %s::timestamptz")
        params.append(until)

    # Exclude records marked ignored by an annotation or source ignore_reason.
    clauses.append(
        """NOT EXISTS (
            SELECT 1 FROM tl.annotations a
            WHERE a.target_channel_id=m.channel_id
              AND a.target_external_id=m.external_id
              AND a.attribute='ignored'
              AND a.status='active'
              AND (a.target_version IS NULL OR a.target_version=m.version)
              AND (a.value->>'ignored')::boolean IS TRUE
        )"""
    )

    # Optional model-free pre-filter (keywords / min_length / exclude_shorts).
    if isinstance(filter_sql, dict):
        if filter_sql.get("skip_shorts"):
            clauses.append("coalesce((m.fields->>'is_short')::boolean, false) IS FALSE")
        if filter_sql.get("keywords"):
            kws = [str(k).strip() for k in filter_sql["keywords"] if str(k).strip()]
            if kws:
                or_parts = []
                for kw in kws:
                    or_parts.append("m.content ILIKE %s")
                    params.append(f"%{kw}%")
                clauses.append("(" + " OR ".join(or_parts) + ")")

    params.append(int(limit))
    rows = conn.execute(
        f"""SELECT m.id, m.channel_id, c.name AS channel, m.external_id, m.version,
            m.thread_id, m.thread_no, t.title AS thread_title,
            m.happened_at, m.ended_at, m.ingested_at,
            m.author_handle, m.person_id, p.handle AS person_handle, p.display_name,
            m.content, m.fields
        FROM tl.messages m
        JOIN tl.channels c ON c.id=m.channel_id
        LEFT JOIN tl.threads t ON t.id=m.thread_id
        LEFT JOIN tl.people p ON p.id=m.person_id
        WHERE {' AND '.join(clauses)}
        ORDER BY m.id ASC
        LIMIT %s""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _group_into_work_items(
    messages: list[dict[str, Any]], grain: str, max_chars: int | None = None
) -> list[dict[str, Any]]:
    if not messages:
        return []
    if grain == "message":
        return [
            {
                "window_key": f"msg:{m['id']}",
                "messages": [m],
                "message_ids": [int(m["id"])],
            }
            for m in messages
        ]

    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    if grain == "thread":
        for m in messages:
            key = f"thread:{m['thread_id']}" if m.get("thread_id") is not None else f"msg:{m['id']}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(m)
    elif grain == "day":
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in messages:
            by_day[_local_day(m["happened_at"])].append(m)
        items: list[dict[str, Any]] = []
        for day in sorted(by_day):
            items.extend(_split_day(day, by_day[day], max_chars))
        return items
    elif grain == "conversation":
        # Group contiguous messages in the same thread within 30 minutes of each other.
        by_thread: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for m in messages:
            by_thread[m.get("thread_id") or f"m:{m['id']}"].append(m)
        for tid, tmsgs in by_thread.items():
            tmsgs.sort(key=lambda x: (x["happened_at"], x["id"]))
            chunk: list[dict[str, Any]] = []
            for m in tmsgs:
                if chunk:
                    prev_t = chunk[-1]["happened_at"]
                    cur_t = m["happened_at"]
                    gap = (cur_t - prev_t).total_seconds() if isinstance(cur_t, datetime) and isinstance(prev_t, datetime) else 0
                    if gap > 1800 or len(chunk) >= 40:
                        wkey = f"conv:{tid}:{chunk[0]['id']}"
                        groups[wkey] = chunk
                        order.append(wkey)
                        chunk = []
                chunk.append(m)
            if chunk:
                wkey = f"conv:{tid}:{chunk[0]['id']}"
                groups[wkey] = chunk
                order.append(wkey)

    return [
        {
            "window_key": key,
            "messages": groups[key],
            "message_ids": [int(m["id"]) for m in groups[key]],
        }
        for key in order
    ]


# --- Day grain (one model call per owner-local day; busy days split by size) -------

DEFAULT_DAY_MAX_CHARS = 150_000
DAY_MESSAGE_OVERHEAD_CHARS = 160  # id, channel, thread, author, local_time per item


def _local_day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(owner_zone()).strftime("%Y-%m-%d")
    return str(value)[:10]


def _day_max_chars(table_def: dict[str, Any]) -> int:
    cfg = table_def.get("worker_config") or {}
    try:
        return max(5_000, int(cfg.get("max_chars_per_item") or DEFAULT_DAY_MAX_CHARS))
    except (TypeError, ValueError):
        return DEFAULT_DAY_MAX_CHARS


def _split_day(day: str, msgs: list[dict[str, Any]], max_chars: int | None) -> list[dict[str, Any]]:
    """One work item per local day; a day larger than max_chars is split between
    conversations (thread order kept contiguous), never inside one message."""
    budget = max_chars or DEFAULT_DAY_MAX_CHARS
    ordered = sorted(
        msgs,
        key=lambda m: (str(m.get("channel") or ""), m.get("thread_id") or 0, m["happened_at"], m["id"]),
    )
    chunks: list[list[dict[str, Any]]] = [[]]
    size = 0
    for m in ordered:
        cost = len(m.get("content") or "") + DAY_MESSAGE_OVERHEAD_CHARS
        if chunks[-1] and size + cost > budget:
            chunks.append([])
            size = 0
        chunks[-1].append(m)
        size += cost
    out = []
    for i, chunk in enumerate(chunks):
        chunk.sort(key=lambda m: (m["happened_at"], m["id"]))
        out.append(
            {
                "window_key": f"day:{day}" if len(chunks) == 1 else f"day:{day}#{i + 1}",
                "day": day,
                "messages": chunk,
                "message_ids": [int(m["id"]) for m in chunk],
            }
        )
    return out



def _effective_since(table_def: dict[str, Any], since: str | None) -> str | None:
    """The latest of the caller's `since`, worker_config.not_before (an ISO date or
    timestamp: never read anything older) and worker_config.lookback_days (a rolling
    window, e.g. a calendar lens has no use for last year's chats)."""
    cfg = table_def.get("worker_config") or {}
    bounds: list[datetime] = []
    for raw in (since, cfg.get("not_before")):
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw))
            except ValueError as exc:
                raise LensSpecError(f"invalid not_before/since: {raw!r}") from exc
            bounds.append(dt if dt.tzinfo else dt.replace(tzinfo=owner_zone()))
    if cfg.get("lookback_days") not in (None, ""):
        days = int(cfg["lookback_days"])
        start = datetime.now(owner_zone()).replace(hour=0, minute=0, second=0, microsecond=0)
        bounds.append(start - timedelta(days=days))
    return max(bounds).isoformat() if bounds else None


def _pending_day_items(
    conn,
    table_def: dict[str, Any],
    cursor: dict[str, Any],
    *,
    backfill: bool,
    since: str | None,
    until: str | None,
    max_days: int,
) -> list[dict[str, Any]]:
    """Day grain: every owner-local day that has messages newer than the cursor is
    re-read in full (all of that day's messages, not only the new ones), so a model
    call always sees the whole day. The message cursor only advances past a day once
    that day and every earlier pending day is done."""
    channels = list(table_def.get("channels") or [])
    if not channels:
        return []
    after_id = 0 if backfill else int(cursor.get("last_message_id") or 0)
    windows = {} if backfill else dict(cursor.get("processed_windows") or {})
    zone = owner_zone()
    # Only a new message or a changed text makes a day pending again: live syncs
    # re-save unchanged messages all the time (new version, same content).
    clauses = [
        "m.superseded_by IS NULL",
        "c.name = ANY(%s)",
        "m.id > %s",
        """(m.version = 1 OR NOT EXISTS (
            SELECT 1 FROM tl.messages o
            WHERE o.superseded_by = m.id AND o.content IS NOT DISTINCT FROM m.content))""",
    ]
    params: list[Any] = [str(zone), channels, after_id]
    since = _effective_since(table_def, since)
    if since:
        clauses.append("m.happened_at >= %s::timestamptz")
        params.append(since)
    if until:
        clauses.append("m.happened_at <= %s::timestamptz")
        params.append(until)
    rows = conn.execute(
        f"""SELECT to_char((m.happened_at AT TIME ZONE %s)::date, 'YYYY-MM-DD') AS day,
                min(m.id) AS min_id, max(m.id) AS max_id
        FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id
        WHERE {' AND '.join(clauses)}
        GROUP BY 1 ORDER BY 1""",
        params,
    ).fetchall()
    pending = [
        r for r in rows if int((windows.get(f"day:{r['day']}") or {}).get("max_id") or 0) < int(r["max_id"])
    ]
    if not pending:
        return []
    # floor_after[i]: highest message id the cursor may reach once days 0..i are done.
    overall_max = max(int(r["max_id"]) for r in rows)
    floor_after: list[int] = []
    for i in range(len(pending)):
        later = [int(r["min_id"]) for r in pending[i + 1 :]]
        floor_after.append(min(later) - 1 if later else overall_max)
    floor_before = [min(int(r["min_id"]) for r in pending[i:]) - 1 for i in range(len(pending))]

    max_chars = _day_max_chars(table_def)
    filter_sql = (table_def.get("worker_config") or {}).get("filter")
    items: list[dict[str, Any]] = []
    for i, r in enumerate(pending[:max_days]):
        day = r["day"]
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
        end = start + timedelta(days=1) - timedelta(microseconds=1)
        msgs = _fetch_messages(
            conn,
            channels,
            after_message_id=0,
            since=start.isoformat(),
            until=end.isoformat(),
            limit=100_000,
            filter_sql=filter_sql,
        )
        day_key = f"day:{day}"
        rerun = day_key in windows or any(k.startswith(day_key + "#") for k in windows)
        chunks = _split_day(day, msgs, max_chars) if msgs else []
        if not chunks:
            # Everything that day is filtered or ignored: record the day as done without a call.
            chunks = [{"window_key": day_key, "day": day, "messages": [], "message_ids": []}]
        for j, chunk in enumerate(chunks):
            last = j == len(chunks) - 1
            chunk["cursor_last_id"] = floor_after[i] if last else floor_before[i]
            if last:
                chunk["day_key"] = day_key
                chunk["day_max_id"] = int(r["max_id"])
            if rerun:
                chunk["is_late_rerun"] = True
            items.append(chunk)
    return items


def _detect_late_windows(
    conn,
    table_def: dict[str, Any],
    cursor: dict[str, Any],
) -> list[dict[str, Any]]:
    """JOB-7: Find already-processed windows that received newer late-arriving messages."""
    processed_windows = cursor.get("processed_windows") or {}
    last_ingested = cursor.get("last_ingested_at")
    channels = list(table_def.get("channels") or [])
    if not processed_windows or not last_ingested or not channels:
        return []
    grain = table_def["grain"]
    if grain not in ("day", "thread"):
        return []

    # Check if any newly ingested messages land in an already-processed day/thread window.
    late_msgs = conn.execute(
        """SELECT m.id, m.channel_id, c.name AS channel, m.thread_id, m.happened_at, m.ingested_at
        FROM tl.messages m
        JOIN tl.channels c ON c.id=m.channel_id
        WHERE m.superseded_by IS NULL
          AND c.name = ANY(%s)
          AND m.ingested_at > %s
        ORDER BY m.id ASC
        LIMIT 500""",
        (channels, last_ingested),
    ).fetchall()
    if not late_msgs:
        return []

    affected_windows: set[str] = set()
    for m in late_msgs:
        if grain == "day":
            dt = m["happened_at"]
            day = dt.astimezone(timezone.utc).strftime("%Y-%m-%d") if isinstance(dt, datetime) else str(dt)[:10]
            wkey = f"day:{day}"
        else:
            wkey = f"thread:{m['thread_id']}" if m.get("thread_id") is not None else f"msg:{m['id']}"
        if wkey in processed_windows:
            affected_windows.add(wkey)

    if not affected_windows:
        return []

    items: list[dict[str, Any]] = []
    for wkey in sorted(affected_windows):
        if grain == "day":
            day = wkey.split(":", 1)[1]
            msgs = _fetch_messages(
                conn,
                channels,
                after_message_id=0,
                since=f"{day}T00:00:00+00:00",
                until=f"{day}T23:59:59.999999+00:00",
                limit=500,
                filter_sql=(table_def.get("worker_config") or {}).get("filter"),
            )
        else:
            tid = int(wkey.split(":", 1)[1])
            msgs = conn.execute(
                """SELECT m.id, m.channel_id, c.name AS channel, m.external_id, m.version,
                    m.thread_id, m.thread_no, t.title AS thread_title,
                    m.happened_at, m.ended_at, m.ingested_at,
                    m.author_handle, m.person_id, p.handle AS person_handle, p.display_name,
                    m.content, m.fields
                FROM tl.messages m
                JOIN tl.channels c ON c.id=m.channel_id
                LEFT JOIN tl.threads t ON t.id=m.thread_id
                LEFT JOIN tl.people p ON p.id=m.person_id
                WHERE m.superseded_by IS NULL AND c.name = ANY(%s) AND m.thread_id=%s
                ORDER BY m.happened_at, m.id""",
                (channels, tid),
            ).fetchall()
            msgs = [dict(r) for r in msgs]
        if msgs:
            items.append(
                {
                    "window_key": wkey,
                    "messages": msgs,
                    "message_ids": [int(m["id"]) for m in msgs],
                    "is_late_rerun": True,
                }
            )
    return items


def _run_context_query(table_def: dict[str, Any], scope: Scope | None = None) -> list[dict[str, Any]]:
    """Execute the table's optional read-only SQL context query (TBL-1)."""
    stmt = table_def.get("context_query")
    if not stmt:
        return []
    try:
        res = query.query_ex(stmt, cap=50, scope=scope or Scope.local())
        return res.get("rows") or []
    except Exception as exc:
        log.warning("context_query failed for table %s: %s", table_def["name"], exc)
        return []


def _load_owner_corrections(conn, table_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Load recent owner-locked corrections to feed back into future runs (TBL-4)."""
    rows = conn.execute(
        """SELECT id, natural_key, data, owner_correction_note, source_message_ids
        FROM tl.table_rows
        WHERE table_id=%s AND owner_locked IS TRUE AND superseded_by IS NULL AND status='active'
        ORDER BY updated_at DESC, id DESC
        LIMIT %s""",
        (table_id, limit),
    ).fetchall()
    return [
        {
            "row_id": r["id"],
            "data": r["data"],
            "correction_note": r["owner_correction_note"],
            "source_message_ids": list(r["source_message_ids"] or []),
        }
        for r in rows
    ]


def _filter_egress_held(conn, table_def: dict[str, Any], work_item: dict[str, Any]) -> dict[str, Any]:
    """Ensure sensitivity-labelled egress-held messages never leave the host to remote vendors."""
    worker = table_def["worker"]
    cfg = table_def.get("worker_config") or {}
    if worker == "code" or cfg.get("provider") == "local":
        return work_item
    if worker == "llm" and not egress.leaves_host(str(cfg.get("base_url") or "https://openrouter.ai/api/v1")):
        return work_item
    mids = work_item.get("message_ids") or []
    if not mids:
        return work_item
    try:
        held = egress.held_messages(conn, mids)
    except Exception:
        held = set()
    if not held:
        return work_item
    kept = [m for m in work_item["messages"] if int(m["id"]) not in held]
    return {
        **work_item,
        "messages": kept,
        "message_ids": [int(m["id"]) for m in kept],
    }


def _write_window_rows_and_advance(
    conn,
    table_def: dict[str, Any],
    work_item: dict[str, Any],
    extracted_rows: list[dict[str, Any]],
    *,
    advance_cursor: bool = True,
    on_before_commit: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """JOB-3 verify-then-advance: write rows + provenance, verify, advance cursor in ONE transaction."""
    table_id = int(table_def["id"])
    tname = table_def["name"]
    job_name = f"table:{tname}"
    job_version = str(table_def["version"])
    wkey = work_item["window_key"]
    written = 0
    updated = 0
    skipped_locked = 0
    touched_keys: set[str] = set()
    written_ids: list[int] = []

    for idx, row in enumerate(extracted_rows):
        data = row["data"]
        src_ids = list(row["source_message_ids"])
        nkey = compute_natural_key(
            list(table_def.get("natural_key") or []),
            data,
            fallback_seed=f"{tname}:{wkey}:{idx}:{json.dumps(src_ids)}",
        )
        same_as = row.get("same_as_row_id")
        if same_as is not None:
            # The model matched an existing row of this lens: update that row (keep its
            # natural key) and keep its earlier citations alongside the new ones.
            target = conn.execute(
                """SELECT natural_key, source_message_ids FROM tl.table_rows
                WHERE id=%s AND table_id=%s AND superseded_by IS NULL AND status='active'""",
                (int(same_as), table_id),
            ).fetchone()
            if target and target["natural_key"] not in touched_keys:
                nkey = target["natural_key"]
                src_ids = sorted(set(src_ids) | set(target["source_message_ids"] or []))
        elif nkey in touched_keys:
            # Two rows of one call for the same item: keep the first.
            continue
        touched_keys.add(nkey)
        existing = conn.execute(
            """SELECT * FROM tl.table_rows
            WHERE table_id=%s AND natural_key=%s AND superseded_by IS NULL AND status='active'""",
            (table_id, nkey),
        ).fetchone()

        if existing:
            # TBL-4: Hand-corrected (owner_locked) rows survive all later runs untouched.
            if existing["owner_locked"]:
                skipped_locked += 1
                continue
            # If identical data & sources & job_version, keep as-is (idempotent).
            if (
                existing["data"] == data
                and list(existing["source_message_ids"] or []) == src_ids
                and existing["job_version"] == job_version
            ):
                continue
            # Supersede existing row cleanly with a new version (mark old superseded first to satisfy partial unique index).
            conn.execute(
                "UPDATE tl.table_rows SET status='superseded', updated_at=clock_timestamp() WHERE id=%s",
                (existing["id"],),
            )
            new_row = conn.execute(
                """INSERT INTO tl.table_rows(
                    table_id, natural_key, window_key, data, version, status,
                    confidence, method, evidence, no_source_reason,
                    job, job_version, owner_locked, source_message_ids
                ) VALUES (%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s,false,%s)
                RETURNING id""",
                (
                    table_id,
                    nkey,
                    wkey,
                    Jsonb(data),
                    int(existing["version"]) + 1,
                    row["confidence"],
                    row["method"],
                    Jsonb(row["evidence"]),
                    row["no_source_reason"],
                    job_name,
                    job_version,
                    src_ids,
                ),
            ).fetchone()
            new_id = int(new_row["id"])
            conn.execute(
                "UPDATE tl.table_rows SET superseded_by=%s WHERE id=%s",
                (new_id, existing["id"]),
            )
            if src_ids:
                lineage.add_provenance(
                    conn,
                    job_name,
                    "table_row",
                    new_id,
                    src_ids,
                    relation="derived_from",
                    origin="job",
                    job=job_name,
                    job_version=job_version,
                )
            updated += 1
            written_ids.append(new_id)
        else:
            new_row = conn.execute(
                """INSERT INTO tl.table_rows(
                    table_id, natural_key, window_key, data, version, status,
                    confidence, method, evidence, no_source_reason,
                    job, job_version, owner_locked, source_message_ids
                ) VALUES (%s,%s,%s,%s,1,'active',%s,%s,%s,%s,%s,%s,false,%s)
                RETURNING id""",
                (
                    table_id,
                    nkey,
                    wkey,
                    Jsonb(data),
                    row["confidence"],
                    row["method"],
                    Jsonb(row["evidence"]),
                    row["no_source_reason"],
                    job_name,
                    job_version,
                    src_ids,
                ),
            ).fetchone()
            new_id = int(new_row["id"])
            if src_ids:
                lineage.add_provenance(
                    conn,
                    job_name,
                    "table_row",
                    new_id,
                    src_ids,
                    relation="derived_from",
                    origin="job",
                    job=job_name,
                    job_version=job_version,
                )
            written += 1
            written_ids.append(new_id)

    # JOB-7: When re-running a window due to late data, retract stale non-locked rows from that window.
    if work_item.get("is_late_rerun"):
        stale = conn.execute(
            """SELECT id, natural_key FROM tl.table_rows
            WHERE table_id=%s AND window_key=%s AND superseded_by IS NULL
              AND status='active' AND owner_locked IS FALSE""",
            (table_id, wkey),
        ).fetchall()
        for s in stale:
            if s["natural_key"] not in touched_keys:
                conn.execute(
                    "UPDATE tl.table_rows SET status='retracted', updated_at=clock_timestamp() WHERE id=%s",
                    (s["id"],),
                )

    # JOB-3 verify: confirm every newly written row is readable before advancing the cursor.
    if written_ids:
        found = conn.execute(
            "SELECT count(*) AS n FROM tl.table_rows WHERE id = ANY(%s)",
            (written_ids,),
        ).fetchone()["n"]
        if int(found) != len(written_ids):
            raise RuntimeError("verify-then-advance check failed: written row missing")

    if on_before_commit is not None:
        on_before_commit(wkey)

    if advance_cursor:
        cur = conn.execute(
            "SELECT * FROM tl.table_cursors WHERE table_id=%s FOR UPDATE",
            (table_id,),
        ).fetchone()
        if not cur:
            conn.execute("INSERT INTO tl.table_cursors(table_id) VALUES (%s)", (table_id,))
            cur = {"last_message_id": 0, "last_ingested_at": None, "done_ids": [], "processed_windows": {}}
        mids = work_item.get("message_ids") or []
        if work_item.get("cursor_last_id") is not None:
            max_id = int(work_item["cursor_last_id"])
        else:
            max_id = max(mids) if mids else int(cur["last_message_id"])
        new_last_id = max(int(cur["last_message_id"]), max_id)
        ingested_times = [m["ingested_at"] for m in (work_item.get("messages") or []) if m.get("ingested_at") is not None]
        max_ing = max(ingested_times) if ingested_times else cur["last_ingested_at"]
        if cur["last_ingested_at"] is not None and max_ing is not None:
            max_ing = max(cur["last_ingested_at"], max_ing)
        windows = dict(cur.get("processed_windows") or {})
        windows[wkey] = {
            "job_version": job_version,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        if work_item.get("day_key"):
            windows[work_item["day_key"]] = dict(windows[wkey], max_id=int(work_item["day_max_id"]))
        conn.execute(
            """UPDATE tl.table_cursors
            SET last_message_id=%s, last_ingested_at=%s, processed_windows=%s, updated_at=clock_timestamp()
            WHERE table_id=%s""",
            (new_last_id, max_ing, Jsonb(windows), table_id),
        )

    return {"written": written, "updated": updated, "skipped_locked": skipped_locked}


# --- Duplicate cleanup -------------------------------------------------------------


def dedupe_lens(name: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Merge rows the lens model judges to be the same real-world item: the kept row
    gets a new version citing every duplicate's sources, duplicates are retracted.
    Hand-locked rows are never changed. run_lens calls it after a run that wrote rows
    when worker_config.dedupe is true."""
    with connect("app") as conn:
        tdef = get_lens(name, conn=conn)
        table_id = int(tdef["id"])
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT id, data FROM tl.table_rows
                WHERE table_id=%s AND superseded_by IS NULL AND status='active' ORDER BY id""",
                (table_id,),
            ).fetchall()
        ]
    groups, tokens = find_duplicate_groups(tdef, rows)
    if dry_run or not groups:
        return {"lens": name, "groups": groups, "merged": 0, "tokens": tokens}
    job_name = f"table:{name}"
    job_version = str(tdef["version"])
    merged = 0
    with connect("app") as conn:
        for g in groups:
            keep = conn.execute(
                """SELECT * FROM tl.table_rows
                WHERE id=%s AND table_id=%s AND superseded_by IS NULL AND status='active' FOR UPDATE""",
                (g["keep_id"], table_id),
            ).fetchone()
            if not keep:
                continue
            dups = conn.execute(
                """SELECT id, source_message_ids FROM tl.table_rows
                WHERE id = ANY(%s) AND table_id=%s AND superseded_by IS NULL AND status='active'
                  AND owner_locked IS FALSE FOR UPDATE""",
                (g["duplicate_ids"], table_id),
            ).fetchall()
            if not dups:
                continue
            dup_ids = [int(d["id"]) for d in dups]
            src = sorted(set(keep["source_message_ids"] or []).union(*[set(d["source_message_ids"] or []) for d in dups]))
            conn.execute(
                "UPDATE tl.table_rows SET status='retracted', updated_at=clock_timestamp() WHERE id = ANY(%s)",
                (dup_ids,),
            )
            merged += len(dups)
            if keep["owner_locked"] or src == sorted(keep["source_message_ids"] or []):
                continue
            conn.execute(
                "UPDATE tl.table_rows SET status='superseded', updated_at=clock_timestamp() WHERE id=%s",
                (keep["id"],),
            )
            new_id = int(
                conn.execute(
                    """INSERT INTO tl.table_rows(
                        table_id, natural_key, window_key, data, version, status,
                        confidence, method, evidence, no_source_reason,
                        job, job_version, owner_locked, source_message_ids
                    ) VALUES (%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s,false,%s) RETURNING id""",
                    (
                        table_id,
                        keep["natural_key"],
                        keep["window_key"],
                        Jsonb(keep["data"]),
                        int(keep["version"]) + 1,
                        keep["confidence"],
                        keep["method"],
                        Jsonb(dict(keep["evidence"] or {}, merged_row_ids=dup_ids)),
                        keep["no_source_reason"],
                        job_name,
                        job_version,
                        src,
                    ),
                ).fetchone()["id"]
            )
            conn.execute("UPDATE tl.table_rows SET superseded_by=%s WHERE id=%s", (new_id, keep["id"]))
            lineage.add_provenance(
                conn,
                job_name,
                "table_row",
                new_id,
                src,
                relation="derived_from",
                origin="job",
                job=job_name,
                job_version=job_version,
            )
    return {"lens": name, "groups": groups, "merged": merged, "tokens": tokens}


# --- Cost quoting (JOB-6, CST-1) -------------------------------------------------


def quote_lens(
    name: str,
    *,
    backfill: bool = False,
    limit: int | None = None,
    since: str | None = None,
    until: str | None = None,
    message_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Quote items, tokens, and USD cost before enabling or running a table (JOB-6)."""
    with connect("app") as conn:
        tdef = get_lens(name, conn=conn)
        cur = tdef.get("cursor") or {"last_message_id": 0, "done_ids": [], "processed_windows": {}}
        max_items = limit or int(tdef["max_items_per_run"])
        after_id = 0 if backfill else int(cur.get("last_message_id") or 0)
        done_ids = [] if backfill else list(cur.get("done_ids") or [])
        if tdef["grain"] == "day" and message_ids is None:
            items = _pending_day_items(
                conn, tdef, cur, backfill=backfill, since=since, until=until, max_days=max_items
            )
        else:
            msgs = _fetch_messages(
                conn,
                list(tdef["channels"]),
                after_message_id=after_id,
                done_ids=done_ids,
                message_ids=message_ids,
                since=since if message_ids is not None else _effective_since(tdef, since),
                until=until,
                limit=max_items * (20 if tdef["grain"] != "message" else 1),
                filter_sql=(tdef.get("worker_config") or {}).get("filter"),
            )
            items = _group_into_work_items(msgs, tdef["grain"], _day_max_chars(tdef))[:max_items]
        ctx_rows = _run_context_query(tdef)
        est = estimate_item_cost(tdef, items, ctx_rows)
        ok_cap, spent, cap = check_monthly_cap(conn, est["cost_usd"])
        return {
            "lens": tdef["name"],
            "version": tdef["version"],
            "backfill": backfill,
            **est,
            "max_cost_per_run": tdef["max_cost_per_run"],
            "monthly_spent_usd": round(spent, 6),
            "monthly_cap_usd": cap,
            "within_monthly_cap": ok_cap,
        }


# --- Single table execution ------------------------------------------------------


# pg advisory lock class for "one cursor run of a lens at a time" (key 2: table id).
LENS_LOCK_CLASS = 32277


@contextmanager
def _lens_lock(table_id: int):
    """Yields True when this process now holds the lens's cursor-run lock,
    False when another run holds it. The session lock sits on an otherwise
    idle connection, so no transaction stays open while a model call is out."""
    with connect("app") as guard:
        # The app role may call pg_advisory_lock but not pg_try_advisory_lock
        # (migration 0024); a short lock_timeout makes it a try.
        try:
            guard.execute("SET lock_timeout = '200ms'")
            guard.execute("SELECT pg_advisory_lock(%s,%s)", (LENS_LOCK_CLASS, table_id))
            got = True
        except psycopg.errors.LockNotAvailable:
            got = False
        guard.rollback()
        guard.execute("SET lock_timeout = 0")
        guard.commit()
        try:
            yield got
        finally:
            if got:
                try:
                    guard.rollback()
                    guard.execute("SELECT pg_advisory_unlock(%s,%s)", (LENS_LOCK_CLASS, table_id))
                except Exception:
                    guard.close()  # never return a connection that still holds the lock
                    raise


def _plan_items(
    conn,
    tdef: dict[str, Any],
    cur: dict[str, Any],
    *,
    backfill: bool = False,
    since: str | None = None,
    until: str | None = None,
    message_ids: list[int] | None = None,
    max_items: int,
) -> tuple[list[dict[str, Any]], bool]:
    """The work items one run of this lens would read, in order, and whether
    more are pending past max_items. Also used by lenses.coalesce to see which
    items a sibling lens is about to read."""
    if tdef.get("output") == "pages" and (tdef.get("worker_config") or {}).get("input") == "flagged" and message_ids is None:
        # Upkeep lenses read the pages health flags, not new records (docs/v11/pages.md).
        from ..pages.health import flagged_items

        return flagged_items(conn, tdef, max_items)
    after_id = 0 if backfill else int(cur.get("last_message_id") or 0)
    done_ids = [] if backfill else list(cur.get("done_ids") or [])
    day_grain = tdef["grain"] == "day" and message_ids is None
    more_pending = False
    if day_grain:
        items = _pending_day_items(
            conn, tdef, cur, backfill=backfill, since=since, until=until, max_days=max_items
        )
        more_pending = len({it.get("day") for it in items}) >= max_items
    else:
        msgs = _fetch_messages(
            conn,
            list(tdef["channels"]),
            after_message_id=after_id,
            done_ids=done_ids,
            message_ids=message_ids,
            since=since if message_ids is not None else _effective_since(tdef, since),
            until=until,
            limit=max_items * (20 if tdef["grain"] != "message" else 1),
            filter_sql=(tdef.get("worker_config") or {}).get("filter"),
        )
        grouped = _group_into_work_items(msgs, tdef["grain"], _day_max_chars(tdef))
        fetch_cap = max_items * (20 if tdef["grain"] != "message" else 1)
        more_pending = len(grouped) > max_items or (message_ids is None and len(msgs) >= fetch_cap)
        items = grouped[:max_items]
    # JOB-7: also pick up already-processed windows that received late backfilled messages.
    # (Day grain already re-reads any day with newer messages.)
    if not backfill and message_ids is None and not day_grain:
        late_items = _detect_late_windows(conn, tdef, cur)
        seen_wkeys = {it["window_key"] for it in items}
        for li in late_items:
            if li["window_key"] not in seen_wkeys:
                items.append(li)
                seen_wkeys.add(li["window_key"])
        more_pending = more_pending or len(items) > max_items
        items = items[:max_items]
    return items, more_pending


def run_lens(
    name: str,
    *,
    trigger_kind: str = "manual",
    backfill: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    since: str | None = None,
    until: str | None = None,
    message_ids: list[int] | None = None,
    max_cost: float | None = None,
    on_before_commit: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute one lens with isolated cursor, verify-then-advance, and cost guard.

    A cursor run (no message_ids, no backfill) holds the lens's run lock: a
    second cursor run of the same lens returns status 'busy' without calling
    anything. Holding the lock also proves any 'running' cursor run row left
    by a crashed process is abandoned; it is closed as 'failed' so the
    scheduler resumes from the cursor."""
    kwargs = dict(
        trigger_kind=trigger_kind,
        backfill=backfill,
        dry_run=dry_run,
        limit=limit,
        since=since,
        until=until,
        message_ids=message_ids,
        max_cost=max_cost,
        on_before_commit=on_before_commit,
    )
    if message_ids is not None or backfill or dry_run:
        return _run_lens(name, **kwargs)
    with connect("app") as conn:
        table_id = int(get_lens(name, conn=conn)["id"])
    with _lens_lock(table_id) as got:
        if not got:
            return {"lens": name, "status": "busy", "reason": "another cursor run of this lens is in progress"}
        with connect("app") as conn:
            conn.execute(
                """UPDATE tl.table_runs
                SET status='failed', finished_at=clock_timestamp(),
                    detail=detail || '{"reason":"abandoned"}'::jsonb
                WHERE table_id=%s AND status='running'
                  AND coalesce(detail->>'mode','cursor')='cursor'
                  AND trigger_kind NOT IN ('rule','backfill')""",
                (table_id,),
            )
        return _run_lens(name, **kwargs)


def _run_lens(
    name: str,
    *,
    trigger_kind: str = "manual",
    backfill: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    since: str | None = None,
    until: str | None = None,
    message_ids: list[int] | None = None,
    max_cost: float | None = None,
    on_before_commit: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    with connect("app") as conn:
        tdef = get_lens(name, conn=conn)
        table_id = int(tdef["id"])
        cur = tdef.get("cursor") or {"last_message_id": 0, "last_ingested_at": None, "done_ids": [], "processed_windows": {}}

    max_items = int(limit or tdef["max_items_per_run"])
    budget_limit = float(tdef["max_cost_per_run"] if max_cost is None else max_cost)
    with connect("app") as conn:
        items, more_pending = _plan_items(
            conn, tdef, cur, backfill=backfill, since=since, until=until, message_ids=message_ids, max_items=max_items
        )

    ctx_rows = _run_context_query(tdef)
    est = estimate_item_cost(tdef, items, ctx_rows)
    # "cursor" runs move the lens forward; lenses.scheduler resumes them after
    # an outage. "ids" and "backfill" runs never move the cursor.
    mode = "ids" if message_ids is not None else ("backfill" if backfill else "cursor")
    if dry_run:
        return {
            "lens": tdef["name"],
            "status": "dry_run",
            "considered": len(items),
            "estimate": est,
        }

    with connect("app") as conn:
        ok_cap, spent, cap = check_monthly_cap(conn, est["cost_usd"])
        if not ok_cap:
            run_row = conn.execute(
                """INSERT INTO tl.table_runs(table_id, trigger_kind, status, considered, detail, finished_at)
                VALUES (%s,%s,'budget_exhausted',%s,%s,clock_timestamp()) RETURNING id""",
                (
                    table_id,
                    trigger_kind,
                    len(items),
                    Jsonb({"reason": "monthly_cap_reached", "spent": spent, "cap": cap, "mode": mode}),
                ),
            ).fetchone()
            return {
                "run_id": int(run_row["id"]),
                "lens": tdef["name"],
                "status": "budget_exhausted",
                "reason": "monthly_cap_reached",
                "monthly_spent_usd": spent,
                "monthly_cap_usd": cap,
                "considered": len(items),
                "processed": 0,
                "written": 0,
                "updated": 0,
                "skipped_locked": 0,
                "invalid": 0,
                "model_calls": 0,
                "tokens": 0,
                "cost": 0.0,
            }
        run_row = conn.execute(
            """INSERT INTO tl.table_runs(table_id, trigger_kind, status, considered, detail)
            VALUES (%s,%s,'running',%s,%s) RETURNING id""",
            (table_id, trigger_kind, len(items), Jsonb({"mode": mode})),
        ).fetchone()
        run_id = int(run_row["id"])

    totals = {
        "processed": 0,
        "written": 0,
        "updated": 0,
        "skipped_locked": 0,
        "invalid": 0,
        "model_calls": 0,
        "tokens": 0,
        "cost": 0.0,
    }
    errors: list[str] = []
    status = "done"
    stop_reason: str | None = None
    advance_cursor = (message_ids is None) and not backfill
    # lenses.coalesce: during a scheduler tick, a cursor run may share model
    # calls with sibling lenses that read the same items with the same model.
    share = coalesce.current(tdef["name"]) if mode == "cursor" and tdef.get("output") != "pages" else None
    riding = bool(share and share.riding(tdef["name"]))
    shared_stats = {"shared_calls": 0, "saved_tokens": 0, "shared_with": []}

    def _sql_runner(stmt: str) -> dict[str, Any]:
        return query.query_ex(stmt, cap=50, scope=Scope.local())

    try:
        for item in items:
            if totals["cost"] > budget_limit and budget_limit > 0:
                status = "budget_exhausted"
                stop_reason = "max_cost_per_run"
                break
            with connect("app") as conn:
                ok_cap, _, _ = check_monthly_cap(conn)
                if not ok_cap:
                    status = "budget_exhausted"
                    stop_reason = "monthly_cap_reached"
                    break
                filtered_item = _filter_egress_held(conn, tdef, item)
                if not filtered_item["messages"]:
                    # Nothing left after egress hold; advance cursor without remote call.
                    if advance_cursor:
                        _write_window_rows_and_advance(conn, tdef, item, [], advance_cursor=True)
                    totals["processed"] += 1
                    continue
                corrections = _load_owner_corrections(conn, table_id)
                if share is not None:
                    wres = share.work(conn, tdef, filtered_item, ctx_rows, corrections, sql_runner=_sql_runner)
                    if wres is None:
                        # Riding along: the rest was not answered by a shared
                        # call; this lens reads it at its own scheduled run.
                        break
                else:
                    wres = execute_worker(
                        conn,
                        tdef,
                        filtered_item,
                        ctx_rows,
                        corrections,
                        sql_runner=_sql_runner,
                    )
                if wres.shared_with:
                    shared_stats["shared_calls"] += 1
                    shared_stats["saved_tokens"] += wres.saved_tokens
                    shared_stats["shared_with"] = sorted(set(shared_stats["shared_with"]) | set(wres.shared_with))
                totals["model_calls"] += wres.calls
                totals["tokens"] += wres.tokens
                totals["cost"] += wres.cost
                totals["invalid"] += wres.invalid
                errors.extend(wres.errors)

                counts = _write_window_rows_and_advance(
                    conn,
                    tdef,
                    item,
                    wres.rows,
                    advance_cursor=(advance_cursor or bool(item.get("is_late_rerun"))) and not item.get("upkeep"),
                    on_before_commit=on_before_commit,
                )
                totals["processed"] += 1
                totals["written"] += counts["written"]
                totals["updated"] += counts["updated"]
                totals["skipped_locked"] += counts["skipped_locked"]
        if status == "done" and more_pending and advance_cursor and not riding:
            # A backlog (e.g. after an outage) larger than one run: the
            # scheduler keeps going on its next tick instead of waiting for
            # the next cron fire.
            status = "max_items"
    except ProviderUnavailable as exc:
        # The item in flight wrote nothing and the cursor stays before it, so
        # the next cursor run (lenses.scheduler.tick) backfills from there.
        status = "provider_unavailable"
        errors.append(str(exc))
    except Exception as exc:
        status = "failed"
        errors.append(str(exc))
        with connect("app") as conn:
            conn.execute(
                """UPDATE tl.table_runs SET status='failed', processed=%s, written=%s, updated=%s,
                    skipped_locked=%s, invalid=%s, model_calls=%s, tokens=%s, cost=%s,
                    detail=%s, finished_at=clock_timestamp()
                WHERE id=%s""",
                (
                    totals["processed"],
                    totals["written"],
                    totals["updated"],
                    totals["skipped_locked"],
                    totals["invalid"],
                    totals["model_calls"],
                    totals["tokens"],
                    totals["cost"],
                    Jsonb({"errors": errors[:20], "mode": mode}),
                    run_id,
                ),
            )
        raise

    with connect("app") as conn:
        conn.execute(
            """UPDATE tl.table_runs SET status=%s, processed=%s, written=%s, updated=%s,
                skipped_locked=%s, invalid=%s, model_calls=%s, tokens=%s, cost=%s,
                detail=%s, finished_at=clock_timestamp()
            WHERE id=%s""",
            (
                status,
                totals["processed"],
                totals["written"],
                totals["updated"],
                totals["skipped_locked"],
                totals["invalid"],
                totals["model_calls"],
                totals["tokens"],
                totals["cost"],
                Jsonb(
                    {
                        "errors": errors[:20],
                        "mode": mode,
                        **({"reason": stop_reason} if stop_reason else {}),
                        **({"shared": shared_stats} if shared_stats["shared_calls"] else {}),
                        **({"ride": True} if riding else {}),
                    }
                ),
                run_id,
            ),
        )

    merged = 0
    cfg = tdef.get("worker_config") or {}
    if cfg.get("dedupe") and (totals["written"] or totals["updated"]):
        try:
            dedupe = dedupe_lens(tdef["name"])
            merged = dedupe["merged"]
            totals["model_calls"] += 1
            totals["tokens"] += dedupe["tokens"]
        except ProviderUnavailable as exc:
            # The next run that writes rows tries again; nothing is lost.
            errors.append(f"dedupe: {exc}")

    return {
        "run_id": run_id,
        "lens": tdef["name"],
        "status": status,
        "considered": len(items),
        **totals,
        "merged": merged,
        **({"reason": stop_reason} if stop_reason else {}),
        **({"shared": shared_stats} if shared_stats["shared_calls"] else {}),
        **({"ride": True} if riding else {}),
        "errors": errors[:20],
    }


# --- Dependency-ordered & single-pass merged batch runner (JOB-4, JOB-5) ---------


def _topo_layers(defs: dict[str, dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group tables into topological dependency layers (JOB-5)."""
    indeg: dict[str, int] = {name: 0 for name in defs}
    dependents: dict[str, list[str]] = defaultdict(list)
    for name, td in defs.items():
        for dep in td.get("depends_on") or []:
            if dep in defs:
                indeg[name] += 1
                dependents[dep].append(name)
    ready = deque(sorted(n for n, d in indeg.items() if d == 0))
    layers: list[list[dict[str, Any]]] = []
    seen = 0
    while ready:
        batch = list(ready)
        ready.clear()
        layers.append([defs[n] for n in batch])
        seen += len(batch)
        next_ready: list[str] = []
        for n in batch:
            for child in dependents[n]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    next_ready.append(child)
        for n in sorted(next_ready):
            ready.append(n)
    if seen != len(defs):
        raise LensSpecError("cyclic depends_on detected across lenses")
    return layers


def _merge_signature(td: dict[str, Any]) -> tuple | None:
    """Signature for JOB-4 single-pass merging: identical inputs, worker, model and schedule."""
    if td["worker"] != "llm":
        return None
    cfg = td.get("worker_config") or {}
    return (
        tuple(sorted(td.get("channels") or [])),
        td["grain"],
        td["worker"],
        str(cfg.get("model") or ""),
        str(cfg.get("base_url") or ""),
        str(td.get("schedule") or ""),
    )


def run_batch(
    names: list[str] | None = None,
    *,
    trigger_kind: str = "batch",
    limit: int | None = None,
) -> dict[str, Any]:
    """Run multiple tables in dependency order (JOB-5), merging identical LLM inputs into 1 call (JOB-4)."""
    with connect("app") as conn:
        if names:
            defs = {n: get_lens(n, conn=conn) for n in names}
        else:
            all_defs = list_lenses(conn=conn)
            defs = {d["name"]: get_lens(d["name"], conn=conn) for d in all_defs if d["enabled"]}

    if not defs:
        return {"lenses": [], "total_model_calls": 0, "execution_order": []}

    layers = _topo_layers(defs)
    results: list[dict[str, Any]] = []
    execution_order: list[str] = []
    total_calls = 0

    for layer in layers:
        # Partition this layer into mergeable groups (JOB-4) vs standalone tables.
        groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        standalone: list[dict[str, Any]] = []
        for td in layer:
            sig = _merge_signature(td)
            if sig is not None:
                groups[sig].append(td)
            else:
                standalone.append(td)

        for td in standalone:
            r = run_lens(td["name"], trigger_kind=trigger_kind, limit=limit)
            results.append(r)
            execution_order.append(td["name"])
            total_calls += int(r.get("model_calls") or 0)

        for sig, group in groups.items():
            if len(group) == 1:
                r = run_lens(group[0]["name"], trigger_kind=trigger_kind, limit=limit)
                results.append(r)
                execution_order.append(group[0]["name"])
                total_calls += int(r.get("model_calls") or 0)
                continue

            # Merged single-pass execution (JOB-4): all tables in `group` share channels/grain/worker/schedule.
            max_items = int(limit or min(int(td["max_items_per_run"]) for td in group))
            min_cursor = min(int((td.get("cursor") or {}).get("last_message_id") or 0) for td in group)
            with connect("app") as conn:
                msgs = _fetch_messages(
                    conn,
                    list(group[0]["channels"]),
                    after_message_id=min_cursor,
                    limit=max_items * (20 if group[0]["grain"] != "message" else 1),
                )
            items = _group_into_work_items(msgs, group[0]["grain"], _day_max_chars(group[0]))[:max_items]
            ctx_by_table = {td["name"]: _run_context_query(td) for td in group}

            per_table = {
                td["name"]: {
                    "lens": td["name"],
                    "status": "done",
                    "merged_with": [x["name"] for x in group if x["name"] != td["name"]],
                    "considered": len(items),
                    "processed": 0,
                    "written": 0,
                    "updated": 0,
                    "skipped_locked": 0,
                    "invalid": 0,
                    "model_calls": 0,
                    "tokens": 0,
                    "cost": 0.0,
                    "errors": [],
                }
                for td in group
            }

            for item in items:
                with connect("app") as conn:
                    corr_by_table = {td["name"]: _load_owner_corrections(conn, int(td["id"])) for td in group}
                    try:
                        batch_out = run_merged_llm_batch(conn, group, item, ctx_by_table, corr_by_table)
                    except ProviderUnavailable as exc:
                        # Nothing written for this item; every cursor stays before it.
                        for pt in per_table.values():
                            pt["status"] = "provider_unavailable"
                            pt["errors"].append(str(exc))
                        break
                    for td in group:
                        tname = td["name"]
                        wres = batch_out[tname]
                        counts = _write_window_rows_and_advance(conn, td, item, wres.rows, advance_cursor=True)
                        pt = per_table[tname]
                        pt["processed"] += 1
                        pt["written"] += counts["written"]
                        pt["updated"] += counts["updated"]
                        pt["skipped_locked"] += counts["skipped_locked"]
                        pt["invalid"] += wres.invalid
                        pt["model_calls"] += wres.calls
                        pt["tokens"] += wres.tokens
                        pt["cost"] += wres.cost
                        pt["errors"].extend(wres.errors)

            for td in group:
                pt = per_table[td["name"]]
                results.append(pt)
                execution_order.append(td["name"])
                total_calls += int(pt["model_calls"])

    return {
        "lenses": results,
        "total_model_calls": total_calls,
        "execution_order": execution_order,
    }


# --- Hand corrections (TBL-4) & gated reads (ACL-4, JOB-5 lazy fill) -------------


def edit_row(
    table_name: str,
    row_id: int,
    patch_data: dict[str, Any],
    *,
    note: str | None = None,
    lock: bool = True,
    status: str | None = None,
    conn=None,
) -> dict[str, Any]:
    """Owner hand-correction of a row: locks against later job overwrites and stores feedback note (TBL-4)."""
    table_name = validate_lens_name(table_name)

    def _apply(c):
        tdef = get_lens(table_name, conn=c)
        existing = c.execute(
            "SELECT * FROM tl.table_rows WHERE id=%s AND table_id=%s",
            (int(row_id), int(tdef["id"])),
        ).fetchone()
        if not existing:
            raise LookupError(f"row {row_id} not found in table '{table_name}'")
        if existing["superseded_by"] is not None:
            raise LensSpecError(f"row {row_id} is already superseded")

        merged_data = dict(existing["data"] or {})
        merged_data.update(patch_data or {})
        cleaned_data = validate_row_data(tdef["columns"], merged_data)
        new_status = status or existing["status"]
        if new_status not in ("active", "retracted"):
            raise LensSpecError("status must be 'active' or 'retracted'")

        c.execute(
            "UPDATE tl.table_rows SET status='superseded', updated_at=clock_timestamp() WHERE id=%s",
            (existing["id"],),
        )
        new_row = c.execute(
            """INSERT INTO tl.table_rows(
                table_id, natural_key, window_key, data, version, status,
                confidence, method, evidence, no_source_reason,
                job, job_version, owner_locked, owner_correction_note, source_message_ids
            ) VALUES (%s,%s,%s,%s,%s,%s,1.0,'owner',%s,%s,%s,%s,%s,%s,%s)
            RETURNING *""",
            (
                existing["table_id"],
                existing["natural_key"],
                existing["window_key"],
                Jsonb(cleaned_data),
                int(existing["version"]) + 1,
                new_status,
                Jsonb(existing["evidence"] or {}),
                existing["no_source_reason"],
                existing["job"],
                existing["job_version"],
                bool(lock),
                (note.strip() if isinstance(note, str) and note.strip() else existing["owner_correction_note"]),
                list(existing["source_message_ids"] or []),
            ),
        ).fetchone()
        c.execute(
            "UPDATE tl.table_rows SET superseded_by=%s WHERE id=%s",
            (new_row["id"], existing["id"]),
        )
        src_ids = list(existing["source_message_ids"] or [])
        if src_ids:
            lineage.add_provenance(
                c,
                "owner",
                "table_row",
                int(new_row["id"]),
                src_ids,
                relation="derived_from",
                origin="owner",
            )
        return dict(new_row)

    if conn is not None:
        return _apply(conn)
    with connect("app") as c:
        return _apply(c)


def query_lens_rows(
    table_name: str,
    *,
    scope: Scope | None = None,
    limit: int = 100,
    include_versions: bool = False,
) -> dict[str, Any]:
    """Read rows from api.lens_<name> through the gated reader path (supports JOB-5 lazy_backfill)."""
    table_name = validate_lens_name(table_name)
    scope = scope or Scope.local()
    # JOB-5: If unrestricted and table has lazy_backfill=true, fill pending items on read.
    if scope.mode == "owner":
        with connect("app") as conn:
            row = conn.execute(
                "SELECT id, lazy_backfill, enabled FROM tl.table_defs WHERE name=%s",
                (table_name,),
            ).fetchone()
            if not row:
                raise LookupError(f"lens '{table_name}' not found")
            lazy = bool(row["lazy_backfill"] and row["enabled"])
        if lazy:
            run_lens(table_name, trigger_kind="lazy")

    view_name = f"lens_{table_name}_versions" if include_versions else f"lens_{table_name}"
    stmt = f"SELECT * FROM api.{view_name} ORDER BY id DESC LIMIT {int(max(1, min(limit, 1000)))}"
    try:
        res = query.query_ex(stmt, cap=max(1, min(limit, 1000)), scope=scope)
    except Exception as exc:
        if "does not exist" in str(exc).lower():
            raise LookupError(f"lens '{table_name}' not found") from None
        raise
    return {"lens": table_name, "view": f"api.{view_name}", "rows": res["rows"], "row_count": res["row_count"]}
