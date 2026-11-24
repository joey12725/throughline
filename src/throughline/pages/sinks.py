"""Exporting pages to other memory systems (GBrain, an Obsidian folder, ...).

Sinks are configured once for the whole page graph in
THROUGHLINE_PAGES_CONFIG (default ~/.throughline/pages.toml):

    [[sinks]]
    kind = "markdown"                 # YAML front matter + Markdown, written atomically
    path = "~/brain/{slug}.md"

    [[sinks]]
    kind = "command"                  # the page Markdown on stdin; exit 0 = delivered
    name = "gbrain"
    argv = ["ssh", "memory-host", "gbrain", "put", "{slug}"]
    kinds = ["people", "events"]      # optional: only these kinds

Templates take {slug}, {kind}, {title}, {name} (the last slug segment). Each
sink receives every new revision once (tl.pages.sink_state); a failed
delivery is retried by the next export. Pages lenses export what they touch;
`throughline lib pages export` sends anything pending.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from ..db import connect
from . import store


def config_path() -> Path:
    raw = (os.getenv("THROUGHLINE_PAGES_CONFIG") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".throughline" / "pages.toml"


def load_sinks(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or config_path()
    if not p.is_file():
        return []
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    sinks = []
    for i, raw in enumerate(data.get("sinks") or []):
        if not isinstance(raw, dict) or raw.get("kind") not in ("markdown", "command"):
            raise ValueError(f"{p}: sinks[{i}].kind must be markdown or command")
        if raw["kind"] == "markdown" and not isinstance(raw.get("path"), str):
            raise ValueError(f"{p}: sinks[{i}].path is required")
        if raw["kind"] == "command" and not (isinstance(raw.get("argv"), list) and raw["argv"]):
            raise ValueError(f"{p}: sinks[{i}].argv is required")
        if raw.get("enabled", True):
            sinks.append({**raw, "name": str(raw.get("name") or f"{raw['kind']}{i}")})
    return sinks


def render(template: str, values: dict[str, str]) -> str:
    return re.sub(r"\{([a-z_]+)\}", lambda m: values.get(m.group(1), m.group(0)), template)


def markdown(page: dict[str, Any]) -> str:
    def q(v: Any) -> str:
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'

    fm = ["---", f"title: {q(page['title'])}", f"slug: {q(page['slug'])}", f"type: {q(page['kind'])}",
          f"throughline_page: {q(page['slug'])}", f"throughline_id: {page['message_id']}",
          f"revision: {page['version']}", f"updated: {q(page['updated_at'].isoformat() if page.get('updated_at') else '')}",
          f"summary: {q(page.get('summary') or '')}", f"sources: {len(page.get('citations') or [])}"]
    if page.get("aliases"):
        fm.append("aliases: [" + ", ".join(q(a) for a in page["aliases"]) + "]")
    if page.get("person_id"):
        fm.append(f"person_id: {page['person_id']}")
    fm.append("---")
    return "\n".join(fm) + "\n\n" + (page.get("content") or "")


def _send(sink: dict[str, Any], page: dict[str, Any]) -> None:
    values = {"slug": page["slug"], "kind": page["kind"], "title": page["title"], "name": page["slug"].rsplit("/", 1)[-1]}
    text = markdown(page)
    if sink["kind"] == "markdown":
        path = Path(os.path.expandvars(render(sink["path"], values))).expanduser()
        if ".." in path.parts:
            raise ValueError("a markdown sink path may not contain '..'")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".page-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return
    proc = subprocess.run([render(a, values) for a in sink["argv"]], input=text.encode("utf-8"),
                          capture_output=True, timeout=int(sink.get("timeout_s", 60)), check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()[-300:]
        raise RuntimeError(f"sink command exited {proc.returncode}: {err}")


def export(slugs: list[str] | None = None, *, sinks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Send every page revision a sink has not received yet."""
    sinks = load_sinks() if sinks is None else sinks
    out: dict[str, Any] = {"sent": 0, "failed": 0, "errors": []}
    if not sinks:
        return out
    with connect() as conn:
        rows = conn.execute(
            "SELECT slug FROM tl.pages WHERE status='active' AND message_id IS NOT NULL AND (%s::text[] IS NULL OR slug = ANY(%s)) ORDER BY slug",
            (slugs, slugs),
        ).fetchall()
        for r in rows:
            page = store.get(conn, r["slug"])
            state = dict(page.get("sink_state") or {})
            dirty = False
            for sink in sinks:
                if sink.get("kinds") and page["kind"] not in sink["kinds"]:
                    continue
                if (state.get(sink["name"]) or {}).get("message_id") == page["message_id"]:
                    continue
                dirty = True
                try:
                    _send(sink, page)
                    state[sink["name"]] = {"message_id": page["message_id"], "at": datetime.now(timezone.utc).isoformat()}
                    out["sent"] += 1
                except Exception as exc:
                    state[sink["name"]] = {**(state.get(sink["name"]) or {}), "error": str(exc)[:300]}
                    out["failed"] += 1
                    out["errors"].append(f"{page['slug']} -> {sink['name']}: {str(exc)[:200]}")
            if dirty:
                conn.execute("UPDATE tl.pages SET sink_state=%s WHERE slug=%s", (Jsonb(state), page["slug"]))
    out["errors"] = out["errors"][:20]
    return out
