/*
 * The preview IS the generated channel page. We build the engine's ChannelMeta
 * from the mapping, shape the sample rows exactly like the channel view, profile
 * them in the browser, and let the lens engine rank the pictures. The lenses
 * that draw the rows as they are render live over the sample with SpecView — no
 * server, no data leaving — so the person sees the real page before importing.
 * Aggregating lenses (which need the full data) are listed as what comes next.
 */
import { useMemo, type ReactNode } from "react";
import { analyzeResult, profileResult, type LensCandidate } from "@/lens";
import { SpecView } from "@/charts";
import { EmptyState } from "@/ui";
import type { ParseResult } from "./parse";
import { buildMessages, previewRows, type Mapping } from "./envelope";
import styles from "./Add.module.css";

export interface PreviewPanelProps {
  mapping: Mapping;
  parsed: ParseResult;
}

export function PreviewPanel({ mapping, parsed }: PreviewPanelProps): ReactNode {
  const model = useMemo(() => {
    const built = buildMessages(mapping, parsed.table.rows);
    const { rows, columns } = previewRows(mapping, built);
    if (rows.length === 0) return { rows, columns, live: [] as LensCandidate[], deferred: [] as LensCandidate[] };
    try {
      const profile = profileResult(rows as never, columns as never, parsed.table.truncated);
      const { candidates } = analyzeResult(profile, "");
      const live: LensCandidate[] = [];
      const deferred: LensCandidate[] = [];
      for (const c of candidates) {
        if (c.spec.provenance.statement === "wrapped") deferred.push(c);
        else live.push(c);
      }
      return { rows, columns, live, deferred };
    } catch {
      return { rows, columns, live: [] as LensCandidate[], deferred: [] as LensCandidate[] };
    }
  }, [mapping, parsed]);

  if (model.rows.length === 0) {
    return <EmptyState compact title="No valid rows yet" description="Every sample row was rejected — check the time and text columns." />;
  }

  const [hero, ...rest] = model.live;
  const grid = rest.slice(0, 4);

  return (
    <div className={styles.preview}>
      <p className={styles.previewMeta}>
        {model.rows.length.toLocaleString()} sample records · {model.columns.length} columns
      </p>

      {hero ? (
        <figure className={styles.heroFig}>
          <figcaption className={styles.lensCap}>
            <strong>{hero.spec.title}</strong>
            <span>{hero.why}</span>
          </figcaption>
          <SpecView
            spec={hero.spec}
            rows={model.rows}
            columns={model.columns}
            truncated={parsed.table.truncated}
            plugin="webimport"
            height={260}
          />
        </figure>
      ) : (
        <EmptyState compact title="Mapping complete" description="Import to see the full generated page." />
      )}

      {grid.length > 0 ? (
        <div className={styles.previewGrid}>
          {grid.map((c) => (
            <figure key={c.spec.title} className={styles.gridFig}>
              <figcaption className={styles.lensCap}>
                <strong>{c.spec.title}</strong>
                <span>{c.why}</span>
              </figcaption>
              <SpecView
                spec={c.spec}
                rows={model.rows}
                columns={model.columns}
                truncated={parsed.table.truncated}
                plugin="webimport"
                height={180}
              />
            </figure>
          ))}
        </div>
      ) : null}

      {model.deferred.length > 0 ? (
        <div className={styles.deferred}>
          <span className={styles.deferredLabel}>Also generated once imported, from the full channel:</span>
          <ul className={styles.deferredList}>
            {model.deferred.slice(0, 6).map((c) => (
              <li key={c.spec.title} title={c.why}>
                {c.spec.title}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
