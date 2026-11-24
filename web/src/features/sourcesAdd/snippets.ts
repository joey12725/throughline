/*
 * Push-over-HTTP snippets. Real endpoint paths and payload shapes (map-http.md
 * sections 9a/9b) with a token placeholder, for the three runtimes a person is
 * most likely to reach for. Pure and tested (snippets.test.ts) so the paths and
 * JSON stay correct.
 */
import type { FieldType } from "@/lens";

export interface ChannelDraft {
  name: string;
  plugin: string;
  description: string;
  fields: Array<{ name: string; type: FieldType; description?: string }>;
}

export interface SnippetOptions {
  baseUrl?: string;
  tokenVar?: string;
}

const DEFAULT_BASE = "https://your-throughline.example";
const TOKEN = "$THROUGHLINE_TOKEN";

function channelBody(draft: ChannelDraft): Record<string, unknown> {
  return {
    name: draft.name,
    plugin: draft.plugin,
    description: draft.description,
    fields: draft.fields.map((f) => ({ name: f.name, type: f.type, ...(f.description ? { description: f.description } : {}) })),
  };
}

function sampleMessage(draft: ChannelDraft): Record<string, unknown> {
  const fields: Record<string, unknown> = {};
  for (const f of draft.fields) fields[f.name] = sampleFor(f.type);
  return {
    channel: draft.name,
    external_id: "row-1",
    thread: { external_id: "thread-1" },
    happened_at: "2024-01-01T12:00:00+00:00",
    content: "the first record",
    ...(Object.keys(fields).length ? { fields } : {}),
    payload: { source: "example" },
  };
}

function sampleFor(type: FieldType): unknown {
  switch (type) {
    case "integer":
      return 1;
    case "number":
      return 1.5;
    case "boolean":
      return true;
    case "timestamp":
      return "2024-01-01T12:00:00+00:00";
    case "json":
      return { k: "v" };
    default:
      return "example";
  }
}

export function curlSnippet(draft: ChannelDraft, opts: SnippetOptions = {}): string {
  const base = opts.baseUrl || DEFAULT_BASE;
  const tok = opts.tokenVar || TOKEN;
  const chan = JSON.stringify(channelBody(draft), null, 2);
  const ingest = JSON.stringify({ plugin: draft.plugin, dry_run: true, messages: [sampleMessage(draft)] }, null, 2);
  return [
    "# 1) register the channel (owner or plugin token)",
    `curl -sS ${base}/v1/channels \\`,
    `  -H "Authorization: Bearer ${tok}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '${chan}'`,
    "",
    "# 2) dry-run one record (full path, rolled back — your preview)",
    `curl -sS ${base}/v1/ingest/messages \\`,
    `  -H "Authorization: Bearer ${tok}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -H "Idempotency-Key: import-$(date +%s)" \\`,
    `  -d '${ingest}'`,
    "",
    "# 3) set dry_run to false to write.",
  ].join("\n");
}

export function pythonSnippet(draft: ChannelDraft, opts: SnippetOptions = {}): string {
  const base = opts.baseUrl || DEFAULT_BASE;
  const chan = JSON.stringify(channelBody(draft), null, 4);
  const msg = JSON.stringify(sampleMessage(draft), null, 4);
  return [
    "import os, httpx",
    "",
    `BASE = "${base}"`,
    'TOKEN = os.environ["THROUGHLINE_TOKEN"]',
    'H = {"Authorization": f"Bearer {TOKEN}"}',
    "",
    "with httpx.Client(base_url=BASE, headers=H) as c:",
    `    c.post("/v1/channels", json=${indent(chan, 4)}).raise_for_status()`,
    `    msg = ${indent(msg, 4)}`,
    '    body = {"plugin": ' + JSON.stringify(draft.plugin) + ', "dry_run": True, "messages": [msg]}',
    '    r = c.post("/v1/ingest/messages", json=body,',
    '               headers={"Idempotency-Key": "import-001"})',
    "    print(r.json())  # {received, inserted, rejected, ...}",
    "    # set dry_run=False to write.",
  ].join("\n");
}

export function nodeSnippet(draft: ChannelDraft, opts: SnippetOptions = {}): string {
  const base = opts.baseUrl || DEFAULT_BASE;
  const chan = JSON.stringify(channelBody(draft), null, 2);
  const msg = JSON.stringify(sampleMessage(draft), null, 2);
  return [
    `const BASE = "${base}";`,
    "const TOKEN = process.env.THROUGHLINE_TOKEN;",
    'const H = { "Authorization": `Bearer ${TOKEN}`, "Content-Type": "application/json" };',
    "",
    `const channel = ${chan};`,
    'await fetch(`${BASE}/v1/channels`, { method: "POST", headers: H, body: JSON.stringify(channel) });',
    "",
    `const message = ${msg};`,
    "const body = { plugin: " + JSON.stringify(draft.plugin) + ', dry_run: true, messages: [message] };',
    'const res = await fetch(`${BASE}/v1/ingest/messages`, {',
    '  method: "POST",',
    '  headers: { ...H, "Idempotency-Key": "import-001" },',
    "  body: JSON.stringify(body),",
    "});",
    "console.log(await res.json());",
    "// set dry_run:false to write.",
  ].join("\n");
}

function indent(json: string, spaces: number): string {
  const pad = " ".repeat(spaces);
  return json
    .split("\n")
    .map((line, i) => (i === 0 ? line : pad + line))
    .join("\n");
}

export type SnippetLang = "curl" | "python" | "node";

export function snippetFor(lang: SnippetLang, draft: ChannelDraft, opts?: SnippetOptions): string {
  if (lang === "python") return pythonSnippet(draft, opts);
  if (lang === "node") return nodeSnippet(draft, opts);
  return curlSnippet(draft, opts);
}
