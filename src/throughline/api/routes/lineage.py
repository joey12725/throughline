"""Provenance writes, link history and source lookup (BRN-6, BRN-13, QRY-29).

POST /v1/provenance records which source messages a derived record rests on.
Owner tokens write origin 'owner'; plugin tokens write origin 'source', only
for page_revision or derived messages of their own channels, citing sources
by {channel, external_id} in any channel. Source keys that do not resolve are
only counted, so the route reveals nothing about other channels. Agent
tokens are refused: their suggestions go through proposals.

GET /v1/links/{id}/history is an app-role read (unrestricted askers only).
GET /v1/sources runs api.sources through the audited reader path, where the
asker gate applies.
"""

from fastapi import APIRouter, HTTPException, Request
from psycopg import sql

from ... import lineage, writes
from ...ingest import resolve_key
from ..deps import (
    Token,
    audit,
    audited,
    authenticated_connection,
    require,
    require_unrestricted,
)

router = APIRouter()

GENERATED = ("page_revision", "derived")


def _key(end):
    return (
        isinstance(end, dict)
        and isinstance(end.get("channel"), str)
        and isinstance(end.get("external_id"), str)
    )


def _id(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _derived(conn, derived, token):
    """(derived_kind, derived_id) named by the request, authorized for token."""
    if not isinstance(derived, dict):
        raise ValueError("derived must be {channel, external_id} or {kind, id}")
    if _key(derived):
        row = conn.execute(
            "SELECT c.plugin,c.record_kind,m.id FROM tl.channels c LEFT JOIN tl.messages m ON m.channel_id=c.id AND m.external_id=%s AND m.superseded_by IS NULL WHERE c.name=%s",
            (derived["external_id"], derived["channel"]),
        ).fetchone()
        if token["kind"] == "plugin":
            if row and row["plugin"] != token["plugin"]:
                raise HTTPException(403, "derived record belongs to another plugin")
            if row and row["record_kind"] not in GENERATED:
                raise HTTPException(
                    403,
                    "plugins record provenance only for page_revision and derived records",
                )
        ident = row["id"] if row else None
        if row and ident is None:
            ident = resolve_key(conn, derived["channel"], derived["external_id"])
        if ident is None:
            raise ValueError("derived record does not exist")
        return "message", ident
    if token["kind"] == "plugin":
        raise HTTPException(
            403, "plugins name derived records by channel and external_id"
        )
    kind, ident = derived.get("kind"), derived.get("id")
    if kind not in lineage.DERIVED_KINDS or not _id(ident):
        raise ValueError(
            "derived kind must be one of " + ", ".join(lineage.DERIVED_KINDS)
        )
    return kind, ident


@router.post("/v1/provenance")
def provenance_add(data: dict, token: Token, request: Request):
    require_unrestricted(token)
    if token["kind"] == "agent":
        raise HTTPException(403, "agents propose provenance; they do not record it")
    require(token, "owner", "plugin")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty list")
    if len(sources) > lineage.MAX_SOURCES:
        raise ValueError(f"at most {lineage.MAX_SOURCES} sources per call")
    relation = data.get("relation") or "derived_from"
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            derived_kind, derived_id = _derived(conn, data.get("derived"), token)
            ids, unresolved = [], 0
            for source in sources:
                if _key(source):
                    # An alias of a record names the record.
                    found = resolve_key(conn, source["channel"], source["external_id"])
                    row = None if found is None else {"id": found}
                elif (
                    token["kind"] == "owner"
                    and isinstance(source, dict)
                    and _id(source.get("id"))
                ):
                    row = conn.execute(
                        "SELECT id FROM tl.messages WHERE id=%s", (source["id"],)
                    ).fetchone()
                else:
                    raise ValueError("a source is {channel, external_id}")
                if row is None or (
                    derived_kind == "message" and row["id"] == derived_id
                ):
                    unresolved += 1
                else:
                    ids.append(row["id"])
            written = lineage.add_provenance(
                conn,
                token,
                derived_kind,
                derived_id,
                ids,
                relation,
                "owner" if token["kind"] == "owner" else "source",
            )
            audit(token, written=written, unresolved=unresolved)
            answer = {"written": written, "unresolved": unresolved, "receipt": None}
            try:
                answer["receipt"] = writes.receipt(
                    conn, "provenance", derived_id, {"derived_kind": derived_kind}
                )
            except LookupError:
                pass  # nothing resolved: no provenance row to read back
            return slot.store(200, answer)


@router.get("/v1/links/{link_id}/history")
def link_history(link_id: int, token: Token):
    require_unrestricted(token)
    require(token, "owner", "agent")
    with authenticated_connection(token) as conn:
        link = conn.execute("SELECT * FROM tl.links WHERE id=%s", (link_id,)).fetchone()
        history = lineage.link_history(conn, link_id)
        if not link and not history:
            raise HTTPException(404, "link not found")
        audit(token, len(history), link_id=link_id)
        return {"link": link, "history": history}


@router.get("/v1/sources")
def sources(kind: str, id: int, token: Token):
    if kind not in lineage.DERIVED_KINDS:
        raise ValueError("kind must be one of " + ", ".join(lineage.DERIVED_KINDS))
    statement = (
        sql.SQL("SELECT * FROM api.sources({},{})")
        .format(sql.Literal(kind), sql.Literal(id))
        .as_string()
    )
    return audited(statement, token)
