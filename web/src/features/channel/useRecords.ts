/*
 * Keyset pagination over a channel view on (happened_at, id). Browse mode pages
 * by time through the raw view; search mode runs the server's own keyword route
 * (api.search_filtered) once and ranks. Both return rows under the same contract,
 * so the readers do not care which path produced them. The database does the
 * work; the browser only draws — no raw rows outside this reader path.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { sql, type Row as ApiRow } from "@/api";
import { lit, quoteIdent } from "@/lens";

export interface RecordsCursor {
  t: string;
  id: number | string;
}

export interface UseRecordsOptions {
  channel: string;
  view: string;
  columns: readonly string[];
  from: string | null;
  to: string | null;
  search?: string;
  /** newest first (default) or oldest first (agenda). */
  order?: "desc" | "asc";
  /** Seed the first page just before this instant (jump-to-date). */
  startBefore?: string | null;
  pageSize?: number;
  enabled?: boolean;
}

export interface RecordsPage {
  rows: ApiRow[];
  next: RecordsCursor | null;
  ranked: boolean;
  truncated: boolean;
}

export const ENVELOPE = [
  "id",
  "happened_at",
  "ended_at",
  "thread_id",
  "thread_no",
  "thread_title",
  "author_handle",
  "person_handle",
  "display_name",
  "content",
  "lat",
  "lon",
  "channel",
] as const;

/** Columns that exist on api.search_results (the keyword route's row shape). */
const SEARCHABLE = new Set<string>(ENVELOPE);

function selectList(columns: readonly string[], extra: readonly string[]): string {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const name of [...extra, ...columns]) {
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push(quoteIdent(name));
  }
  return out.join(", ");
}

function browseSql(o: UseRecordsOptions, cursor: RecordsCursor | null, limit: number): string {
  const cmp = o.order === "asc" ? ">" : "<";
  const dir = o.order === "asc" ? "ASC" : "DESC";
  const where: string[] = ["happened_at IS NOT NULL"];
  if (o.from) where.push(`happened_at >= ${lit.timestamp(o.from)}`);
  if (o.to) where.push(`happened_at < ${lit.timestamp(o.to)}`);
  if (cursor) {
    const t = lit.timestamp(cursor.t);
    const id = lit.int(cursor.id);
    where.push(`(happened_at ${cmp} ${t} OR (happened_at = ${t} AND id ${cmp} ${id}))`);
  }
  return [
    `SELECT ${selectList(o.columns, ENVELOPE)}`,
    `FROM ${o.view}`,
    `WHERE ${where.join(" AND ")}`,
    `ORDER BY happened_at ${dir}, id ${dir}`,
    `LIMIT ${lit.int(limit)}`,
  ].join("\n");
}

function searchSql(o: UseRecordsOptions, limit: number): string {
  const cols = o.columns.filter((c) => SEARCHABLE.has(c));
  const after = o.from ? lit.timestamp(o.from) : "NULL";
  const before = o.to ? lit.timestamp(o.to) : "NULL";
  return [
    `SELECT ${selectList(cols, ENVELOPE)}`,
    `FROM api.search_filtered(${lit.text(o.search)}, ${lit.int(limit)}, NULL, ${lit.text(o.channel)}, ${after}, ${before}, NULL)`,
  ].join("\n");
}

export function useRecords(o: UseRecordsOptions) {
  const pageSize = o.pageSize ?? 60;
  const searching = !!o.search && o.search.trim().length > 0;
  const seedCursor: RecordsCursor | null = o.startBefore ? { t: o.startBefore, id: o.order === "asc" ? 0 : Number.MAX_SAFE_INTEGER } : null;

  const query = useInfiniteQuery<RecordsPage>({
    queryKey: ["records", o.channel, o.from, o.to, o.order ?? "desc", o.startBefore ?? "", searching ? o.search : ""],
    enabled: o.enabled !== false,
    initialPageParam: seedCursor as unknown,
    staleTime: 60_000,
    queryFn: async ({ pageParam, signal }) => {
      if (searching) {
        const res = await sql<ApiRow>(searchSql(o, 200), { rowCap: 200, priority: "visible", signal });
        return { rows: res.rows as ApiRow[], next: null, ranked: true, truncated: res.truncated };
      }
      const cursor = (pageParam as RecordsCursor | null) ?? null;
      const res = await sql<ApiRow>(browseSql(o, cursor, pageSize + 1), { rowCap: pageSize + 1, priority: "visible", signal });
      const rows = res.rows.slice(0, pageSize) as ApiRow[];
      const more = res.rows.length > pageSize;
      const last = rows[rows.length - 1];
      const next: RecordsCursor | null =
        more && last ? { t: String(last.happened_at), id: (last.id as number | string) ?? 0 } : null;
      return { rows, next, ranked: false, truncated: false };
    },
    getNextPageParam: (lastPage) => lastPage.next ?? undefined,
  });

  const rows = query.data?.pages.flatMap((p) => p.rows) ?? [];
  const ranked = query.data?.pages[0]?.ranked ?? false;
  return {
    rows,
    ranked,
    fetchNext: query.fetchNextPage,
    hasNext: query.hasNextPage,
    isLoading: query.isLoading,
    isFetchingNext: query.isFetchingNextPage,
    isError: query.isError,
    error: query.error,
    refetch: query.refetch,
  };
}
