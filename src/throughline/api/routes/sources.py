"""Source health over HTTP (throughline.health, docs/v11/sources.md).

GET /v1/source-health is the one place to see whether every data source is up:
state, last success, how far behind, the open outage and the last recovery.
It notes silent outages it finds (no run reported within twice the interval)
in tl.source_outages, the same as `throughline lib sources check`.

GET /v1/plugins/{name}/health lets an importer see its own state before a
run (the SDK uses it to widen a fixed lookback after an outage).

Pause and resume are owner plumbing on tl.plugins and tl.channel_pauses: a
paused source or channel is shown but never alerts or stalls its source. All of these use the app role after require_unrestricted;
plugins, like the people tables, are not readable by a restricted asker.
"""

from fastapi import APIRouter, HTTPException

from ... import health
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    plugin_access,
    require,
    require_unrestricted,
)

router = APIRouter()


@router.get("/v1/source-health")
def sources_list(token: Token):
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        entries = health.sources(conn)
        audit(token, len(entries))
        return dict(summary=health.summary(entries), sources=entries)


@router.get("/v1/source-health/outages")
def sources_outages(token: Token, plugin: str | None = None, limit: int = 50):
    require_unrestricted(token)
    require(token, "owner", "agent")
    if not 1 <= limit <= 1000:
        raise HTTPException(422, "limit must be 1..1000")
    with authenticated_connection(token) as conn:
        health.sweep(conn)
        rows = health.outages(conn, plugin, limit)
        audit(token, len(rows))
        return dict(outages=rows)


@router.post("/v1/source-health/{name}/pause")
def sources_pause(name: str, token: Token, data: dict | None = None):
    require_unrestricted(token)
    require(token, "owner")
    reason = (data or {}).get("reason")
    with authenticated_connection(token) as conn:
        try:
            return health.pause(
                conn, name, None if reason is None else str(reason)[:200]
            )
        except LookupError as error:
            raise HTTPException(404, str(error)) from None


@router.post("/v1/source-health/{name}/resume")
def sources_resume(name: str, token: Token):
    require_unrestricted(token)
    require(token, "owner")
    with authenticated_connection(token) as conn:
        try:
            return health.resume(conn, name)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/v1/plugins/{name}/health")
def plugin_health(name: str, token: Token):
    require_unrestricted(token)
    plugin_access(token, name)
    with authenticated_connection(token) as conn:
        # Read only: the run's own heartbeat records the outage state.
        entries = health.sources(conn, [name], record=False)
        if not entries:
            return dict(name=name, state="never", last_success_at=None, outage=None)
        entry = entries[0]
        return {
            key: entry[key]
            for key in (
                "name",
                "state",
                "kind",
                "interval_s",
                "last_success_at",
                "age_s",
                "newest_at",
                "consecutive_failures",
                "overdue",
                "outage",
            )
        }
