"""The page store: one namespace of slugs, page revisions as records, links.

A page is identified by its slug ('people/alex-rivera', 'events/2026-11-21-
thanksgiving', 'deals/acme-renewal'). Its kind is free-form (people, events,
deals, places, topics, days, dynamics, ...). Each revision is a page_revision
record on the native channel 'pages' (external_id = slug), so it is
versioned, cited through provenance, split into passages, embedded and found
by recall. tl.pages is the registry: kind, title, aliases, summary, the
current revision, status.

Two kinds of links leave a page:

- body links: ``[[slug]]``, ``[[slug|label]]`` and ``[[person:ID|Name]]`` in
  the text. They follow the text: each revision's body links are reconciled
  (job 'pages', method 'rule').
- asserted links: written by a ``link`` action with evidence message ids
  (job 'librarian', method 'model'). They stay until an ``unlink``.

Ingest moves the links of a page to each new revision, so both survive edits.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from ..ingest import add_link, ingest, register_channel
from ..lineage import MAX_SOURCES, add_provenance
from ..models import Ingest

CHANNEL = "pages"
BODY_JOB = "pages"
LINK_JOB = "librarian"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*){0,7}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
CITE_RE = re.compile(r"\[m:\s*(\d+(?:\s*,\s*(?:m:)?\s*\d+)*)\s*\]")
WIKI_RE = re.compile(r"\[\[\s*([^\]|]+?)\s*(?:\|([^\]]*))?\]\]")

PAGE_FIELDS = [
    {"name": "page_kind", "type": "text", "description": "Page kind (people, events, deals, ...)"},
    {"name": "title", "type": "text", "description": "Page title"},
    {"name": "summary", "type": "text", "description": "What the page says now, in a sentence or two"},
    {"name": "aliases", "type": "json", "description": "Other names of what the page is about"},
    {"name": "about_person", "type": "integer", "description": "The person the page is about"},
    {"name": "citations", "type": "integer", "description": "Evidence records cited"},
    {"name": "written_by", "type": "text", "description": "The lens (or owner) that wrote this revision"},
    {"name": "reason", "type": "text", "description": "Why this revision was written"},
]


class PageError(ValueError):
    """A page action that cannot be applied."""


def slugify(text: str) -> str:
    import unicodedata

    norm = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")[:80] or "page"


def check_slug(slug: Any) -> str:
    if not isinstance(slug, str):
        raise PageError("slug must be a string such as 'people/ada-lovelace'")
    slug = slug.strip().strip("/").lower()
    if not SLUG_RE.fullmatch(slug) or len(slug) > 255:
        raise PageError(f"invalid slug {slug!r}: lowercase segments of a-z 0-9 . _ - separated by '/', e.g. 'events/2026-11-21-dinner'")
    return slug


def ensure_channel(conn) -> None:
    # Registering rebuilds the channel's views (slow): only when the channel
    # is missing or its declaration changed.
    row = conn.execute(
        "SELECT fields, passage_text_paths, record_kind FROM tl.channels WHERE name=%s", (CHANNEL,)
    ).fetchone()
    if row and row["record_kind"] == "page_revision" and list(row["passage_text_paths"] or []) == ["/title", "/body"] \
            and {f.get("name") for f in row["fields"] or []} >= {f["name"] for f in PAGE_FIELDS}:
        return
    register_channel(conn, dict(
        name=CHANNEL, plugin="native",
        description="Maintained pages (written by page lenses and the owner)",
        fields=PAGE_FIELDS, record_kind="page_revision",
        # Passages are built from the title and the body, the way GBrain
        # pages are (title, compiled truth, timeline), so identical text keeps
        # its vectors (passage embeddings are keyed by text hash).
        passage_text_paths=["/title", "/body"],
    ))


# --- reading ------------------------------------------------------------------


def get(conn, slug: str) -> dict[str, Any] | None:
    slug = canonical(conn, slug)
    row = conn.execute(
        """SELECT p.*, m.content, m.happened_at AS revised_at, r.payload
        FROM tl.pages p LEFT JOIN tl.messages m ON m.id=p.message_id
        LEFT JOIN tl.raw_payloads r ON r.message_id=p.message_id
        WHERE p.slug=%s""",
        (slug,),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    payload = out.pop("payload", None) or {}
    out["body"] = payload.get("body") if isinstance(payload, dict) and payload.get("body") is not None else (out.get("content") or "")
    out["citations"] = parse_citations(out["body"])
    out["truth"], _, _ = split_timeline(out["body"])
    return out


def parse_citations(body: str) -> list[int]:
    ids: list[int] = []
    for m in CITE_RE.finditer(body or ""):
        ids.extend(int(x) for x in re.findall(r"\d+", m.group(1)))
    return list(dict.fromkeys(ids))


def clean_citations(body: str, allowed: set[int]) -> tuple[str, list[int], int]:
    """Drop citation ids outside `allowed`: (body, kept ids, removed count)."""
    removed = 0
    kept: list[int] = []

    def sub(m: re.Match[str]) -> str:
        nonlocal removed
        ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
        good = list(dict.fromkeys(i for i in ids if i in allowed))
        removed += len(ids) - len(good)
        kept.extend(good)
        return "[m:" + ",".join(map(str, good)) + "]" if good else ""

    out = CITE_RE.sub(sub, body or "")
    out = re.sub(r"[ \t]+([.,;:])", r"\1", out)
    return out, list(dict.fromkeys(kept)), removed


def parse_wikilinks(body: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    for m in WIKI_RE.finditer(body or ""):
        target = m.group(1).strip()
        if target not in seen:
            seen.add(target)
            out.append((target, (m.group(2) or "").strip()))
    return out


def sections(body: str) -> list[tuple[str, str]]:
    """[(heading, text)] split at '## ' headings; heading '' is the preamble."""
    parts: list[tuple[str, str]] = []
    current, buf = "", []
    for line in (body or "").splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            parts.append((current, "\n".join(buf).strip()))
            current, buf = m.group(1), []
        else:
            buf.append(line)
    parts.append((current, "\n".join(buf).strip()))
    return [(h, t) for h, t in parts if h or t]


def edit_section(body: str, section: str, mode: str, text: str) -> str:
    parts = sections(body)
    want = section.strip().lower()
    for i, (h, t) in enumerate(parts):
        if h.strip().lower() == want:
            new = text.strip() if mode == "replace" else (t + "\n" + text.strip()).strip()
            parts[i] = (h, new)
            break
    else:
        parts.append((section.strip(), text.strip()))
    return "\n\n".join((f"## {h}\n{t}" if h else t) for h, t in parts).strip()


# --- compiled truth + timeline (GBrain's page anatomy) ----------------------------
#
# The top of a page is compiled truth: rewritten when the picture changes. The
# '## Timeline' section is the dated evidence log: append-only. Its lines are
# rows in tl.page_timeline (deduplicated on slug, date, summary) and every
# revision renders all of them, newest first, so no write can drop one.

TIMELINE_HEADING = "Timeline"
TIMELINE_LINE_RE = re.compile(
    r"^\s*[-*]\s+\*{0,2}(\d{4}-\d{2}-\d{2})\*{0,2}\s*(?:\|[^—–]*?)?\s*(?:—|–|--|-|:)\s+(.+?)\s*$"
)


def split_timeline(body: str) -> tuple[str, list[tuple[str, str]], int]:
    """(compiled truth, [(date, summary)], unparsed line count)."""
    top, entries, unparsed = [], [], 0
    for heading, text in sections(body):
        if heading.strip().lower() in ("timeline", "history"):
            for line in text.splitlines():
                if not line.strip():
                    continue
                m = TIMELINE_LINE_RE.match(line)
                if not m:
                    unparsed += 1
                    continue
                try:
                    date.fromisoformat(m.group(1))
                except ValueError:
                    unparsed += 1
                    continue
                entries.append((m.group(1), m.group(2).strip()))
        else:
            top.append(f"## {heading}\n{text}" if heading else text)
    return "\n\n".join(t for t in top if t.strip()).strip(), entries, unparsed


def timeline_line(day: str, summary: str) -> str:
    return f"- **{day}** — {summary.strip()}"


def timeline_rows(conn, slug: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT date, summary, message_ids, written_by, created_at FROM tl.page_timeline WHERE slug=%s ORDER BY date DESC, id DESC",
        (slug,))]


def render(top: str, rows: list[dict[str, Any]]) -> str:
    body = top.strip()
    if rows:
        body += ("\n\n" if body else "") + f"## {TIMELINE_HEADING}\n" + "\n".join(
            timeline_line(r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]), r["summary"]) for r in rows)
    return body


def add_timeline(conn, slug: str, entries: list[tuple[str, str]], by: str) -> int:
    """Insert timeline rows (exact duplicates collapse). Returns rows added."""
    added = 0
    for day, summary in entries:
        summary = summary.strip()[:2000]
        if not summary:
            continue
        row = conn.execute(
            """INSERT INTO tl.page_timeline(slug, date, summary, message_ids, written_by) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (slug, date, md5(summary)) DO NOTHING RETURNING id""",
            (slug, day, summary, parse_citations(summary), by),
        ).fetchone()
        added += 1 if row else 0
    return added


def chronicle(conn, *, on: str | None = None, since: str | None = None, until: str | None = None,
              slug: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Timeline rows across pages (GBrain's chronicle day views)."""
    rows = conn.execute(
        """SELECT t.date, t.slug, p.title, p.kind, t.summary, t.message_ids FROM tl.page_timeline t
           JOIN tl.pages p ON p.slug=t.slug AND p.status='active'
        WHERE (%(on)s::date IS NULL OR t.date=%(on)s::date) AND (%(since)s::date IS NULL OR t.date >= %(since)s::date)
          AND (%(until)s::date IS NULL OR t.date <= %(until)s::date) AND (%(slug)s::text IS NULL OR t.slug=%(slug)s)
        ORDER BY t.date DESC, t.id DESC LIMIT %(lim)s""",
        {"on": on, "since": since, "until": until, "slug": slug, "lim": max(1, min(int(limit), 500))},
    ).fetchall()
    return [dict(r) for r in rows]


def ensure_kind(conn, kind: str, origin: str = "lens") -> str:
    """The kind's status; a kind nobody declared is recorded as a candidate."""
    row = conn.execute("SELECT status FROM tl.page_kinds WHERE kind=%s", (kind,)).fetchone()
    if row:
        if row["status"] == "retired":
            raise PageError(f"page kind {kind!r} is retired")
        return row["status"]
    conn.execute(
        "INSERT INTO tl.page_kinds(kind, status, origin) VALUES (%s, 'candidate', %s) ON CONFLICT DO NOTHING",
        (kind, "owner" if origin == "owner" else "lens"),
    )
    return "candidate"


def kinds(conn) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT kind, description, sections, guidance, half_life_days, status FROM tl.page_kinds WHERE status<>'retired' ORDER BY kind")]


def canonical(conn, slug: str) -> str:
    """Follow merge redirects (at most a few hops)."""
    seen = set()
    while slug not in seen:
        seen.add(slug)
        row = conn.execute("SELECT slug FROM tl.page_redirects WHERE old_slug=%s", (slug,)).fetchone()
        if not row:
            break
        slug = row["slug"]
    return slug


def search(conn, query: str | None, *, kind: str | None = None, limit: int = 10, include_archived: bool = False) -> list[dict[str, Any]]:
    """Pages by title, alias, summary or text. Empty query: most recently updated."""
    limit = max(1, min(int(limit), 50))
    q = (query or "").strip()
    params: dict[str, Any] = {"kind": kind, "lim": limit, "arch": include_archived}
    base = "(%(kind)s::text IS NULL OR p.kind=%(kind)s) AND (%(arch)s OR p.status='active')"
    if not q:
        sql = f"SELECT p.slug, p.kind, p.title, p.aliases, p.summary, p.updated_at, 0.0 AS score FROM tl.pages p WHERE {base} ORDER BY p.updated_at DESC LIMIT %(lim)s"
    else:
        params.update(q=q, ql=q.lower(), like=f"%{q.lower()}%")
        sql = f"""SELECT p.slug, p.kind, p.title, p.aliases, p.summary, p.updated_at,
              (CASE WHEN lower(p.title)=%(ql)s OR %(ql)s = ANY(SELECT lower(a) FROM unnest(p.aliases) a) THEN 3.0 ELSE 0 END
               + CASE WHEN lower(p.title) LIKE %(like)s OR p.slug LIKE %(like)s THEN 1.5 ELSE 0 END
               + CASE WHEN EXISTS (SELECT 1 FROM unnest(p.aliases) a WHERE lower(a) LIKE %(like)s) THEN 1.0 ELSE 0 END
               + coalesce(ts_rank_cd(m.tsv, websearch_to_tsquery('english', %(q)s)), 0) * 2) AS score
            FROM tl.pages p LEFT JOIN tl.messages m ON m.id=p.message_id
            WHERE {base} AND (lower(p.title) LIKE %(like)s OR p.slug LIKE %(like)s
                   OR EXISTS (SELECT 1 FROM unnest(p.aliases) a WHERE lower(a) LIKE %(like)s)
                   OR lower(coalesce(p.summary,'')) LIKE %(like)s
                   OR m.tsv @@ websearch_to_tsquery('english', %(q)s))
            ORDER BY score DESC, p.updated_at DESC LIMIT %(lim)s"""
    return [dict(r) for r in conn.execute(sql, params)]


def neighbors(conn, slug: str, limit: int = 40) -> dict[str, list[dict[str, Any]]]:
    """Pages and people linked from and to a page (current links)."""
    page = conn.execute("SELECT message_id FROM tl.pages WHERE slug=%s", (slug,)).fetchone()
    if not page or not page["message_id"]:
        return {"out": [], "in": []}
    mid = page["message_id"]
    out = conn.execute(
        """SELECT l.type, l.method, l.job, l.to_kind, l.to_id, l.evidence, q.slug, q.title, q.kind,
               coalesce(pp.display_name, pp.handle) AS person
        FROM tl.links l
        LEFT JOIN tl.pages q ON l.to_kind='message' AND q.message_id=l.to_id
        LEFT JOIN tl.people pp ON l.to_kind='person' AND pp.id=l.to_id
        WHERE l.status='active' AND l.from_kind='message' AND l.from_id=%s
          AND (q.slug IS NOT NULL OR l.to_kind='person')
        ORDER BY l.id LIMIT %s""",
        (mid, limit),
    ).fetchall()
    inn = conn.execute(
        """SELECT l.type, l.method, l.job, q.slug, q.title, q.kind
        FROM tl.links l JOIN tl.pages q ON l.from_kind='message' AND q.message_id=l.from_id
        WHERE l.status='active' AND l.to_kind='message' AND l.to_id=%s
        ORDER BY l.id LIMIT %s""",
        (mid, limit),
    ).fetchall()
    return {"out": [dict(r) for r in out], "in": [dict(r) for r in inn]}


# --- writing ------------------------------------------------------------------


def _subject(when: Any) -> dict[str, Any] | None:
    """{start, end} dates (YYYY-MM-DD or ISO) as a record subject in the owner zone."""
    if not isinstance(when, dict) or not when.get("start"):
        return None
    from ..agent_view import owner_zone

    zone = owner_zone()

    def parse(v: Any, end: bool) -> tuple[datetime, str]:
        s = str(v).strip()
        if len(s) == 10:
            d = date.fromisoformat(s) + (timedelta(days=1) if end else timedelta())
            return datetime.combine(d, time(0), tzinfo=zone).astimezone(timezone.utc), "day"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=zone)).astimezone(timezone.utc), "exact"

    try:
        start, sp = parse(when["start"], False)
        end, ep = parse(when.get("end") or when["start"], True)
    except ValueError as exc:
        raise PageError(f"when: {exc}") from None
    if end < start:
        raise PageError("when.end is before when.start")
    return {"start": start.isoformat(), "end": end.isoformat(), "start_precision": sp, "end_precision": ep}


def write(
    conn,
    *,
    slug: str,
    kind: str,
    title: str,
    body: str,
    by: str,
    summary: str = "",
    aliases: list[str] | None = None,
    person_id: int | None = None,
    when: dict[str, Any] | None = None,
    reason: str = "",
    now: datetime | None = None,
    tags: list[str] | None = None,
    source_ref: str | None = None,
    meta: dict[str, Any] | None = None,
    status: str = "active",
) -> dict[str, Any]:
    """Store a revision of the page (create or replace). The body's
    '## Timeline' lines are added to the page's timeline rows and the stored
    body renders all rows (append-only). Returns {slug, outcome:
    created|revised|unchanged, message_id, version, citations, links,
    timeline_added, truth_changed}."""
    slug = check_slug(slug)
    kind = (kind or slug.split("/")[0]).strip().lower()
    if not KIND_RE.fullmatch(kind):
        raise PageError(f"invalid kind {kind!r}")
    title = (title or "").strip()[:300]
    if not title:
        raise PageError("title is required")
    body = (body or "").strip()
    if not body:
        raise PageError("body is empty")
    aliases = list(dict.fromkeys(a.strip() for a in aliases or [] if isinstance(a, str) and a.strip()))[:20]
    if person_id is not None and not conn.execute("SELECT 1 FROM tl.people WHERE id=%s", (person_id,)).fetchone():
        raise PageError(f"person {person_id} does not exist")
    now = now or datetime.now(timezone.utc)
    ensure_channel(conn)
    ensure_kind(conn, kind, "owner" if by == "owner" else "lens")
    existing = conn.execute("SELECT * FROM tl.pages WHERE slug=%s FOR UPDATE", (slug,)).fetchone()
    top, entries, unparsed = split_timeline(body)
    tags = list(dict.fromkeys(t.strip() for t in tags or [] if isinstance(t, str) and t.strip()))[:40] if tags is not None else None
    if not existing:
        conn.execute(
            """INSERT INTO tl.pages(slug, kind, title, aliases, summary, person_id, version, created_by, updated_by, tags, source_ref)
               VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s)""",
            (slug, kind, title, aliases, summary or "", person_id, by, by, tags or [], source_ref),
        )
    elif tags is not None or source_ref:
        conn.execute("UPDATE tl.pages SET tags=coalesce(%s, tags), source_ref=coalesce(%s, source_ref) WHERE slug=%s",
                     (tags, source_ref, slug))
    timeline_added = add_timeline(conn, slug, entries, by)
    rows = timeline_rows(conn, slug)
    body = render(top, rows)
    if not body:
        raise PageError("body is empty")
    prior = get(conn, slug) if existing and existing["message_id"] else None
    truth_changed = prior is None or prior["truth"].strip() != top.strip()
    if prior and prior["body"].strip() == body and prior["title"] == title and (prior["aliases"] or []) == aliases \
            and (prior["summary"] or "") == (summary or prior["summary"] or "") and (person_id is None or prior["person_id"] == person_id) \
            and prior["status"] == status:
        return {"slug": slug, "outcome": "unchanged", "message_id": prior["message_id"], "version": prior["version"],
                "citations": len(prior["citations"]), "links": {}, "timeline_added": 0, "truth_changed": False}
    citations = parse_citations(body)
    message: dict[str, Any] = {
        "channel": CHANNEL,
        "external_id": slug,
        # One thread per page: pages have no conversations to group, and a
        # shared thread would regroup every page of a kind on each write.
        "thread": {"external_id": slug, "title": title},
        "happened_at": now.isoformat(),
        "content": f"# {title}\n\n{body}\n",
        "fields": {
            "page_kind": kind, "title": title, "summary": (summary or "")[:500], "aliases": aliases,
            "citations": len(citations), "written_by": by,
            **({"about_person": person_id} if person_id else {}),
            **({"reason": reason[:300]} if reason else {}),
        },
        "payload": {"slug": slug, "kind": kind, "title": title, "aliases": aliases, "summary": summary or "",
                    "body": body, "person_id": person_id, **({"source": meta} if meta else {})},
    }
    subj = _subject(when)
    if subj:
        message["subject"] = subj
        message["payload"]["when"] = when
    result = ingest(conn, Ingest(plugin="native", messages=[message]))
    if result["rejected"]:
        raise PageError(f"revision rejected: {result['rejected'][0]}")
    row = conn.execute(
        """SELECT m.id, m.version FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id
        WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL""",
        (CHANNEL, slug),
    ).fetchone()
    changed = bool(result["inserted"] or result["new_versions"])
    conn.execute(
        """UPDATE tl.pages SET kind=%s, title=%s, aliases=%s, summary=%s,
             person_id=coalesce(%s, person_id), message_id=%s, version=%s, status=%s,
             updated_by=%s, updated_at=CASE WHEN %s THEN clock_timestamp() ELSE updated_at END,
             truth_updated_at=CASE WHEN %s THEN clock_timestamp() ELSE truth_updated_at END
           WHERE slug=%s""",
        (kind, title, aliases, summary or (existing or {}).get("summary") or "", person_id, row["id"], row["version"], status, by,
         changed, changed and truth_changed, slug),
    )
    links: dict[str, Any] = {}
    if changed:
        if citations:
            alive = [r["id"] for r in conn.execute("SELECT id FROM tl.messages WHERE id = ANY(%s)", (citations[:MAX_SOURCES],))]
            if alive:
                add_provenance(conn, BODY_JOB, "message", row["id"], alive, relation="cites", origin="job", job=BODY_JOB, job_version=by[:60])
        links = reconcile_body_links(conn, slug, row["id"], body, person_id, by)
        conn.execute(
            "UPDATE tl.pages SET stats = stats || %s WHERE slug=%s",
            (Jsonb({"citations": len(citations), "chars": len(body), "truth_chars": len(top), "body_links": links.get("links", 0),
                    "timeline": len(rows), "timeline_unparsed": unparsed, "unresolved": links.get("unresolved", [])[:30]}), slug),
        )
    if not existing:
        resolve_waiting(conn, slug)
    outcome = "unchanged" if not changed else ("revised" if existing else "created")
    return {"slug": slug, "outcome": outcome, "message_id": row["id"], "version": row["version"],
            "citations": len(citations), "links": links, "timeline_added": timeline_added,
            "truth_changed": bool(changed and truth_changed)}


def resolve_ref(conn, ref: str) -> tuple[str, int] | None:
    """'person:ID' -> ('person', id); a page slug -> ('message', current revision id)."""
    ref = (ref or "").strip()
    if ref.startswith("person:"):
        try:
            pid = int(ref.split(":", 1)[1])
        except ValueError:
            return None
        row = conn.execute("SELECT id, merged_into FROM tl.people WHERE id=%s", (pid,)).fetchone()
        if not row:
            return None
        return ("person", row["merged_into"] or row["id"])
    row = conn.execute("SELECT message_id FROM tl.pages WHERE slug=%s AND status='active'", (canonical(conn, ref.strip("/").lower()),)).fetchone()
    if not row or not row["message_id"]:
        return None
    return ("message", row["message_id"])


def reconcile_body_links(conn, slug: str, mid: int, body: str, person_id: int | None, by: str) -> dict[str, Any]:
    """Make the page's body links ('pages' job) match its text."""
    want: dict[tuple[str, str, int], str] = {}
    unresolved: list[str] = []
    for target, _label in parse_wikilinks(body):
        if target.strip().lower() == slug:
            continue
        ref = resolve_ref(conn, target)
        if not ref:
            unresolved.append(target)
            continue
        kind, ident = ref
        ltype = "about" if (kind == "person" and ident == person_id) else "mentions"
        want.setdefault((ltype, kind, ident), _context(body, target))
    if person_id:
        want.setdefault(("about", "person", person_id), "")
    have = conn.execute(
        "SELECT id, type, to_kind, to_id FROM tl.links WHERE from_kind='message' AND from_id=%s AND status='active' AND job=%s",
        (mid, BODY_JOB),
    ).fetchall()
    seen, removed, added = set(), 0, 0
    for r in have:
        key = (r["type"], r["to_kind"], r["to_id"])
        if key in want and key not in seen:
            seen.add(key)
        else:
            conn.execute("UPDATE tl.links SET status='removed' WHERE id=%s", (r["id"],))
            removed += 1
    for key in want:
        if key in seen:
            continue
        with conn.transaction():
            add_link(conn, {"type": key[0], "from": {"kind": "message", "id": mid},
                            "to": {"kind": key[1], "id": key[2]}, "method": "rule",
                            "evidence": {"context": want[key]} if want[key] else {},
                            "job": BODY_JOB, "job_version": by[:60]})
        added += 1
    return {"links": len(want), "added": added, "removed": removed, "unresolved": unresolved}


def _context(body: str, target: str, width: int = 120) -> str:
    """The text around a [[link]] (GBrain keeps a context snippet per link)."""
    i = body.find("[[" + target)
    if i < 0:
        return ""
    return " ".join(body[max(0, i - width): i + len(target) + width].split())[:2 * width + 40]


def resolve_waiting(conn, slug: str) -> int:
    """A new page resolves the [[slug]] links other pages already made to it."""
    n = 0
    for r in conn.execute(
        "SELECT slug, message_id, person_id, updated_by FROM tl.pages WHERE stats->'unresolved' ? %s AND message_id IS NOT NULL",
        (slug,),
    ).fetchall():
        page = get(conn, r["slug"])
        res = reconcile_body_links(conn, r["slug"], r["message_id"], page["body"], r["person_id"], r["updated_by"])
        conn.execute("UPDATE tl.pages SET stats = stats || %s WHERE slug=%s",
                     (Jsonb({"body_links": res["links"], "unresolved": res["unresolved"][:30]}), r["slug"]))
        n += 1
    return n


def link_type(conn, name: str) -> str:
    row = conn.execute(
        "SELECT t.type, t.status FROM tl.link_types t WHERE t.type = coalesce(tl.resolve_link_type(%s), tl.normalize_link_type(%s))",
        (name, name),
    ).fetchone()
    if not row or row["status"] != "active":
        raise PageError(f"link type {name!r} is not an active registered type (see GET /v1/link-types)")
    return row["type"]


def link(conn, *, src: str, dst: str, type: str, evidence: list[int], by: str, note: str | None = None) -> dict[str, Any]:
    """Assert a link between two pages (or a page and a person), with evidence."""
    a, b = resolve_ref(conn, src), resolve_ref(conn, dst)
    if a is None or b is None:
        raise PageError(f"unknown link end: {src if a is None else dst}")
    if a[0] != "message" and b[0] != "message":
        raise PageError("a link needs at least one page end")
    if a == b:
        raise PageError("a page cannot link to itself")
    if not evidence:
        raise PageError("a link needs evidence: the message ids that support it")
    ltype = link_type(conn, type or "related_to")
    ev = {"message_ids": list(dict.fromkeys(int(i) for i in evidence))[:50]}
    if note:
        ev["note"] = str(note)[:300]
    existing = conn.execute(
        """SELECT id FROM tl.links WHERE status='active' AND type=%s AND from_kind=%s AND from_id=%s
           AND to_kind=%s AND to_id=%s AND job=%s""",
        (ltype, a[0], a[1], b[0], b[1], LINK_JOB),
    ).fetchone()
    if existing:
        return {"outcome": "unchanged", "link_id": existing["id"], "type": ltype}
    with conn.transaction():
        row = add_link(conn, {"type": ltype, "from": {"kind": a[0], "id": a[1]}, "to": {"kind": b[0], "id": b[1]},
                              "method": "model", "evidence": ev, "job": LINK_JOB, "job_version": by[:60]})
    return {"outcome": "created", "link_id": row["id"], "type": ltype}


def unlink(conn, *, src: str, dst: str, type: str | None, by: str) -> dict[str, Any]:
    a, b = resolve_ref(conn, src), resolve_ref(conn, dst)
    if a is None or b is None:
        raise PageError(f"unknown link end: {src if a is None else dst}")
    params: list[Any] = [a[0], a[1], b[0], b[1], LINK_JOB]
    extra = ""
    if type:
        extra = " AND type=%s"
        params.append(link_type(conn, type))
    rows = conn.execute(
        f"""UPDATE tl.links SET status='removed' WHERE status='active' AND from_kind=%s AND from_id=%s
            AND to_kind=%s AND to_id=%s AND job=%s{extra} RETURNING id""",
        params,
    ).fetchall()
    return {"outcome": "removed" if rows else "unchanged", "removed": len(rows)}


def archive(conn, slug: str, *, by: str, reason: str = "") -> dict[str, Any]:
    row = conn.execute(
        "UPDATE tl.pages SET status='archived', updated_by=%s, updated_at=clock_timestamp(), stats = stats || %s WHERE slug=%s AND status='active' RETURNING slug",
        (by, Jsonb({"archived_reason": reason[:300]}), slug),
    ).fetchone()
    return {"outcome": "archived" if row else "unchanged"}


def merge(conn, *, src: str, into: str, by: str, reason: str = "") -> dict[str, Any]:
    """Merge page `src` into `into` (GBrain: merge + slug alias). The target
    keeps its text and gains src's aliases (and title as an alias) and its
    timeline; links to and from src move to the target; src redirects to the
    target and is archived."""
    src, into = canonical(conn, check_slug(src)), canonical(conn, check_slug(into))
    if src == into:
        raise PageError("a page cannot be merged into itself")
    a, b = get(conn, src), get(conn, into)
    if not a or not b or a["status"] != "active" or b["status"] != "active":
        raise PageError("both pages must exist and be active")
    moved = conn.execute(
        """INSERT INTO tl.page_timeline(slug, date, summary, message_ids, written_by)
           SELECT %s, date, summary, message_ids, written_by FROM tl.page_timeline WHERE slug=%s
           ON CONFLICT (slug, date, md5(summary)) DO NOTHING RETURNING id""",
        (into, src),
    ).fetchall()
    aliases = list(dict.fromkeys([*(b["aliases"] or []), a["title"], *(a["aliases"] or [])]))
    aliases = [x for x in aliases if x and x.lower() != b["title"].lower()][:20]
    res = write(conn, slug=into, kind=b["kind"], title=b["title"], body=b["body"], by=by, summary=b["summary"] or "",
                aliases=aliases, person_id=b["person_id"] or a["person_id"], reason=reason or f"merged {src}")
    new_mid = res["message_id"]
    for side in ("from", "to"):
        conn.execute(
            f"UPDATE tl.links SET {side}_id=%s WHERE {side}_kind='message' AND {side}_id=%s AND status='active'",
            (new_mid, a["message_id"]),
        )
    conn.execute(
        "DELETE FROM tl.links WHERE status='active' AND from_kind='message' AND to_kind='message' AND from_id=%s AND to_id=%s",
        (new_mid, new_mid),
    )
    conn.execute(
        """INSERT INTO tl.page_redirects(old_slug, slug, reason) VALUES (%s,%s,%s)
           ON CONFLICT (old_slug) DO UPDATE SET slug=EXCLUDED.slug, reason=EXCLUDED.reason""",
        (src, into, reason[:300] or None),
    )
    conn.execute("UPDATE tl.page_redirects SET slug=%s WHERE slug=%s", (into, src))
    archive(conn, src, by=by, reason=f"merged into {into}" + (f": {reason}" if reason else ""))
    return {"outcome": "merged", "slug": into, "from": src, "timeline_moved": len(moved), "message_id": new_mid}
