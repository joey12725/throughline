/*
 * <SpecView /> draws a lens over rows a caller already has — the query editor's
 * Chart tab reuses the rows in the browser rather than re-running. It infers
 * roles from the result so cells render by role, then hands off to LensChart.
 */
import { useMemo, type ReactNode } from "react";
import { inferRoles, profileResult, type Row, type SqlColumn, type ViewSpec } from "@/lens";
import { LensChart } from "./LensChart";

export interface SpecViewProps {
  spec: ViewSpec;
  rows: readonly Row[];
  columns: readonly SqlColumn[];
  truncated?: boolean;
  plugin?: string;
  grain?: string | null;
  height?: number;
}

export function SpecView({ spec, rows, columns, truncated = false, plugin, grain, height }: SpecViewProps): ReactNode {
  const roles = useMemo(() => {
    if (rows.length === 0 || columns.length === 0) return undefined;
    try {
      return inferRoles(profileResult(rows.slice(0, 1500) as never, columns as never, truncated));
    } catch {
      return undefined;
    }
  }, [rows, columns, truncated]);
  return <LensChart spec={spec} rows={rows} columns={columns} truncated={truncated} plugin={plugin} grain={grain} roles={roles} height={height} />;
}
