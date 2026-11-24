/*
 * The live completion catalogue as a hook. Channels come from the bootstrap; the
 * api function signatures from GET /v1/describe?subject=functions (cached for the
 * session); typed columns are fetched per view on demand (a zero-row validate) and
 * merged in. `getCatalogue` is a stable getter the editor's completion source reads
 * so new columns appear without re-creating the editor.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { request } from "@/api";
import { useBootstrap } from "@/api";
import { buildCatalogue, type Catalogue, type CatColumn, type DescribeFunction } from "./catalogue";
import { fetchViewColumns } from "./validateClient";

function useFunctions(): DescribeFunction[] {
  const q = useQuery({
    queryKey: ["describe", "functions"],
    queryFn: () => request<DescribeFunction[]>("/describe?subject=functions"),
    staleTime: Infinity,
    gcTime: Infinity,
    retry: 1,
  });
  return Array.isArray(q.data) ? q.data : [];
}

export interface CatalogueHandle {
  catalogue: Catalogue;
  getCatalogue: () => Catalogue;
  ensureColumns: (view: string) => void;
}

export function useCatalogue(): CatalogueHandle {
  const { data: boot } = useBootstrap();
  const functions = useFunctions();
  const [columns, setColumns] = useState<Record<string, CatColumn[]>>({});
  const inflight = useRef<Set<string>>(new Set());

  const catalogue = useMemo(
    () => buildCatalogue(boot ?? null, functions, columns),
    [boot, functions, columns],
  );

  const catRef = useRef(catalogue);
  catRef.current = catalogue;
  const getCatalogue = useCallback(() => catRef.current, []);

  const ensureColumns = useCallback(
    (view: string) => {
      if (!view || columns[view] || inflight.current.has(view)) return;
      inflight.current.add(view);
      fetchViewColumns(view)
        .then((cols) => {
          if (cols.length) setColumns((prev) => ({ ...prev, [view]: cols }));
        })
        .catch(() => {
          /* leave declared fields as the fallback */
        })
        .finally(() => inflight.current.delete(view));
    },
    [columns],
  );

  // Warm the columns of the few largest channels so completion is rich up front.
  useEffect(() => {
    for (const v of catalogue.views.slice(0, 6)) ensureColumns(v.view);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalogue.views.length]);

  return { catalogue, getCatalogue, ensureColumns };
}
