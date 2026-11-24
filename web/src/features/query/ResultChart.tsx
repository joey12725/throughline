/*
 * The Chart tab: ranked lenses for this result from the lens engine, drawn over the
 * rows already in the browser (no re-run). A pick-a-lens strip switches lenses, and
 * "Save as board panel" pins the chosen spec to a board in UI state. A spec carried
 * in on a deep link or applied from the helper is offered first.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { BarChart3, Pin } from "lucide-react";
import { Button, EmptyState } from "@/ui";
import { toast } from "@/ui";
import { getState, putState } from "@/api";
import { SpecView } from "@/charts";
import { lensesForResult, profileResult, type LensCandidate, type ViewSpec } from "@/lens";
import type { SqlResult } from "@/api/types";
import styles from "./query.module.css";

export interface ResultChartProps {
  result: SqlResult;
  statement: string;
  plugin?: string;
  externalSpec?: ViewSpec | null;
}

export function ResultChart({ result, statement, plugin, externalSpec }: ResultChartProps): ReactNode {
  const rows = result.rows;
  const columns = result.columns;

  const candidates: LensCandidate[] = useMemo(() => {
    if (rows.length === 0 || columns.length === 0) return [];
    try {
      const profile = profileResult(rows.slice(0, 2000) as never, columns as never, result.truncated);
      return lensesForResult(profile, statement);
    } catch {
      return [];
    }
  }, [rows, columns, result.truncated, statement]);

  const options: { label: string; spec: ViewSpec; why?: string }[] = useMemo(() => {
    const list = candidates.map((c) => ({ label: c.spec.title || c.spec.lens, spec: c.spec, why: c.why }));
    if (externalSpec) return [{ label: "From link", spec: externalSpec, why: externalSpec.why }, ...list];
    return list;
  }, [candidates, externalSpec]);

  const [idx, setIdx] = useState(0);
  useEffect(() => setIdx(0), [options.length, statement]);

  if (rows.length === 0) {
    return <EmptyState icon={<BarChart3 size={22} />} title="No rows to chart" description="Run a query that returns rows, then the best lenses appear here." compact />;
  }
  if (options.length === 0) {
    return <EmptyState icon={<BarChart3 size={22} />} title="No lens fits this shape" description="The result has no obvious picture — the Table and Profile tabs may serve it better." compact />;
  }

  const chosen = options[Math.min(idx, options.length - 1)] as { label: string; spec: ViewSpec; why?: string };

  const save = async (): Promise<void> => {
    try {
      const existing = await getState<ViewSpec[]>("board", "default").catch(() => null);
      const list = Array.isArray(existing?.value) ? existing!.value : [];
      await putState("board", "default", [...list, chosen.spec]);
      toast.ok("Saved to board");
    } catch {
      toast.error("Could not save");
    }
  };

  return (
    <div className={styles.chartTab}>
      <div className={styles.lensStrip} role="tablist" aria-label="Lenses for this result">
        {options.map((o, i) => (
          <button
            key={`${o.spec.lens}-${i}`}
            type="button"
            role="tab"
            aria-selected={i === idx}
            className={`${styles.lensChip} ${i === idx ? styles.lensChipOn : ""}`}
            onClick={() => setIdx(i)}
            title={o.why}
          >
            {o.label}
          </button>
        ))}
        <div className={styles.lensStripSpacer} />
        <Button size="sm" variant="ghost" icon={<Pin size={14} />} onClick={() => void save()}>
          Save as board panel
        </Button>
      </div>
      <div className={styles.chartBody}>
        <SpecView
          key={`${chosen.spec.lens}-${idx}`}
          spec={chosen.spec}
          rows={rows}
          columns={columns}
          truncated={result.truncated}
          plugin={plugin}
        />
      </div>
      {chosen.why ? <p className={styles.chartWhy}>{chosen.why}</p> : null}
    </div>
  );
}
