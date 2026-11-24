/*
 * On-device query history: the last 200 runs, each with its statement, when it
 * ran, the row count and the wall time. Stored with idb-keyval so it survives a
 * reload without ever leaving the device (never the server — these are the owner's
 * raw statements). All access is wrapped so blocked storage never throws into the
 * UI.
 */
import { get, set } from "idb-keyval";

const KEY = "tl.query.history";
const MAX = 200;

export interface HistoryEntry {
  id: string;
  statement: string;
  at: number;
  rowCount: number | null;
  ms: number | null;
  ok: boolean;
  error?: string;
}

export async function loadHistory(): Promise<HistoryEntry[]> {
  try {
    const v = await get<HistoryEntry[]>(KEY);
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

export async function pushHistory(entry: Omit<HistoryEntry, "id" | "at">): Promise<HistoryEntry[]> {
  const row: HistoryEntry = { ...entry, id: `h_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`, at: Date.now() };
  try {
    const existing = await loadHistory();
    // Collapse an immediate repeat of the same statement.
    const deduped = existing[0]?.statement.trim() === row.statement.trim() ? existing.slice(1) : existing;
    const next = [row, ...deduped].slice(0, MAX);
    await set(KEY, next);
    return next;
  } catch {
    return [row];
  }
}

export async function clearHistory(): Promise<void> {
  try {
    await set(KEY, []);
  } catch {
    /* ignore */
  }
}
