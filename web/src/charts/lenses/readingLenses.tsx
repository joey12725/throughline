/*
 * The reading lenses — reader, table, stat, json_tree — are React components, not
 * drawings. The table is virtualised with role-aware cells; the reader shows the
 * records in the form their archetype reads best; stat is a few large numbers;
 * json_tree is an outline of a JSON field merged over recent rows.
 */
import { useRef, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Sensitive, Stat } from "@/ui";
import type { RoleId } from "@/lens";
import { col1, colN, numAt, optStr, rawAt, strAt, timeMsAt, unitOf } from "../runtime/accessors";
import { clip, formatMeasure, humanLabel, tooltipTime } from "../runtime/format";
import { Cell, cellKind } from "./cells";
import { ChartEmpty } from "../primitives/parts";
import type { DrillEvent, LensRenderProps } from "../types";
import styles from "./reading.module.css";

function roleOf(p: LensRenderProps, name: string): RoleId | undefined {
  return p.roles?.[name]?.role;
}

/* ---- table ---------------------------------------------------------------- */

export function TableChart(p: LensRenderProps): ReactNode {
  const cols = colN(p.spec, "columns").filter((c) => p.columns.some((cc) => cc.name === c));
  const shown = cols.length ? cols : p.columns.map((c) => c.name);
  const idCol = p.spec.drill?.key ?? (shown.includes("id") ? "id" : undefined);
  const scrollRef = useRef<HTMLDivElement>(null);
  const rowH = 30;
  const v = useVirtualizer({ count: p.rows.length, getScrollElement: () => scrollRef.current, estimateSize: () => rowH, overscan: 12 });
  if (p.rows.length === 0) return <ChartEmpty message="No rows" />;
  const colMeta = shown.map((name) => {
    const c = p.columns.find((cc) => cc.name === name) ?? { name, type: "text" };
    return { name, kind: cellKind(c, roleOf(p, name)), unit: unitOf(p.spec, name) };
  });
  const gridTemplate = colMeta.map((c) => (c.kind === "text" ? "minmax(120px, 2fr)" : c.kind === "time" ? "140px" : "minmax(80px, 1fr)")).join(" ");
  return (
    <div className={styles.tableWrap} style={{ height: p.height }}>
      <div className={styles.tableHead} style={{ gridTemplateColumns: gridTemplate }}>
        {colMeta.map((c) => (
          <div key={c.name} className={styles.th} title={c.name}>
            {humanLabel(c.name)}
          </div>
        ))}
      </div>
      <div ref={scrollRef} className={styles.tableBody}>
        <div style={{ height: v.getTotalSize(), position: "relative" }}>
          {v.getVirtualItems().map((item) => {
            const row = p.rows[item.index]!;
            return (
              <div
                key={item.key}
                className={styles.tr}
                style={{ position: "absolute", top: 0, left: 0, width: "100%", height: rowH, transform: `translateY(${item.start}px)`, gridTemplateColumns: gridTemplate, cursor: idCol ? "pointer" : "default" }}
                onClick={idCol ? () => p.onDrill?.({ kind: "record", id: String(rawAt(row, idCol) ?? ""), channel: p.spec.provenance.channel }) : undefined}
              >
                {colMeta.map((c) => (
                  <div key={c.name} className={styles.td}>
                    <Cell value={rawAt(row, c.name)} kind={c.kind} unit={c.unit} />
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/* ---- reader --------------------------------------------------------------- */

export function ReaderChart(p: LensRenderProps): ReactNode {
  const variant = optStr(p.spec, "variant") ?? "cards";
  const idCol = col1(p.spec, "id");
  const timeCol = col1(p.spec, "time");
  const textCol = col1(p.spec, "text");
  const authorCol = col1(p.spec, "author");
  const titleCol = col1(p.spec, "title");
  const speakerCol = col1(p.spec, "speaker");
  const urlCol = col1(p.spec, "url");
  const actorCol = col1(p.spec, "actor");
  const statusCol = col1(p.spec, "status");
  const placeCol = col1(p.spec, "place");
  const startOffCol = col1(p.spec, "start_offset");

  const scrollRef = useRef<HTMLDivElement>(null);
  const v = useVirtualizer({ count: p.rows.length, getScrollElement: () => scrollRef.current, estimateSize: () => (variant === "document" ? 96 : variant === "feed" ? 64 : 84), overscan: 8 });
  if (p.rows.length === 0) return <ChartEmpty message="No records to read" />;

  return (
    <div ref={scrollRef} className={styles.reader} style={{ height: p.height }}>
      <div style={{ height: v.getTotalSize(), position: "relative" }}>
        {v.getVirtualItems().map((item) => {
          const row = p.rows[item.index]!;
          const text = textCol ? strAt(row, textCol) : "";
          const who = speakerCol ? strAt(row, speakerCol) : authorCol ? strAt(row, authorCol) : "";
          const title = titleCol ? strAt(row, titleCol) : "";
          const url = urlCol ? strAt(row, urlCol) : "";
          const actor = actorCol ? strAt(row, actorCol) : "";
          const status = statusCol ? strAt(row, statusCol) : "";
          const place = placeCol ? strAt(row, placeCol) : "";
          const tMs = timeCol ? timeMsAt(row, timeCol) : NaN;
          const off = startOffCol ? numAt(row, startOffCol) : NaN;
          const mine = /^(me|self|owner|outgoing|sent|you)$/i.test(who);
          const open = () => idCol && p.onDrill?.({ kind: "record", id: String(rawAt(row, idCol) ?? ""), channel: p.spec.provenance.channel });
          return (
            <div
              key={item.key}
              ref={v.measureElement}
              data-index={item.index}
              className={`${styles.record} ${styles[`rv_${variant}`] ?? ""} ${mine && variant === "bubbles" ? styles.mine : ""}`}
              style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${item.start}px)` }}
              onClick={open}
              role={idCol ? "button" : undefined}
              tabIndex={idCol ? 0 : undefined}
              onKeyDown={idCol ? (e) => { if (e.key === "Enter") open(); } : undefined}
            >
              <div className={styles.recordHead}>
                {Number.isFinite(off) && <span className={styles.offset}>{formatMeasure(off, "s")}</span>}
                {(who || actor) && (
                  <Sensitive>
                    <span className={styles.who}>{clip(who || actor, 40)}</span>
                  </Sensitive>
                )}
                {place && (
                  <Sensitive>
                    <span className={styles.place}>{clip(place, 28)}</span>
                  </Sensitive>
                )}
                {status && <span className={styles.recStatus}>{clip(status, 20)}</span>}
                {Number.isFinite(tMs) && <span className={styles.when} title={tooltipTime(tMs, p.tz ?? "UTC")}>{tooltipTime(tMs, p.tz ?? "UTC")}</span>}
              </div>
              {title && (
                <div className={styles.recTitle}>
                  <Sensitive>{clip(title, 140)}</Sensitive>
                </div>
              )}
              {text && (
                <div className={variant === "document" || variant === "transcript" ? styles.bodyRead : styles.body}>
                  <Sensitive>{clip(text, variant === "feed" ? 160 : variant === "document" ? 400 : 600)}</Sensitive>
                </div>
              )}
              {url && /^https?:\/\//i.test(url) && (
                <a className={styles.recUrl} href={url} target="_blank" rel="noreferrer noopener" onClick={(e) => e.stopPropagation()}>
                  {clip(url.replace(/^https?:\/\//, ""), 60)}
                </a>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ---- stat ----------------------------------------------------------------- */

export function StatChart(p: LensRenderProps): ReactNode {
  const values = colN(p.spec, "values").filter((c) => p.columns.some((cc) => cc.name === c));
  const agg = optStr(p.spec, "agg");
  if (p.rows.length === 0 || values.length === 0) return <ChartEmpty message="No totals" />;
  const compute = (col: string): number => {
    const nums = p.rows.map((r) => numAt(r, col)).filter(Number.isFinite);
    if (nums.length === 0) return NaN;
    if (agg === "avg") return nums.reduce((a, b) => a + b, 0) / nums.length;
    if (agg === "sum") return nums.reduce((a, b) => a + b, 0);
    return nums[0]!; // latest / single row
  };
  return (
    <div className={styles.statGrid} style={{ minHeight: p.height }}>
      {values.slice(0, 8).map((col) => {
        const n = compute(col);
        const unit = unitOf(p.spec, col);
        return <Stat key={col} label={humanLabel(col)} value={Number.isFinite(n) ? formatMeasure(n, unit) : "—"} size="lg" />;
      })}
    </div>
  );
}

/* ---- json_tree ------------------------------------------------------------ */

interface JsonNode {
  key: string;
  type: string;
  count: number;
  example?: string;
  children?: Map<string, JsonNode>;
}

export function JsonTreeChart(p: LensRenderProps): ReactNode {
  const valueCol = col1(p.spec, "value");
  const root = new Map<string, JsonNode>();
  let seen = 0;
  for (const r of p.rows.slice(0, 300)) {
    const raw = rawAt(r, valueCol);
    const parsed = parse(raw);
    if (parsed === undefined) continue;
    seen++;
    walk(parsed, root);
  }
  if (seen === 0) return <ChartEmpty message="No JSON to inspect" hint="This field held no object or list values in the sampled rows." />;
  const lines: ReactNode[] = [];
  render(root, 0, lines, seen);
  return (
    <div className={styles.json} style={{ height: p.height }}>
      <div className={styles.jsonInner}>{lines}</div>
    </div>
  );
}

function parse(raw: unknown): unknown {
  if (raw === null || raw === undefined) return undefined;
  if (typeof raw === "object") return raw;
  if (typeof raw === "string") {
    const s = raw.trim();
    if (s.startsWith("{") || s.startsWith("[")) {
      try {
        return JSON.parse(s);
      } catch {
        return undefined;
      }
    }
  }
  return undefined;
}

function typeOf(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  return typeof v;
}

function walk(value: unknown, into: Map<string, JsonNode>): void {
  if (Array.isArray(value)) {
    for (const el of value.slice(0, 20)) {
      if (el && typeof el === "object") walk(el, into);
      else bump(into, "[]", typeOf(el), el);
    }
    return;
  }
  if (value && typeof value === "object") {
    for (const [k, val] of Object.entries(value as Record<string, unknown>)) {
      const node = bump(into, k, typeOf(val), val);
      if (val && typeof val === "object") {
        node.children ??= new Map();
        walk(val, node.children);
      }
    }
  }
}

function bump(into: Map<string, JsonNode>, key: string, type: string, example: unknown): JsonNode {
  let n = into.get(key);
  if (!n) into.set(key, (n = { key, type, count: 0 }));
  n.count++;
  if (n.example === undefined && example !== null && typeof example !== "object") n.example = clip(String(example), 40);
  return n;
}

function render(map: Map<string, JsonNode>, depth: number, out: ReactNode[], total: number): void {
  const nodes = [...map.values()].sort((a, b) => b.count - a.count);
  for (const n of nodes) {
    const presence = Math.round((n.count / Math.max(1, total)) * 100);
    out.push(
      <div key={`${depth}-${n.key}-${out.length}`} className={styles.jsonRow} style={{ paddingLeft: depth * 14 + 2 }}>
        <span className={styles.jsonKey}>{n.key}</span>
        <span className={styles.jsonType}>{n.type}</span>
        {n.example !== undefined && (
          <Sensitive>
            <span className={styles.jsonExample}>{n.example}</span>
          </Sensitive>
        )}
        <span className={styles.jsonPresence}>{presence}%</span>
      </div>,
    );
    if (n.children && depth < 3) render(n.children, depth + 1, out, n.count);
  }
}
