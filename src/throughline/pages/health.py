"""Health of the page graph, of pages lenses, and of other page stores.

- graph: pages by kind, orphans (no link to or from another page), pages
  citing nothing, unresolved [[links]], average page degree, connected
  components, stale pages (a cited conversation has newer records than the
  page), people with a lot of recent activity and no page about them.
- lenses: per pages lens, its last runs (from tl.table_runs) and its actions
  in the last 7 days (applied, unchanged, refused, with the refusal reasons).
- stores: any other page_revision channel (gbrain.pages, notion.pages):
  pages, revised in the last 7 / 30 days, untouched for 90 days, no links,
  no sources, oversized.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import connect


def _verdict(problems: list[str], warnings: list[str]) -> str:
    return "bad" if problems else "warn" if warnings else "ok"


def graph_health(conn, now: datetime | None = None, *, gap_messages: int = 40, gap_days: int = 60) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    pages = [dict(r) for r in conn.execute(
        "SELECT slug, kind, message_id, person_id, updated_at, stats FROM tl.pages WHERE status='active' AND message_id IS NOT NULL")]
    by_mid = {p["message_id"]: p for p in pages}
    mids = list(by_mid)
    edges = conn.execute(
        """SELECT from_id, to_id FROM tl.links WHERE status='active' AND from_kind='message' AND to_kind='message'
           AND from_id = ANY(%(m)s) AND to_id = ANY(%(m)s)""",
        {"m": mids},
    ).fetchall() if mids else []
    adj: dict[int, set[int]] = {m: set() for m in mids}
    for e in edges:
        adj[e["from_id"]].add(e["to_id"])
        adj[e["to_id"]].add(e["from_id"])
    orphans = [by_mid[m]["slug"] for m, ns in adj.items() if not ns]
    seen: set[int] = set()
    components = 0
    for m in mids:
        if m in seen:
            continue
        components += 1
        stack = [m]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(adj[x] - seen)
    uncited = [p["slug"] for p in pages if not int((p["stats"] or {}).get("citations") or 0)]
    unresolved = sum(len((p["stats"] or {}).get("unresolved") or []) for p in pages)
    stale = conn.execute(
        """SELECT p.slug, count(DISTINCT n.id) AS newer FROM tl.pages p
        JOIN tl.provenance pv ON pv.derived_kind='message' AND pv.derived_id=p.message_id
        JOIN tl.messages s ON s.id=pv.source_message_id
        JOIN tl.messages n ON n.thread_id=s.thread_id AND n.superseded_by IS NULL AND n.id > p.message_id
        WHERE p.status='active' AND s.thread_id IS NOT NULL
        GROUP BY p.slug ORDER BY 2 DESC LIMIT 15"""
    ).fetchall() if mids else []
    gaps = conn.execute(
        """SELECT m.person_id, coalesce(pp.display_name, pp.handle) AS name, count(*) AS messages
        FROM tl.messages m JOIN tl.people pp ON pp.id=m.person_id AND NOT pp.is_owner AND pp.merged_into IS NULL
        WHERE m.superseded_by IS NULL AND m.happened_at >= %s
          AND NOT EXISTS (SELECT 1 FROM tl.pages q WHERE q.person_id=m.person_id AND q.status='active')
        GROUP BY 1, 2 HAVING count(*) >= %s ORDER BY 3 DESC LIMIT 15""",
        (now - timedelta(days=gap_days), gap_messages),
    ).fetchall()
    flags = page_flags(conn)
    stale_truth = sorted((s for s, f in flags.items() if f.get("stale_truth")), key=lambda s: -flags[s]["stale_truth"])
    thin = [s for s, f in flags.items() if "thin" in f]
    wanted = conn.execute(
        """SELECT u AS slug, count(*) AS refs FROM tl.pages p, jsonb_array_elements_text(p.stats->'unresolved') u
        WHERE p.status='active' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 15"""
    ).fetchall()
    candidate_kinds = conn.execute(
        """SELECT k.kind, count(p.slug) AS pages FROM tl.page_kinds k LEFT JOIN tl.pages p ON p.kind=k.kind AND p.status='active'
        WHERE k.status='candidate' GROUP BY 1 ORDER BY 2 DESC"""
    ).fetchall()
    timeline_lines = conn.execute("SELECT count(*) AS n FROM tl.page_timeline").fetchone()["n"]
    degree = [len(ns) for ns in adj.values()]
    problems, warnings = [], []
    if not pages:
        warnings.append("no pages yet")
    else:
        if len(orphans) / len(pages) > 0.3:
            warnings.append(f"{len(orphans)} of {len(pages)} pages link to no other page")
        if uncited:
            warnings.append(f"{len(uncited)} pages cite nothing")
        if components > max(3, len(pages) // 5):
            warnings.append(f"the graph is split into {components} components")
    if gaps:
        warnings.append(f"{len(gaps)} active people have no page")
    if stale_truth:
        warnings.append(f"{len(stale_truth)} pages have timeline lines newer than their compiled truth")
    if candidate_kinds:
        warnings.append(f"{len(candidate_kinds)} page kinds are undeclared candidates (lib pages kinds)")
    return {
        "verdict": _verdict(problems, warnings), "problems": problems, "warnings": warnings,
        "pages": len(pages), "by_kind": dict(Counter(p["kind"] for p in pages).most_common()),
        "links_between_pages": len(edges), "avg_degree": round(sum(degree) / len(degree), 2) if degree else 0.0,
        "components": components, "orphans": orphans[:30], "orphan_count": len(orphans),
        "uncited": uncited[:30], "unresolved_links": unresolved,
        "stale": [dict(r) for r in stale], "people_without_page": [dict(r) for r in gaps],
        "stale_truth": stale_truth[:30], "thin": thin[:30], "wanted_pages": [dict(r) for r in wanted],
        "candidate_kinds": [dict(r) for r in candidate_kinds], "timeline_lines": timeline_lines,
        "flagged": len(flags), "awaiting_upkeep": sum(1 for f in flags.values() if not f["seen"]),
    }


THIN_KINDS = ("people", "places", "organizations", "projects", "topics", "ideas", "trips")
THIN_CHARS = 400


def page_flags(conn, slugs: list[str] | None = None, *, limit: int = 500) -> dict[str, dict[str, Any]]:
    """What upkeep should look at, per page (GBrain maintain: stale, orphan,
    thin, uncited, dead links):
    - stale_truth: timeline lines newer than the top was last rewritten;
    - stale_evidence: conversations the page cites have newer records;
    - thin: an entity page whose top is under 400 characters;
    - orphan: no link to or from another page;
    - uncited: cites nothing;
    - unresolved: [[links]] to pages that do not exist."""
    import hashlib

    rows = conn.execute(
        """SELECT p.slug, p.kind, p.message_id, p.truth_updated_at, p.stats,
              (SELECT max(t.created_at) FROM tl.page_timeline t WHERE t.slug=p.slug) AS last_line_at,
              (SELECT count(*) FROM tl.page_timeline t WHERE t.slug=p.slug AND t.created_at > p.truth_updated_at) AS lines_since,
              (SELECT count(DISTINCT n.id) FROM tl.provenance pv JOIN tl.messages s ON s.id=pv.source_message_id
                 JOIN tl.messages n ON n.thread_id=s.thread_id AND n.superseded_by IS NULL AND n.id > p.message_id
               WHERE pv.derived_kind='message' AND pv.derived_id=p.message_id AND s.thread_id IS NOT NULL) AS newer,
              EXISTS (SELECT 1 FROM tl.links l JOIN tl.pages q ON q.message_id = CASE WHEN l.from_id=p.message_id THEN l.to_id ELSE l.from_id END
                      WHERE l.status='active' AND l.from_kind='message' AND l.to_kind='message'
                        AND (l.from_id=p.message_id OR l.to_id=p.message_id)) AS linked
        FROM tl.pages p WHERE p.status='active' AND p.message_id IS NOT NULL
          AND (%(slugs)s::text[] IS NULL OR p.slug = ANY(%(slugs)s))
        ORDER BY p.updated_at LIMIT %(lim)s""",
        {"slugs": slugs, "lim": limit},
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        st = r["stats"] or {}
        flags: dict[str, Any] = {}
        if r["lines_since"]:
            flags["stale_truth"] = int(r["lines_since"])
        if r["newer"]:
            flags["stale_evidence"] = int(r["newer"])
        if r["kind"] in THIN_KINDS and int(st.get("truth_chars") or st.get("chars") or 0) < THIN_CHARS:
            flags["thin"] = int(st.get("truth_chars") or st.get("chars") or 0)
        if not r["linked"]:
            flags["orphan"] = True
        if not int(st.get("citations") or 0):
            flags["uncited"] = True
        if st.get("unresolved"):
            flags["unresolved"] = list(st["unresolved"])[:10]
        if flags:
            h = hashlib.md5(json.dumps(flags, sort_keys=True).encode()).hexdigest()[:12]
            out[r["slug"]] = {**flags, "hash": h, "kind": r["kind"],
                              "seen": (st.get("upkeep") or {}).get("flags") == h}
    return out


FLAG_PRIORITY = ("stale_truth", "stale_evidence", "unresolved", "thin", "uncited", "orphan")


def flagged_items(conn, tdef: dict[str, Any], max_items: int) -> tuple[list[dict[str, Any]], bool]:
    """Work items for a pages lens with worker_config.input = 'flagged': one
    per flagged page whose flags changed since its last upkeep, most urgent
    first. kinds limits them to the lens's kinds."""
    kinds = set((tdef.get("worker_config") or {}).get("kinds") or [])
    flags = {s: f for s, f in page_flags(conn).items() if not f["seen"] and (not kinds or f["kind"] in kinds)}
    order = sorted(flags, key=lambda s: min((FLAG_PRIORITY.index(k) for k in flags[s] if k in FLAG_PRIORITY), default=9))
    picked = order[:max_items]
    if not picked:
        return [], False
    rows = conn.execute(
        """SELECT m.id, m.channel_id, c.name AS channel, m.external_id, m.version, m.thread_id, m.thread_no,
              t.title AS thread_title, m.happened_at, m.ended_at, m.ingested_at, m.author_handle, m.person_id,
              NULL::text AS person_handle, NULL::text AS display_name, m.content, m.fields
        FROM tl.pages p JOIN tl.messages m ON m.id=p.message_id JOIN tl.channels c ON c.id=m.channel_id
        LEFT JOIN tl.threads t ON t.id=m.thread_id WHERE p.slug = ANY(%s)""",
        (picked,),
    ).fetchall()
    by = {r["external_id"]: dict(r) for r in rows}
    items = [{"window_key": f"upkeep:{s}", "messages": [by[s]], "message_ids": [by[s]["id"]], "upkeep": True}
             for s in picked if s in by]
    return items, len(order) > max_items


def lens_health(conn, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    out = []
    for d in conn.execute("SELECT id, name, enabled, schedule, grain, channels FROM tl.table_defs WHERE output='pages' ORDER BY name"):
        runs = [dict(r) for r in conn.execute(
            """SELECT id, trigger_kind, status, started_at, finished_at, processed, invalid, model_calls, cost, detail->'errors' AS errors
            FROM tl.table_runs WHERE table_id=%s ORDER BY id DESC LIMIT 5""", (d["id"],))]
        acts = conn.execute(
            """SELECT op, status, count(*) AS n FROM tl.page_actions WHERE lens=%s AND at >= %s GROUP BY 1, 2""",
            (d["name"], now - timedelta(days=7)),
        ).fetchall()
        reasons = [r["reason"] for r in conn.execute(
            """SELECT detail->>'reason' AS reason FROM tl.page_actions WHERE lens=%s AND status='refused' AND at >= %s
               ORDER BY id DESC LIMIT 8""", (d["name"], now - timedelta(days=7)))]
        counts: dict[str, dict[str, int]] = {}
        for a in acts:
            counts.setdefault(a["op"], {})[a["status"]] = a["n"]
        applied = sum(a["n"] for a in acts if a["status"] == "applied")
        refused = sum(a["n"] for a in acts if a["status"] == "refused")
        problems, warnings = [], []
        if not d["enabled"]:
            warnings.append("disabled")
        if not runs:
            problems.append("never run")
        elif runs[0]["status"] in ("failed", "provider_unavailable"):
            problems.append(f"last run {runs[0]['status']}")
        if refused and refused >= max(3, applied):
            warnings.append(f"{refused} actions refused in 7 days (applied {applied})")
        out.append({"lens": d["name"], "verdict": _verdict(problems, warnings), "problems": problems, "warnings": warnings,
                    "schedule": d["schedule"], "grain": d["grain"], "channels": list(d["channels"]),
                    "actions_7d": counts, "refusals": reasons, "runs": runs})
    return out


def channel_health(conn, channel: str, now: datetime | None = None, *, oversized_chars: int = 20_000) -> dict[str, Any]:
    """Health of any page store channel (GBrain, Notion, a page family)."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT id, record_kind FROM tl.channels WHERE name=%s", (channel,)).fetchone()
    if not row:
        raise LookupError(f"no channel named {channel!r}")
    stats = conn.execute(
        """WITH cur AS (
            SELECT m.id, m.happened_at, length(m.content) AS chars,
                   coalesce(m.fields->>'page_type', m.fields->>'type', m.fields->>'family', '(none)') AS page_type
            FROM tl.messages m WHERE m.channel_id=%s AND m.superseded_by IS NULL
              AND coalesce((m.fields->>'deleted')::boolean, false) IS FALSE),
        linked AS (
            SELECT c.id,
              EXISTS (SELECT 1 FROM tl.links l WHERE l.status='active'
                      AND ((l.from_kind='message' AND l.from_id=c.id) OR (l.to_kind='message' AND l.to_id=c.id))) AS has_link,
              EXISTS (SELECT 1 FROM tl.provenance p WHERE p.derived_kind='message' AND p.derived_id=c.id) AS has_source
            FROM cur c)
        SELECT count(*) AS pages,
          count(*) FILTER (WHERE c.happened_at >= %s - interval '7 days') AS updated_7d,
          count(*) FILTER (WHERE c.happened_at >= %s - interval '30 days') AS updated_30d,
          count(*) FILTER (WHERE c.happened_at < %s - interval '90 days') AS older_90d,
          count(*) FILTER (WHERE NOT l.has_link) AS no_links,
          count(*) FILTER (WHERE NOT l.has_source) AS no_sources,
          count(*) FILTER (WHERE c.chars > %s) AS oversized,
          percentile_disc(0.5) WITHIN GROUP (ORDER BY c.chars) AS median_chars,
          max(c.happened_at) AS last_revision
        FROM cur c JOIN linked l ON l.id=c.id""",
        (row["id"], now, now, now, oversized_chars),
    ).fetchone()
    types = conn.execute(
        """SELECT coalesce(m.fields->>'page_type', m.fields->>'type', m.fields->>'family', '(none)') AS page_type,
                  count(*) AS pages, max(m.happened_at) AS last_revision
        FROM tl.messages m WHERE m.channel_id=%s AND m.superseded_by IS NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15""",
        (row["id"],),
    ).fetchall()
    pages = int(stats["pages"] or 0)
    problems, warnings = [], []
    if pages == 0:
        problems.append("no pages")
    else:
        if stats["last_revision"] and now - stats["last_revision"] > timedelta(days=7):
            problems.append(f"no page revised since {stats['last_revision'].date()}")
        if stats["no_links"] / pages > 0.4:
            warnings.append(f"{stats['no_links']} of {pages} pages have no link")
        if stats["older_90d"] / pages > 0.5:
            warnings.append(f"{stats['older_90d']} of {pages} pages not revised in 90 days")
        if stats["oversized"]:
            warnings.append(f"{stats['oversized']} pages over {oversized_chars} characters")
    return {
        "channel": channel,
        "record_kind": row["record_kind"],
        "verdict": _verdict(problems, warnings),
        "problems": problems,
        "warnings": warnings,
        **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(stats).items()},
        "by_type": [
            {**dict(t), "last_revision": t["last_revision"].isoformat() if t["last_revision"] else None} for t in types
        ],
    }


def report(channels: list[str] | None = None) -> dict[str, Any]:
    with connect() as conn:
        out: dict[str, Any] = {"graph": graph_health(conn), "lenses": lens_health(conn)}
        if channels is None:
            channels = [r["name"] for r in conn.execute(
                "SELECT name FROM tl.channels WHERE record_kind='page_revision' AND name <> 'pages' ORDER BY name")]
        out["stores"] = [channel_health(conn, c) for c in channels]
    verdicts = [out["graph"]["verdict"], *(x["verdict"] for x in out["lenses"]), *(s["verdict"] for s in out["stores"])]
    out["verdict"] = "bad" if "bad" in verdicts else "warn" if "warn" in verdicts else "ok"
    return out
