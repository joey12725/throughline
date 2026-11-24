"""The page graph over HTTP (docs/v11/pages.md).

Pages are written by lenses with output 'pages' (the Librarian) and by the
owner. Reads: unrestricted owner and agent tokens (the registry and graph are
identity-grade data; restricted askers read page records through the gated
record views like any record). Writes: owner only, with an Idempotency-Key.
"""

from fastapi import APIRouter, HTTPException, Request

from ... import pages, writes
from ..deps import Token, audit, authenticated_connection, require, require_unrestricted

router = APIRouter()


def _reader(token):
    require_unrestricted(token)
    require(token, "owner", "agent")


def _owner(token):
    require_unrestricted(token)
    require(token, "owner")


@router.get("/v1/pages")
def page_list(token: Token, kind: str | None = None, q: str | None = None, limit: int = 30):
    _reader(token)
    with authenticated_connection(token) as conn:
        rows = pages.search(conn, q, kind=kind, limit=limit)
    audit(token, len(rows))
    return rows


@router.get("/v1/pages/health")
def page_health(token: Token):
    _reader(token)
    rep = pages.report()
    audit(token, rep["graph"]["pages"])
    return rep


@router.get("/v1/pages/actions")
def page_actions(token: Token, lens: str | None = None, status: str | None = None, limit: int = 50):
    _reader(token)
    with authenticated_connection(token) as conn:
        rows = conn.execute(
            """SELECT id, at, lens, window_key, op, slug, status, detail FROM tl.page_actions
            WHERE (%s::text IS NULL OR lens=%s) AND (%s::text IS NULL OR status=%s) ORDER BY id DESC LIMIT %s""",
            (lens, lens, status, status, max(1, min(limit, 500))),
        ).fetchall()
    audit(token, len(rows))
    return rows


@router.get("/v1/pages/timeline")
def page_timeline(token: Token, on: str | None = None, since: str | None = None, until: str | None = None,
                  slug: str | None = None, limit: int = 100):
    """Timeline lines across pages: a day (on), a span, or one page."""
    _reader(token)
    with authenticated_connection(token) as conn:
        try:
            rows = pages.chronicle(conn, on=on, since=since, until=until, slug=slug, limit=limit)
        except Exception as exc:
            raise HTTPException(400, str(exc)[:200]) from None
    audit(token, len(rows))
    return rows


@router.get("/v1/pages/kinds")
def page_kinds(token: Token):
    _reader(token)
    with authenticated_connection(token) as conn:
        rows = pages.kinds(conn)
    audit(token, len(rows))
    return rows


@router.post("/v1/pages/merge")
def page_merge(data: dict, token: Token, request: Request):
    """Merge one page into another: {from, into, reason?}."""
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            try:
                res = pages.merge(conn, src=str(data.get("from") or ""), into=str(data.get("into") or ""),
                                  by="owner", reason=str(data.get("reason") or ""))
            except pages.PageError as exc:
                raise HTTPException(400, str(exc)) from None
            audit(token, 1)
            return slot.store(200, res)


@router.post("/v1/pages")
def page_write(data: dict, token: Token, request: Request):
    """Write a page by hand: {slug, title, body, kind?, summary?, aliases?, person_id?, when?}."""
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            try:
                res = pages.write(conn, slug=data.get("slug"), kind=data.get("kind") or str(data.get("slug") or "").split("/")[0],
                                  title=data.get("title"), body=data.get("body"), by="owner", summary=data.get("summary") or "",
                                  aliases=data.get("aliases"), person_id=data.get("person_id"), when=data.get("when"))
            except pages.PageError as exc:
                raise HTTPException(400, str(exc)) from None
            audit(token, 1, slug=res["slug"])
            return slot.store(200, res)


@router.post("/v1/pages/links")
def page_link(data: dict, token: Token, request: Request):
    """Link two pages (or a page and person:ID): {from, to, type?, evidence: [ids], note?}."""
    _owner(token)
    with authenticated_connection(token) as conn:
        with writes.idempotent(conn, token, request, data) as slot:
            if slot.replayed:
                return slot.response
            try:
                res = pages.link(conn, src=str(data.get("from") or ""), dst=str(data.get("to") or ""),
                                 type=str(data.get("type") or "related_to"), evidence=data.get("evidence") or [],
                                 by="owner", note=data.get("note"))
            except pages.PageError as exc:
                raise HTTPException(400, str(exc)) from None
            audit(token, 1)
            return slot.store(200, res)


@router.get("/v1/pages/{slug:path}")
def page_get(slug: str, token: Token):
    _reader(token)
    with authenticated_connection(token) as conn:
        page = pages.get(conn, slug.strip("/").lower())
        if not page:
            raise HTTPException(404, "no such page")
        page["links"] = pages.neighbors(conn, page["slug"])
        page["timeline"] = pages.chronicle(conn, slug=page["slug"], limit=200)
    page.pop("payload", None)
    audit(token, 1)
    return page
