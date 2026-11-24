"""`throughline lib pages ...`: the page graph the Librarian maintains.

Pages are written by lenses with output 'pages' (run them with `lib lens run
NAME` or the scheduler's `lib lens tick`). These commands read the graph,
check its health, export it, and let the owner write or link pages by hand.
They run in this process against THROUGHLINE_DATABASE_URL."""

from __future__ import annotations

import json as json_module
import shutil
from pathlib import Path

import typer


def _out(data, as_json: bool) -> None:
    from ..cli import output

    output(data, as_json)


def _mark(v: str) -> str:
    return {"ok": "ok  ", "warn": "WARN", "bad": "BAD "}[v]


def register(group):
    app = typer.Typer(no_args_is_help=True)
    group.add_typer(app, name="pages", short_help="The Librarian's page graph.")

    @app.command("init")
    def init(
        directory: Path = typer.Option(Path.home() / ".throughline" / "lenses", "--dir"),
        overwrite: bool = typer.Option(False, "--overwrite"),
    ):
        """Copy the example pages lenses into the lens directory (then `lib lens define FILE`)."""
        from ..pages import librarian

        src = Path(librarian.__file__).parent / "examples"
        directory.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.glob("*.toml")):
            target = directory / f.name
            if target.exists() and not overwrite:
                typer.echo(f"kept     {target}")
                continue
            shutil.copyfile(f, target)
            typer.echo(f"written  {target}")

    @app.command("list")
    def list_(
        kind: str | None = typer.Option(None, "--kind"),
        query: str | None = typer.Option(None, "--query", "-q"),
        limit: int = typer.Option(30, "--limit"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        from .. import pages
        from ..db import connect

        with connect() as conn:
            rows = pages.search(conn, query, kind=kind, limit=limit)
        if as_json:
            _out(rows, True)
            return
        for r in rows:
            typer.echo(f"{r['slug']:<48} {r['kind']:<12} {r['updated_at']:%Y-%m-%d}  {(r['summary'] or '')[:70]}")

    @app.command("show")
    def show(slug: str, sources: bool = typer.Option(False, "--sources"), as_json: bool = typer.Option(False, "--json")):
        """A page, its links in and out, and (with --sources) the records it cites."""
        from .. import pages
        from ..db import connect

        with connect() as conn:
            page = pages.get(conn, slug)
            if not page:
                raise typer.BadParameter(f"no page {slug}")
            nb = pages.neighbors(conn, slug)
            cited = []
            if sources and page["message_id"]:
                cited = [dict(r) for r in conn.execute(
                    """SELECT m.id, c.name AS channel, m.happened_at, left(m.content, 140) AS excerpt
                    FROM tl.provenance p JOIN tl.messages m ON m.id=p.source_message_id JOIN tl.channels c ON c.id=m.channel_id
                    WHERE p.derived_kind='message' AND p.derived_id=%s ORDER BY m.happened_at""", (page["message_id"],))]
        if as_json:
            page.pop("payload", None)
            _out({**page, "links": nb, "sources": cited}, True)
            return
        typer.echo(page["content"] or page["body"])
        typer.echo(f"\n-- {page['slug']} ({page['kind']}, {page['status']}) revision {page['version']}, "
                   f"{len(page['citations'])} records cited, by {page['updated_by']} at {page['updated_at']:%Y-%m-%d %H:%M}")
        for n in nb["out"]:
            typer.echo(f"  -> {n['type']:<11} {n['slug'] or 'person:' + str(n['to_id'])}  {n['title'] or n['person'] or ''}")
        for n in nb["in"]:
            typer.echo(f"  <- {n['type']:<11} {n['slug']}  {n['title']}")
        for c in cited:
            typer.echo(f"  [m:{c['id']}] {c['channel']} {c['happened_at']:%Y-%m-%d %H:%M} {c['excerpt']!r}")

    @app.command("write")
    def write(
        slug: str,
        body_file: Path = typer.Option(..., "--body", help="Markdown file with the page body"),
        title: str = typer.Option(..., "--title"),
        kind: str | None = typer.Option(None, "--kind"),
        summary: str = typer.Option("", "--summary"),
        alias: list[str] = typer.Option([], "--alias"),
        person: int | None = typer.Option(None, "--person"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """Write a page by hand (the owner's revision)."""
        from .. import pages
        from ..db import connect

        with connect() as conn:
            _out(pages.write(conn, slug=slug, kind=kind or slug.split("/")[0], title=title, body=body_file.read_text(),
                             by="owner", summary=summary, aliases=alias, person_id=person), as_json)

    @app.command("link")
    def link(
        src: str, dst: str,
        type_: str = typer.Option("related_to", "--type"),
        evidence: str = typer.Option(..., "--evidence", help="Comma-separated message ids"),
        note: str | None = typer.Option(None, "--note"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """Link two pages (or a page and person:ID) by hand, with evidence."""
        from .. import pages
        from ..db import connect

        ids = [int(x) for x in evidence.split(",") if x.strip()]
        with connect() as conn:
            _out(pages.link(conn, src=src, dst=dst, type=type_, evidence=ids, by="owner", note=note), as_json)

    @app.command("timeline")
    def timeline(
        slug: str | None = typer.Option(None, "--slug"),
        on: str | None = typer.Option(None, "--on", help="YYYY-MM-DD"),
        since: str | None = typer.Option(None, "--since"),
        until: str | None = typer.Option(None, "--until"),
        limit: int = typer.Option(60, "--limit"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """Dated timeline lines across pages (a day, a span, or one page)."""
        from .. import pages
        from ..db import connect

        with connect() as conn:
            rows = pages.chronicle(conn, on=on, since=since, until=until, slug=slug, limit=limit)
        if as_json:
            _out(rows, True)
            return
        for r in rows:
            typer.echo(f"{r['date']} {r['slug']:<40} {r['summary'][:150]}")

    @app.command("kinds")
    def kinds_(
        declare: str | None = typer.Option(None, "--declare", help="Declare (or update) a kind"),
        description: str = typer.Option("", "--description"),
        section: list[str] = typer.Option([], "--section"),
        guidance: str = typer.Option("", "--guidance"),
        half_life: int | None = typer.Option(None, "--half-life-days"),
        retire: str | None = typer.Option(None, "--retire"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """Page kinds: list them, declare one (or promote a candidate), retire one."""
        from ..db import connect

        with connect() as conn:
            if declare:
                conn.execute(
                    """INSERT INTO tl.page_kinds(kind, description, sections, guidance, half_life_days, status, origin)
                    VALUES (%s,%s,%s,%s,%s,'active','owner') ON CONFLICT (kind) DO UPDATE SET status='active',
                      description=coalesce(nullif(EXCLUDED.description,''), tl.page_kinds.description),
                      sections=CASE WHEN cardinality(EXCLUDED.sections)>0 THEN EXCLUDED.sections ELSE tl.page_kinds.sections END,
                      guidance=coalesce(nullif(EXCLUDED.guidance,''), tl.page_kinds.guidance),
                      half_life_days=coalesce(EXCLUDED.half_life_days, tl.page_kinds.half_life_days)""",
                    (declare, description, section, guidance, half_life),
                )
            if retire:
                conn.execute("UPDATE tl.page_kinds SET status='retired' WHERE kind=%s", (retire,))
            rows = [dict(r) for r in conn.execute(
                """SELECT k.kind, k.status, k.half_life_days, k.sections, count(p.slug) AS pages FROM tl.page_kinds k
                LEFT JOIN tl.pages p ON p.kind=k.kind AND p.status='active' GROUP BY k.kind ORDER BY k.status, k.kind""")]
        if as_json:
            _out(rows, True)
            return
        for r in rows:
            typer.echo(f"{r['kind']:<16} {r['status']:<10} pages {r['pages']:<5} half-life {r['half_life_days'] or '-':<5} {', '.join(r['sections'])}")

    @app.command("merge")
    def merge(src: str, into: str, reason: str = typer.Option("", "--reason"), as_json: bool = typer.Option(False, "--json")):
        """Merge page SRC into INTO (aliases, timeline and links move; SRC redirects)."""
        from .. import pages
        from ..db import connect

        with connect() as conn:
            _out(pages.merge(conn, src=src, into=into, by="owner", reason=reason), as_json)

    @app.command("import-gbrain")
    def import_gbrain(
        dry_run: bool = typer.Option(False, "--dry-run", help="Count what would move; write nothing."),
        limit: int | None = typer.Option(None, "--limit"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """Move the imported GBrain (gbrain.pages, gbrain.legacy, timelines, links) into native pages. Re-runnable."""
        from ..pages import gbrain_import

        _out(gbrain_import.run(dry_run=dry_run, limit=limit), as_json)

    @app.command("actions")
    def actions(
        lens: str | None = typer.Option(None, "--lens"),
        status: str | None = typer.Option(None, "--status", help="applied, unchanged or refused"),
        limit: int = typer.Option(40, "--limit"),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """What pages lenses did, newest first."""
        from ..db import connect

        with connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """SELECT id, at, lens, window_key, op, slug, status, detail FROM tl.page_actions
                WHERE (%s::text IS NULL OR lens=%s) AND (%s::text IS NULL OR status=%s) ORDER BY id DESC LIMIT %s""",
                (lens, lens, status, status, limit))]
        if as_json:
            _out(rows, True)
            return
        for r in rows:
            extra = r["detail"].get("reason") or r["detail"].get("outcome") or ""
            typer.echo(f"{r['at']:%m-%d %H:%M} {r['lens']:<18} {r['op']:<7} {r['status']:<9} {r['slug'] or '':<44} {str(extra)[:80]}")

    @app.command("health")
    def health(
        channel: list[str] = typer.Option([], "--channel", help="Page stores to check (default: every other page channel)."),
        as_json: bool = typer.Option(False, "--json"),
    ):
        """The page graph, the pages lenses, and other page stores (GBrain, Notion)."""
        from .. import pages

        rep = pages.report(channel or None)
        if as_json:
            _out(rep, True)
            return
        g = rep["graph"]
        typer.echo(f"[{_mark(g['verdict'])}] graph: {g['pages']} pages {g['by_kind']}, {g['links_between_pages']} page links "
                   f"(avg degree {g['avg_degree']}), {g['components']} components, orphans {g['orphan_count']}, "
                   f"uncited {len(g['uncited'])}, unresolved links {g['unresolved_links']}")
        for w in g["problems"] + g["warnings"]:
            typer.echo(f"       {w}")
        for s in g["stale"][:5]:
            typer.echo(f"       stale: {s['slug']} ({s['newer']} newer records in cited conversations)")
        typer.echo(f"       timeline lines {g['timeline_lines']}, stale truth {len(g['stale_truth'])}, thin {len(g['thin'])}, "
                   f"awaiting upkeep {g['awaiting_upkeep']} of {g['flagged']} flagged")
        for w in g["wanted_pages"][:5]:
            typer.echo(f"       wanted: {w['slug']} ({w['refs']} pages link to it)")
        for k in g["candidate_kinds"][:5]:
            typer.echo(f"       candidate kind: {k['kind']} ({k['pages']} pages); declare with lib pages kinds --declare")
        for p in g["people_without_page"][:5]:
            typer.echo(f"       no page: {p['name']} (person {p['person_id']}, {p['messages']} recent messages)")
        for lh in rep["lenses"]:
            last = lh["runs"][0] if lh["runs"] else None
            typer.echo(f"[{_mark(lh['verdict'])}] lens {lh['lens']}: actions 7d {json_module.dumps(lh['actions_7d'])}"
                       + (f", last run {last['status']} {last['started_at']:%m-%d %H:%M}" if last else ""))
            for w in lh["problems"] + lh["warnings"]:
                typer.echo(f"       {w}")
            for r in lh["refusals"][:3]:
                typer.echo(f"       refused: {r[:120]}")
        for st in rep["stores"]:
            typer.echo(f"[{_mark(st['verdict'])}] {st['channel']}: {st['pages']} pages, revised 7d {st['updated_7d']} / 30d {st['updated_30d']}, "
                       f"untouched 90d+ {st['older_90d']}, no links {st['no_links']}, oversized {st['oversized']}")
            for w in st["problems"] + st["warnings"]:
                typer.echo(f"       {w}")
        typer.echo(f"overall: {rep['verdict']}")
        if rep["verdict"] == "bad":
            raise typer.Exit(1)

    @app.command("export")
    def export(slug: list[str] = typer.Option([], "--slug"), as_json: bool = typer.Option(False, "--json")):
        """Send pending page revisions to the sinks in ~/.throughline/pages.toml."""
        from .. import pages

        _out(pages.export(slug or None), as_json)
