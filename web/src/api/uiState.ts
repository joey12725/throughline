/*
 * UI state: saved queries, boards, lens overrides, source-colour assignments and
 * import mappings. On a current server these live in tl.web_state behind
 *   GET    /v1/web/state/{kind}            -> [{key, value, updated_at}]
 *   GET    /v1/web/state/{kind}/{key}      -> {key, value, updated_at}
 *   PUT    /v1/web/state/{kind}/{key}  body {value}
 *   DELETE /v1/web/state/{kind}/{key}
 * When features.web_state is false (or the routes 404) everything falls back to
 * localStorage, so the shell keeps working. Reads/writes are wrapped so blocked
 * storage never throws into the UI.
 */
import { ApiError } from "./errors";
import { request } from "./client";
import type { UiStateEntry, UiStateKind } from "./types";

let serverAvailable = false;
// Per-kind collection cache. All server reads go through the collection route
// (GET /web/state/{kind} -> [] when empty), never the item route, so a missing
// document is an absent map entry rather than a logged 404 in the console.
const collections = new Map<UiStateKind, Promise<Map<string, UiStateEntry>>>();

export function setWebStateAvailable(v: boolean): void {
  if (v !== serverAvailable) collections.clear();
  serverAvailable = v;
}

function lsKey(kind: UiStateKind, key: string): string {
  return `tl.state.${kind}.${key}`;
}
function lsPrefix(kind: UiStateKind): string {
  return `tl.state.${kind}.`;
}

/**
 * Load (and cache) a whole kind's documents from the collection route. On a 404
 * the feature is absent on this server, so we flip to the localStorage fallback
 * and surface the absence to callers.
 */
async function loadCollection(kind: UiStateKind): Promise<Map<string, UiStateEntry> | null> {
  let pending = collections.get(kind);
  if (!pending) {
    pending = (async () => {
      const list = await request<UiStateEntry[]>(`/web/state/${kind}`);
      return new Map(list.map((e) => [e.key, e]));
    })();
    collections.set(kind, pending);
  }
  try {
    return await pending;
  } catch (err) {
    collections.delete(kind);
    if (err instanceof ApiError && err.status === 404) {
      serverAvailable = false;
      return null;
    }
    throw err;
  }
}

export async function listState<V = unknown>(kind: UiStateKind): Promise<UiStateEntry<V>[]> {
  if (serverAvailable) {
    const map = await loadCollection(kind);
    if (map) return [...map.values()] as UiStateEntry<V>[];
  }
  return listLocal<V>(kind);
}

export async function getState<V = unknown>(
  kind: UiStateKind,
  key: string,
): Promise<UiStateEntry<V> | null> {
  if (serverAvailable) {
    const map = await loadCollection(kind);
    if (map) return (map.get(key) as UiStateEntry<V> | undefined) ?? null;
  }
  return getLocal<V>(kind, key);
}

export async function putState<V = unknown>(
  kind: UiStateKind,
  key: string,
  value: V,
): Promise<void> {
  if (serverAvailable) {
    try {
      await request(`/web/state/${kind}/${encodeURIComponent(key)}`, {
        method: "PUT",
        body: { value },
      });
      // Keep the cached collection coherent so a later read reflects this write.
      const map = await collections.get(kind)?.catch(() => null);
      map?.set(key, { key, value, updated_at: new Date().toISOString() });
      return;
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 404) throw err;
      serverAvailable = false;
      collections.delete(kind);
    }
  }
  putLocal(kind, key, value);
}

export async function deleteState(kind: UiStateKind, key: string): Promise<void> {
  if (serverAvailable) {
    try {
      await request(`/web/state/${kind}/${encodeURIComponent(key)}`, { method: "DELETE" });
      const map = await collections.get(kind)?.catch(() => null);
      map?.delete(key);
      return;
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 404) throw err;
      serverAvailable = false;
      collections.delete(kind);
    }
  }
  deleteLocal(kind, key);
}

/* ---- localStorage fallback ----------------------------------------------- */

function listLocal<V>(kind: UiStateKind): UiStateEntry<V>[] {
  const out: UiStateEntry<V>[] = [];
  try {
    const prefix = lsPrefix(kind);
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith(prefix)) continue;
      const raw = localStorage.getItem(k);
      if (raw === null) continue;
      out.push({ key: k.slice(prefix.length), value: JSON.parse(raw) as V, updated_at: null });
    }
  } catch {
    /* storage blocked */
  }
  return out;
}

function getLocal<V>(kind: UiStateKind, key: string): UiStateEntry<V> | null {
  try {
    const raw = localStorage.getItem(lsKey(kind, key));
    if (raw === null) return null;
    return { key, value: JSON.parse(raw) as V, updated_at: null };
  } catch {
    return null;
  }
}

function putLocal<V>(kind: UiStateKind, key: string, value: V): void {
  try {
    localStorage.setItem(lsKey(kind, key), JSON.stringify(value));
  } catch {
    /* storage blocked */
  }
}

function deleteLocal(kind: UiStateKind, key: string): void {
  try {
    localStorage.removeItem(lsKey(kind, key));
  } catch {
    /* storage blocked */
  }
}
