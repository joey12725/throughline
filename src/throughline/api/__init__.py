"""HTTP boundary. Every operation is authenticated and transactional."""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from psycopg import Error, sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import identity, operators, writes
from ..budget import Budget, clamp
from ..db import connect
from ..ingest import add_link, ingest, link_batch, link_status, register_channel
from ..lineage import VALIDITY, validity
from ..location import detect_visits, ingest_track
from ..models import Channel, Ingest, IngestResult, TrackBatch
from ..retrieval.types import Budgets
from .deps import (
    Token,
    audit,
    audited,
    authenticate,
    authenticated_connection,
    bearer,
    flush_trace,
    is_unrestricted,
    own_working_state,
    plugin_access,
    require,
    require_unrestricted,
    scope_of,
)
from .routes import routers

__all__ = [
    "Token",
    "audit",
    "audited",
    "authenticate",
    "authenticated_connection",
    "bearer",
    "create_app",
    "flush_trace",
    "is_unrestricted",
    "own_working_state",
    "plugin_access",
    "require",
    "require_unrestricted",
    "scope_of",
]

log = logging.getLogger("throughline.api")

# Items per link or entity batch, and messages per request with receipts.
MAX_BATCH = 1000


# SQLSTATE classes whose messages describe the caller's own statement.
STATEMENT_ERROR_CLASSES = ("42", "22", "0A", "P0", "2F", "39", "54", "57014")


def statement_error(exc):
    """The primary message and hint of an error caused by the statement, or None."""
    code = getattr(exc, "sqlstate", None) or ""
    if not any(code.startswith(prefix) for prefix in STATEMENT_ERROR_CLASSES):
        return None
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) or ""
    if not primary:
        return None
    if code == "57014":
        primary = "statement timed out; narrow it (scope by channel, time or person, add LIMIT)"
    hint = getattr(diag, "message_hint", None)
    text = primary + (f" (hint: {hint})" if hint else "")
    return text[:500]


class EmbedRequest(BaseModel):
    text: str = Field(min_length=1)


class SQLRequest(BaseModel):
    statement: str
    params: list[Any] | None = Field(default=None, max_length=200)
    row_cap: int = Field(default=1000, ge=1, le=10000)
    # Budgets can only lower the token's limits (CST-4). Without token_budget
    # only the row cap and the response size bound the result.
    deadline_ms: int | None = Field(default=None, ge=1)
    token_budget: int | None = Field(default=None, ge=1)
    trace: bool = False
    # Semantic SQL (api.recall, ...): one budget for every operator of the
    # statement, a dry run that only plans and estimates, the approval of an
    # earlier approval_required response, and strict exactness (aggregates
    # over ranked or sampled rows are rejected instead of warned about).
    budgets: Budgets | None = None
    # Agent-sized output (agent_view): reader-useless columns dropped, long
    # cells cut, rows stop at max_chars of JSON. raw=true returns rows as is.
    raw: bool = False
    max_chars: int | None = Field(default=None, ge=1000, le=2_000_000)
    max_cell: int | None = Field(default=None, ge=200, le=1_000_000)
    dry_run: bool = False
    approval_id: str | None = Field(default=None, max_length=64)
    strict_exact: bool = False
    # Check and describe the statement without running it: the answer is
    # {valid, error, code, columns} and no row is produced.
    validate_only: bool = False


class SQLStreamRequest(BaseModel):
    statement: str
    params: list[Any] | None = Field(default=None, max_length=200)
    format: Literal["ndjson", "csv"] = "ndjson"
    row_cap: int = Field(default=100_000, ge=1, le=1_000_000)
    deadline_ms: int | None = Field(default=None, ge=1)
    budgets: Budgets | None = None
    approval_id: str | None = Field(default=None, max_length=64)
    strict_exact: bool = False


class Person(BaseModel):
    handle: str
    display_name: str | None = None
    is_owner: bool = False
    status: str = "active"
    note: str | None = None
    kind: str = "human"
    source_system: str | None = None
    source_id: str | None = None
    created_at: datetime | None = None


class SQLResponse(BaseModel):
    # A run answers rows, truncated, row_count, execution and columns; a
    # validate_only request answers valid, error, code and columns. Keys that
    # do not belong to the answer are left out, never sent as null.
    rows: list[dict] = []
    truncated: bool = False
    row_count: int = 0
    execution: dict | None = None
    # Output columns in order, {name, type} with PostgreSQL type names; present
    # for a result without rows, duplicate names kept.
    columns: list[dict] | None = None
    valid: bool | None = None
    error: str | None = None
    code: str | None = None


# The only values of X-Throughline-Client that reach the audit detail.
CLIENTS = ("web", "cli", "sdk")


def sync_registries():
    """Publish what this process registered (operators, model providers).

    Best effort: the API must start on a database that is behind on
    migrations, so a failure is logged and never raised.
    """
    try:
        with connect("app") as conn:
            operators.sync(conn)
    except Exception as exc:
        log.warning("operator registry was not synchronised: %s", exc)
    try:
        from .. import providers

        if hasattr(providers, "sync_state"):
            with connect("app") as conn:
                providers.sync_state(conn)
    except Exception as exc:
        log.warning("provider state was not synchronised: %s", exc)


@asynccontextmanager
async def lifespan(app):
    _start_setup()
    sync_registries()
    from ..auth import config as auth_config

    for problem in auth_config.settings().problems:
        log.warning("sign-in provider left out: %s", problem)
    yield


def _start_setup():
    """Onboarding: apply the settings saved from the browser, mark jobs a
    restart cut off, start the plugin scheduler, and print the first-run
    setup link when no account exists yet."""
    from ..onboarding import jobs, owner, store

    store.apply()
    try:
        jobs.interrupt_stale()
    except Exception:
        log.warning("setup jobs could not be checked")
    jobs.start_scheduler()
    announce = os.getenv("THROUGHLINE_SETUP_ANNOUNCE")
    if announce:
        owner.announce(announce)


def required(data, name):
    """A required body field, or a 400 naming it (never a KeyError 500)."""
    if not isinstance(data, dict) or data.get(name) is None:
        raise ValueError(f"{name} is required")
    return data[name]


def create_app():
    app = FastAPI(
        title="Throughline",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def audit_requests(request, call_next):
        # Every authenticated request, read or write, leaves exactly one row:
        # token, asker, what ran (the SQL statement for query routes, otherwise
        # method and path), how many rows it returned and the HTTP status.
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            token = getattr(request.state, "token", None)
            if token:
                entry = token.get("audit") or {}
                route = request.method + " " + request.url.path
                detail = dict(entry.get("detail") or {})
                if entry.get("statement") is not None:
                    detail["route"] = route
                client = request.headers.get("x-throughline-client")
                if client in CLIENTS:
                    detail["client"] = client
                if token.get("account"):
                    # A browser session: which account and how it signed in.
                    detail["account_id"] = token["account"]["id"]
                    detail["via"] = token["session"]["method"]
                with authenticated_connection(token) as conn:
                    conn.execute(
                        "INSERT INTO tl.audit_log(token_id,asker,statement,row_count,http_status,detail) VALUES (%s,%s,%s,%s,%s,%s)",
                        (
                            token["id"],
                            getattr(request.state, "asker", None),
                            entry["statement"]
                            if entry.get("statement") is not None
                            else route,
                            entry.get("row_count"),
                            response.status_code if response else 500,
                            Jsonb(detail) if detail else None,
                        ),
                    )

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        from fastapi.responses import JSONResponse

        content = {"detail": str(exc)}
        if getattr(exc, "code", None):
            content["code"] = exc.code
        return JSONResponse(status_code=400, content=content)

    @app.exception_handler(Error)
    async def database_error(request, exc):
        from fastapi.responses import JSONResponse

        # Database details may contain payloads or credentials; never expose
        # them. Errors about the statement itself (syntax, unknown names, bad
        # literals, casts, raised by api functions) only echo what the caller
        # sent, so their message and hint are returned to help it fix the query.
        content = {"detail": "database operation rejected", "code": exc.sqlstate}
        message = statement_error(exc)
        if message:
            content["detail"] = "database operation rejected: " + message
        return JSONResponse(status_code=400, content=content)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """Liveness for container health checks and update scripts: no token,
        no data, only whether the database answers."""
        from fastapi.responses import JSONResponse

        from ..db import ping

        if not ping():
            return JSONResponse(status_code=503, content={"ok": False})
        return {"ok": True}

    @app.get("/v1/openapi.json")
    def openapi(token: Token):
        return app.openapi()

    @app.post("/v1/channels")
    def channel_register(data: Channel, token: Token):
        require_unrestricted(token)
        plugin_access(token, data.plugin)
        if token["kind"] == "plugin" and (
            data.name == "visits" or data.plugin == "native"
        ):
            raise HTTPException(403, "native channels are reserved")
        with authenticated_connection(token) as conn:
            existing = conn.execute(
                "SELECT plugin FROM tl.channels WHERE name=%s", (data.name,)
            ).fetchone()
            if existing and existing["plugin"] != data.plugin:
                raise HTTPException(409, "channel belongs to another plugin")
            return register_channel(conn, data.model_dump())

    @app.get("/v1/channels")
    def channels(token: Token):
        require_unrestricted(token)
        with authenticated_connection(token) as conn:
            rows = conn.execute("SELECT * FROM tl.channels ORDER BY name").fetchall()
            audit(token, len(rows))
            return rows

    @app.post("/v1/ingest/messages", response_model=IngestResult)
    def messages(data: Ingest, token: Token, request: Request):
        require_unrestricted(token)
        plugin_access(token, data.plugin)
        if data.receipts and len(data.messages) > MAX_BATCH:
            raise ValueError(f"receipts are returned for at most {MAX_BATCH} messages")
        for link in data.links:
            link_method(link, token)
        with authenticated_connection(token) as conn:
            with writes.idempotent(conn, token, request, data) as slot:
                if slot.replayed:
                    return slot.response
                # Messages first: a batch may link to messages it delivers, so
                # a link can only be judged once those exist. Every link is
                # authorized before any is written: an authorization failure
                # rolls the whole batch back (403). A vocabulary refusal is a
                # per-item result; any other link error rolls the batch back.
                keys = []
                result = ingest(
                    conn,
                    data.model_copy(update={"links": []}),
                    token["plugin"] if token["kind"] == "plugin" else None,
                    keys=keys,
                )
                for link in data.links:
                    authorize_link(conn, link, token)
                result["link_results"] = link_batch(conn, data.links)
                if data.receipts:
                    result["receipts"] = message_receipts(conn, keys)
                if data.dry_run:
                    conn.rollback()
                    return result
                audit(
                    token,
                    inserted=result["inserted"],
                    new_versions=result["new_versions"],
                    links=len(data.links),
                )
                return slot.store(200, result)

    def message_receipts(conn, keys):
        """Receipts of the current records of resolved message items."""
        if not keys:
            return []
        ids = {
            (r["channel_id"], r["external_id"]): r["id"]
            for r in conn.execute(
                "SELECT m.id,m.channel_id,m.external_id FROM tl.messages m JOIN unnest(%s::bigint[],%s::text[]) k(channel_id,external_id) ON m.channel_id=k.channel_id AND m.external_id=k.external_id WHERE m.superseded_by IS NULL",
                ([k[1] for k in keys], [k[2] for k in keys]),
            )
        }
        return [
            dict(writes.receipt(conn, "message", ids[(cid, ext)]), index=index)
            for index, cid, ext in keys
            if (cid, ext) in ids
        ]

    @app.post("/v1/ingest/track")
    def track(data: TrackBatch, token: Token):
        require_unrestricted(token)
        require(token, "owner", "plugin")
        if token["kind"] == "plugin" and data.points and not token["location_source"]:
            raise HTTPException(403, "token is not an owner-approved location source")
        if token["kind"] == "plugin" and any(
            p.source != token["plugin"] for p in data.points
        ):
            raise HTTPException(403, "track source must match plugin scope")
        with authenticated_connection(token) as conn:
            return ingest_track(conn, data)

    @app.post("/v1/track/detect-visits")
    def visits(
        token: Token,
        radius_m: float = 100,
        min_stay_s: int = 600,
        max_gap_s: float | None = None,
    ):
        require_unrestricted(token)
        # max_gap_s omitted: THROUGHLINE_VISIT_MAX_GAP_S, else 12 hours.
        require(token, "owner", "agent")
        with authenticated_connection(token) as conn:
            return detect_visits(conn, radius_m, min_stay_s, max_gap_s)

    def link_method(data, token):
        if not (
            isinstance(data.get("type"), str)
            and isinstance(data.get("from"), dict)
            and isinstance(data.get("to"), dict)
        ):
            raise ValueError("link requires type, from and to")
        method = data.get("method", "source")
        if method == "owner" and token["kind"] != "owner":
            raise HTTPException(403, "only owner may assert owner links")
        if token["kind"] == "plugin" and method != "source":
            raise HTTPException(403, "plugin links must use source method")

    def message_plugin(conn, message_id):
        row = conn.execute(
            "SELECT c.plugin FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE m.id=%s",
            (message_id,),
        ).fetchone()
        return row["plugin"] if row else None

    def authorize_link(conn, data, token):
        """Decide whether this token may assert this link.

        Owner-method links are owner only. A plugin token asserts source links
        about its own data: every message end must be in one of its own
        channels, and any other end must be an entity (by id, or a placeholder
        named by kind and external_ref, which is how connectors describe works,
        albums, shows and publishers, including entity-to-entity structure).
        People and other kinds are identity and derived data, which plugins
        cannot read or assert. Ends are classified exactly as ingest.endpoint
        resolves them. The manifest's link_types list is written by the plugin
        itself and is documentation, not an authorization list.
        """
        link_method(data, token)
        if token["kind"] != "plugin":
            return
        for side in ("from", "to"):
            end = data[side]
            if "id" in end:
                if end.get("kind") == "message":
                    if message_plugin(conn, end["id"]) != token["plugin"]:
                        raise HTTPException(
                            403, "plugin link message belongs to another plugin"
                        )
                elif end.get("kind") != "entity":
                    raise HTTPException(
                        403, "plugin links may only join its own messages and entities"
                    )
            elif "channel" in end:
                row = conn.execute(
                    "SELECT plugin FROM tl.channels WHERE name=%s", (end["channel"],)
                ).fetchone()
                # An unknown channel is reported by add_link as a missing message.
                if row and row["plugin"] != token["plugin"]:
                    raise HTTPException(
                        403, "plugin link message belongs to another plugin"
                    )
            elif not (
                isinstance(end.get("kind"), str)
                and isinstance(end.get("external_ref"), str)
            ):
                raise ValueError("link end requires id, channel or external_ref")

    def plugin_owns_link(conn, link, token):
        """A plugin may change links its own plugin wrote, under any of its tokens."""
        if link["method"] != "source":
            return False
        if link["token_id"] is not None:
            return link["writer_kind"] == "plugin" and (
                link["writer_plugin"] == token["plugin"]
            )
        # Written before links recorded a token: decide from the endpoints.
        # Without a message of its own the writer cannot be established.
        own = False
        for side in ("from", "to"):
            if link[side + "_kind"] == "message":
                if message_plugin(conn, link[side + "_id"]) != token["plugin"]:
                    return False
                own = True
            elif link[side + "_kind"] != "entity":
                return False
        return own

    @app.post("/v1/links")
    def links(data: dict, token: Token, request: Request):
        require_unrestricted(token)
        with authenticated_connection(token) as conn:
            with writes.idempotent(conn, token, request, data) as slot:
                if slot.replayed:
                    return slot.response
                authorize_link(conn, data, token)
                # A new row is stamped with this token by trigger. An existing
                # row keeps its writer: re-posting a link must never transfer it.
                row, created = add_link(conn, data, outcome=True)
                audit(token, link_id=row["id"])
                return slot.store(
                    200,
                    dict(
                        row,
                        receipt=writes.receipt(
                            conn,
                            "link",
                            row["id"],
                            {"outcome": link_status(conn, data, row, created)},
                        ),
                    ),
                )

    @app.post("/v1/links/batch")
    def links_batch(data: dict, token: Token, request: Request):
        """Up to 1,000 links with a result per item. Authorization is checked
        for every item first: any failure is 403 for the whole batch, nothing
        written and no per-item detail. Then each item runs in its own
        savepoint: created, existing, folded, candidate, refused (vocabulary)
        or error."""
        require_unrestricted(token)
        items = data.get("links")
        if not isinstance(items, list) or not items:
            raise ValueError("links must be a non-empty list")
        if len(items) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} links per batch")
        with authenticated_connection(token) as conn:
            with writes.idempotent(conn, token, request, data) as slot:
                if slot.replayed:
                    return slot.response
                shape = {}
                for index, link in enumerate(items):
                    try:
                        if not isinstance(link, dict):
                            raise ValueError("a link is an object")
                        authorize_link(conn, link, token)
                    except HTTPException as error:
                        if error.status_code == 403:
                            raise HTTPException(
                                403,
                                "a link of this batch is not allowed for this token",
                            ) from None
                        raise
                    except ValueError as error:
                        shape[index] = str(error)
                usable = [i for i in range(len(items)) if i not in shape]
                written = link_batch(conn, [items[i] for i in usable], errors="item")
                outcome = {usable[r["index"]]: r for r in written}
                results = []
                for index in range(len(items)):
                    if index in shape:
                        item = dict(status="error", reason=shape[index], link_id=None)
                    else:
                        item = dict(outcome[index])
                    item["index"] = index
                    item["receipt"] = (
                        writes.receipt(conn, "link", item["link_id"])
                        if item["link_id"] is not None
                        else None
                    )
                    results.append(item)
                counts = {}
                for item in results:
                    counts[item["status"]] = counts.get(item["status"], 0) + 1
                audit(
                    token, len(results), **{"links_" + k: v for k, v in counts.items()}
                )
                return slot.store(200, {"results": results, "counts": counts})

    def changeable_link(conn, link_id, data, token):
        """Lock a link and decide whether this token may change it as `data`
        asks (PATCH and supersede share the rule)."""
        existing = conn.execute(
            "SELECT l.*,t.kind AS writer_kind,t.plugin AS writer_plugin FROM tl.links l LEFT JOIN tl.tokens t ON t.id=l.token_id WHERE l.id=%s FOR UPDATE OF l",
            (link_id,),
        ).fetchone()
        if not existing:
            raise HTTPException(404, "link not found")
        if (
            existing["method"] == "owner"
            and token["kind"] != "owner"
            or data.get("method") == "owner"
            and token["kind"] != "owner"
        ):
            raise HTTPException(403, "owner link protected")
        if token["kind"] == "plugin" and data.get("method", "source") != "source":
            raise HTTPException(403, "plugin links must use source method")
        if token["kind"] == "plugin" and not plugin_owns_link(conn, existing, token):
            raise HTTPException(403, "link belongs to another writer")
        return existing

    @app.patch("/v1/links/{link_id}")
    def link_update(link_id: int, data: dict, token: Token, request: Request):
        require_unrestricted(token)
        body = dict(data)
        data = dict(data)
        expected = writes.expected_hash(data)
        allowed = {"confidence", "method", "evidence", "status", "type", *VALIDITY}
        if not data or set(data) - allowed:
            raise ValueError("unsupported link fields")
        with authenticated_connection(token) as conn:
            with writes.idempotent(conn, token, request, body) as slot:
                if slot.replayed:
                    return slot.response
                existing = changeable_link(conn, link_id, data, token)
                writes.check_hash(conn, "link", link_id, expected)
                values = dict(data)
                values.update(validity(data, existing))
                audit(token, link_id=link_id, fields=sorted(data))
                # The image before this change is kept in tl.link_history.
                row = conn.execute(
                    sql.SQL("UPDATE tl.links SET {} WHERE id=%s RETURNING *").format(
                        sql.SQL(",").join(
                            sql.SQL("{}=%s").format(sql.Identifier(k)) for k in values
                        )
                    ),
                    [Jsonb(v) if k == "evidence" else v for k, v in values.items()]
                    + [link_id],
                ).fetchone()
                return slot.store(
                    200, dict(row, receipt=writes.receipt(conn, "link", link_id))
                )

    @app.post("/v1/links/{link_id}/supersede")
    def link_supersede(link_id: int, data: dict, token: Token, request: Request):
        """Replace a link by a new one and keep both: the old row stays with
        status 'superseded' and superseded_by naming the new link. Fields
        not given are taken from the old link."""
        require_unrestricted(token)
        body = dict(data)
        data = dict(data)
        expected = writes.expected_hash(data)
        allowed = {
            "type",
            "from",
            "to",
            "confidence",
            "method",
            "evidence",
            "job",
            "job_version",
            *VALIDITY,
        }
        if set(data) - allowed:
            raise ValueError("unsupported link fields")
        with authenticated_connection(token) as conn:
            with writes.idempotent(conn, token, request, body) as slot:
                if slot.replayed:
                    return slot.response
                old = changeable_link(conn, link_id, data, token)
                writes.check_hash(conn, "link", link_id, expected)
                if old["status"] != "active" or old["superseded_by"] is not None:
                    raise HTTPException(409, "only an active link can be superseded")
                new = {
                    "type": old["type"],
                    "from": {"kind": old["from_kind"], "id": old["from_id"]},
                    "to": {"kind": old["to_kind"], "id": old["to_id"]},
                    "confidence": old["confidence"],
                    "method": old["method"],
                    "evidence": old["evidence"],
                    "job": old["job"],
                    "job_version": old["job_version"],
                    **{key: old[key] for key in VALIDITY},
                } | data
                authorize_link(conn, new, token)
                row = add_link(conn, new)
                if row["id"] == link_id:
                    raise ValueError("a supersession must change the link")
                superseded = conn.execute(
                    "UPDATE tl.links SET status='superseded',superseded_by=%s WHERE id=%s RETURNING *",
                    (row["id"], link_id),
                ).fetchone()
                audit(token, link_id=link_id, superseded_by=row["id"])
                return slot.store(
                    200,
                    {
                        "superseded": superseded,
                        "link": row,
                        "receipt": writes.receipt(conn, "link", row["id"]),
                    },
                )

    def insert_entity(conn, data):
        # tl.entities.token_id is stamped by trigger from this connection.
        return conn.execute(
            "INSERT INTO tl.entities(kind,name,external_ref,is_placeholder,metadata) VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (
                required(data, "kind"),
                data.get("name"),
                data.get("external_ref"),
                data.get("is_placeholder", False),
                Jsonb(data.get("metadata", {})),
            ),
        ).fetchone()

    @app.post("/v1/entities")
    def entity_create(data: dict, token: Token, request: Request):
        require_unrestricted(token)
        with authenticated_connection(token) as conn:
            with writes.idempotent(conn, token, request, data) as slot:
                if slot.replayed:
                    return slot.response
                row = insert_entity(conn, data)
                audit(token, entity_id=row["id"])
                return slot.store(
                    200, dict(row, receipt=writes.receipt(conn, "entity", row["id"]))
                )

    def authorize_entity(item, token):
        """Whether this token may create this entity. Entities carry no
        owner of their own yet, so every token that passes the route's
        checks may; the batch keeps the whole-batch rule for when one does."""
        if not isinstance(item, dict):
            raise ValueError("an entity is an object")

    @app.post("/v1/entities/batch")
    def entities_batch(data: dict, token: Token, request: Request):
        """Up to 1,000 entities with a result per item: created, existing (same
        kind and external_ref) or error. Authorization is checked for every
        item first (any failure is 403 for the whole batch); then each item
        runs in its own savepoint."""
        require_unrestricted(token)
        items = data.get("entities")
        if not isinstance(items, list) or not items:
            raise ValueError("entities must be a non-empty list")
        if len(items) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} entities per batch")
        with authenticated_connection(token) as conn:
            with writes.idempotent(conn, token, request, data) as slot:
                if slot.replayed:
                    return slot.response
                shape = {}
                for index, item in enumerate(items):
                    try:
                        authorize_entity(item, token)
                    except HTTPException as error:
                        if error.status_code == 403:
                            raise HTTPException(
                                403,
                                "an entity of this batch is not allowed for this token",
                            ) from None
                        raise
                    except ValueError as error:
                        shape[index] = str(error)
                results, counts = [], {}
                for index, item in enumerate(items):
                    result = dict(
                        index=index, status="error", reason=None, entity_id=None
                    )
                    if index in shape:
                        result["reason"] = shape[index]
                    else:
                        try:
                            with conn.transaction():
                                row = None
                                if isinstance(item.get("external_ref"), str):
                                    row = conn.execute(
                                        "SELECT id FROM tl.entities WHERE kind=%s AND external_ref=%s",
                                        (required(item, "kind"), item["external_ref"]),
                                    ).fetchone()
                                if row is not None:
                                    result["status"] = "existing"
                                else:
                                    row = insert_entity(conn, item)
                                    result["status"] = "created"
                                result["entity_id"] = row["id"]
                        except (ValueError, TypeError) as error:
                            result["reason"] = str(error)
                        except Error as error:
                            result["reason"] = (
                                f"database operation rejected ({error.sqlstate})"
                            )
                    result["receipt"] = (
                        writes.receipt(conn, "entity", result["entity_id"])
                        if result["entity_id"] is not None
                        else None
                    )
                    counts[result["status"]] = counts.get(result["status"], 0) + 1
                    results.append(result)
                audit(
                    token,
                    len(results),
                    **{"entities_" + k: v for k, v in counts.items()},
                )
                return slot.store(200, {"results": results, "counts": counts})

    @app.post("/v1/entities/merge")
    def entity_merge(data: dict, token: Token, request: Request):
        require_unrestricted(token)
        require(token, "owner", "agent")
        body = dict(data)
        data = dict(data)
        expected = writes.expected_hash(data)
        source, target = required(data, "source"), required(data, "target")
        if source == target:
            raise ValueError("cannot merge entity into self")
        audit(token, source=source, target=target)
        with (
            authenticated_connection(token) as conn,
            writes.idempotent(conn, token, request, body) as slot,
        ):
            if slot.replayed:
                return slot.response
            if (
                len(
                    conn.execute(
                        "SELECT id FROM tl.entities WHERE id=ANY(%s) AND merged_into IS NULL FOR UPDATE",
                        ([source, target],),
                    ).fetchall()
                )
                != 2
            ):
                raise ValueError("entity not found")
            writes.check_hash(conn, "entity", source, expected)
            sides = {}
            for side in ("from", "to"):
                for row in conn.execute(
                    sql.SQL(
                        "UPDATE tl.links SET {}=%s WHERE {}='entity' AND {}=%s RETURNING id"
                    ).format(
                        sql.Identifier(side + "_id"),
                        sql.Identifier(side + "_kind"),
                        sql.Identifier(side + "_id"),
                    ),
                    (target, source),
                ):
                    sides.setdefault(row["id"], []).append(side)
            # A split restores exactly these links, and only while each is
            # still in the state the merge left (compared by hash, so no link
            # content is copied into the event).
            moved = [
                {"link_id": r["id"], "sides": sides[r["id"]], "hash": r["hash"]}
                for r in conn.execute(
                    "SELECT l.id,md5(to_jsonb(l)::text) AS hash FROM tl.links l WHERE l.id=ANY(%s) ORDER BY l.id",
                    (list(sides),),
                )
            ]
            conn.execute(
                "UPDATE tl.entities SET merged_into=%s WHERE id=%s", (target, source)
            )
            event = conn.execute(
                "INSERT INTO tl.entity_events(op,source,target,moved) VALUES ('merge',%s,%s,%s) RETURNING id",
                (source, target, Jsonb(moved)),
            ).fetchone()
            return slot.store(
                200,
                {
                    "ok": True,
                    "event_id": event["id"],
                    "moved": len(moved),
                    "receipt": writes.receipt(conn, "entity", source),
                },
            )

    @app.post("/v1/entities/split")
    def entity_split(data: dict, token: Token, request: Request):
        require_unrestricted(token)
        require(token, "owner", "agent")
        body = dict(data)
        data = dict(data)
        expected = writes.expected_hash(data)
        event_id = data.get("event_id")
        if isinstance(event_id, bool) or not isinstance(event_id, int):
            raise ValueError("event_id required")
        audit(token, event_id=event_id)
        with (
            authenticated_connection(token) as conn,
            writes.idempotent(conn, token, request, body) as slot,
        ):
            if slot.replayed:
                return slot.response
            event = conn.execute(
                "SELECT e.*,t.kind AS writer_kind FROM tl.entity_events e LEFT JOIN tl.tokens t ON t.id=e.token_id WHERE e.id=%s AND e.op='merge'",
                (event_id,),
            ).fetchone()
            if not event:
                raise HTTPException(404, "merge event not found")
            if event["writer_kind"] == "owner" and token["kind"] != "owner":
                raise HTTPException(403, "owner merge protected")
            if conn.execute(
                "SELECT 1 FROM tl.entity_events WHERE op='split' AND merge_event=%s",
                (event_id,),
            ).fetchone():
                raise HTTPException(409, "merge already split")
            entity = conn.execute(
                "SELECT merged_into FROM tl.entities WHERE id=%s FOR UPDATE",
                (event["source"],),
            ).fetchone()
            if not entity or entity["merged_into"] != event["target"]:
                raise HTTPException(409, "the merged entity changed since the merge")
            writes.check_hash(conn, "entity", event["source"], expected)
            moved = event["moved"]
            current = {
                r["id"]: r["hash"]
                for r in conn.execute(
                    "SELECT l.id,md5(to_jsonb(l)::text) AS hash FROM tl.links l WHERE l.id=ANY(%s) FOR UPDATE",
                    ([m["link_id"] for m in moved],),
                )
            }
            if any(current.get(m["link_id"]) != m["hash"] for m in moved):
                raise HTTPException(409, "a moved link changed since the merge")
            for m in moved:
                for side in m["sides"]:
                    conn.execute(
                        sql.SQL("UPDATE tl.links SET {}=%s WHERE id=%s").format(
                            sql.Identifier(side + "_id")
                        ),
                        (event["source"], m["link_id"]),
                    )
            conn.execute(
                "UPDATE tl.entities SET merged_into=NULL WHERE id=%s",
                (event["source"],),
            )
            split = conn.execute(
                "INSERT INTO tl.entity_events(op,source,target,moved,merge_event) VALUES ('split',%s,%s,%s,%s) RETURNING id",
                (event["source"], event["target"], Jsonb(moved), event_id),
            ).fetchone()
            return slot.store(
                200,
                {
                    "ok": True,
                    "event_id": split["id"],
                    "restored": len(moved),
                    "receipt": writes.receipt(conn, "entity", event["source"]),
                },
            )

    @app.put("/v1/plugins/{name}/cursor/{key}")
    def cursor_put(name: str, key: str, data: dict, token: Token):
        require_unrestricted(token)
        plugin_access(token, name)
        with authenticated_connection(token) as conn:
            return conn.execute(
                "INSERT INTO tl.plugin_cursors(plugin,key,cursor) VALUES (%s,%s,%s) ON CONFLICT(plugin,key) DO UPDATE SET cursor=EXCLUDED.cursor,updated_at=now() RETURNING *",
                (name, key, Jsonb(data)),
            ).fetchone()

    @app.get("/v1/plugins/{name}/cursor/{key}")
    def cursor_get(name: str, key: str, token: Token):
        require_unrestricted(token)
        plugin_access(token, name)
        with authenticated_connection(token) as conn:
            row = conn.execute(
                "SELECT * FROM tl.plugin_cursors WHERE plugin=%s AND key=%s",
                (name, key),
            ).fetchone()
            if not row:
                raise HTTPException(404, "cursor not found")
            audit(token, 1)
            return row

    @app.post("/v1/plugins/{name}/heartbeat")
    def heartbeat(name: str, data: dict, token: Token):
        require_unrestricted(token)
        plugin_access(token, name)
        from .. import health

        with authenticated_connection(token) as conn:
            return health.beat(conn, name, data)

    @app.post("/v1/plugins/{name}/manifest")
    def manifest(name: str, data: dict, token: Token):
        require_unrestricted(token)
        plugin_access(token, name)
        with authenticated_connection(token) as conn:
            return conn.execute(
                "INSERT INTO tl.plugins(name,manifest) VALUES (%s,%s) ON CONFLICT(name) DO UPDATE SET manifest=EXCLUDED.manifest,updated_at=now() RETURNING *",
                (name, Jsonb(data)),
            ).fetchone()

    @app.post(
        "/v1/sql", response_model=SQLResponse, response_model_exclude_unset=True
    )
    def run_sql(data: SQLRequest, token: Token):
        require(token, "owner", "agent")
        limits = clamp(Budget(deadline_ms=data.deadline_ms), token)
        token_budget = None
        if data.token_budget is not None:
            token_budget = clamp(Budget(tokens=data.token_budget), token).tokens
        budgets = data.budgets.model_dump() if data.budgets is not None else None
        if data.validate_only:
            return _validate_only(data, token, budgets)
        columns = []
        result = audited(
            data.statement,
            token,
            data.row_cap,
            params=data.params,
            deadline_ms=limits.deadline_ms,
            token_budget=token_budget,
            trace=data.trace or None,
            budgets=budgets,
            dry_run=data.dry_run,
            approval_id=data.approval_id,
            strict_exact=data.strict_exact,
            columns=columns,
        )
        if data.raw or not isinstance(result.get("rows"), list):
            return dict(result, columns=columns)
        from ..agent_view import DEFAULT_MAX_CELL, DEFAULT_MAX_CHARS, shape_rows

        rows, info = shape_rows(
            result["rows"],
            max_cell=data.max_cell or DEFAULT_MAX_CELL,
            max_chars=data.max_chars or DEFAULT_MAX_CHARS,
        )
        result = dict(result, rows=rows, columns=columns)
        notes = []
        if info["rows_left_out"]:
            result["truncated"] = True
            notes.append(
                f"{info['rows_left_out']} more rows left out at the {info['max_chars']}"
                "-character response budget: select fewer columns, cut text with"
                " left(content, n), add LIMIT/OFFSET, or pass max_chars"
            )
        if info["cells_cut"]:
            notes.append(
                f"{info['cells_cut']} long text cells cut; read the rest with substr()"
                " or pass max_cell"
            )
        if notes:
            execution = dict(result.get("execution") or {})
            execution["warnings"] = list(execution.get("warnings") or []) + notes
            result["execution"] = execution
        return result

    def _validate_only(data, token, budgets):
        # The same validator and the same reader scope as a run, but nothing
        # is executed: the database only plans the statement, so names and
        # types are checked and the columns are known. The audit row carries
        # the statement like any query.
        from .. import semantic
        from ..query import describe_sql

        token.setdefault("audit", {}).update(statement=data.statement, row_count=0)
        audit(token, validate_only=True)
        columns = []
        try:
            if semantic.find_calls(data.statement):
                # Semantic SQL is planned and estimated by its own dry run.
                audited(
                    data.statement,
                    token,
                    data.row_cap,
                    params=data.params,
                    budgets=budgets,
                    dry_run=True,
                    strict_exact=data.strict_exact,
                    columns=columns,
                )
            else:
                columns = describe_sql(
                    data.statement, scope=scope_of(token), params=data.params
                )
        except ValueError as exc:
            return dict(valid=False, error=str(exc), code=None, columns=[])
        except Error as exc:
            return dict(
                valid=False,
                error="database operation rejected",
                code=exc.sqlstate,
                columns=[],
            )
        return dict(valid=True, error=None, code=None, columns=columns)

    @app.post("/v1/sql/stream")
    def run_sql_stream(data: SQLStreamRequest, token: Token):
        """Stream raw rows from one validated SELECT as NDJSON or CSV.

        Runs through the exact same reader role, asker security barrier, and
        AST validator as POST /v1/sql, without the 10,000-row / 4 MiB JSON
        envelope or agent cell truncation.
        """
        require(token, "owner", "agent")
        from ..query import iter_stream, prepare_stream

        token.setdefault("audit", {}).update(statement=data.statement, row_count=0)
        audit(
            token,
            streaming=True,
            format=data.format,
            **({"param_count": len(data.params)} if data.params else {}),
        )
        limits = clamp(Budget(deadline_ms=data.deadline_ms), token)
        prepared = prepare_stream(
            data.statement,
            scope=scope_of(token),
            params=data.params,
            row_cap=data.row_cap,
            deadline_ms=limits.deadline_ms,
            token_row=token,
            budgets=data.budgets.model_dump() if data.budgets is not None else None,
            approval_id=data.approval_id,
            strict_exact=data.strict_exact,
        )
        media_type = (
            "text/csv; charset=utf-8"
            if data.format == "csv"
            else "application/x-ndjson"
        )
        return StreamingResponse(
            iter_stream(prepared, fmt=data.format),
            media_type=media_type,
            headers={"X-Throughline-Format": data.format},
        )

    @app.get("/v1/catalog")
    def catalog(token: Token):
        return audited("SELECT api.catalog() AS catalog", token)["rows"][0]["catalog"]

    @app.get("/v1/context")
    def context(message_id: int, token: Token, before: int = 3, after: int = 3):
        return audited(
            f"SELECT * FROM api.context({message_id},{before},{after})", token
        )

    @app.post("/v1/embed")
    def embed_query(data: EmbedRequest, token: Token):
        require_unrestricted(token)
        require(token, "owner", "agent")
        from ..embeddings import embed_search

        embedding, model = embed_search(
            data.text, with_model=True, scope=scope_of(token)
        )
        return {"embedding": embedding, "model": model}

    @app.get("/v1/search")
    def search(
        q: str,
        token: Token,
        max_results: int = 20,
        mode: str = "keyword",
        person: int | None = None,
        channel: str | None = None,
        after_at: str | None = None,
        before_at: str | None = None,
        place: str | None = None,
    ):
        require(token, "owner", "agent")
        if mode not in ("keyword", "meaning", "fused"):
            raise ValueError("mode must be keyword, meaning or fused")
        if not 1 <= max_results <= 1000:
            raise ValueError("max_results must be 1..1000")

        def execute(function, value, limit):
            args = [value, limit, person, channel, after_at, before_at, place]
            statement = (
                sql.SQL("SELECT * FROM api.{}({})")
                .format(
                    sql.Identifier(function),
                    sql.SQL(",").join(sql.Literal(v) for v in args),
                )
                .as_string()
            )
            return audited(statement, token)

        if mode == "keyword":
            return execute("search_filtered", q, max_results)
        from ..embeddings import embed_search, vector

        query_vector, model = embed_search(q, with_model=True, scope=scope_of(token))
        embedding = vector(query_vector)
        args = ([q, embedding] if mode == "fused" else [embedding]) + [
            max_results,
            person,
            channel,
            after_at,
            before_at,
            place,
        ]
        if mode == "meaning":
            args.append(q)
        args.append(model)
        statement = (
            sql.SQL("SELECT * FROM api.{}({})")
            .format(
                sql.Identifier("search_fused" if mode == "fused" else "search_meaning"),
                sql.SQL(",").join(sql.Literal(v) for v in args),
            )
            .as_string()
        )
        return audited(statement, token)

    @app.get("/v1/person")
    def person(q: str, token: Token):
        return audited(
            sql.SQL("SELECT * FROM api.person({})").format(sql.Literal(q)).as_string(),
            token,
        )

    @app.get("/v1/status")
    def status(token: Token):
        require_unrestricted(token)
        require(token, "owner", "agent")
        with authenticated_connection(token) as conn:
            return dict(
                embeddings=dict(
                    available=_embedding_available(),
                    models=conn.execute(
                        "SELECT model,dimensions,active FROM tl.embedding_models ORDER BY model"
                    ).fetchall(),
                    ledger=conn.execute(
                        "SELECT model,sum(tokens) tokens,sum(cost) cost FROM tl.embedding_ledger GROUP BY model"
                    ).fetchall(),
                ),
                plugins=conn.execute(
                    "SELECT *,last_success_at IS NULL OR now()>last_success_at+make_interval(secs=>interval_s*2) stale FROM tl.plugins"
                ).fetchall(),
                channels=conn.execute(
                    "SELECT c.name,count(m.id),max(m.ingested_at) last_ingest FROM tl.channels c LEFT JOIN tl.messages m ON m.channel_id=c.id AND m.superseded_by IS NULL GROUP BY c.name"
                ).fetchall(),
                staleness=_staleness(conn),
            )

    def _embedding_available():
        from .. import providers

        try:
            return not providers.get("embedding").local
        except ValueError:
            return False

    def _staleness(conn):
        # PLG-7: how old the newest data, each plugin's last
        # success, coverage and the last backup are. Best effort: a database
        # behind on migrations answers without the parts it lacks.
        newest = conn.execute(
            "SELECT max(ingested_at) AS at,extract(epoch FROM now()-max(ingested_at)) AS age FROM tl.messages"
        ).fetchone()
        result = dict(
            newest_ingest_at=newest["at"],
            newest_ingest_age_s=None
            if newest["age"] is None
            else round(float(newest["age"]), 3),
            plugins=conn.execute(
                "SELECT name,last_success_at,extract(epoch FROM now()-last_success_at)::float8 AS age_s,last_success_at IS NULL OR now()>last_success_at+make_interval(secs=>interval_s*2) AS stale FROM tl.plugins ORDER BY name"
            ).fetchall(),
            coverage=None,
            backup=None,
            sources=None,
        )
        if conn.execute(
            "SELECT to_regclass('tl.source_outages') IS NOT NULL AS ok"
        ).fetchone()["ok"]:
            from .. import health

            # Read only, and never fatal: status must answer even when the
            # health evaluation cannot.
            try:
                with conn.transaction():
                    result["sources"] = health.summary(
                        health.sources(conn, record=False)
                    )
            except Exception:
                result["sources"] = dict(error="unavailable")
        if conn.execute(
            "SELECT to_regclass('tl.coverage') IS NOT NULL AS ok"
        ).fetchone()["ok"]:
            rows = conn.execute(
                "SELECT channel,status FROM tl.coverage_all WHERE channel_id IS NOT NULL ORDER BY channel"
            ).fetchall()
            partial = sorted({r["channel"] for r in rows if r["status"] == "partial"})
            best = sorted(
                {r["channel"] for r in rows if r["status"] == "best_effort"}
                - set(partial)
            )
            result["coverage"] = dict(
                partial=partial,
                best_effort=best,
                open_gaps=conn.execute(
                    "SELECT count(*) AS n FROM tl.coverage_gaps WHERE closed_at IS NULL"
                ).fetchone()["n"],
            )
            try:
                from .. import backups

                limit = backups.stale_limits(backups.load())[0]
            except Exception:  # unreadable settings: fall back to the env
                try:
                    limit = float(os.getenv("THROUGHLINE_BACKUP_STALE_HOURS") or 36)
                except ValueError:
                    limit = 36.0
            backup = conn.execute(
                """SELECT max(finished_at) FILTER (WHERE kind='dump' AND status='ok' AND coalesce((detail->'bundle'->>'ok')::boolean,true)) AS last_dump_at,
                          max(finished_at) FILTER (WHERE kind='verify' AND status='ok') AS last_verified_at
                   FROM tl.backup_runs"""
            ).fetchone()
            age = None
            if backup["last_dump_at"] is not None:
                age = conn.execute(
                    "SELECT extract(epoch FROM now()-%s::timestamptz)/3600.0 AS h",
                    (backup["last_dump_at"],),
                ).fetchone()["h"]
                age = round(float(age), 3)
            result["backup"] = dict(
                last_dump_at=backup["last_dump_at"],
                last_verified_at=backup["last_verified_at"],
                age_hours=age,
                stale=age is None or age > limit,
            )
        return result

    @app.post("/v1/identity/people")
    def people_create(data: Person, token: Token, request: Request):
        require_unrestricted(token)
        require(token, "owner", "agent")
        if data.is_owner and token["kind"] != "owner":
            raise HTTPException(403, "only owner can designate owner")
        with (
            authenticated_connection(token) as conn,
            writes.idempotent(conn, token, request, data) as slot,
        ):
            if slot.replayed:
                return slot.response
            row = conn.execute(
                "INSERT INTO tl.people(handle,display_name,is_owner,status,note,kind,source_system,source_id,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s,now())) RETURNING *",
                tuple(data.model_dump().values()),
            ).fetchone()
            row = conn.execute(
                "UPDATE tl.people SET asserted_by=%s WHERE id=%s RETURNING *",
                (token["kind"], row["id"]),
            ).fetchone()
            identity.event(conn, "create", token["kind"], jsonable_encoder(row))
            return slot.store(
                200, dict(row, receipt=writes.receipt(conn, "person", row["id"]))
            )

    @app.get("/v1/identity/people")
    def people(token: Token):
        require_unrestricted(token)
        require(token, "owner", "agent")
        with authenticated_connection(token) as conn:
            rows = conn.execute("SELECT * FROM tl.people ORDER BY id").fetchall()
            audit(token, len(rows))
            return rows

    @app.get("/v1/identity/people/{person_id}")
    def people_get(person_id: int, token: Token):
        require_unrestricted(token)
        require(token, "owner", "agent")
        with authenticated_connection(token) as conn:
            row = conn.execute(
                "SELECT * FROM tl.people WHERE id=%s", (person_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "person not found")
            audit(token, 1)
            return row

    @app.post("/v1/identity/{operation}")
    def identity_operation(operation: str, data: dict, token: Token, request: Request):
        require_unrestricted(token)
        require(token, "owner", "agent")
        with (
            authenticated_connection(token) as conn,
            writes.idempotent(conn, token, request, data) as slot,
        ):
            if slot.replayed:
                return slot.response
            result = identity_change(conn, operation, data, token)
            if isinstance(result, dict) and isinstance(result.get("id"), int):
                kind = {
                    "rename": "person",
                    "attach": "identifier",
                    "detach": "identifier",
                }.get(operation)
                if operation == "ignore" and "identifier_id" in data:
                    kind = "identifier"
                if kind is not None:
                    result = dict(
                        result, receipt=writes.receipt(conn, kind, result["id"])
                    )
            return slot.store(200, result)

    def identity_change(conn, operation, data, token):
        actor = token["kind"]
        if operation in ("attach", "detach"):
            return identity.attach(
                conn,
                data["identifier_id"],
                data.get("person_id") if operation == "attach" else None,
                actor,
            )
        if operation == "merge":
            return identity.merge(
                conn,
                data["source"],
                data["target"],
                actor,
                data.get("override_not_same", False),
            )
        if operation == "split":
            return identity.split(conn, data["event_id"], actor)
        if operation == "rename":
            person = conn.execute(
                "SELECT * FROM tl.people WHERE id=%s AND merged_into IS NULL FOR UPDATE",
                (data["person_id"],),
            ).fetchone()
            if (
                not person
                or actor != "owner"
                and (person["is_owner"] or person["asserted_by"] == "owner")
            ):
                raise ValueError("active unprotected person required")
            conn.execute(
                "UPDATE tl.people SET asserted_by=%s WHERE id=%s",
                (actor, data["person_id"]),
            )
            row = conn.execute(
                "UPDATE tl.people SET handle=%s,display_name=%s WHERE id=%s RETURNING *",
                (data["handle"], data.get("display_name"), data["person_id"]),
            ).fetchone()
        elif operation == "ignore" and "thread_id" in data:
            prior = conn.execute(
                "SELECT metadata FROM tl.threads WHERE id=%s FOR UPDATE",
                (data["thread_id"],),
            ).fetchone()
            if (
                prior
                and (prior["metadata"] or {}).get("asserted_by") == "owner"
                and actor != "owner"
            ):
                raise ValueError("owner assertion is protected")
            row = conn.execute(
                "UPDATE tl.threads SET metadata=metadata || jsonb_build_object('ignored',true,'asserted_by',%s::text) WHERE id=%s RETURNING *",
                (actor, data["thread_id"]),
            ).fetchone()
        elif operation == "ignore":
            row = conn.execute(
                "SELECT * FROM tl.identifiers WHERE id=%s FOR UPDATE",
                (data["identifier_id"],),
            ).fetchone()
            if not row:
                raise ValueError("identifier not found")
            if row["asserted_by"] == "owner" and actor != "owner":
                raise ValueError("owner assertion is protected")
            row = conn.execute(
                "UPDATE tl.identifiers SET status='ignored',asserted_by=%s WHERE id=%s RETURNING *",
                (actor, data["identifier_id"]),
            ).fetchone()
        else:
            raise HTTPException(404, "unknown identity operation")
        identity.event(conn, operation, actor, jsonable_encoder(data))
        return row

    @app.get("/v1/identity/unclaimed")
    def unclaimed(token: Token):
        require_unrestricted(token)
        require(token, "owner", "agent")
        with authenticated_connection(token) as conn:
            rows = conn.execute(
                "SELECT * FROM tl.identifiers WHERE person_id IS NULL AND status!='ignored' ORDER BY id"
            ).fetchall()
            audit(token, len(rows))
            return rows

    # Import primitives share allow-listed column sets; no client SQL identifiers.
    imports = {
        "names": (
            "person_names",
            (
                "person_id",
                "name",
                "kind",
                "is_primary",
                "note",
                "source",
                "created_at",
                "retired_at",
            ),
        ),
        "identifiers": (
            "identifiers",
            (
                "person_id",
                "system",
                "kind",
                "value",
                "value_norm",
                "original_value_norm",
                "status",
                "label",
                "evidence",
                "asserted_by",
                "original_asserted_by",
                "created_at",
                "updated_at",
            ),
        ),
        "schemes": (
            "identifier_schemes",
            ("system", "kind", "same_system", "same_kind", "normalizer"),
        ),
        "fact-keys": ("fact_keys", ("key",)),
        "facts": (
            "facts",
            (
                "person_id",
                "key",
                "value",
                "note",
                "source",
                "as_of",
                "asserted_by",
                "original_asserted_by",
                "created_at",
                "retired_at",
                *VALIDITY,
            ),
        ),
        "relation-kinds": ("relation_kinds", ("kind", "inverse")),
        "relations": (
            "relations",
            (
                "from_id",
                "to_id",
                "kind",
                "note",
                "asserted_by",
                "original_asserted_by",
                "created_at",
                "ended_at",
                *VALIDITY,
            ),
        ),
        "not-same": ("not_same", ("person_a", "person_b", "reason", "created_at")),
    }

    def import_record(resource, data, token, request=None):
        require_unrestricted(token)
        require(token, "owner", "agent")
        if resource not in imports:
            raise HTTPException(404, "unknown identity resource")
        table, columns = imports[resource]
        if set(data) - set(columns):
            raise ValueError("unsupported fields")
        values = dict(data)
        if table in ("facts", "relations"):
            # Validity bounds and precisions, checked together (BRN-14).
            values.update(validity(values))
        if table == "not_same":
            values["person_a"], values["person_b"] = sorted(
                (values["person_a"], values["person_b"])
            )
        people_keys = ("person_id", "from_id", "to_id", "person_a", "person_b")
        authority = token["kind"]
        if table in ("identifiers", "facts", "relations"):
            # Authority comes from the token and can never be raised by the
            # client. It can be lowered: an owner importing what an agent or
            # model proposed sends asserted_by "agent", and the record keeps
            # agent authority instead of becoming an owner assertion. Any other
            # label is a foreign vocabulary and is kept as original_asserted_by.
            if "asserted_by" in values:
                values.setdefault("original_asserted_by", values["asserted_by"])
            if token["kind"] == "owner" and values.get("asserted_by") == "agent":
                authority = "agent"
            values["asserted_by"] = authority
        for k in ("value", "evidence"):
            if k in values and (table == "facts" or k == "evidence"):
                values[k] = Jsonb(values[k])

        def write_import(conn):
            referenced = [
                values[key] for key in people_keys if values.get(key) is not None
            ]
            # Only an owner assertion protects the people it is about.
            identity.protect_people(conn, authority, referenced)
            for person_id in referenced:
                if not conn.execute(
                    "SELECT 1 FROM tl.people WHERE id=%s AND merged_into IS NULL",
                    (person_id,),
                ).fetchone():
                    raise ValueError("active person required")
            # Same rule as identity.attach and rename: only the owner may bind
            # an identifier or a name to the owner or an owner-asserted person.
            if (
                token["kind"] != "owner"
                and table in ("identifiers", "person_names")
                and referenced
                and conn.execute(
                    "SELECT 1 FROM tl.people WHERE id=ANY(%s) AND (is_owner OR asserted_by='owner')",
                    (referenced,),
                ).fetchone()
            ):
                raise ValueError("owner assertion is protected")
            if (
                token["kind"] != "owner"
                and table == "facts"
                and conn.execute(
                    "SELECT 1 FROM tl.facts WHERE person_id=%s AND key=%s AND asserted_by='owner' AND retired_at IS NULL",
                    (values["person_id"], values["key"]),
                ).fetchone()
            ):
                raise ValueError("owner assertion is protected")
            if table == "identifiers":
                from ..ingest import normalize_author
                from ..models import Author

                schemes = {
                    (r["system"], r["kind"]): r
                    for r in conn.execute("SELECT * FROM tl.identifier_schemes")
                }
                values.setdefault("original_value_norm", values.get("value_norm"))
                system, kind, value, norm = normalize_author(
                    conn,
                    Author(
                        system=values["system"],
                        kind=values["kind"],
                        value=values["value"],
                    ),
                    schemes,
                )
                values.update(system=system, kind=kind, value_norm=norm)
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (repr((system, kind, norm)),),
                )
                old = conn.execute(
                    "SELECT * FROM tl.identifiers WHERE system=%s AND kind=%s AND value_norm=%s FOR UPDATE",
                    (system, kind, norm),
                ).fetchone()
                if old:
                    if old["asserted_by"] == "owner" and token["kind"] != "owner":
                        raise ValueError("owner assertion is protected")
                    if old["person_id"] is not None and old["person_id"] != values.get(
                        "person_id"
                    ):
                        raise ValueError(
                            "identifier belongs to another person; merge required"
                        )
                    conn.execute(
                        sql.SQL("UPDATE tl.identifiers SET {} WHERE id=%s").format(
                            sql.SQL(",").join(
                                sql.SQL("{}=%s").format(sql.Identifier(k))
                                for k in values
                            )
                        ),
                        list(values.values()) + [old["id"]],
                    )
                    person = (
                        conn.execute(
                            "SELECT id,asserted_by FROM tl.people WHERE id=%s",
                            (values["person_id"],),
                        ).fetchone()
                        if values.get("person_id") is not None
                        else None
                    )
                    identity.attach(
                        conn, old["id"], values.get("person_id"), token["kind"]
                    )
                    if authority != token["kind"]:
                        # attach records the acting token; the assertion itself
                        # keeps the lower authority the owner imported it with.
                        conn.execute(
                            "UPDATE tl.identifiers SET asserted_by=%s WHERE id=%s",
                            (authority, old["id"]),
                        )
                        if person:
                            conn.execute(
                                "UPDATE tl.people SET asserted_by=%s WHERE id=%s",
                                (person["asserted_by"], person["id"]),
                            )
                    return conn.execute(
                        "SELECT * FROM tl.identifiers WHERE id=%s", (old["id"],)
                    ).fetchone()
            row = conn.execute(
                sql.SQL("INSERT INTO tl.{} ({}) VALUES ({}) RETURNING *").format(
                    sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, values)),
                    sql.SQL(",").join(sql.Placeholder() * len(values)),
                ),
                list(values.values()),
            ).fetchone()
            if table == "identifier_schemes":
                from ..ingest import reconcile_scheme

                reconcile_scheme(conn, row, token["kind"])
            identity.event(
                conn, "import:" + resource, token["kind"], jsonable_encoder(data)
            )
            return row

        with (
            authenticated_connection(token) as conn,
            writes.idempotent(
                conn, token, request, {"resource": resource, "data": data}
            ) as slot,
        ):
            if slot.replayed:
                return slot.response
            row = write_import(conn)
            if table == "identifiers":
                receipt = writes.receipt(conn, "identifier", row["id"])
            else:
                receipt = writes.row_receipt(
                    resource,
                    row.get("id", row.get("key", row.get("kind"))),
                    row,
                )
            return slot.store(200, dict(row, receipt=receipt))

    @app.post("/v1/registry/{resource}")
    def registry_import(resource: str, data: dict, token: Token, request: Request):
        return import_record(resource, data, token, request)

    @app.get("/v1/registry/{resource}")
    def registry_list(resource: str, token: Token):
        require_unrestricted(token)
        require(token, "owner", "agent")
        if resource not in imports:
            raise HTTPException(404, "unknown identity resource")
        with authenticated_connection(token) as conn:
            rows = conn.execute(
                sql.SQL("SELECT * FROM tl.{}").format(
                    sql.Identifier(imports[resource][0])
                )
            ).fetchall()
            audit(token, len(rows))
            return rows

    @app.post("/v1/reparse")
    def reparse_channel(data: dict, token: Token):
        require_unrestricted(token)
        require(token, "owner")
        from ..reparse import reparse

        with authenticated_connection(token) as conn:
            return reparse(conn, required(data, "channel"))

    # Route modules (api/routes/<name>.py exporting `router`), in name order.
    # Authentication is attached here, so no discovered route can be public.
    for _name, router in routers():
        app.include_router(router, dependencies=[Depends(authenticate)])

    # Sign-in (public: nobody is signed in yet), then the session-aware API.
    from .auth_routes import router as auth_router

    app.include_router(auth_router)

    # The web interface's static shell: plain Starlette routes, public by
    # design (it holds no data), outside the schema and outside the audit.
    from .. import web

    web.install(app)

    return app
