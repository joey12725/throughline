/*
 * React hooks over the API. Feature authors use these rather than calling the
 * client directly, so caching, the SQL scheduler and the UI-state fallback all
 * behave consistently.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { loadBootstrap } from "./bootstrap";
import { longPollArrivals } from "./arrivals";
import { setWebStateAvailable, getState, putState, deleteState } from "./uiState";
import { sql, type SqlOptions } from "./sql";
import type { ArrivalsPage, Bootstrap, Row, SqlResult, UiStateKind } from "./types";

/* ---- Bootstrap ----------------------------------------------------------- */

export function useBootstrap(): UseQueryResult<Bootstrap> {
  return useQuery({
    queryKey: ["bootstrap"],
    queryFn: async ({ signal }) => {
      const b = await loadBootstrap(signal);
      setWebStateAvailable(b.features.web_state);
      return b;
    },
    staleTime: 5 * 60_000,
  });
}

/* ---- SQL ----------------------------------------------------------------- */

export interface UseSqlOptions extends SqlOptions {
  enabled?: boolean;
  /** stable extra key parts (e.g. scope params) so the cache keys correctly */
  keyParts?: ReadonlyArray<string | number | boolean | null>;
}

export function useSql<T = Row>(
  statement: string | null,
  opts: UseSqlOptions = {},
): UseQueryResult<SqlResult<T>> {
  const { enabled = true, keyParts = [], ...sqlOpts } = opts;
  return useQuery({
    queryKey: ["sql", statement, sqlOpts.rowCap ?? null, ...keyParts],
    enabled: enabled && !!statement,
    queryFn: ({ signal }) => sql<T>(statement as string, { ...sqlOpts, signal }),
  });
}

/* ---- UI state (server with localStorage fallback) ------------------------ */

export interface UiStateHandle<V> {
  value: V | undefined;
  loading: boolean;
  set: (value: V) => Promise<void>;
  remove: () => Promise<void>;
}

export function useUiState<V = unknown>(
  kind: UiStateKind,
  key: string,
  initial?: V,
): UiStateHandle<V> {
  const qc = useQueryClient();
  const queryKey = ["uiState", kind, key] as const;
  const query = useQuery({
    queryKey,
    queryFn: async () => {
      const entry = await getState<V>(kind, key);
      return entry ? entry.value : (initial ?? null);
    },
    staleTime: Infinity,
  });

  const set = useCallback(
    async (value: V) => {
      qc.setQueryData(queryKey, value);
      await putState(kind, key, value);
    },
    [qc, kind, key, queryKey],
  );

  const remove = useCallback(async () => {
    qc.setQueryData(queryKey, null);
    await deleteState(kind, key);
  }, [qc, kind, key, queryKey]);

  const raw = query.data;
  return {
    value: (raw === null ? undefined : (raw as V)) ?? initial,
    loading: query.isLoading,
    set,
    remove,
  };
}

/* ---- Arrivals long poll --------------------------------------------------- */

export interface ArrivalsState {
  page: ArrivalsPage | null;
  unsupported: boolean;
}

export function useArrivals(opts: { channels?: readonly string[]; enabled?: boolean }): ArrivalsState {
  const { channels, enabled = true } = opts;
  const [page, setPage] = useState<ArrivalsPage | null>(null);
  const [unsupported, setUnsupported] = useState(false);
  const channelsKey = channels?.join(",") ?? "";
  const latest = useRef<ArrivalsPage | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void longPollArrivals(
      (p) => {
        if (p.unsupported) {
          setUnsupported(true);
          return;
        }
        // Merge: newest rows first, keep a bounded window.
        const prev = latest.current;
        const merged: ArrivalsPage = prev
          ? { ...p, rows: [...p.rows, ...prev.rows].slice(0, 400) }
          : p;
        latest.current = merged;
        setPage(merged);
      },
      { channels, signal: controller.signal },
    );
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, channelsKey]);

  return { page, unsupported };
}
