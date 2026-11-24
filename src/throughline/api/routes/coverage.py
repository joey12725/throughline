"""Source coverage over HTTP (PLG-7, ACL-8).

Writes (reports, attempts) and the plugin's own gap list use the app role:
plugin tokens for their own plugin, or owner tokens; require_unrestricted
runs first. Only authentication and plugin ownership give 4xx: a report
entry naming another plugin's channel is refused and counted, an entry of
an unexpected shape is degraded and stored. GET /v1/coverage reads through
the audited reader path, where the database gate shows only the channels
the asker may read (plugin-level rows for unrestricted callers only).

Write contract: the report and attempt routes honour the
Idempotency-Key the SDK sends (one fresh key per delivery, kept across its
retries). A replay returns the stored response with receipt.replayed true and
writes nothing; the same key with another body is 409. A keyed answer
carries a receipt read back in the same transaction (an un-keyed one keeps
the shape it always had): kind coverage_report (id = the
last report row written, stored_hash over the report ids and the coverage
rows they touched) or coverage_attempt (id = the last coverage row touched).
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from ... import coverage, writes
from ..deps import (
    Token,
    audit,
    audited,
    authenticated_connection,
    plugin_access,
    require,
    require_unrestricted,
)

router = APIRouter()


def _reports(data):
    if not isinstance(data, dict):
        raise HTTPException(422, "body must be an object with reports")
    reports = data.get("reports")
    if not isinstance(reports, list):
        raise HTTPException(422, "reports must be a list")
    if len(reports) > coverage.MAX_REPORTS:
        raise HTTPException(413, f"at most {coverage.MAX_REPORTS} reports per request")
    return reports


# Columns of a coverage row a receipt hashes (what the report changed).
STATE = "c.id,c.channel_id,c.scope_key,c.status,c.reasons,c.last_attempt_at,c.last_success_at,c.covered_from,c.covered_to,c.high_water_at,c.rows_delivered,c.pending_retries"


def _report_receipt(conn, name, first_id):
    """Receipt of the reports this transaction stored for the plugin."""
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM tl.coverage_reports WHERE plugin=%s AND id>%s AND at=now() ORDER BY id",
            (name, first_id),
        )
    ]
    rows = conn.execute(
        f"SELECT {STATE} FROM tl.coverage c WHERE c.plugin=%s AND EXISTS (SELECT 1 FROM tl.coverage_reports r WHERE r.id=ANY(%s) AND r.plugin=c.plugin AND r.channel_id IS NOT DISTINCT FROM c.channel_id AND r.scope_key=c.scope_key) ORDER BY c.id",
        (name, ids),
    ).fetchall()
    gaps = conn.execute(
        "SELECT id,closed_at IS NULL AS open FROM tl.coverage_gaps WHERE opened_by_report=ANY(%s) OR closed_by_report=ANY(%s) ORDER BY id",
        (ids, ids),
    ).fetchall()
    return writes.row_receipt(
        "coverage_report",
        ids[-1] if ids else 0,
        dict(plugin=name, reports=ids, coverage=rows, gaps=gaps),
        dict(status="stored" if ids else "refused", reports=len(ids)),
    )


@router.post("/v1/plugins/{name}/coverage")
def coverage_report(name: str, data: dict, token: Token, request: Request):
    """Store coverage reports; answers {stored, degraded, refused,
    gaps_opened: [{index, id, from, to}], gaps_closed, receipt}."""
    require_unrestricted(token)
    plugin_access(token, name)
    reports = _reports(data)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                audit(token, 0, plugin=name, replayed=True)
                return slot.response
            first = conn.execute(
                "SELECT coalesce(max(id),0) AS n FROM tl.coverage_reports"
            ).fetchone()["n"]
            result = coverage.record_many(conn, name, reports)
            if slot.key is not None:
                result["receipt"] = _report_receipt(conn, name, first)
            slot.store(200, result)
    audit(
        token,
        result["stored"],
        plugin=name,
        refused=result["refused"],
        degraded=len(result["degraded"]),
        gaps_opened=len(result["gaps_opened"]),
        gaps_closed=len(result["gaps_closed"]),
    )
    return result


@router.post("/v1/plugins/{name}/coverage/attempt")
def coverage_attempt(
    name: str, token: Token, request: Request, data: dict | None = None
):
    """Note an import attempt for the plugin's channels (all when none named);
    answers {attempted, refused, receipt}."""
    require_unrestricted(token)
    plugin_access(token, name)
    channels = (data or {}).get("channels")
    if channels is not None and not isinstance(channels, list):
        raise HTTPException(422, "channels must be a list")
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data or {}) as slot:
            if slot.replayed:
                audit(token, 0, plugin=name, replayed=True)
                return slot.response
            result = coverage.attempt(conn, name, channels)
            if slot.key is None:
                slot.store(200, result)
                audit(
                    token,
                    len(result["attempted"]),
                    plugin=name,
                    refused=result["refused"],
                )
                return result
            rows = conn.execute(
                f"SELECT {STATE} FROM tl.coverage c JOIN tl.channels ch ON ch.id=c.channel_id WHERE c.plugin=%s AND ch.name=ANY(%s) ORDER BY c.id",
                (name, result["attempted"]),
            ).fetchall()
            result["receipt"] = writes.row_receipt(
                "coverage_attempt",
                rows[-1]["id"] if rows else 0,
                dict(plugin=name, coverage=rows),
                dict(status="attempted", channels=len(rows)),
            )
            slot.store(200, result)
    audit(token, len(result["attempted"]), plugin=name, refused=result["refused"])
    return result


@router.get("/v1/plugins/{name}/coverage/gaps")
def coverage_gaps(
    name: str, token: Token, channel: str | None = None, scope_key: str | None = None
):
    """Open gaps of the plugin's own channels with ids and windows."""
    require_unrestricted(token)
    plugin_access(token, name)
    with authenticated_connection(token) as conn:
        rows = coverage.gaps(conn, name, channel, scope_key)
    audit(token, len(rows), plugin=name)
    return dict(gaps=rows)


def _when(value, label):
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"{label} must be an ISO timestamp") from None


@router.get("/v1/coverage")
def coverage_read(
    token: Token,
    channels: str | None = None,
    after: str | None = None,
    before: str | None = None,
):
    """Coverage of the channels you may read over a window (comma-separated
    channels); plugin-level rows for unrestricted callers only."""
    require(token, "owner", "agent")
    names = [c for c in (channels or "").split(",") if c] or None
    result = audited(
        coverage.statement(names, _when(after, "after"), _when(before, "before")),
        token,
        10000,
    )
    plugins = audited(coverage.PLUGIN_ROWS, token, 10000)
    audit(token, result["row_count"] + plugins["row_count"])
    return dict(
        channels=result["rows"],
        plugins=plugins["rows"],
        execution=result["execution"],
    )
