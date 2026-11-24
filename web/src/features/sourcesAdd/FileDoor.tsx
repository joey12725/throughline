/*
 * Drop a file and walk it to a live channel. Parse in the browser, infer a
 * mapping to the envelope, let it be edited, preview the real generated channel
 * page from the sample, then validate (dry-run) and ingest in idempotent
 * batches. The mapping is remembered by the file's column signature, so next
 * month's export is one click.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { UploadCloud, FileText } from "lucide-react";
import { useUiState } from "@/api";
import { EmptyState, Panel } from "@/ui";
import { parseFile, type ParseResult } from "./parse";
import {
  inferMapping,
  columnSignature,
  validateMapping,
  type Mapping,
} from "./envelope";
import { MappingForm } from "./MappingForm";
import { PreviewPanel } from "./PreviewPanel";
import { IngestRunner } from "./IngestRunner";
import styles from "./Add.module.css";

const MAX_BYTES = 25 * 1024 * 1024;

export function FileDoor(): ReactNode {
  const [parsed, setParsed] = useState<ParseResult | null>(null);
  const [filename, setFilename] = useState("");
  const [mapping, setMapping] = useState<Mapping | null>(null);
  const [signature, setSignature] = useState<string>("");
  const [parseError, setParseError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // The remembered mapping for this column set (server UI state, local fallback).
  const saved = useUiState<Mapping>("import_mapping", signature || "none");

  const ingest = async (file: File): Promise<void> => {
    setParseError(null);
    if (file.size > MAX_BYTES) {
      setParseError(`That file is ${(file.size / 1e6).toFixed(0)} MB — too large to parse in the browser. Use "Push over HTTP".`);
      return;
    }
    const text = await file.text();
    try {
      const result = parseFile(file.name, text);
      if (result.columns.length === 0 || result.table.rows.length === 0) {
        setParseError("No rows or columns found. Is this a table of records?");
        return;
      }
      const sig = columnSignature(result.table);
      setParsed(result);
      setFilename(file.name);
      setSignature(sig);
      const suggested = suggestName(file.name);
      setMapping(inferMapping(result.columns, suggested));
    } catch (e) {
      setParseError(`Could not parse that file: ${String((e as Error).message ?? e)}`);
    }
  };

  // Once a saved mapping for this signature loads, fold it in (keep the current
  // channel name if the user already typed one that differs from the default).
  const appliedFor = useRef<string>("");
  useEffect(() => {
    if (!parsed || !signature || saved.loading) return;
    if (appliedFor.current === signature) return;
    if (saved.value && saved.value.slots) {
      appliedFor.current = signature;
      setMapping((cur) => reconcile(saved.value as Mapping, parsed, cur));
    }
  }, [parsed, signature, saved.loading, saved.value]);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files?.[0];
    if (file) void ingest(file);
  }, []);

  const reset = (): void => {
    setParsed(null);
    setMapping(null);
    setSignature("");
    setParseError(null);
    appliedFor.current = "";
  };

  if (!parsed || !mapping) {
    return (
      <div
        className={styles.dropZone}
        data-over={dragOver}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
        }}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".csv,.tsv,.json,.ndjson,.jsonl,text/csv,application/json"
          className={styles.fileInput}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void ingest(f);
          }}
          data-testid="file-input"
        />
        <UploadCloud size={32} aria-hidden className={styles.dropIcon} />
        <p className={styles.dropTitle}>Drop a CSV, TSV, JSON or NDJSON file</p>
        <p className={styles.dropHint}>It is parsed here in your browser — nothing leaves until you import.</p>
        {parseError ? <p className={styles.dropError} role="alert">{parseError}</p> : null}
      </div>
    );
  }

  const problems = validateMapping(mapping);
  const blocking = problems.filter((p) => p.level === "error");

  return (
    <div className={styles.fileFlow}>
      <div className={styles.fileSummary}>
        <FileText size={16} aria-hidden />
        <span className={styles.fileName}>{filename}</span>
        <span className={styles.fileMeta}>
          {parsed.table.kind.toUpperCase()} · {parsed.table.rows.length.toLocaleString()} rows
          {parsed.table.truncated ? " (preview capped)" : ""} · {parsed.columns.length} columns
        </span>
        {appliedFor.current === signature && saved.value ? (
          <span className={styles.savedBadge}>saved mapping applied</span>
        ) : null}
        <button className={styles.resetBtn} onClick={reset}>
          Use a different file
        </button>
      </div>

      <div className={styles.twoCol}>
        <div className={styles.mapCol}>
          <MappingForm parsed={parsed} mapping={mapping} onChange={setMapping} problems={problems} />
        </div>
        <div className={styles.previewCol}>
          <Panel title="Preview" subtitle="The real generated channel page, built from your sample">
            {blocking.length > 0 ? (
              <EmptyState compact title="Finish the mapping" description="Map the required columns to see the preview." />
            ) : (
              <PreviewPanel mapping={mapping} parsed={parsed} />
            )}
          </Panel>
        </div>
      </div>

      <IngestRunner
        mapping={mapping}
        parsed={parsed}
        disabled={blocking.length > 0}
        onBeforeImport={() => void saved.set(mapping)}
      />
    </div>
  );
}

function suggestName(filename: string): string {
  const base = filename
    .replace(/\.[a-z0-9]+$/i, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 40);
  const safe = base && /^[a-z]/.test(base) ? base : `file_${base}`;
  return `import.${safe || "my_source"}`;
}

/** Fold a saved mapping onto freshly parsed columns, keeping only columns that
 *  still exist and re-inferring anything new. */
function reconcile(savedMapping: Mapping, parsed: ParseResult, current: Mapping | null): Mapping {
  const cols = new Set(parsed.columns.map((c) => c.name));
  const slots = Object.fromEntries(
    Object.entries(savedMapping.slots).filter(([, col]) => col && cols.has(col)),
  ) as Mapping["slots"];
  const fields = savedMapping.fields.filter((f) => cols.has(f.column));
  return {
    ...savedMapping,
    slots,
    fields,
    channelName: current?.channelName || savedMapping.channelName,
  };
}
