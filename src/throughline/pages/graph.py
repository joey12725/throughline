"""Graph-aware page results for search (POST /v1/find).

Messages stay the answer: find still returns its ranked episodes of primary
records with full thread context. Pages are added beside them as a map:

1. seeds: page records the search itself ranked (any page_revision channel:
   pages, gbrain.pages, notion.pages), and pages whose title or alias matches
   the query;
2. graph: each seed brings its linked pages (both directions) and people;
   a page's weight grows with how many other pages link to it (a small,
   logarithmic centrality bonus, as in a wiki), and neighbours inherit a share
   of their seed's weight;
3. ground truth: every page carries its most recent cited records (message id,
   time, channel, preview), so a reader goes from the page to the evidence
   with api.context.

Only for unrestricted askers (like matched_people); restricted askers keep
plain search results.
"""

from __future__ import annotations

import math
from typing import Any

PAGE_KIND = "page_revision"


def _page_rows(conn, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    rows = conn.execute(
        """SELECT m.id, c.name AS channel, m.external_id, m.happened_at,
               coalesce(p.title, m.fields->>'title', split_part(m.content, E'\\n', 1)) AS title,
               coalesce(p.kind, m.fields->>'page_kind', m.fields->>'page_type') AS kind,
               coalesce(p.summary, m.fields->>'summary') AS summary,
               (SELECT k.half_life_days FROM tl.page_kinds k WHERE k.kind=p.kind) AS half_life,
               coalesce(p.truth_updated_at, m.happened_at) AS fresh_at,
               (SELECT count(*) FROM tl.links l JOIN tl.messages s ON s.id=l.from_id AND s.superseded_by IS NULL
                  JOIN tl.channels sc ON sc.id=s.channel_id AND sc.record_kind='page_revision'
                WHERE l.status='active' AND l.from_kind='message' AND l.to_kind='message' AND l.to_id=m.id) AS indegree
        FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id
        LEFT JOIN tl.pages p ON c.name='pages' AND p.slug=m.external_id
        WHERE m.id = ANY(%s) AND m.superseded_by IS NULL AND c.record_kind='page_revision'
          AND coalesce(p.status, 'active')='active'""",
        (ids,),
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _edges(conn, ids: list[int], per_page: int = 8) -> dict[int, list[dict[str, Any]]]:
    """Active links between current page revisions and to people, both directions."""
    if not ids:
        return {}
    rows = conn.execute(
        """SELECT * FROM (
            SELECT l.from_id AS page, l.type, 'out' AS dir, l.to_kind AS kind, l.to_id AS other,
                   row_number() OVER (PARTITION BY l.from_id ORDER BY l.id DESC) AS rn
            FROM tl.links l WHERE l.status='active' AND l.from_kind='message' AND l.from_id = ANY(%(ids)s)
              AND l.to_kind IN ('message','person')
            UNION ALL
            SELECT l.to_id, l.type, 'in', 'message', l.from_id,
                   row_number() OVER (PARTITION BY l.to_id ORDER BY l.id DESC)
            FROM tl.links l WHERE l.status='active' AND l.to_kind='message' AND l.to_id = ANY(%(ids)s)
              AND l.from_kind='message') e
        WHERE rn <= %(n)s""",
        {"ids": ids, "n": per_page * 2},
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["page"], []).append(dict(r))
    return out


def _evidence(conn, ids: list[int], per_page: int = 3) -> dict[int, list[dict[str, Any]]]:
    if not ids:
        return {}
    rows = conn.execute(
        """SELECT * FROM (
            SELECT p.derived_id AS page, m.id AS message_id, c.name AS channel, m.happened_at,
                   left(regexp_replace(m.content, '\\s+', ' ', 'g'), 140) AS preview,
                   row_number() OVER (PARTITION BY p.derived_id ORDER BY m.happened_at DESC) AS rn
            FROM tl.provenance p JOIN tl.messages m ON m.id=p.source_message_id
            JOIN tl.channels c ON c.id=m.channel_id
            WHERE p.derived_kind='message' AND p.derived_id = ANY(%s) AND c.record_kind <> 'page_revision') x
        WHERE rn <= %s""",
        (ids, per_page),
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        d = dict(r)
        d.pop("page")
        d.pop("rn")
        out.setdefault(r["page"], []).append(d)
    return out


def related_pages(conn, query: str, records: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    """The page map for a search: see the module docstring."""
    from ..agent_view import local_time

    seeds: dict[int, tuple[float, str]] = {}
    hits = [r for r in records if r.get("kind") == PAGE_KIND]
    # A record of a page store that moved into Pages (GBrain) stands for its
    # native page (tl.pages.source_ref), so the map never shows both.
    refs = [f"{r.get('channel')}:{r.get('external_id')}" for r in hits if not str(r.get("channel")).startswith("pages")]
    moved = {r["source_ref"]: r["message_id"] for r in conn.execute(
        "SELECT source_ref, message_id FROM tl.pages WHERE source_ref = ANY(%s) AND status='active' AND message_id IS NOT NULL",
        (refs,))} if refs else {}
    rank = 0
    for rec in hits:
        mid = moved.get(f"{rec.get('channel')}:{rec.get('external_id')}", rec.get("message_id"))
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            continue
        rank += 1
        seeds.setdefault(mid, (1.0 / (1 + 0.5 * rank), "search hit"))
    q = (query or "").strip()
    if q:
        low = q.lower()
        rows = conn.execute(
            """SELECT p.message_id AS id FROM tl.pages p
            WHERE p.status='active' AND p.message_id IS NOT NULL
              AND (strpos(%(q)s, lower(p.title)) > 0 AND length(p.title) >= 4
                   OR EXISTS (SELECT 1 FROM unnest(p.aliases) a WHERE length(a) >= 4 AND strpos(%(q)s, lower(a)) > 0))
            LIMIT 10""",
            {"q": low},
        ).fetchall()
        for r in rows:
            seeds[r["id"]] = (max(seeds.get(r["id"], (0.0, ""))[0], 1.2), "title match")
    if not seeds:
        return []
    edges = _edges(conn, list(seeds))
    neighbour_ids = {e["other"] for es in edges.values() for e in es if e["kind"] == "message"}
    pages = _page_rows(conn, list(set(seeds) | neighbour_ids))
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    def recency(p: dict[str, Any]) -> float:
        # GBrain's per-kind half-life (days pages fade, people and ideas do
        # not), kept mild: a stale page drops at most 30%.
        hl = p.get("half_life")
        if not hl or not p.get("fresh_at"):
            return 1.0
        age = max(0.0, (now - p["fresh_at"]).total_seconds() / 86400.0)
        return 0.7 + 0.3 * (0.5 ** (age / hl))

    scores: dict[int, tuple[float, str]] = {}
    for mid, (s, why) in seeds.items():
        if mid in pages:
            scores[mid] = ((s + 0.15 * math.log1p(pages[mid]["indegree"])) * recency(pages[mid]), why)
    for mid, (s, _why) in list(scores.items()):
        for e in edges.get(mid, []):
            o = e["other"]
            if e["kind"] != "message" or o not in pages or o in seeds:
                continue
            inherited = (0.45 * s + 0.1 * math.log1p(pages[o]["indegree"])) * recency(pages[o])
            if inherited > scores.get(o, (0.0, ""))[0]:
                scores[o] = (inherited, f"linked {'from' if e['dir'] == 'in' else 'to'} {pages[mid]['title']}")
    top = sorted(scores.items(), key=lambda kv: -kv[1][0])[:limit]
    ids = [mid for mid, _ in top]
    ev = _evidence(conn, ids)
    person_ids = {e["other"] for mid in ids for e in edges.get(mid, []) if e["kind"] == "person"}
    names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, coalesce(display_name, handle) AS name FROM tl.people WHERE id = ANY(%s)", (list(person_ids),))} if person_ids else {}
    lines: dict[str, list[str]] = {}
    slugs = [pages[mid]["external_id"] for mid in ids if pages[mid]["channel"] == "pages"]
    if slugs:
        for r in conn.execute(
            """SELECT slug, date, summary FROM (SELECT slug, date, summary,
                 row_number() OVER (PARTITION BY slug ORDER BY date DESC, id DESC) AS rn
               FROM tl.page_timeline WHERE slug = ANY(%s)) x WHERE rn <= 3""",
            (slugs,),
        ):
            lines.setdefault(r["slug"], []).append(f"{r['date']} — {r['summary'][:200]}")
    out = []
    for mid, (score, why) in top:
        p = pages[mid]
        es = edges.get(mid, [])
        links = [{"title": pages[e["other"]]["title"], "ref": pages[e["other"]]["external_id"], "type": e["type"], "dir": e["dir"]}
                 for e in es if e["kind"] == "message" and e["other"] in pages][:6]
        people = sorted({names[e["other"]] for e in es if e["kind"] == "person" and e["other"] in names})[:8]
        out.append({
            "channel": p["channel"], "ref": p["external_id"], "message_id": mid, "title": p["title"],
            "kind": p["kind"], "summary": (p["summary"] or "")[:300], "revised": local_time(p["happened_at"]),
            "why": why, "score": round(score, 3), "linked_pages": links, "people": people,
            "evidence": [{**e, "happened_at": local_time(e["happened_at"])} for e in ev.get(mid, [])],
            **({"recent": lines[p["external_id"]]} if p["external_id"] in lines and p["channel"] == "pages" else {}),
            "generated": True,
        })
    return out


def related_pages_for_scope(scope, query: str, records: list[dict[str, Any]], matched_people: list[dict[str, Any]], *, limit: int = 5):
    """Unrestricted-only page map plus person-page lookup for POST /v1/find."""
    if scope.asker is not None or not limit:
        return [], {}
    from ..db import connect

    try:
        with connect("app") as conn:
            pages = related_pages(conn, query, records, limit=limit)
            person_pages = {}
            ids = [p["id"] for p in matched_people]
            if ids:
                for r in conn.execute(
                    "SELECT DISTINCT ON (person_id) person_id, slug FROM tl.pages WHERE status='active' AND person_id = ANY(%s) ORDER BY person_id, updated_at DESC",
                    (ids,),
                ):
                    person_pages[r["person_id"]] = r["slug"]
            return pages, person_pages
    except Exception:
        return [], {}
