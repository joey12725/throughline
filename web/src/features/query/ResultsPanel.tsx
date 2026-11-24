/*
 * Results: tabbed Table / Chart / Profile / Execution / JSON with a toolbar carrying
 * the row count, elapsed time, a truncation banner, exporters (CSV/TSV/JSON), "copy
 * as CLI" and share-by-URL. The banner for a truncated result rides above every tab,
 * so no picture ever silently implies a complete dataset.
 */
import { useRef, useState, type ReactNode } from "react";
import { Copy, Download, Link2, Terminal, AlertTriangle } from "lucide-react";
import { Tabs, IconButton, EmptyState, ErrorState, Skeleton, Menu, usePopover, toast } from "@/ui";
import type { ViewSpec } from "@/lens";
import type { SqlResult } from "@/api/types";
import { ResultTable } from "./ResultTable";
import { ResultChart } from "./ResultChart";
import { ResultProfile } from "./ResultProfile";
import { ResultExecution } from "./ResultExecution";
import { ResultJson } from "./ResultJson";
import { toCsv, toTsv, toJson, toCliCommand } from "./exporters";
import styles from "./query.module.css";

type Tab = "table" | "chart" | "profile" | "execution" | "json";

async function copy(text: string, label: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    toast.ok(`${label} copied`);
  } catch {
    toast.error("Clipboard unavailable");
  }
}

export interface ResultsPanelProps {
  result: SqlResult | null;
  statement: string;
  ranStatement: string;
  running: boolean;
  error: unknown;
  ms: number | null;
  plugin?: string;
  recordChannel?: string | null;
  externalSpec?: ViewSpec | null;
  onShare: () => void;
  onRetry?: () => void;
}

export function ResultsPanel(props: ResultsPanelProps): ReactNode {
  const { result, running, error } = props;
  const [tab, setTab] = useState<Tab>("table");
  const exportPop = usePopover();
  const exportRef = useRef<HTMLButtonElement | null>(null);

  const tabs = [
    { value: "table" as const, label: "Table" },
    { value: "chart" as const, label: "Chart" },
    { value: "profile" as const, label: "Profile" },
    { value: "execution" as const, label: "Execution" },
    { value: "json" as const, label: "JSON" },
  ];

  const rowsText = result ? `${result.rowCount.toLocaleString("en-US")}${result.truncated ? "+" : ""} rows` : "";

  return (
    <div className={styles.results}>
      <div className={styles.resultsBar}>
        <Tabs items={tabs} value={tab} onChange={(v) => setTab(v as Tab)} />
        <div className={styles.resultsMeta}>
          {result && (
            <>
              <span className={styles.metaRows}>{rowsText}</span>
              {props.ms !== null && <span className={`tnum ${styles.metaMs}`}>{Math.round(props.ms)} ms</span>}
              {result.truncated && (
                <span className={styles.metaTrunc} title="The server capped this result">
                  <AlertTriangle size={12} aria-hidden /> truncated
                </span>
              )}
            </>
          )}
        </div>
        <div className={styles.resultsActions}>
          <IconButton ref={exportRef} size="sm" variant="ghost" label="Export" onClick={exportPop.toggle} disabled={!result}>
            <Download size={15} />
          </IconButton>
          <IconButton
            size="sm"
            variant="ghost"
            label="Copy as CLI command"
            onClick={() => void copy(toCliCommand(props.ranStatement || props.statement), "CLI command")}
            disabled={!props.ranStatement && !props.statement}
          >
            <Terminal size={15} />
          </IconButton>
          <IconButton size="sm" variant="ghost" label="Share by URL" onClick={props.onShare} disabled={!props.statement}>
            <Link2 size={15} />
          </IconButton>
        </div>
        <Menu
          open={exportPop.open}
          onClose={exportPop.close}
          anchorRef={exportRef}
          align="end"
          aria-label="Export result"
          entries={[
            { key: "csv", label: <span><Copy size={13} /> Copy CSV</span>, onSelect: () => { if (result) void copy(toCsv(result.rows, result.columns), "CSV"); } },
            { key: "tsv", label: <span><Copy size={13} /> Copy TSV</span>, onSelect: () => { if (result) void copy(toTsv(result.rows, result.columns), "TSV"); } },
            { key: "json", label: <span><Copy size={13} /> Copy JSON</span>, onSelect: () => { if (result) void copy(toJson(result.rows), "JSON"); } },
          ]}
        />
      </div>

      <div className={styles.resultsBody}>
        {running ? (
          <div className={styles.resultsLoading}>
            <Skeleton height={28} radius={6} />
            <Skeleton height={28} radius={6} />
            <Skeleton height={28} radius={6} />
            <Skeleton height={28} radius={6} />
          </div>
        ) : error ? (
          <ErrorState error={error} onRetry={props.onRetry} />
        ) : !result ? (
          <EmptyState title="No result yet" description="Write a statement and press Run (⌘⏎), or pick a question above." />
        ) : tab === "table" ? (
          <ResultTable result={result} recordChannel={props.recordChannel} />
        ) : tab === "chart" ? (
          <ResultChart result={result} statement={props.ranStatement} plugin={props.plugin} externalSpec={props.externalSpec} />
        ) : tab === "profile" ? (
          <ResultProfile result={result} />
        ) : tab === "execution" ? (
          <ResultExecution result={result} />
        ) : (
          <ResultJson result={result} />
        )}
      </div>
    </div>
  );
}
