"""Moving the owner's GBrain into native Pages (docs/v11/pages.md).

The GBrain plugin keeps importing GBrain into Throughline as records
(gbrain.pages, gbrain.legacy, gbrain.timeline, and links between them). This
module turns those records into native pages, so there is one page graph:

- every live GBrain page becomes a page (its slug, normalized; its type mapped
  to a kind; title, aliases, tags; compiled truth as the top), and every
  legacy page an archived page;
- GBrain's timeline (the page's timeline text and the gbrain.timeline rows)
  becomes timeline rows, plus one line citing the imported record the page
  came from, so every migrated page is cited;
- person pages are tied to their person (the confirmed 'gbrain' identifier),
  and a person page a lens wrote for the same person is merged in;
- links between GBrain pages become links between native pages (registered
  types kept; any other type becomes related_to with the GBrain type kept in
  the evidence);
- the original GBrain slugs redirect to the native ones.

It is re-runnable (after each GBrain sync, until GBrain is retired): a page
is updated only when its GBrain source changed, and a page the Librarian or
the owner has edited since keeps its text; only new timeline lines are added.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from ..db import connect
from ..ingest import add_link
from . import store

BY = "gbrain-import"
SOURCES = ("gbrain.pages", "gbrain.legacy")
KINDS = {
    "person": "people", "people": "people", "event": "events", "place": "places", "organization": "organizations",
    "company": "organizations", "project": "projects", "media": "media", "concept": "concepts",
    "commitment": "commitments", "conversation": "conversations", "report": "reports", "document": "documents",
    "dossier": "dossiers", "index": "indexes", "routine": "routines", "note": "notes", "pet": "pets",
}


def normalize_slug(raw: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", raw or "").encode("ascii", "ignore").decode("ascii").lower()
    parts = []
    for seg in text.strip("/").split("/"):
        seg = re.sub(r"[^a-z0-9._-]+", "-", seg).strip("-._")
        if seg:
            parts.append(seg[:80])
    if not parts:
        parts = ["page"]
    if len(parts) > 8:
        parts = parts[:7] + ["-".join(parts[7:])[:80]]
    slug = "/".join(parts)
    return slug[:255].rstrip("/-._")


def _records(conn, channel: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """SELECT m.id, m.external_id, m.happened_at, m.fields, r.payload
        FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id JOIN tl.raw_payloads r ON r.message_id=m.id
        WHERE c.name=%s AND m.superseded_by IS NULL ORDER BY m.id""",
        (channel,),
    )]


def _timeline_rows(conn) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        """SELECT m.id, m.fields->>'page_external_id' AS page, r.payload FROM tl.messages m
        JOIN tl.channels c ON c.id=m.channel_id JOIN tl.raw_payloads r ON r.message_id=m.id
        WHERE c.name='gbrain.timeline' AND m.superseded_by IS NULL"""
    ):
        out.setdefault(r["page"], []).append({"id": r["id"], **(r["payload"] or {})})
    return out


def _date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(value)[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", str(value)) else None


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _plan(conn) -> list[dict[str, Any]]:
    """One entry per GBrain record: native slug, kind, title, body parts."""
    people = {r["value"].lower(): r["person_id"] for r in conn.execute(
        "SELECT value, person_id FROM tl.identifiers WHERE system='gbrain' AND status='confirmed' AND person_id IS NOT NULL")}
    lines = _timeline_rows(conn)
    taken: dict[str, str] = {}
    plan = []
    for channel in SOURCES:
        for rec in _records(conn, channel):
            payload = rec["payload"] or {}
            page = payload.get("page") or {}
            fields = rec["fields"] or {}
            raw_slug = str(page.get("slug") or fields.get("slug") or rec["external_id"])
            gtype = str(page.get("type") or fields.get("page_type") or "note").lower()
            kind = KINDS.get(gtype, "notes")
            slug = normalize_slug(raw_slug)
            ref = f"{channel}:{rec['external_id']}"
            base, n = slug, 1
            while slug in taken and taken[slug] != ref:
                n += 1
                slug = f"{base[:250]}-{n}"
            taken[slug] = ref
            version = payload.get("version") or {}
            truth = str(version.get("compiled_truth") or page.get("compiled_truth") or "").strip()
            title = str(page.get("title") or fields.get("title") or raw_slug.rsplit("/", 1)[-1]).strip()[:300] or slug
            aliases = [a for a in (fields.get("aliases") or []) if isinstance(a, str)]
            for a in payload.get("page_aliases") or []:
                v = (a.get("alias") or a.get("alias_norm")) if isinstance(a, dict) else a
                if isinstance(v, str):
                    aliases.append(v)
            tags = [(t.get("tag") if isinstance(t, dict) else t) for t in (payload.get("tags") or fields.get("tags") or [])]
            tags = [t for t in tags if isinstance(t, str)]
            redirects = {raw_slug.lower()} | {
                str(a.get("alias_slug") if isinstance(a, dict) else a).lower()
                for a in payload.get("slug_aliases") or [] if (a.get("alias_slug") if isinstance(a, dict) else a)
            }
            entries: list[tuple[str, str]] = []
            _, parsed, _ = store.split_timeline("## Timeline\n" + str(page.get("timeline") or ""))
            entries += parsed
            for row in lines.get(rec["external_id"], []):
                d = _date(row.get("date"))
                if not d or not row.get("summary"):
                    continue
                text = " ".join(str(row["summary"]).split())
                if row.get("source"):
                    text += f" ({' '.join(str(row['source']).split())[:120]})"
                if row.get("detail"):
                    text += " — " + " ".join(str(row["detail"]).split())[:600]
                entries.append((d, f"{text} [m:{row['id']}]"))
            created = _date(page.get("created_at")) or rec["happened_at"].date().isoformat()
            entries.append((created, f"Page created in GBrain ({gtype}, {raw_slug[:120]}) [m:{rec['id']}]"))
            when = {"start": _date(page["effective_date"])} if page.get("effective_date") and _date(page.get("effective_date")) else None
            plan.append({
                "ref": ref, "record_id": rec["id"], "slug": slug, "kind": kind, "title": title,
                "aliases": list(dict.fromkeys(a.strip() for a in aliases if a.strip() and a.strip() != title))[:20],
                "tags": tags, "truth": truth or f"_{title}: imported from GBrain without compiled truth._",
                "entries": entries, "person_id": people.get(raw_slug.lower()) if kind == "people" else None,
                "when": when, "status": "archived" if channel == "gbrain.legacy" else "active",
                "hash": str(fields.get("source_hash") or page.get("content_hash") or ""),
                "updated_at": _ts(page.get("updated_at")) or rec["happened_at"],
                "created_at": _ts(page.get("created_at")),
                "redirects": {r for r in redirects if r},
                "meta": {"system": "gbrain", "record": ref, "slug": raw_slug, "type": gtype, "id": page.get("id"),
                         "source_uri": page.get("source_uri"), "frontmatter": page.get("frontmatter")},
            })
    return plan


def _body(item: dict[str, Any]) -> str:
    lines = "\n".join(store.timeline_line(d, t) for d, t in item["entries"])
    return item["truth"] + ("\n\n## Timeline\n" + lines if lines else "")


LOCK = (72_462, 1)  # pg advisory lock: one import at a time


def run(*, dry_run: bool = False, limit: int | None = None, batch: int = 100) -> dict[str, Any]:
    if dry_run:
        return _run(dry_run=True, limit=limit, batch=batch)
    with connect() as guard:
        if not guard.execute("SELECT pg_try_advisory_lock(%s,%s) AS ok", LOCK).fetchone()["ok"]:
            return {"status": "locked", "detail": "another GBrain import is running"}
        guard.commit()
        try:
            return _run(dry_run=False, limit=limit, batch=batch)
        finally:
            guard.rollback()
            guard.execute("SELECT pg_advisory_unlock(%s,%s)", LOCK)


def _run(*, dry_run: bool, limit: int | None, batch: int) -> dict[str, Any]:
    started = time.perf_counter()
    out: dict[str, Any] = {"created": 0, "updated": 0, "adopted": 0, "unchanged": 0, "kept_native": 0,
                           "timeline_added": 0, "merged_people": 0, "links": 0, "links_retyped": 0,
                           "redirects": 0, "errors": []}
    with connect() as conn:
        store.ensure_channel(conn)
        plan = _plan(conn)
    if limit:
        plan = plan[:limit]
    out["planned"] = len(plan)
    if dry_run:
        from collections import Counter

        out["by_kind"] = dict(Counter(p["kind"] for p in plan).most_common())
        out["archived"] = sum(1 for p in plan if p["status"] == "archived")
        out["people_tied"] = sum(1 for p in plan if p["person_id"])
        out["renamed_slugs"] = sum(1 for p in plan if p["slug"] != p["meta"]["slug"])
        return out
    by_ref = {p["ref"]: p for p in plan}
    for i in range(0, len(plan), batch):
        with connect() as conn:
            for item in plan[i:i + batch]:
                try:
                    with conn.transaction():
                        _import_one(conn, item, out)
                except Exception as exc:  # one bad page never stops the import
                    out["errors"].append(f"{item['ref']}: {type(exc).__name__}: {str(exc)[:200]}")
    with connect() as conn:
        _redirects(conn, plan, out)
        _merge_people(conn, plan, out)
        _links(conn, by_ref, out)
    out["errors"] = out["errors"][:50]
    out["seconds"] = round(time.perf_counter() - started, 1)
    return out


def _import_one(conn, item: dict[str, Any], out: dict[str, Any]) -> None:
    page = conn.execute("SELECT * FROM tl.pages WHERE source_ref=%s", (item["ref"],)).fetchone()
    target = page["slug"] if page else item["slug"]
    if page is None:
        n = 1
        while True:
            page = conn.execute("SELECT * FROM tl.pages WHERE slug=%s", (target,)).fetchone()
            if page is None or page["source_ref"] is None:
                break  # free, or a page a lens wrote there (adopted)
            n += 1  # another imported page owns this slug
            target = f"{item['slug'][:248]}-{n}"
    stats = (page or {}).get("stats") or {}
    if page and page["source_ref"] == item["ref"] and stats.get("gbrain_hash") == item["hash"]:
        out["unchanged"] += 1
        return
    if page and page["source_ref"] == item["ref"] and page["updated_by"] != BY:
        # Edited natively since it moved in: keep its text, add new timeline lines.
        added = store.add_timeline(conn, page["slug"], item["entries"], BY)
        if added:
            cur = store.get(conn, page["slug"])
            store.write(conn, slug=page["slug"], kind=cur["kind"], title=cur["title"], body=cur["body"], by=page["updated_by"],
                        summary=cur["summary"] or "", aliases=cur["aliases"], person_id=cur["person_id"], status=cur["status"],
                        reason="timeline lines from GBrain")
        out["timeline_added"] += added
        out["kept_native"] += 1
        conn.execute("UPDATE tl.pages SET stats = stats || %s WHERE slug=%s", (Jsonb({"gbrain_hash": item["hash"]}), page["slug"]))
        return
    res = store.write(
        conn, slug=target, kind=item["kind"], title=item["title"], body=_body(item), by=BY,
        aliases=item["aliases"], person_id=item["person_id"], when=item["when"], now=item["updated_at"],
        tags=item["tags"], source_ref=item["ref"], meta=item["meta"], status=item["status"],
        reason="moved in from GBrain" if not page else "updated in GBrain",
    )
    conn.execute(
        """UPDATE tl.pages SET stats = stats || %s, truth_updated_at=%s,
             created_at=coalesce(%s, created_at), updated_at=greatest(updated_at, %s) WHERE slug=%s""",
        (Jsonb({"gbrain_hash": item["hash"], "origin": "gbrain"}), item["updated_at"], item["created_at"],
         item["updated_at"], res["slug"]),
    )
    out["timeline_added"] += res.get("timeline_added", 0)
    if page is None:
        out["created"] += 1
    elif page["source_ref"] is None:
        out["adopted"] += 1  # a page a lens had written at the same slug
    else:
        out["updated"] += 1


def _redirects(conn, plan: list[dict[str, Any]], out: dict[str, Any]) -> None:
    for item in plan:
        row = conn.execute("SELECT slug FROM tl.pages WHERE source_ref=%s", (item["ref"],)).fetchone()
        if not row:
            continue
        for old in item["redirects"]:
            if old == row["slug"] or conn.execute("SELECT 1 FROM tl.pages WHERE slug=%s", (old,)).fetchone():
                continue
            conn.execute(
                """INSERT INTO tl.page_redirects(old_slug, slug, reason) VALUES (%s,%s,'GBrain slug')
                   ON CONFLICT (old_slug) DO NOTHING""",
                (old, row["slug"]),
            )
            out["redirects"] += 1


def _merge_people(conn, plan: list[dict[str, Any]], out: dict[str, Any]) -> None:
    """A person page a lens wrote for someone GBrain already had a page about
    is merged into the GBrain page (its timeline, aliases and links move)."""
    for item in plan:
        if not item["person_id"] or item["status"] != "active":
            continue
        g = conn.execute("SELECT slug FROM tl.pages WHERE source_ref=%s AND status='active'", (item["ref"],)).fetchone()
        if not g:
            continue
        for other in conn.execute(
            "SELECT slug FROM tl.pages WHERE person_id=%s AND status='active' AND source_ref IS NULL AND kind='people' AND slug<>%s",
            (item["person_id"], g["slug"]),
        ).fetchall():
            try:
                with conn.transaction():
                    store.merge(conn, src=other["slug"], into=g["slug"], by=BY, reason="same person as the GBrain page")
                out["merged_people"] += 1
            except store.PageError as exc:
                out["errors"].append(f"merge {other['slug']}: {exc}")


def _links(conn, by_ref: dict[str, dict[str, Any]], out: dict[str, Any]) -> None:
    rows = conn.execute(
        """SELECT l.type, l.evidence, ca.name AS from_ch, a.external_id AS from_ext, cb.name AS to_ch, b.external_id AS to_ext
        FROM tl.links l
        JOIN tl.messages a ON l.from_kind='message' AND a.id=l.from_id JOIN tl.channels ca ON ca.id=a.channel_id
        JOIN tl.messages b ON l.to_kind='message' AND b.id=l.to_id JOIN tl.channels cb ON cb.id=b.channel_id
        WHERE l.status='active' AND ca.name = ANY(%(s)s) AND cb.name = ANY(%(s)s)""",
        {"s": list(SOURCES)},
    ).fetchall()
    # GBrain grew ~975 one-off link types (registered with origin 'legacy');
    # only the curated vocabulary is kept, the rest become related_to with the
    # GBrain type in the evidence (nothing is lost; lenses can retype later).
    active = {r["type"] for r in conn.execute("SELECT type FROM tl.link_types WHERE status='active' AND origin <> 'legacy'")}
    pages = {r["source_ref"]: r["message_id"] for r in conn.execute(
        "SELECT source_ref, message_id FROM tl.pages WHERE source_ref IS NOT NULL AND message_id IS NOT NULL")}
    for r in rows:
        a = pages.get(f"{r['from_ch']}:{r['from_ext']}")
        b = pages.get(f"{r['to_ch']}:{r['to_ext']}")
        if not a or not b or a == b:
            continue
        resolved = conn.execute("SELECT tl.resolve_link_type(%s) AS t", (r["type"],)).fetchone()["t"]
        ltype = resolved if resolved in active else "related_to"
        evidence: dict[str, Any] = {"gbrain_type": r["type"]}
        if isinstance(r["evidence"], dict) and r["evidence"].get("context"):
            evidence["context"] = str(r["evidence"]["context"])[:300]
        try:
            with conn.transaction():
                add_link(conn, {"type": ltype, "from": {"kind": "message", "id": a}, "to": {"kind": "message", "id": b},
                                "method": "rule", "evidence": evidence, "job": BY, "job_version": "1"})
            out["links"] += 1
            out["links_retyped"] += ltype != r["type"]
        except Exception as exc:
            out["errors"].append(f"link {r['type']}: {str(exc)[:120]}")
