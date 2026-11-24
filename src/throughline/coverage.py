"""Source coverage (PLG-7, BEN-20, LIB-9, ACL-8).

What each source says it delivered, per channel and scope key, stored in
tl.coverage (latest state), tl.coverage_reports (append-only log of what was
received and what it was normalised to) and tl.coverage_gaps (windows the
source did not deliver).

Principle: a strict gate must never turn partial data
into zero progress. normalize() never raises on shape: an unknown status
degrades to 'partial' with the raw value kept, a detail that is not an object
is wrapped, an unparsable timestamp becomes null, an unknown reason code is
kept in raw_detail. Each of these is listed as a degradation reason, and the
report is stored anyway.

Gaps open only from an explicit report.gaps entry or over the report window
of a report whose normalised status is 'partial'. A best_effort report opens
no window gap (sources without history listings would pile up permanent
gaps). A gap closes only when a report names it in `closes` (by id, or by
{channel, scope_key, from, to}) or covers [gap_from, gap_to] within its
window with status complete and (rows_delivered > 0 or verified true). A
healthy empty report without verified never closes an earlier failed
interval.

Writers take an app-role connection; routes authorize first. Readers
(summary, recall_summary, packet_coverage) run on the reader path, where the
database gate shows only the channels the asker may read.
"""

import json
import os
from datetime import datetime, timezone

from psycopg import sql
from psycopg.types.json import Jsonb

from .query import query_ex
from .scope import Scope

STATUSES = ("complete", "partial", "best_effort")
# Worst first: the status of several channels is the first one present.
ORDER = ("best_effort", "partial", "complete")
REASONS = (
    "not_reported",
    "run_failed",
    "stale",
    "source_cap",
    "source_error",
    "rate_limited",
    "auth_failed",
    "quarantined_rows",
    "filtered_by_policy",
    "listing_lag",
    "partial_history",
    "unknown_shape",
    "unrecognised_status",
    "unrecognised_reason",
    "retry_pending",
    "empty_verified",
)
DEFAULT_SYNONYMS = ("ok", "success", "done", "empty", "empty_thread")
EMPTY_LIKE = ("empty", "empty_thread")
MAX_REPORTS = 1000
MAX_RECEIVED_BYTES = 64 * 1024
MAX_DETAIL_BYTES = 16 * 1024
MAX_ITEMS = 1000


def synonyms():
    """Statuses read as complete (THROUGHLINE_COVERAGE_COMPLETE_SYNONYMS,
    comma separated; default ok, success, done, empty, empty_thread). An
    empty-like one is complete only when the report is verified."""
    raw = os.getenv("THROUGHLINE_COVERAGE_COMPLETE_SYNONYMS")
    if raw is None:
        return set(DEFAULT_SYNONYMS)
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _time(value, degraded):
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            degraded.add("unknown_shape")
            return None
    else:
        degraded.add("unknown_shape")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _int(value, degraded):
    if value is None:
        return None
    if isinstance(value, bool):
        degraded.add("unknown_shape")
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        degraded.add("unknown_shape")
        return None
    if number < 0 or number > 2**62:
        degraded.add("unknown_shape")
        return None
    return number


def _size(value):
    return len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))


def _capped(value, limit):
    """value, or a marker of its size when its JSON is above limit bytes."""
    size = _size(value)
    if size <= limit:
        return value, False
    marker = {"truncated": True, "bytes": size}
    if isinstance(value, dict):
        marker["keys"] = sorted(str(k) for k in value)[:50]
    return marker, True


def _window(value, degraded):
    if value is None:
        return None, None
    if not isinstance(value, dict):
        degraded.add("unknown_shape")
        return None, None
    start = _time(value.get("from"), degraded)
    end = _time(value.get("to"), degraded)
    if start is not None and end is not None and end < start:
        degraded.add("unknown_shape")
        return None, None
    return start, end


def _gap(value, degraded):
    if not isinstance(value, dict):
        degraded.add("unknown_shape")
        return None
    start = _time(value.get("from"), degraded)
    end = _time(value.get("to"), degraded)
    if start is None or end is None or end < start:
        degraded.add("unknown_shape")
        return None
    reason = value.get("reason")
    if reason is None:
        reason = "source_error"
    elif not isinstance(reason, str) or reason not in REASONS:
        degraded.add("unrecognised_reason")
        reason = "unrecognised_reason"
    return {"from": start, "to": end, "reason": reason}


def _close(value, degraded):
    if isinstance(value, int) and not isinstance(value, bool):
        return {"id": value}
    if isinstance(value, str) and value.isdigit():
        return {"id": int(value)}
    if isinstance(value, dict):
        if "id" in value and set(value) <= {"id"}:
            number = _int(value["id"], degraded)
            return {"id": number} if number is not None else None
        start = _time(value.get("from"), degraded)
        end = _time(value.get("to"), degraded)
        channel = value.get("channel")
        scope_key = value.get("scope_key") or ""
        if start is None or end is None or not isinstance(scope_key, str):
            degraded.add("unknown_shape")
            return None
        if channel is not None and not isinstance(channel, str):
            degraded.add("unknown_shape")
            return None
        return {"channel": channel, "scope_key": scope_key, "from": start, "to": end}
    degraded.add("unknown_shape")
    return None


def _list(value, degraded):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        degraded.add("unknown_shape")
        return []
    items = list(value)
    if len(items) > MAX_ITEMS:
        degraded.add("unknown_shape")
        items = items[:MAX_ITEMS]
    return items


def normalize(report):
    """The normalised form of one coverage report. Never raises on shape.

    Returns a dict: channel, scope_key, status, reasons, raw_status,
    raw_detail, window_from, window_to, high_water, rows_delivered,
    pending_retries, gaps [{from, to, reason}], closes [{id} | {channel,
    scope_key, from, to}], verified, degraded (sorted reason codes of what
    was degraded; they are also among reasons).
    """
    degraded = set()
    if not isinstance(report, dict):
        report = {"detail": report}
        degraded.add("unknown_shape")
    channel = report.get("channel")
    if channel is not None and not isinstance(channel, str):
        degraded.add("unknown_shape")
        channel = str(channel)
    scope_key = report.get("scope_key")
    if scope_key is None:
        scope_key = ""
    elif not isinstance(scope_key, str):
        degraded.add("unknown_shape")
        scope_key = str(scope_key)
    scope_key = scope_key[:500]
    verified = report.get("verified") is True
    if "verified" in report and not isinstance(report.get("verified"), bool):
        if report.get("verified") is not None:
            degraded.add("unknown_shape")

    # Detail: an object is kept; anything else is wrapped.
    detail = report.get("detail")
    if detail is None:
        raw_detail = {}
    elif isinstance(detail, dict):
        raw_detail = dict(detail)
    else:
        raw_detail = {"value": detail}
        degraded.add("unknown_shape")

    # Status.
    raw = report.get("status")
    raw_status = None if raw is None else str(raw)[:200]
    word = raw.strip().lower() if isinstance(raw, str) else None
    reasons = []
    if word in STATUSES:
        status = word
    elif word is not None and word in synonyms():
        if word in EMPTY_LIKE:
            if verified:
                status = "complete"
                reasons.append("empty_verified")
            else:
                status = "partial"
                degraded.add("unrecognised_status")
        else:
            status = "complete"
    else:
        status = "partial"
        degraded.add("unrecognised_status")

    # Reasons: known codes are kept, unknown ones go to raw_detail.
    given = report.get("reasons")
    unknown = []
    if given is not None and not isinstance(given, (list, tuple)):
        degraded.add("unknown_shape")
        given = [given]
    for code in list(given or [])[:MAX_ITEMS]:
        if isinstance(code, str) and code in REASONS:
            if code not in reasons:
                reasons.append(code)
        else:
            unknown.append(code if isinstance(code, (str, int, float)) else str(code))
    if unknown:
        degraded.add("unrecognised_reason")
        raw_detail["unrecognised_reasons"] = unknown

    window_from, window_to = _window(report.get("window"), degraded)
    high_water = _time(report.get("high_water"), degraded)
    rows_delivered = _int(report.get("rows_delivered"), degraded)
    pending_retries = _int(report.get("pending_retries"), degraded)
    if pending_retries is not None and pending_retries > 2**31 - 1:
        pending_retries = 2**31 - 1
    gaps = [
        g for g in (_gap(v, degraded) for v in _list(report.get("gaps"), degraded)) if g
    ]
    closes = [
        c
        for c in (_close(v, degraded) for v in _list(report.get("closes"), degraded))
        if c
    ]
    raw_detail, truncated = _capped(raw_detail, MAX_DETAIL_BYTES)
    if truncated:
        degraded.add("unknown_shape")
    if degraded - {"unrecognised_reason"} and status == "complete":
        # A report whose shape was not understood is not proof of completeness.
        status = "partial"
    for code in sorted(degraded):
        if code not in reasons:
            reasons.append(code)
    return dict(
        channel=channel,
        scope_key=scope_key,
        status=status,
        reasons=reasons,
        raw_status=raw_status,
        raw_detail=raw_detail or None,
        window_from=window_from,
        window_to=window_to,
        high_water=high_water,
        rows_delivered=rows_delivered,
        pending_retries=pending_retries,
        gaps=gaps,
        closes=closes,
        verified=verified,
        degraded=sorted(degraded),
    )


def _wire(value):
    """JSON-safe copy (datetimes as ISO text)."""
    return json.loads(json.dumps(value, default=_default, ensure_ascii=False))


def _default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _received(report):
    try:
        value = _wire(report)
    except (TypeError, ValueError):
        value = {"unserialisable": True}
    size = _size(value)
    if size <= MAX_RECEIVED_BYTES:
        return value, False
    text = json.dumps(value, ensure_ascii=False)
    return {
        "truncated": True,
        "bytes": size,
        "head": text[: MAX_RECEIVED_BYTES // 2],
    }, True


def _channel(conn, plugin, name):
    """The id of a channel of this plugin, or None (unknown or foreign)."""
    if name is None:
        return None
    row = conn.execute(
        "SELECT id,plugin FROM tl.channels WHERE name=%s", (name,)
    ).fetchone()
    if not row or row["plugin"] != plugin:
        return False
    return row["id"]


def record(conn, plugin, report):
    """Store one report for `plugin` (app-role connection).

    Returns {stored, refused, report_id, status, reasons, degraded,
    gaps_opened: [{id, from, to}], gaps_closed: [ids]}. A report naming a
    channel that is not this plugin's (or does not exist) is refused and
    nothing is stored. Never raises on the report's shape.
    """
    normalized = normalize(report)
    received, truncated = _received(report)
    if truncated and "unknown_shape" not in normalized["degraded"]:
        normalized["degraded"].append("unknown_shape")
        if "unknown_shape" not in normalized["reasons"]:
            normalized["reasons"].append("unknown_shape")
    channel_id = _channel(conn, plugin, normalized["channel"])
    result = dict(
        stored=False,
        refused=False,
        report_id=None,
        status=normalized["status"],
        reasons=normalized["reasons"],
        degraded=normalized["degraded"],
        gaps_opened=[],
        gaps_closed=[],
    )
    if channel_id is False:
        result["refused"] = True
        return result
    scope_key = normalized["scope_key"]
    stored = _wire(dict(normalized, received_truncated=truncated))
    report_id = conn.execute(
        "INSERT INTO tl.coverage_reports(plugin,channel_id,scope_key,received,normalized) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (plugin, channel_id, scope_key, Jsonb(received), Jsonb(stored)),
    ).fetchone()["id"]
    status = normalized["status"]
    conn.execute(
        """INSERT INTO tl.coverage(plugin,channel_id,scope_key,status,reasons,raw_status,raw_detail,
             last_attempt_at,last_success_at,covered_from,covered_to,high_water_at,high_water_ingested_at,
             rows_delivered,pending_retries,updated_at)
           VALUES (%(plugin)s,%(channel_id)s,%(scope_key)s,%(status)s,%(reasons)s,%(raw_status)s,%(raw_detail)s,
             now(),CASE WHEN %(success)s THEN now() END,%(window_from)s,%(window_to)s,%(high_water)s,
             CASE WHEN %(high_water)s::timestamptz IS NOT NULL THEN now() END,%(rows_delivered)s,coalesce(%(pending_retries)s,0),now())
           ON CONFLICT (plugin,channel_id,scope_key) DO UPDATE SET
             status=EXCLUDED.status,reasons=EXCLUDED.reasons,raw_status=EXCLUDED.raw_status,raw_detail=EXCLUDED.raw_detail,
             last_attempt_at=now(),
             last_success_at=coalesce(EXCLUDED.last_success_at,tl.coverage.last_success_at),
             covered_from=least(tl.coverage.covered_from,EXCLUDED.covered_from),
             covered_to=greatest(tl.coverage.covered_to,EXCLUDED.covered_to),
             high_water_at=greatest(tl.coverage.high_water_at,EXCLUDED.high_water_at),
             high_water_ingested_at=CASE WHEN EXCLUDED.high_water_at>coalesce(tl.coverage.high_water_at,'-infinity'::timestamptz)
               THEN now() ELSE tl.coverage.high_water_ingested_at END,
             rows_delivered=coalesce(EXCLUDED.rows_delivered,tl.coverage.rows_delivered),
             pending_retries=EXCLUDED.pending_retries,
             updated_at=now()""",
        dict(
            plugin=plugin,
            channel_id=channel_id,
            scope_key=scope_key,
            status=status,
            reasons=normalized["reasons"],
            raw_status=normalized["raw_status"],
            raw_detail=Jsonb(normalized["raw_detail"])
            if normalized["raw_detail"] is not None
            else None,
            success=status != "partial",
            window_from=normalized["window_from"],
            window_to=normalized["window_to"],
            high_water=normalized["high_water"],
            rows_delivered=normalized["rows_delivered"],
            pending_retries=normalized["pending_retries"],
        ),
    )
    if channel_id is not None and scope_key != "" and status != "partial":
        # A run_failed row of an earlier failed run (the SDK posts it at scope
        # key '') is superseded by this run's successful report for a scope
        # key of the same channel: it becomes an attempt placeholder, which
        # api.coverage leaves out while the channel has reported rows.
        conn.execute(
            "UPDATE tl.coverage SET status='best_effort',reasons=ARRAY['not_reported'],raw_status=NULL,raw_detail=NULL,updated_at=now() WHERE plugin=%s AND channel_id=%s AND scope_key='' AND reasons=ARRAY['run_failed']",
            (plugin, channel_id),
        )
    closed = _close_gaps(conn, plugin, channel_id, scope_key, normalized, report_id)
    opened = []
    wanted = list(normalized["gaps"])
    if (
        not wanted
        and status == "partial"
        and normalized["window_from"] is not None
        and normalized["window_to"] is not None
    ):
        reason = next(
            (r for r in normalized["reasons"] if r not in ("unrecognised_reason",)),
            "source_error",
        )
        wanted.append(
            {
                "from": normalized["window_from"],
                "to": normalized["window_to"],
                "reason": reason,
            }
        )
    for gap in wanted:
        opened.append(_open_gap(conn, plugin, channel_id, scope_key, gap, report_id))
    result.update(
        stored=True, report_id=report_id, gaps_opened=opened, gaps_closed=closed
    )
    return result


def _open_gap(conn, plugin, channel_id, scope_key, gap, report_id):
    """Open a gap, or return the identical open one (same window)."""
    row = conn.execute(
        "SELECT id,gap_from,gap_to FROM tl.coverage_gaps WHERE plugin=%s AND channel_id IS NOT DISTINCT FROM %s AND scope_key=%s AND gap_from=%s AND gap_to=%s AND closed_at IS NULL ORDER BY id LIMIT 1",
        (plugin, channel_id, scope_key, gap["from"], gap["to"]),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "INSERT INTO tl.coverage_gaps(plugin,channel_id,scope_key,gap_from,gap_to,reason,opened_by_report) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id,gap_from,gap_to",
            (
                plugin,
                channel_id,
                scope_key,
                gap["from"],
                gap["to"],
                gap["reason"],
                report_id,
            ),
        ).fetchone()
    return {"id": row["id"], "from": row["gap_from"], "to": row["gap_to"]}


def _close_gaps(conn, plugin, channel_id, scope_key, normalized, report_id):
    closed = []

    def close(where, params):
        rows = conn.execute(
            sql.SQL(
                "UPDATE tl.coverage_gaps SET closed_at=now(),closed_by_report=%s WHERE closed_at IS NULL AND plugin=%s AND "
            )
            + sql.SQL(where)
            + sql.SQL(" RETURNING id"),
            (report_id, plugin, *params),
        ).fetchall()
        closed.extend(r["id"] for r in rows if r["id"] not in closed)

    for item in normalized["closes"]:
        if "id" in item:
            close("id=%s", (item["id"],))
            continue
        if item["channel"] is None:
            target = None
        else:
            target = _channel(conn, plugin, item["channel"])
            if target is False:
                continue
        close(
            "channel_id IS NOT DISTINCT FROM %s AND scope_key=%s AND gap_from=%s AND gap_to=%s",
            (target, item["scope_key"], item["from"], item["to"]),
        )
    if (
        normalized["status"] == "complete"
        and normalized["window_from"] is not None
        and normalized["window_to"] is not None
        and ((normalized["rows_delivered"] or 0) > 0 or normalized["verified"])
    ):
        close(
            "channel_id IS NOT DISTINCT FROM %s AND scope_key=%s AND gap_from>=%s AND gap_to<=%s",
            (channel_id, scope_key, normalized["window_from"], normalized["window_to"]),
        )
    return sorted(closed)


def record_many(conn, plugin, reports):
    """POST /v1/plugins/{name}/coverage: store every report of a batch.

    Returns {stored, degraded: [{index, reasons}], refused, gaps_opened:
    [{index, id, from, to}], gaps_closed: [ids]}.
    """
    if not isinstance(reports, list):
        raise ValueError("reports must be a list")
    if len(reports) > MAX_REPORTS:
        raise ValueError(f"at most {MAX_REPORTS} reports per request")
    answer = dict(stored=0, degraded=[], refused=0, gaps_opened=[], gaps_closed=[])
    for index, report in enumerate(reports):
        result = record(conn, plugin, report)
        if result["refused"]:
            answer["refused"] += 1
            continue
        answer["stored"] += 1
        if result["degraded"]:
            answer["degraded"].append(dict(index=index, reasons=result["degraded"]))
        for gap in result["gaps_opened"]:
            answer["gaps_opened"].append(dict(index=index, **gap))
        for gap_id in result["gaps_closed"]:
            if gap_id not in answer["gaps_closed"]:
                answer["gaps_closed"].append(gap_id)
    return answer


def attempt(conn, plugin, channels=None):
    """Note an import attempt: last_attempt_at for the plugin's channels
    (all of them when channels is None). Returns {attempted, refused}."""
    rows = conn.execute(
        "SELECT id,name FROM tl.channels WHERE plugin=%s ORDER BY name", (plugin,)
    ).fetchall()
    own = {r["name"]: r["id"] for r in rows}
    if channels is None:
        wanted = list(own)
        refused = 0
    else:
        if not isinstance(channels, list):
            raise ValueError("channels must be a list")
        wanted = [c for c in channels if isinstance(c, str) and c in own]
        refused = len(channels) - len(wanted)
    for name in wanted:
        updated = conn.execute(
            "UPDATE tl.coverage SET last_attempt_at=now(),updated_at=now() WHERE plugin=%s AND channel_id=%s RETURNING id",
            (plugin, own[name]),
        ).fetchall()
        if not updated:
            conn.execute(
                "INSERT INTO tl.coverage(plugin,channel_id,scope_key,status,reasons,last_attempt_at) VALUES (%s,%s,'','best_effort',ARRAY['not_reported'],now()) ON CONFLICT (plugin,channel_id,scope_key) DO UPDATE SET last_attempt_at=now(),updated_at=now()",
                (plugin, own[name]),
            )
    return dict(attempted=sorted(set(wanted)), refused=refused)


def gaps(conn, plugin, channel=None, scope_key=None):
    """Open gaps of the plugin's own channels (and plugin-level ones), with
    ids and windows, so a stateless importer can close them."""
    rows = conn.execute(
        """SELECT g.id,c.name AS channel,g.scope_key,g.gap_from AS "from",g.gap_to AS "to",g.reason,g.opened_at
           FROM tl.coverage_gaps g LEFT JOIN tl.channels c ON c.id=g.channel_id
           WHERE g.plugin=%s AND g.closed_at IS NULL AND (g.channel_id IS NULL OR c.plugin=%s)
            AND (%s::text IS NULL OR c.name=%s) AND (%s::text IS NULL OR g.scope_key=%s)
           ORDER BY g.gap_from,g.id""",
        (plugin, plugin, channel, channel, scope_key, scope_key),
    ).fetchall()
    return rows


# --- reading ---------------------------------------------------------------


def statement(channels=None, after=None, before=None, kinds=None):
    """The api.coverage_for statement (validated SQL text)."""
    from .retrieval.scope_sql import lit

    def arg(value, array=False):
        if value is None:
            return "NULL"
        if array:
            return lit(list(value), "text[]")
        if isinstance(value, datetime):
            value = value.isoformat()
        return lit(str(value)) + "::timestamptz"

    return (
        "SELECT * FROM api.coverage_for("
        + ",".join(
            (
                arg(channels, True),
                arg(after),
                arg(before),
                arg(kinds, True),
            )
        )
        + ")"
    )


PLUGIN_ROWS = (
    "SELECT plugin,scope_key,status,reasons,raw_status,last_attempt_at,last_success_at,"
    "covered_from,covered_to,high_water_at,rows_delivered,pending_retries,stale "
    "FROM api.coverage WHERE channel_id IS NULL ORDER BY plugin,scope_key"
)


def summary(scope, channels=None, after=None, before=None, kinds=None):
    """Coverage of the channels the scope may read (and plugin-level rows for
    unrestricted scopes) -> {channels: [...], plugins: [...]}."""
    if not isinstance(scope, Scope):
        raise TypeError("coverage.summary requires a Scope")
    rows = query_ex(statement(channels, after, before, kinds), 10000, scope=scope)
    plugins = query_ex(PLUGIN_ROWS, 10000, scope=scope)
    return dict(channels=rows["rows"], plugins=plugins["rows"])


def worst(statuses):
    present = set(statuses)
    for status in ORDER:
        if status in present:
            return status
    return None


def _channel_entry(row):
    return dict(
        channel=row["channel"],
        status=row["status"],
        window_status=row["window_status"],
        reasons=list(row["reasons"] or []),
        stale=row["stale"],
        covered_from=row["covered_from"],
        covered_to=row["covered_to"],
        last_success_at=row["last_success_at"],
    )


def describe(rows, hits):
    """The coverage object of a result over coverage_for rows.

    status: the worst over the channels (best_effort when there is none);
    absence (only when there are no hits): not_found_in_covered_sources when
    every channel is complete and covers the window, else
    not_found_in_coverage with the partial channels and gaps. It never says
    that something did not happen.
    """
    channels = [_channel_entry(r) for r in rows]
    gap_list = []
    for row in rows:
        for gap in row.get("gaps") or []:
            gap_list.append(gap)
    status = worst(c["status"] for c in channels) or "best_effort"
    partial = [
        c["channel"]
        for c in channels
        if c["status"] != "complete" or c["window_status"] != "covered"
    ]
    covered = bool(channels) and not partial
    absence = None
    if not hits:
        absence = "not_found_in_covered_sources" if covered else "not_found_in_coverage"
    return dict(
        status=status,
        covered=covered,
        channels=channels,
        partial_channels=partial,
        gaps=gap_list,
        absence=absence,
    )


def missing_terms(ctx, hits):
    """The hints.must_terms of the request that no final hit contains (the
    same term test the planner uses), so absence is stated for them too."""
    wanted = (ctx.request.hints or {}).get("must_terms") or []
    if not isinstance(wanted, list):
        return []
    wanted = [str(t) for t in wanted if str(t).strip()]
    if not wanted:
        return []
    from .retrieval import selection

    found = set()
    for record in hits:
        found |= selection.terms(record.get("content") or "")
    return [t for t in wanted if not selection.terms(t) <= found]


def recall_summary(ctx, records):
    """Set execution.coverage of a recall from its FINAL records.

    One statement (api.coverage_for over the channels in scope, or every
    channel eligible for the scope's kinds, and the window). Adds the warning
    absence_is_not_evidence when coverage is not complete, and
    coverage_current_only when as_of or known_by is set. A failure degrades
    the recall (execution.incomplete) and never fails it.
    """
    from .retrieval import executor
    from .retrieval.scope_sql import DEFAULT_KINDS

    execution = ctx.execution
    scope = ctx.request.scope
    if scope.as_of is not None or scope.known_by is not None:
        execution.warn("coverage_current_only")
    kinds = None
    if scope.kinds is not None:
        kinds = [k for k in scope.kinds if k != "ignored"] or list(DEFAULT_KINDS)
    else:
        kinds = list(DEFAULT_KINDS)
    if scope.include_reference and "reference" not in kinds:
        kinds.append("reference")
    try:
        rows = ctx.sql(
            statement(scope.channels, scope.after, scope.before, kinds), cap=10000
        )
    except Exception as exc:
        reason = executor.failure_reason(exc)
        execution.degrade("coverage", reason)
        execution.set("coverage", dict(status="unknown", error=reason, absence=None))
        execution.warn("absence_is_not_evidence")
        return None
    hit_records = [r for r in records if (r.get("role") or "hit") == "hit"]
    missing = missing_terms(ctx, hit_records)
    value = describe(rows, bool(hit_records) and not missing)
    if missing:
        value["missing_terms"] = missing
    execution.set("coverage", value)
    if not value["covered"]:
        execution.warn("absence_is_not_evidence")
    return value


def packet_coverage(scope, channels):
    """(known_source_gaps, source_status) for a packet's channels under the
    caller's scope. Failure gives a note instead of raising."""
    names = sorted({c for c in channels if c})
    if not names:
        return [], {}
    try:
        rows = query_ex(statement(names), 10000, scope=scope, materialize=False)["rows"]
    except Exception as exc:
        return [dict(kind="coverage_unavailable", reason=type(exc).__name__)], {}
    gaps_out, status = [], {}
    for row in rows:
        status[row["channel"]] = dict(
            status=row["status"],
            window_status=row["window_status"],
            reasons=list(row["reasons"] or []),
            stale=row["stale"],
            last_success_at=row["last_success_at"],
        )
        if row["status"] != "complete" or row["window_status"] != "covered":
            gaps_out.append(
                dict(
                    kind="partial_channel",
                    channel=row["channel"],
                    status=row["status"],
                    window_status=row["window_status"],
                    reasons=list(row["reasons"] or []),
                )
            )
        for gap in row.get("gaps") or []:
            gaps_out.append(dict(kind="gap", **gap))
    return gaps_out, status
