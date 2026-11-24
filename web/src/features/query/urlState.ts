/*
 * URL state for the query screen. Two shapes must round-trip:
 *
 *   1. Our own share links:  /ui/query?q=<encoded>&run=1
 *      <encoded> is gzip+base64url when the browser has CompressionStream and the
 *      statement is long, else a plain base64url of the UTF-8 text. Both carry a
 *      one-char scheme marker so decoding is never ambiguous.
 *
 *   2. ChartPanel's "Open in Query":  /ui/query?q=<raw SQL>&spec=<JSON>
 *      Here `q` is the already-URL-decoded statement text (no marker) and `spec`
 *      is a view spec as JSON. We honour it exactly: load the statement, pre-load
 *      the chart spec, and run.
 *
 * Everything is standalone and unit-tested; no React, no network.
 */
import type { ViewSpec } from "@/lens";

const GZIP = "~"; // gzip + base64url
const PLAIN = "_"; // base64url of UTF-8

export interface QueryUrlState {
  statement?: string;
  spec?: ViewSpec | null;
  run?: boolean;
}

/* ---- base64url ----------------------------------------------------------- */

function bytesToB64url(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i] as number);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlToBytes(s: string): Uint8Array {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
  const bin = atob(b64 + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function streamToBytes(stream: ReadableStream<Uint8Array>): Promise<Uint8Array> {
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) {
      chunks.push(value);
      total += value.length;
    }
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

async function pipe(stream: "CompressionStream" | "DecompressionStream", bytes: Uint8Array): Promise<Uint8Array> {
  const cs = stream === "CompressionStream" ? new CompressionStream("gzip") : new DecompressionStream("gzip");
  const writer = (cs.writable as WritableStream<Uint8Array>).getWriter();
  void writer.write(bytes);
  void writer.close();
  return streamToBytes(cs.readable as ReadableStream<Uint8Array>);
}

const gzip = (bytes: Uint8Array): Promise<Uint8Array> => pipe("CompressionStream", bytes);
const gunzip = (bytes: Uint8Array): Promise<Uint8Array> => pipe("DecompressionStream", bytes);

const hasCompression =
  typeof CompressionStream !== "undefined" && typeof DecompressionStream !== "undefined";

/** Encode a statement for the `q` parameter. Compresses when worthwhile. */
export async function encodeStatement(statement: string): Promise<string> {
  const bytes = new TextEncoder().encode(statement);
  if (hasCompression && statement.length > 80) {
    try {
      const packed = await gzip(bytes);
      // Only keep compression if it actually helped.
      if (packed.length < bytes.length) return GZIP + bytesToB64url(packed);
    } catch {
      /* fall through to plain */
    }
  }
  return PLAIN + bytesToB64url(bytes);
}

/** Decode a `q` value back to the statement. Unmarked values are raw SQL. */
export async function decodeStatement(value: string): Promise<string> {
  if (value.startsWith(GZIP)) {
    try {
      const bytes = b64urlToBytes(value.slice(1));
      const raw = await gunzip(bytes);
      return new TextDecoder().decode(raw);
    } catch {
      return value;
    }
  }
  if (value.startsWith(PLAIN)) {
    try {
      return new TextDecoder().decode(b64urlToBytes(value.slice(1)));
    } catch {
      return value;
    }
  }
  // Anything else is a raw statement (ChartPanel's "Open in Query").
  return value;
}

function parseSpec(raw: string | null): ViewSpec | null {
  if (!raw) return null;
  try {
    const obj = JSON.parse(raw) as unknown;
    if (obj && typeof obj === "object" && (obj as { v?: unknown }).v === 1) return obj as ViewSpec;
  } catch {
    /* ignore malformed spec */
  }
  return null;
}

/**
 * Read query state from a location's search and hash. Search wins; the hash is a
 * fallback so a `#q=…` link still opens. Returns `undefined` statement when none.
 */
export async function readQueryUrl(search: string, hash: string): Promise<QueryUrlState> {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  let q = params.get("q");
  let spec = params.get("spec");
  let run = params.get("run");
  if (q === null && hash) {
    const h = hash.startsWith("#") ? hash.slice(1) : hash;
    const hp = new URLSearchParams(h);
    q = hp.get("q");
    spec = spec ?? hp.get("spec");
    run = run ?? hp.get("run");
  }
  const out: QueryUrlState = {};
  if (q !== null) out.statement = await decodeStatement(q);
  const parsedSpec = parseSpec(spec);
  if (parsedSpec) out.spec = parsedSpec;
  if (run === "1" || run === "true") out.run = true;
  return out;
}

/** Build a shareable "?q=…" string (no leading path) for the current statement. */
export async function shareQueryString(statement: string, run = true): Promise<string> {
  const params = new URLSearchParams();
  params.set("q", await encodeStatement(statement));
  if (run) params.set("run", "1");
  return params.toString();
}
