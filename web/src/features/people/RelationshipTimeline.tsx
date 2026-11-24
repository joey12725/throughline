/*
 * The person page's relationship-over-time panel: a source-coloured stacked area
 * of records per bucket, split by the channel each record came through, so you
 * read both the overall rhythm of a relationship and how it migrated across
 * sources over the years. It is a bespoke chart (Observable Plot) rather than the
 * generic timeseries lens, because the stack is the whole point — but it keeps a
 * view spec underneath, so the same SQL, the spec JSON and "Open in Query" are
 * one click away, and brushing the area sets the global time scope like any other
 * time axis. A sparse relationship degrades to a designed empty state.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import * as Plot from "@observablehq/plot";
import { Braces, Code2, Pin } from "lucide-react";
import { useSql } from "@/api";
import {
  Panel,
  IconButton,
  Popover,
  usePopover,
  CodeBlock,
  Skeleton,
  EmptyState,
  ErrorState,
  toast,
  useSourceColor,
} from "@/ui";
import { getState, putState } from "@/api/uiState";
import { compileView, type ViewSpec, type Scope } from "@/lens";
import { useChartColors } from "@/charts";
import { useScopes } from "@/store/scopes";
import styles from "./Person.module.css";

interface Row {
  bucket: string;
  series: string;
  n: number | string;
}

export function RelationshipTimeline({
  id,
  spec,
  scope,
  pluginOf,
  chIndex,
  height = 232,
}: {
  id: number;
  spec: ViewSpec;
  scope: Partial<Scope>;
  pluginOf: Map<string, string>;
  chIndex: Map<string, number>;
  height?: number;
}): ReactNode {
  const navigate = useNavigate();
  const { from, to, setRange } = useScopes();
  const { cssFor } = useSourceColor();
  const colors = useChartColors();

  const compiled = useMemo(() => {
    try {
      return compileView(spec, { from, to, ...scope });
    } catch (e) {
      return { sql: "", row_cap: spec.source.row_cap, grain: null as string | null, unresolved: [], error: e instanceof Error ? e.message : String(e) };
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec, from, to, JSON.stringify(scope)]);

  const q = useSql<Row>(compiled.sql || null, {
    rowCap: compiled.row_cap,
    priority: "visible",
    keyParts: ["rel-timeline", id, from ?? "", to ?? "", compiled.grain ?? ""],
  });

  const rows = q.data?.rows ?? [];
  const truncated = q.data?.truncated ?? false;

  // Normalise rows → points, and list the channels present (ordered by volume)
  // so the stack order and the legend agree.
  const { points, channels } = useMemo(() => {
    const totals = new Map<string, number>();
    const pts = rows
      .map((r) => {
        const t = Date.parse(r.bucket);
        const value = typeof r.n === "number" ? r.n : Number(r.n) || 0;
        if (!Number.isFinite(t)) return null;
        totals.set(r.series, (totals.get(r.series) ?? 0) + value);
        return { t: new Date(t), channel: r.series, value };
      })
      .filter((p): p is { t: Date; channel: string; value: number } => p != null);
    const chans = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([c]) => c);
    return { points: pts, channels: chans };
  }, [rows]);

  const colorFor = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of channels) m.set(c, cssFor(pluginOf.get(c) ?? c, chIndex.get(c) ?? 0));
    return m;
  }, [channels, cssFor, pluginOf, chIndex]);

  const sqlPop = usePopover();
  const specPop = usePopover();

  const openInQuery = (): void => {
    const params = new URLSearchParams();
    params.set("q", compiled.sql);
    params.set("spec", JSON.stringify(spec));
    navigate(`/query?${params.toString()}`);
  };
  const pin = async (): Promise<void> => {
    try {
      const existing = await getState<ViewSpec[]>("board", `person-${id}`).catch(() => null);
      const list = Array.isArray(existing?.value) ? existing!.value : [];
      await putState("board", `person-${id}`, [...list, spec]);
      toast.ok("Pinned");
    } catch {
      toast.error("Could not pin");
    }
  };

  const actions = (
    <div className={styles.panelActions}>
      <IconButton ref={sqlPop.anchorRef} size="sm" variant="ghost" label="Show SQL" onClick={sqlPop.toggle}>
        <Code2 size={15} />
      </IconButton>
      <IconButton ref={specPop.anchorRef} size="sm" variant="ghost" label="Show spec" onClick={specPop.toggle}>
        <Braces size={15} />
      </IconButton>
      <IconButton size="sm" variant="ghost" label="Pin to board" onClick={() => void pin()}>
        <Pin size={15} />
      </IconButton>
    </div>
  );

  return (
    <Panel as="article" title={spec.title} subtitle={spec.why} actions={actions}>
      <div style={{ height }}>
        {"error" in compiled && compiled.error ? (
          <ErrorState title="This panel could not be compiled" error={new Error(String(compiled.error))} />
        ) : q.isLoading ? (
          <Skeleton height="100%" radius={8} />
        ) : q.isError ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        ) : points.length === 0 ? (
          <EmptyState compact title="Not enough history" description="Too few records to draw a timeline for this relationship." />
        ) : (
          <StackedArea
            points={points}
            channels={channels}
            colorFor={colorFor}
            colors={colors}
            onBrush={(a, b) => setRange(a, b)}
          />
        )}
      </div>
      {points.length > 0 && (
        <div className={styles.timelineFoot}>
          <Legend channels={channels} colorFor={colorFor} />
          <span className={styles.timelineMeta}>
            {rows.length.toLocaleString("en-US")}
            {truncated ? "+" : ""} rows{compiled.grain ? ` · ${compiled.grain}` : ""}
          </span>
        </div>
      )}
      <Popover open={sqlPop.open} onClose={sqlPop.close} anchorRef={sqlPop.anchorRef} align="end" aria-label="Resolved statement">
        <div className={styles.pop}>
          <div className={styles.popTitle}>Resolved statement</div>
          <CodeBlock code={compiled.sql || "— could not compile —"} lang="sql" wrap />
          <div className={styles.popActions}>
            <button type="button" className={styles.popBtn} onClick={openInQuery}>
              Open in Query
            </button>
          </div>
        </div>
      </Popover>
      <Popover open={specPop.open} onClose={specPop.close} anchorRef={specPop.anchorRef} align="end" aria-label="View spec">
        <div className={styles.pop}>
          <div className={styles.popTitle}>View spec</div>
          <CodeBlock code={JSON.stringify(spec, null, 2)} lang="json" wrap />
        </div>
      </Popover>
    </Panel>
  );
}

/* ----------------------------------------------------- the stacked area itself */

interface Pt {
  t: Date;
  channel: string;
  value: number;
}

interface PlotNode extends HTMLElement {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  scale(name: string): any;
}

function StackedArea({
  points,
  channels,
  colorFor,
  colors,
  onBrush,
}: {
  points: Pt[];
  channels: string[];
  colorFor: Map<string, string>;
  colors: ReturnType<typeof useChartColors>;
  onBrush: (from: string, to: string) => void;
}): ReactNode {
  const host = useRef<HTMLDivElement>(null);
  const plotRef = useRef<PlotNode | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [sel, setSel] = useState<{ x0: number; x1: number } | null>(null);
  const drag = useRef<{ x0: number } | null>(null);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      setSize({ w: Math.floor(r.width), h: Math.floor(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const domain = useMemo(() => {
    const ts = points.map((p) => +p.t);
    return [new Date(Math.min(...ts)), new Date(Math.max(...ts))] as [Date, Date];
  }, [points]);

  // (Re)build the Plot figure whenever data or size changes.
  useEffect(() => {
    const el = host.current;
    if (!el || size.w <= 0 || size.h <= 0) return;
    const range = channels.map((c) => colorFor.get(c) ?? colors.tokens.ink3);
    const node = Plot.plot({
      width: size.w,
      height: size.h,
      marginLeft: 38,
      marginRight: 12,
      marginTop: 8,
      marginBottom: 22,
      style: { background: "transparent", color: colors.tokens.ink2, fontFamily: colors.tokens.fontUi, fontSize: "11px", overflow: "visible" },
      x: { type: "utc", domain, grid: false, ticks: 5, label: null },
      y: { grid: false, label: null, ticks: 4, tickFormat: "~s" },
      color: { domain: channels, range },
      marks: [
        Plot.areaY(points, {
          x: "t",
          y: "value",
          z: "channel",
          fill: "channel",
          order: channels,
          fillOpacity: 0.85,
          // A hairline in the panel colour outlines each band, so stacked layers
          // stay legible even when two channels share a source hue.
          stroke: colors.tokens.bg1,
          strokeWidth: 1,
          strokeOpacity: 0.85,
          curve: "monotone-x",
        }),
        Plot.ruleY([0], { stroke: colors.tokens.line, strokeWidth: 1 }),
      ],
    }) as unknown as PlotNode;
    el.replaceChildren(node);
    plotRef.current = node;
    return () => {
      plotRef.current = null;
      node.remove?.();
    };
  }, [points, channels, colorFor, colors.tokens, size.w, size.h, domain]);

  // Brush: drag across the area to set the global time scope. Events bubble up
  // from Plot's SVG so its own hover tip keeps working underneath.
  const pxToDate = (clientX: number): Date | null => {
    const node = plotRef.current;
    const el = host.current;
    if (!node || !el) return null;
    const r = el.getBoundingClientRect();
    const px = clientX - r.left;
    try {
      const sx = node.scale("x");
      if (sx && typeof sx.invert === "function") return sx.invert(px);
    } catch {
      /* fall through to linear */
    }
    const [lo, hi] = domain;
    const frac = (px - 38) / Math.max(1, size.w - 38 - 12);
    return new Date(+lo + Math.min(1, Math.max(0, frac)) * (+hi - +lo));
  };

  const onDown = (e: React.PointerEvent): void => {
    const el = host.current;
    if (!el) return;
    const x = e.clientX - el.getBoundingClientRect().left;
    drag.current = { x0: x };
    setSel({ x0: x, x1: x });
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
  };
  const onMove = (e: React.PointerEvent): void => {
    if (!drag.current || !host.current) return;
    const x = e.clientX - host.current.getBoundingClientRect().left;
    setSel({ x0: drag.current.x0, x1: x });
  };
  const onUp = (e: React.PointerEvent): void => {
    const d = drag.current;
    drag.current = null;
    setSel(null);
    if (!d) return;
    const x1 = e.clientX - (host.current?.getBoundingClientRect().left ?? 0);
    if (Math.abs(x1 - d.x0) < 5) return; // a click, not a brush
    const a = pxToDate(d.x0 + (host.current?.getBoundingClientRect().left ?? 0));
    const b = pxToDate(e.clientX);
    if (!a || !b) return;
    const lo = a < b ? a : b;
    const hi = a < b ? b : a;
    onBrush(lo.toISOString(), hi.toISOString());
  };

  return (
    <div className={styles.areaWrap} onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerLeave={() => { drag.current = null; setSel(null); }}>
      <div ref={host} className={styles.areaHost} style={{ width: "100%", height: "100%" }} />
      {sel && Math.abs(sel.x1 - sel.x0) > 2 && (
        <div
          className={styles.brushRect}
          style={{ left: Math.min(sel.x0, sel.x1), width: Math.abs(sel.x1 - sel.x0) }}
          aria-hidden="true"
        />
      )}
    </div>
  );
}

function Legend({ channels, colorFor }: { channels: string[]; colorFor: Map<string, string> }): ReactNode {
  return (
    <div className={styles.legend}>
      {channels.slice(0, 8).map((c) => (
        <span key={c} className={styles.legendItem}>
          <span className={styles.legendSwatch} style={{ background: colorFor.get(c) }} />
          {c}
        </span>
      ))}
    </div>
  );
}
