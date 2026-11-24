"""Connecting agents: the remote MCP endpoint, client snippets, and the
copyable setup prompt.

MCP: POST /v1/mcp speaks the Model Context Protocol's Streamable HTTP
transport with plain JSON responses (no server-sent events). Each tool is a
thin wrapper over an existing retrieval endpoint, called back over loopback
with the caller's own bearer token, so scope, askers, budgets and the audit
log apply exactly as for direct HTTP use.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import jobs

PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "throughline", "title": "Throughline", "version": "0.1.0"}

INSTRUCTIONS = (
    "Throughline holds one person's life record (chats, email, calendar, meeting transcripts, location, "
    "health, media, notes). Search with `find` (several wordings), read around hits with `sql` "
    "(SELECT * FROM api.context(id, 5, 5)), use `person` for people, `timeline` for counts and patterns, "
    "`orient` for the current week. Cite channel and date for every claim; say what you could not find."
)

_scope = {
    "after": {"type": "string", "description": "ISO date; only records at or after it"},
    "before": {"type": "string", "description": "ISO date; only records before it"},
    "channels": {"type": "array", "items": {"type": "string"}, "description": "Limit to these channels"},
}

TOOLS = [
    dict(
        name="find",
        title="Search everything",
        description="Hybrid keyword + meaning search returning compact episodes with surrounding thread context, "
        "owner-local times and channel/external_id. A ranked sample: search several wordings.",
        inputSchema={"type": "object", "properties": {
            "query": {"type": "string", "description": "Natural words about the topic"},
            "k": {"type": "integer", "default": 15, "minimum": 1, "maximum": 100},
            "sort": {"type": "string", "enum": ["relevance", "recent"]},
            "persons": {"type": "array", "items": {"type": "integer"}},
            **_scope}, "required": ["query"]},
        route=("POST", "/v1/find"),
    ),
    dict(
        name="timeline",
        title="Every occurrence over time",
        description="Exhaustive chronological episodes for counting and patterns ('how many times', 'over the years').",
        inputSchema={"type": "object", "properties": {
            "queries": {"type": "array", "items": {"type": "string"}, "description": "3-6 synonyms"},
            "semantic": {"type": "boolean", "default": True},
            "max_episodes": {"type": "integer", "default": 150},
            "sample": {"type": "string", "enum": ["head", "spread"]},
            **_scope}, "required": ["queries"]},
        route=("POST", "/v1/timeline"),
    ),
    dict(
        name="orient",
        title="Current snapshot",
        description="Recent telemetry, upcoming calendar, recent threads and voice notes; pass people for catch-ups.",
        inputSchema={"type": "object", "properties": {
            "days": {"type": "integer", "default": 14, "minimum": 1, "maximum": 90},
            "people": {"type": "array", "items": {"type": "string"}, "maxItems": 6}}},
        route=("POST", "/v1/orient"),
    ),
    dict(
        name="sql",
        title="Read-only SQL",
        description="One SELECT over the api.* views (api.messages, api.threads, api.context(id,before,after), "
        "api.person('name'), api.person_brief(id), api.dms_with(...), api.day(...)).",
        inputSchema={"type": "object", "properties": {
            "statement": {"type": "string", "description": "A single SELECT statement"}},
            "required": ["statement"]},
        route=("POST", "/v1/sql"),
    ),
    dict(
        name="person",
        title="Who someone is",
        description="Resolve a name to people and return each one's brief (channels, last contact, open loops).",
        inputSchema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        route=("SQL", "SELECT p.id, b.* FROM api.person({name}) p, LATERAL api.person_brief(p.id) b LIMIT 5"),
    ),
    dict(
        name="describe",
        title="What's in the archive",
        description="Channels, their fields, functions and recipes.",
        inputSchema={"type": "object", "properties": {}},
        route=("GET", "/v1/describe"),
    ),
]


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _call(method, path, token, body=None, asker=None):
    url = jobs.api_url() + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "X-Throughline-Client": "mcp", **({"X-Throughline-Asker": asker} if asker else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return False, response.read().decode()
    except urllib.error.HTTPError as error:
        return True, error.read().decode() or f"HTTP {error.code}"


def call_tool(name, arguments, token, asker=None):
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        return dict(isError=True, content=[{"type": "text", "text": f"Unknown tool {name!r}."}])
    arguments = dict(arguments or {})
    method, target = tool["route"]
    if method == "SQL":
        statement = target.format(name=_literal(arguments.get("name", "")))
        failed, text = _call("POST", "/v1/sql", token, {"statement": statement}, asker)
    elif method == "GET":
        failed, text = _call("GET", target, token, None, asker)
    else:
        failed, text = _call("POST", target, token, arguments, asker)
    return dict(isError=failed, content=[{"type": "text", "text": text}])


def handle(message, token, asker=None):
    """One JSON-RPC message → response dict, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    method, rid = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if rid is None:
        return None  # notifications (initialized, cancelled, ...)
    if method == "initialize":
        return _result(rid, dict(
            protocolVersion=params.get("protocolVersion") or PROTOCOL if params.get("protocolVersion") in (PROTOCOL, "2025-03-26", "2024-11-05") else PROTOCOL,
            capabilities={"tools": {"listChanged": False}},
            serverInfo=SERVER_INFO,
            instructions=INSTRUCTIONS,
        ))
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, dict(tools=[{k: v for k, v in t.items() if k != "route"} for t in TOOLS]))
    if method == "tools/call":
        return _result(rid, call_tool(params.get("name"), params.get("arguments"), token, asker))
    return _error(rid, -32601, f"Method not found: {method}")


def _result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


# ───────────── client snippets ─────────────


def clients(base_url, token=None):
    mcp = base_url.rstrip("/") + "/v1/mcp"
    tok = token or "<YOUR_AGENT_TOKEN>"
    return [
        dict(id="claude-code", label="Claude Code", kind="mcp",
             snippet=f'claude mcp add --transport http throughline {mcp} --header "Authorization: Bearer {tok}"',
             steps=["Run this in a terminal.", "Start Claude Code and run /mcp to confirm Throughline is connected.",
                    "Ask: “What did I promise people this week?”"]),
        dict(id="claude-desktop", label="Claude Desktop", kind="mcp",
             snippet=json.dumps({"mcpServers": {"throughline": {"command": "npx", "args": [
                 "-y", "mcp-remote@0.1.29", mcp, "--header", f"Authorization:Bearer {tok}"]}}}, indent=2),
             steps=["Open Settings → Developer → Edit Config.", "Merge this into claude_desktop_config.json.",
                    "Restart Claude Desktop."]),
        dict(id="cursor", label="Cursor", kind="mcp",
             snippet=json.dumps({"mcpServers": {"throughline": {"url": mcp, "headers": {
                 "Authorization": f"Bearer {tok}"}}}}, indent=2),
             steps=["Open ~/.cursor/mcp.json (or Settings → MCP → Add).", "Paste this and save."]),
        dict(id="codex", label="Codex", kind="mcp",
             snippet=f'[mcp_servers.throughline]\nurl = "{mcp}"\nbearer_token_env_var = "THROUGHLINE_TOKEN"\n\n# then: export THROUGHLINE_TOKEN={tok}',
             steps=["Add this to ~/.codex/config.toml.", "Export the token in your shell profile."]),
        dict(id="chatgpt", label="ChatGPT", kind="mcp",
             snippet=f"Server URL: {mcp}\nAuthentication: API key / bearer → {tok}",
             steps=["ChatGPT needs a public HTTPS address: expose Throughline through a tunnel or reverse proxy first.",
                    "Settings → Connectors → Create → paste the URL and token."]),
        dict(id="other", label="Any agent (HTTP + skill)", kind="skill",
             snippet=f"export THROUGHLINE_URL={base_url.rstrip('/')}\nexport THROUGHLINE_TOKEN={tok}\n# Give the agent skill/throughline/SKILL.md",
             steps=["Agents without MCP can call the HTTP API directly.", "Copy SKILL.md into the agent's skills or system prompt."]),
    ]


# ───────────── the setup prompt ─────────────


def setup_prompt(base_url):
    base = base_url.rstrip("/")
    return f"""# Set up Throughline with me

You are helping me set up **Throughline**, my self-hosted personal data hub, at `{base}`.
Walk me through it one step at a time, conversationally. Do the work yourself through the API;
only ask me for things only I can give (keys, exports, which sources I use, who's who).

## How to talk to Throughline
- Every call: `Authorization: Bearer $TOKEN` and `Content-Type: application/json` against `{base}`.
- I'll give you an **owner token** (create one on the server with
  `throughline token create setup-agent --json`, or Settings → Agents). Never print it back.
- Errors come back as `detail: {{title, message, hint, problems[]}}`. Read the hint, fix what it
  says, and explain it to me in plain words. Don't retry blindly.

## The steps (GET `/v1/setup/state` first and skip what's done)
1. **What's already here** – `GET /v1/setup/detect`. If `existing` is true, this is a restore or a
   continued install: tell me which sources are already connected (with record counts) and skip them.
2. **Intelligence** – `GET /v1/setup/providers`. Ask which I have: OpenRouter, Voyage, OpenAI,
   Anthropic, Gemini, a CLI proxy (any OpenAI-compatible URL) or local models. For each key I give:
   `POST /v1/setup/providers/{{id}}/test {{"values": {{...}}}}`, and only if `ok`,
   `PUT /v1/setup/providers/{{id}} {{"values": {{...}}}}`. Then
   `PUT /v1/setup/roles {{"embedding": {{"provider": "...", "model": null}}, "assistant": {{...}}, "semantic": {{...}}}}`
   (null model = the recommended one). Embeddings first: they make meaning search work.
3. **Photos** – offer to connect an existing Immich (provider id `immich`, same test/save calls).
4. **Sources** – `GET /v1/setup/catalog`. Ask me what I actually use (most people use only a few).
   Don't list all of them; group by category and suggest the `starter` ones that fit what I said.
   If I paste a GitHub link, `POST /v1/setup/repos {{"url": "..."}}` and show what it contains
   (warn me it runs code from that repository).
5. **Connect each source** – for each chosen plugin: if `status.installed` is false,
   `POST /v1/setup/plugins/{{id}}/install` and poll `GET /v1/setup/jobs/{{job_id}}`. Walk me through
   `setup.steps`, collect `setup.fields` (ask only for required and visible ones; secrets: tell me
   where to find them), `PUT /v1/setup/plugins/{{id}}/config {{"values": {{...}}}}`, then
   `POST /v1/setup/plugins/{{id}}/check` and explain the result. For a `file` field (e.g. Google
   Timeline's Timeline.json), have me upload it in the web UI at {base}/ui/welcome/connect or give
   you a path on the server.
6. **Desktop companion** – if a chosen source `requires` "companion", `POST /v1/setup/companion/pair
   {{"name": "laptop"}}` and give me the pairing code plus the download link from
   `GET /v1/setup/companion`. Poll until my device shows up.
7. **Import history** – `POST /v1/setup/plugins/{{id}}/backfill` for each configured source, then poll
   `GET /v1/setup/jobs` and give me short progress updates (records, how far back, ETA). Failed
   jobs have `error` + `log_tail`: explain and fix, then `POST /v1/setup/jobs/{{id}}/retry`.
8. **Who's who** – `GET /v1/setup/people`: confirm which `me_candidates` are me
   (`POST /v1/setup/people/me {{"identifier_ids": [...]}}`; ids are the negatives shown), then
   `POST /v1/setup/people/resolve`. Certain matches are applied automatically; just summarise
   them. Then ask me each open question naturally ("Is Birdie on Discord the same person as Robin
   Vale from email?"), with its evidence, and answer with
   `POST /v1/setup/people/questions/{{id}} {{"answer": "same"|"different"|"skip", "name": "optional"}}`.
   Let me stop whenever I like.
9. **First lenses** – `GET /v1/setup/lenses`. Lenses are tables an AI keeps filled from my
   conversations (plans & commitments, recommendations, important dates). If `model.ready` is false,
   say why (`model.hint`) and offer to go back to step 2. Ask how fresh I want things versus how much
   it costs (`paces`: live, balanced, thrifty), then `PUT /v1/setup/lenses {{"templates": [...],
   "pace": "..."}}` and tell me the `quote` (items, cost). Offer `POST /v1/setup/lenses/{{name}}/run`
   and show me a few rows from `POST /v1/sql {{"statement": "SELECT * FROM api.lens_<name> LIMIT 5"}}`.
10. **Pages** – `GET /v1/setup/pages`: daily pages, person profiles, event pages (pages lenses, docs/v11/pages.md). Same pace.
   `PUT /v1/setup/pages {{"families": [...], "pace": "...", "markdown_dir": null}}` (a folder makes
   every page a Markdown file too, e.g. for Obsidian). Offer `POST /v1/setup/pages/daily/run` and
   read me yesterday's page from `GET /v1/pages/daily/<YYYY-MM-DD>`.
11. **First queries** – `GET /v1/setup/queries` returns starter questions as runnable requests
   (`kind` sql → `POST /v1/sql`, find → `POST /v1/find`, timeline → `POST /v1/timeline`). Run two or
   three that fit what I connected, show me the answers briefly, and explain how I'd ask my own.
12. **Connect my agents** – `GET /v1/setup/agent`; `POST /v1/setup/agent/token
   {{"name": "...", "client": "claude-code"}}` gives a token and ready-to-paste snippets (show once).
13. **Finish** – `POST /v1/setup/complete`, then show me three things to try, e.g.
    “What did I promise people this week?”, “When did I last see Sam?”, “How has my sleep been?”

Keep messages short. After each step, say what's done and what's next.
"""
