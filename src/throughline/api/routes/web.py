"""Routes of the web interface (docs/web/backend.md).

GET  /v1/web/bootstrap             everything the interface needs to draw its
                                   first screen, in one request
GET|PUT|DELETE /v1/web/state/...   the interface's own documents
GET  /v1/web/arrivals              what was ingested since a cursor (long poll)

Bootstrap reads through the reader path under the caller's scope, so a
restricted asker gets exactly the channels and coverage its grants show; the
parts only the app role can read (plugins, embedding flags, vector count) are
added for an unrestricted scope and left out otherwise. The state routes serve
the caller's own working state. Arrivals reads tl.messages below the gate and
therefore refuses a restricted asker.

Nothing here pushes: arrivals is a plain request that the server may hold for
up to 25 s before answering with JSON.
"""

import asyncio
import base64
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, JsonValue
from starlette.concurrency import run_in_threadpool

from ...query import query_ex
from ...web import store
from ..deps import (
    Token,
    audit,
    authenticated_connection,
    is_unrestricted,
    own_working_state,
    require,
    require_unrestricted,
    scope_of,
)

router = APIRouter()

# --- bootstrap ---------------------------------------------------------------

_STATUS_RANK = {"partial": 0, "best_effort": 1, "complete": 2}

_PLUGIN_ROWS = """
SELECT to_jsonb(p) AS plugin,
       p.last_success_at IS NULL OR now()>p.last_success_at+make_interval(secs=>p.interval_s*2) AS stale,
       greatest(p.updated_at,
                (to_jsonb(p)->>'last_attempt_at')::timestamptz,
                (to_jsonb(p)->>'last_error_at')::timestamptz,
                (to_jsonb(p)->>'paused_at')::timestamptz) AS changed_at
FROM tl.plugins p
"""


def _version():
    try:
        from importlib.metadata import version

        return version("throughline")
    except Exception:
        return "0.1.0"


def _plugin_rows(conn, since=None):
    """(rows, newest change as text) of tl.plugins in the bootstrap shape.

    Columns are read through to_jsonb, so the source-health columns appear
    when that migration is present and nothing fails when it is not. since
    limits the rows to those changed after it.
    """
    rows = conn.execute(
        "SELECT x.plugin,x.stale,x.changed_at::text AS changed_at FROM ("
        + _PLUGIN_ROWS
        + ") x WHERE %s::timestamptz IS NULL OR x.changed_at>%s::timestamptz ORDER BY x.plugin->>'name'",
        (since, since),
    ).fetchall()
    newest = conn.execute(
        "SELECT max(x.changed_at)::text AS at FROM (" + _PLUGIN_ROWS + ") x"
    ).fetchone()["at"]
    outages = {}
    if rows and conn.execute(
        "SELECT to_regclass('tl.source_outages') IS NOT NULL AS ok"
    ).fetchone()["ok"]:
        if conn.execute(
            "SELECT has_table_privilege(current_user,'tl.source_outages','SELECT') AS ok"
        ).fetchone()["ok"]:
            for row in conn.execute(
                "SELECT to_jsonb(o) AS outage FROM tl.source_outages o WHERE o.restored_at IS NULL ORDER BY o.detected_at"
            ):
                outages[row["outage"].get("plugin")] = row["outage"]
    result = []
    for row in rows:
        plugin = row["plugin"]
        manifest = plugin.get("manifest") or {}
        item = dict(
            name=plugin["name"],
            version=manifest.get("version"),
            mode=manifest.get("run_mode") or manifest.get("mode"),
            interval_s=plugin.get("interval_s"),
            last_success_at=plugin.get("last_success_at"),
            last_error=plugin.get("last_error"),
            messages_ingested=plugin.get("messages_ingested"),
            stale=row["stale"],
            updated_at=plugin.get("updated_at"),
        )
        for name in store.HEALTH_COLUMNS:
            if name in plugin:
                item[name] = plugin[name]
        item["open_outage"] = outages.get(plugin["name"])
        result.append(item)
    return result, newest


def _owner_extras(token):
    """What only the app role can read; an unrestricted scope only."""
    require_unrestricted(token)
    with authenticated_connection(token) as conn:
        plugins, _ = _plugin_rows(conn)
        embed = {
            row["name"]: row["embed"]
            for row in conn.execute(
                "SELECT c.name,to_jsonb(c)->'embed' AS embed FROM tl.channels c"
            )
        }
        vectors = conn.execute(
            "SELECT (SELECT count(*) FROM tl.embeddings)+(SELECT count(*) FROM tl.passage_embeddings) AS n"
            if conn.execute(
                "SELECT to_regclass('tl.passage_embeddings') IS NOT NULL AS ok"
            ).fetchone()["ok"]
            else "SELECT count(*) AS n FROM tl.embeddings"
        ).fetchone()["n"]
    return dict(plugins=plugins, embed=embed, vectors=vectors)


def _embeddings():
    from ... import providers

    try:
        provider = providers.get("embedding")
        return dict(
            available=not provider.local, model=getattr(provider, "model", None)
        )
    except ValueError:
        return dict(available=False, model=None)


def _assistant():
    """The in-interface assistant's configuration, when that package exists."""
    try:
        from ... import assistant

        return dict(assistant.describe())
    except Exception:
        return dict(provider="none", model=None, loopback=True, default_tier=None)


@router.get("/v1/web/bootstrap")
def bootstrap(token: Token):
    require(token, "owner", "agent")
    scope = scope_of(token)
    unrestricted = is_unrestricted(token)

    def read(statement):
        # The reader path: gated views under the caller's scope.
        return query_ex(statement, 10000, scope=scope, materialize=False)["rows"]

    available = store.features(scope)
    channels = read(
        "SELECT id,name,plugin,description,record_kind,is_reference,is_outside,"
        "conversation_whole_thread AS whole_thread,fields FROM api.channels ORDER BY name"
    )
    stats = {
        row["channel_id"]: row
        for row in read(
            "SELECT channel_id,count(*) AS rows,min(happened_at) AS first_at,max(happened_at) AS last_at,"
            "max(ingested_at) AS last_ingest_at,count(lat) AS with_geo,count(ended_at) AS with_ended_at,"
            "count(*) FILTER (WHERE images<>'[]'::jsonb) AS with_images FROM api.messages GROUP BY channel_id"
        )
    }
    threads = {
        row["channel_id"]: row["n"]
        for row in read(
            "SELECT channel_id,count(*) AS n FROM api.threads GROUP BY channel_id"
        )
    }
    coverage = {}
    for row in read(
        "SELECT channel_id,status,reasons,covered_from,covered_to FROM api.coverage WHERE channel_id IS NOT NULL"
    ):
        item = coverage.setdefault(
            row["channel_id"],
            dict(status=None, reasons=set(), covered_from=None, covered_to=None),
        )
        # Several scopes of one channel: the weakest status, every reason and
        # the widest window.
        if item["status"] is None or _STATUS_RANK.get(
            row["status"], -1
        ) < _STATUS_RANK.get(item["status"], -1):
            item["status"] = row["status"]
        item["reasons"].update(row["reasons"] or [])
        for key, pick in (("covered_from", min), ("covered_to", max)):
            if row[key] is not None:
                item[key] = row[key] if item[key] is None else pick(item[key], row[key])
    gaps = {
        row["channel_id"]: row["n"]
        for row in read(
            "SELECT channel_id,count(*) AS n FROM api.coverage_gaps WHERE open GROUP BY channel_id"
        )
    }
    counts = read(
        "SELECT (SELECT count(*) FROM api.people) AS people,(SELECT count(*) FROM api.entities) AS entities,"
        "(SELECT count(*) FROM api.links) AS links,"
        "(SELECT id FROM api.people WHERE is_owner ORDER BY id LIMIT 1) AS owner_person_id"
    )[0]
    egress = read("SELECT api.describe('egress') AS egress")[0]["egress"]
    extras = _owner_extras(token) if unrestricted else None

    listed = []
    for channel in channels:
        own = stats.get(channel["id"]) or {}
        covered = coverage.get(channel["id"])
        view = "ch_" + channel["name"].replace(".", "_")
        listed.append(
            dict(
                name=channel["name"],
                plugin=channel["plugin"],
                description=channel["description"],
                record_kind=channel["record_kind"],
                is_reference=channel["is_reference"],
                is_outside=channel["is_outside"],
                whole_thread=channel["whole_thread"],
                embed=extras["embed"].get(channel["name"]) if extras else None,
                fields=[
                    dict(
                        name=field.get("name"),
                        type=field.get("type"),
                        description=field.get("description"),
                    )
                    for field in channel["fields"] or []
                    if isinstance(field, dict)
                ],
                view="api." + view if view in available["channel_views"] else None,
                rows=own.get("rows", 0),
                threads=threads.get(channel["id"], 0),
                first_at=own.get("first_at"),
                last_at=own.get("last_at"),
                last_ingest_at=own.get("last_ingest_at"),
                with_geo=own.get("with_geo", 0),
                with_ended_at=own.get("with_ended_at", 0),
                with_images=own.get("with_images", 0),
                coverage=dict(
                    status=covered["status"] if covered else "unknown",
                    reasons=sorted(covered["reasons"]) if covered else [],
                    covered_from=covered["covered_from"] if covered else None,
                    covered_to=covered["covered_to"] if covered else None,
                    open_gaps=gaps.get(channel["id"], 0),
                ),
            )
        )
    plugins = extras["plugins"] if extras else []
    audit(token, len(listed))
    return dict(
        version=_version(),
        now=datetime.now(timezone.utc),
        token=dict(
            id=token["id"],
            kind=token["kind"],
            name=token.get("name"),
            asker=token.get("asker"),
            unrestricted=unrestricted,
            # Records are written by the owner (and by plugins, which have no
            # interface); an agent reads.
            can_write=unrestricted and token["kind"] == "owner",
        ),
        features=dict(
            web_state=available["web_state"],
            source_health=available["source_health"],
            embeddings=_embeddings(),
            assistant=_assistant(),
            egress=egress,
        ),
        owner_person_id=counts["owner_person_id"],
        totals=dict(
            records=sum(item["rows"] for item in listed),
            channels=len(listed),
            plugins=len(plugins)
            if extras
            else len({item["plugin"] for item in listed}),
            people=counts["people"],
            entities=counts["entities"],
            links=counts["links"],
            threads=sum(item["threads"] for item in listed),
            vectors=extras["vectors"] if extras else None,
        ),
        plugins=plugins,
        channels=listed,
    )


# --- state --------------------------------------------------------------------


class StateDocument(BaseModel):
    value: JsonValue


def _namespace(token):
    """The namespace of the caller's documents, derived from the token and
    its scope, never supplied:
      ''               an owner token with an unrestricted scope: the owner's
                       interface, the same on every device;
      'token:<id>'     any other unrestricted token (an agent): its own, so it
                       can never replace a saved query the owner's browser
                       runs later;
      'asker:<name>'   a restricted asker (whichever token names it)."""
    require(token, "owner", "agent")
    if is_unrestricted(token):
        if token["kind"] == "owner":
            return ""
        return f"token:{token['id']}"
    scope = scope_of(token)
    if scope.mode == "asker" and scope.asker:
        return "asker:" + scope.asker
    raise HTTPException(403, "this scope has no interface state")


@router.get("/v1/web/state/{kind}")
@own_working_state
def state_list(kind: str, token: Token):
    namespace = _namespace(token)
    store.check_kind(kind)
    with authenticated_connection(token) as conn:
        rows = store.list_documents(conn, namespace, kind)
    audit(token, len(rows))
    return rows


@router.get("/v1/web/state/{kind}/{key}")
@own_working_state
def state_get(kind: str, key: str, token: Token):
    namespace = _namespace(token)
    store.check_kind(kind)
    store.check_key(key)
    with authenticated_connection(token) as conn:
        row = store.get_document(conn, namespace, kind, key)
    if row is None:
        raise HTTPException(404, "no such document")
    audit(token, 1)
    return row


@router.put("/v1/web/state/{kind}/{key}")
@own_working_state
def state_put(kind: str, key: str, data: StateDocument, token: Token):
    namespace = _namespace(token)
    store.check_kind(kind)
    store.check_key(key)
    try:
        with authenticated_connection(token) as conn:
            row = store.put_document(
                conn, namespace, kind, key, data.value, token["id"]
            )
    except store.TooLarge as exc:
        raise HTTPException(413, str(exc)) from None
    audit(token, 1)
    return row


@router.delete("/v1/web/state/{kind}/{key}")
@own_working_state
def state_delete(kind: str, key: str, token: Token):
    namespace = _namespace(token)
    store.check_kind(kind)
    store.check_key(key)
    with authenticated_connection(token) as conn:
        deleted = store.delete_document(conn, namespace, kind, key)
    audit(token, int(deleted))
    return dict(deleted=deleted)


# --- arrivals -----------------------------------------------------------------
#
# The cursor is exact although transactions commit out of order. It holds
#   w     a watermark: every record with id <= w has been delivered,
#   seen  the ids above w that have been delivered, as inclusive ranges,
#   a     the last audit row looked at, p the newest plugin change seen.
# A poll reads the ids above w that are not in `seen` (the gaps), so a record
# of a transaction that commits late is delivered when it appears, however
# many higher ids were delivered before it.
#
# The watermark only moves over ids that can no longer appear. Ingest
# transactions must commit within the settle window S of their start (a
# database guarantee, docs/v11/consumers.md) and ingested_at is that start. If
# a visible record R has ingested_at <= now - 2S, every lower id was allocated
# before R's (ids come from one sequence), so by a transaction that started
# before R committed (<= ingested_at(R) + S) and itself committed within S:
# before ingested_at(R) + 2S <= now. So every id <= id(R) is final.
#
# `settled` on a row is the consumers' own definition (ingested_at at or
# before api.consumer_horizon()): what api.news already delivers.

MAX_WAIT_S = 25
POLL_S = 0.75
SCAN = 5000  # records taken into account by one answer
MAX_RANGES = 128
MAX_LIMIT = 500
EXCERPT = 280
STATEMENT = 300
READS = 100
_BIG = 2**62
_waiting = 0


def _max_waiters():
    try:
        return max(1, int(os.getenv("THROUGHLINE_WEB_WAITERS") or 16))
    except ValueError:
        return 16


def _encode(cursor):
    raw = json.dumps(
        [1, cursor["w"], cursor["seen"], cursor["a"], cursor["p"]],
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(text):
    try:
        if len(text) > 8192:
            raise ValueError
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        version, w, seen, a, p = json.loads(raw)
        ok = (
            version == 1
            and type(w) is int
            and type(a) is int
            and 0 <= w < _BIG
            and 0 <= a < _BIG
            and isinstance(seen, list)
            and len(seen) <= MAX_RANGES
            and (p is None or (isinstance(p, str) and len(p) <= 40))
        )
        last = w
        for item in seen if ok else ():
            lo, hi = item
            if not (type(lo) is int and type(hi) is int and last < lo <= hi < _BIG):
                ok = False
                break
            last = hi
        if not ok:
            raise ValueError
        if p is not None:
            datetime.fromisoformat(p)
    except Exception:
        raise ValueError("invalid cursor") from None
    return dict(w=w, seen=[[lo, hi] for lo, hi in seen], a=a, p=p)


def _ranges(ids):
    """Sorted ids -> inclusive ranges of consecutive ids."""
    result = []
    for value in ids:
        if result and value == result[-1][1] + 1:
            result[-1][1] = value
        else:
            result.append([value, value])
    return result


def _merge(seen, ids):
    """Ranges of `seen` plus the sorted ids (none of them in a range)."""
    merged = []
    for lo, hi in sorted(seen + _ranges(ids)):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def _clock(conn):
    return conn.execute(
        "SELECT now() AS now,h.at AS horizon,h.at-(now()-h.at) AS final_before,"
        "extract(epoch FROM now()-h.at)::float8 AS settle_s FROM (SELECT api.consumer_horizon() AS at) h"
    ).fetchone()


def _anchor(conn, clock):
    """The highest id known to be final, with every id below it (see above)."""
    row = conn.execute(
        """SELECT greatest(
             (SELECT m.id FROM tl.messages m WHERE m.ingested_at<=%(t)s ORDER BY m.ingested_at DESC,m.id DESC LIMIT 1),
             (SELECT m.id FROM tl.messages m WHERE m.id=(SELECT max(x.id) FROM tl.messages x) AND m.ingested_at<=%(t)s)
           ) AS id""",
        dict(t=clock["final_before"]),
    ).fetchone()
    return row["id"] or 0


def _details(conn, ids, clock, names):
    rows = conn.execute(
        "SELECT m.id,m.channel_id,m.thread_id,m.happened_at,m.ingested_at,m.author_handle AS author,"
        "m.person_id,left(m.content,%s) AS excerpt,m.version FROM tl.messages m WHERE m.id=ANY(%s) ORDER BY m.id",
        (EXCERPT, list(ids)),
    ).fetchall()
    return [
        dict(
            id=row["id"],
            channel=names.get(row["channel_id"]),
            thread_id=row["thread_id"],
            happened_at=row["happened_at"],
            ingested_at=row["ingested_at"],
            author=row["author"],
            person_id=row["person_id"],
            excerpt=row["excerpt"],
            version=row["version"],
            settled=row["ingested_at"] <= clock["horizon"],
        )
        for row in rows
    ]


def _channel_names(conn):
    return {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM tl.channels")}


def _reads(conn, after, owner, newest=False):
    """(rows, last audit id looked at). What tokens asked: audit rows of
    requests that answered rows (a query, a search, a listing). Requests
    tagged client=web, every /v1/web route (this one included, or a poller
    without the tag would wake itself) and plugin traffic are left out;
    statement text is cut to 300 characters. Owner tokens only."""
    top = conn.execute("SELECT max(id) AS id FROM tl.audit_log").fetchone()["id"] or 0
    if not owner:
        return [], top
    where = (
        " FROM tl.audit_log a LEFT JOIN tl.tokens t ON t.id=a.token_id"
        " WHERE a.id>%s AND a.id<=%s AND coalesce(a.detail->>'client','')<>'web'"
        " AND coalesce(t.kind,'')<>'plugin' AND a.statement !~ '^[A-Z]+ /v1/web/'"
        " AND a.row_count IS NOT NULL"
    )
    select = (
        "SELECT a.id,a.at,t.name AS token,a.asker,left(a.statement,%s) AS statement,"
        "a.row_count,a.http_status AS status"
    )
    if newest:
        rows = conn.execute(
            select + where + " ORDER BY a.id DESC LIMIT 20", (STATEMENT, 0, top)
        ).fetchall()[::-1]
        last = top
    else:
        rows = conn.execute(
            select + where + " ORDER BY a.id LIMIT %s",
            (STATEMENT, min(after, top), top, READS),
        ).fetchall()
        last = rows[-1]["id"] if len(rows) == READS else top
    return [{k: v for k, v in row.items() if k != "id"} for row in rows], last


def _snapshot(token, limit, wanted):
    """The answer without a cursor: the newest records and where to go on."""
    with authenticated_connection(token) as conn:
        clock = _clock(conn)
        names = _channel_names(conn)
        w = _anchor(conn, clock)
        # What is visible above the watermark has been "delivered" by this
        # answer. At most SCAN ids are looked at, the newest ones (a backward
        # walk of the primary key): when more are visible, the floor moves up
        # to just below that window, and ids under it count as delivered
        # (records that old are what a fresh cursor starts after anyway).
        above = [
            row["id"]
            for row in conn.execute(
                "SELECT m.id FROM tl.messages m WHERE m.id>%s ORDER BY m.id DESC LIMIT %s",
                (w, SCAN + 1),
            )
        ][::-1]
        if len(above) > SCAN:
            w, above = above[0], above[1:]
        seen = _ranges(above)
        if len(seen) > MAX_RANGES:
            # Too fragmented: the oldest ranges fold into the floor.
            w = seen[-MAX_RANGES - 1][1]
            seen = seen[-MAX_RANGES:]
        channel_ids = [i for i, name in names.items() if name in wanted]
        newest = conn.execute(
            "SELECT m.id FROM tl.messages m WHERE %s::bigint[] IS NULL OR m.channel_id=ANY(%s::bigint[])"
            " ORDER BY m.id DESC LIMIT %s",
            (
                channel_ids if wanted else None,
                channel_ids if wanted else None,
                limit,
            ),
        ).fetchall()
        # Never show a record the cursor would deliver again as an arrival.
        covered = seen[-1][1] if seen else w
        rows = _details(
            conn, [r["id"] for r in newest if r["id"] <= covered], clock, names
        )
        plugins, changed = _plugin_rows(conn)
        reads, last = _reads(conn, 0, token["kind"] == "owner", newest=True)
    cursor = dict(w=w, seen=seen, a=last, p=changed)
    return cursor, _answer(cursor, rows, {}, False, plugins, reads, clock)


def _answer(cursor, rows, counts, sampled, plugins, reads, clock):
    return dict(
        cursor=_encode(cursor),
        rows=rows,
        counts=counts,
        sampled=sampled,
        plugins=plugins,
        reads=reads,
        horizon=clock["horizon"],
        settle_s=round(clock["settle_s"], 3),
    )


def _poll(token, cursor, limit, wanted):
    """One short transaction: (next cursor, answer, whether it has news)."""
    with authenticated_connection(token) as conn:
        clock = _clock(conn)
        top = conn.execute("SELECT max(id) AS id FROM tl.messages").fetchone()["id"]
        if cursor["w"] > (top or 0):
            return None  # another database than the cursor's: start over
        seen = cursor["seen"]
        lows = [cursor["w"]] + [hi for _, hi in seen]
        highs = [lo for lo, _ in seen] + [_BIG]
        found = conn.execute(
            "SELECT m.id,m.channel_id FROM unnest(%s::bigint[],%s::bigint[]) AS g(lo,hi)"
            " CROSS JOIN LATERAL (SELECT x.id,x.channel_id FROM tl.messages x"
            "  WHERE x.id>g.lo AND x.id<g.hi ORDER BY x.id LIMIT %s) m ORDER BY m.id LIMIT %s",
            (lows, highs, SCAN + 1, SCAN + 1),
        ).fetchall()
        # Everything visible at or below `complete` has been delivered once
        # this answer is; None means no bound.
        complete = None
        if len(found) > SCAN:
            found = found[:SCAN]
            complete = found[-1]["id"]
        merged = _merge(seen, [r["id"] for r in found])
        anchor = _anchor(conn, clock)
        w = max(cursor["w"], anchor if complete is None else min(anchor, complete))
        merged = [[max(lo, w + 1), hi] for lo, hi in merged if hi > w]
        if len(merged) > MAX_RANGES:
            # Too fragmented to remember: the oldest ranges fold into the
            # watermark (their gaps are given up), so nothing delivered is
            # forgotten and delivered again.
            w = merged[-MAX_RANGES - 1][1]
            merged = merged[-MAX_RANGES:]

        names = _channel_names(conn) if found else {}
        if wanted:
            found = [r for r in found if names.get(r["channel_id"]) in wanted]
        counts = dict(
            sorted(Counter(names.get(r["channel_id"]) for r in found).items())
        )
        ids = [r["id"] for r in found]
        sampled = len(ids) > limit
        if sampled:
            # Evenly spread over the batch, the newest record included.
            step = (len(ids) - 1) / (limit - 1) if limit > 1 else 0
            ids = (
                sorted({ids[round(i * step)] for i in range(limit)})
                if limit > 1
                else ids[-1:]
            )
        rows = _details(conn, ids, clock, names) if ids else []
        plugins, changed = _plugin_rows(conn, cursor["p"])
        reads, last = _reads(conn, cursor["a"], token["kind"] == "owner")
    following = dict(w=w, seen=merged, a=last, p=changed or cursor["p"])
    news = bool(found or plugins or reads)
    return (
        following,
        _answer(following, rows, counts, sampled, plugins, reads, clock),
        news,
    )


def _guard(token):
    require(token, "owner", "agent")
    require_unrestricted(token)


@router.get("/v1/web/arrivals")
async def arrivals(
    request: Request,
    token: Token,
    after: Annotated[str | None, Query(max_length=8192)] = None,
    wait: Annotated[float, Query(ge=0, le=MAX_WAIT_S)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 200,
    channels: Annotated[str | None, Query(max_length=4000)] = None,
):
    """Records ingested since `after`, as plain JSON.

    Without `after` the answer is immediate: the newest records and a cursor.
    With it, the request is held for at most `wait` seconds and answered as
    soon as a record, a plugin change or a read appears; no database
    connection is kept while it waits (one short transaction per look).
    Unrestricted scopes only: refused by require_unrestricted in _guard.
    """
    global _waiting
    await run_in_threadpool(_guard, token)
    wanted = frozenset(n.strip() for n in (channels or "").split(",") if n.strip())
    if after is None:
        _, answer = await run_in_threadpool(_snapshot, token, limit, wanted)
        audit(token, len(answer["rows"]))
        return answer
    cursor = _decode(after)
    deadline = time.monotonic() + wait
    counted = False
    try:
        while True:
            result = await run_in_threadpool(_poll, token, cursor, limit, wanted)
            if result is None:
                _, answer = await run_in_threadpool(_snapshot, token, limit, wanted)
                break
            cursor, answer, news = result
            remaining = deadline - time.monotonic()
            if news or remaining <= 0 or await request.is_disconnected():
                break
            if not counted:
                if _waiting >= _max_waiters():
                    raise HTTPException(
                        429,
                        "too many waiting requests; retry shortly",
                        headers={"Retry-After": "2"},
                    )
                _waiting += 1
                counted = True
            await asyncio.sleep(min(POLL_S, remaining))
    finally:
        if counted:
            _waiting -= 1
    audit(token, len(answer["rows"]))
    return answer
