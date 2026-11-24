/*
 * The graph on hand-drawn Canvas 2D. Nodes are entities and people, coloured by
 * type and sized by connectivity; edges are shared-message co-occurrence, their
 * width the weight. The layout is computed once (layout.ts) and frozen — no
 * animation loop — then fit to the box so the whole network fills the frame.
 * Wheel/buttons zoom, drag pans, hover lights a node's neighbourhood and dims
 * the rest, a click selects. Both themes, DPR-aware, keyboard reachable.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Minus, Plus, Maximize2 } from "lucide-react";
import { useChartColors } from "@/charts";
import { useCanvas, useElementSize, useReducedMotion } from "@/charts/runtime/hooks";
import { fitTransform, layoutGraph, type GEdge, type GNode, type Positioned } from "./layout";
import styles from "./graph.module.css";

interface Props {
  nodes: GNode[];
  edges: GEdge[];
  kindColor: (kind: string) => string;
  selectedKey: string | null;
  onSelect: (node: GNode) => void;
  /** Entity kinds present, ordered, for the on-canvas legend. */
  legendKinds: string[];
  plugin?: string;
}

interface Hit {
  key: string;
  x: number;
  y: number;
  r: number;
  node: GNode;
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

export default function GraphCanvas({ nodes, edges, kindColor, selectedKey, onSelect, legendKinds, plugin = "" }: Props) {
  const c = useChartColors(plugin);
  const reduce = useReducedMotion();
  const [hostRef, size] = useElementSize<HTMLDivElement>();
  const [t, setT] = useState({ k: 1, tx: 0, ty: 0 });
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const [tip, setTip] = useState<{ x: number; y: number; node: GNode } | null>(null);
  const hitsRef = useRef<Hit[]>([]);
  const dragRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);

  const layoutKey = nodes.map((n) => n.key).join(",") + "|" + edges.length + "|" + Math.round(size.width) + "x" + Math.round(size.height);

  const positions = useMemo<Map<string, Positioned>>(() => {
    if (size.width < 2 || size.height < 2) return new Map();
    return layoutGraph(nodes, edges, { width: size.width, height: size.height });
    // layoutKey encodes nodes/edges/size; intentionally the only dep.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutKey]);

  // Fit the whole network to the box after every relayout (focus change, expand,
  // resize) — the required fit-to-view on every change.
  useEffect(() => {
    if (positions.size === 0) return;
    setT(fitTransform(positions, size));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutKey]);

  // Adjacency for neighbourhood highlighting on hover.
  const adj = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const e of edges) {
      if (!m.has(e.source)) m.set(e.source, new Set());
      if (!m.has(e.target)) m.set(e.target, new Set());
      m.get(e.source)!.add(e.target);
      m.get(e.target)!.add(e.source);
    }
    return m;
  }, [edges]);

  const highlight = useMemo<Set<string> | null>(() => {
    if (!hoverKey) return null;
    const s = new Set<string>([hoverKey]);
    for (const k of adj.get(hoverKey) ?? []) s.add(k);
    return s;
  }, [hoverKey, adj]);

  const maxW = nodes.reduce((m, n) => Math.max(m, n.weight), 1);
  const maxE = edges.reduce((m, e) => Math.max(m, e.weight), 1);
  const hasDirect = edges.some((e) => e.kind != null && e.kind !== "shared");
  const toScreen = (x: number, y: number): [number, number] => [x * t.k + t.tx, y * t.k + t.ty];
  const radiusOf = (w: number) => (4 + 10 * Math.sqrt(w / maxW)) * clamp(t.k, 0.75, 1.5);

  const canvasRef = useCanvas(
    (ctx, box) => {
      ctx.clearRect(0, 0, box.width, box.height);
      const dim = (key: string) => (highlight && !highlight.has(key) ? true : false);

      // Edges.
      ctx.lineCap = "round";
      for (const e of edges) {
        const a = positions.get(e.source);
        const b = positions.get(e.target);
        if (!a || !b) continue;
        const [ax, ay] = toScreen(a.x, a.y);
        const [bx, by] = toScreen(b.x, b.y);
        const touchesSel = selectedKey != null && (e.source === selectedKey || e.target === selectedKey);
        const touchesHover = hoverKey != null && (e.source === hoverKey || e.target === hoverKey);
        const faded = highlight ? !touchesHover : false;
        const direct = e.kind != null && e.kind !== "shared";
        if (touchesHover || touchesSel) ctx.strokeStyle = c.tokens.brass;
        else if (direct) ctx.strokeStyle = c.tokens.patina;
        else ctx.strokeStyle = c.tokens.line;
        ctx.globalAlpha = faded ? 0.05 : touchesHover ? 0.78 : touchesSel ? 0.62 : direct ? 0.5 : 0.3;
        ctx.lineWidth = (0.6 + 3 * Math.sqrt(e.weight / maxE)) * (touchesHover || touchesSel ? 1.25 : 1);
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;

      // Nodes.
      const hits: Hit[] = [];
      for (const nd of nodes) {
        const p = positions.get(nd.key);
        if (!p) continue;
        const [x, y] = toScreen(p.x, p.y);
        const r = radiusOf(nd.weight);
        const selected = nd.key === selectedKey;
        const isHover = nd.key === hoverKey;
        const faded = dim(nd.key) && !selected;
        ctx.globalAlpha = faded ? 0.22 : 1;
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fillStyle = kindColor(nd.kind);
        ctx.fill();
        if (selected || isHover) {
          ctx.lineWidth = selected ? 2.5 : 2;
          ctx.strokeStyle = c.tokens.brass;
          ctx.stroke();
        } else if (nd.focus) {
          ctx.lineWidth = 1.5;
          ctx.strokeStyle = c.tokens.ink2;
          ctx.stroke();
        } else {
          ctx.lineWidth = 1;
          ctx.strokeStyle = c.tokens.bg1;
          ctx.stroke();
        }
        hits.push({ key: nd.key, x, y, r, node: nd });
      }
      ctx.globalAlpha = 1;
      hitsRef.current = hits;

      // Labels: on hover, label the lit neighbourhood; otherwise the hubs plus
      // the selected node. De-collided against already-placed labels.
      ctx.font = `12px ${c.tokens.fontUi}`;
      ctx.textBaseline = "middle";
      const toLabel = highlight
        ? nodes.filter((n) => highlight.has(n.key)).sort((a, b) => b.weight - a.weight).slice(0, 18)
        : [...nodes]
            .sort((a, b) => (b.focus ? 1 : 0) - (a.focus ? 1 : 0) || b.weight - a.weight)
            .slice(0, 12);
      const forced = new Set<string>([selectedKey ?? "", hoverKey ?? ""]);
      const placed: [number, number][] = [];
      for (const nd of toLabel) {
        const p = positions.get(nd.key);
        if (!p) continue;
        const [x, y] = toScreen(p.x, p.y);
        if (x < -40 || x > box.width + 40 || y < 6 || y > box.height - 6) continue;
        const must = forced.has(nd.key);
        if (!must && placed.some(([px, py]) => Math.abs(px - x) < 108 && Math.abs(py - y) < 14)) continue;
        const r = radiusOf(nd.weight);
        const label = clip(nd.label || nd.kind);
        const w = ctx.measureText(label).width;
        ctx.fillStyle = c.tokens.bg1;
        ctx.globalAlpha = 0.72;
        ctx.fillRect(x + r + 2, y - 8, w + 6, 16);
        ctx.globalAlpha = 1;
        ctx.fillStyle = nd.key === selectedKey || nd.key === hoverKey ? c.tokens.brass : c.tokens.ink;
        ctx.fillText(label, x + r + 5, y);
        placed.push([x, y]);
      }
    },
    size,
    [nodes, edges, positions, t, selectedKey, hoverKey, highlight, c.tokens, c.theme],
  );

  const hitAt = (px: number, py: number): Hit | null => {
    let best: Hit | null = null;
    let bestD = Infinity;
    for (const h of hitsRef.current) {
      const d = Math.hypot(px - h.x, py - h.y);
      if (d < h.r + 6 && d < bestD) {
        bestD = d;
        best = h;
      }
    }
    return best;
  };

  const zoomAround = (factor: number, px = size.width / 2, py = size.height / 2) => {
    setT((prev) => {
      const k = clamp(prev.k * factor, 0.2, 5);
      const wx = (px - prev.tx) / prev.k;
      const wy = (py - prev.ty) / prev.k;
      return { k, tx: px - wx * k, ty: py - wy * k };
    });
  };

  const onWheel = (e: React.WheelEvent) => {
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    zoomAround(Math.pow(1.0015, -e.deltaY), e.clientX - host.left, e.clientY - host.top);
  };
  const onPointerDown = (e: React.PointerEvent) => {
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    dragRef.current = { x: e.clientX, y: e.clientY, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const px = e.clientX - host.left;
    const py = e.clientY - host.top;
    if (dragRef.current) {
      const dx = e.clientX - dragRef.current.x;
      const dy = e.clientY - dragRef.current.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) dragRef.current.moved = true;
      dragRef.current.x = e.clientX;
      dragRef.current.y = e.clientY;
      setT((prev) => ({ ...prev, tx: prev.tx + dx, ty: prev.ty + dy }));
      return;
    }
    const h = hitAt(px, py);
    setHoverKey(h ? h.key : null);
    setTip(h ? { x: px, y: py, node: h.node } : null);
  };
  const endDrag = (e: React.PointerEvent) => {
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    dragRef.current = null;
  };
  const onPointerLeave = () => {
    setHoverKey(null);
    setTip(null);
  };
  const onClick = (e: React.MouseEvent) => {
    if (dragRef.current?.moved) return;
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const h = hitAt(e.clientX - host.left, e.clientY - host.top);
    if (h) onSelect(h.node);
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "+" || e.key === "=") {
      zoomAround(1.2);
      e.preventDefault();
    } else if (e.key === "-" || e.key === "_") {
      zoomAround(1 / 1.2);
      e.preventDefault();
    } else if (e.key === "0") {
      if (positions.size) setT(fitTransform(positions, size));
      e.preventDefault();
    }
  };

  return (
    <div
      ref={hostRef}
      className={styles.graphHost}
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onPointerLeave={onPointerLeave}
      onClick={onClick}
      onKeyDown={onKeyDown}
      tabIndex={0}
      role="application"
      aria-label="Graph canvas. Arrow a node into view, press plus or minus to zoom, zero to fit. Click a node to read it."
      data-reduce={reduce ? "1" : undefined}
    >
      <canvas ref={canvasRef} className={styles.graphCanvas} style={{ cursor: tip ? "pointer" : dragRef.current ? "grabbing" : "grab" }} />

      <div className={styles.zoomBar} role="group" aria-label="Zoom">
        <button type="button" className={styles.zoomBtn} aria-label="Zoom in" onClick={() => zoomAround(1.25)}><Plus size={15} /></button>
        <button type="button" className={styles.zoomBtn} aria-label="Zoom out" onClick={() => zoomAround(1 / 1.25)}><Minus size={15} /></button>
        <button type="button" className={styles.zoomBtn} aria-label="Fit to view" onClick={() => positions.size && setT(fitTransform(positions, size))}><Maximize2 size={14} /></button>
      </div>

      {legendKinds.length > 0 && (
        <div className={styles.legend} aria-hidden>
          <div className={styles.legendRow}>
            {legendKinds.map((k) => (
              <span key={k} className={styles.legendItem}>
                <span className={styles.legendDot} style={{ background: kindColor(k) }} />
                {k}
              </span>
            ))}
          </div>
          <div className={styles.legendRow}>
            <span className={styles.legendItem}>
              <span className={styles.legendLine} style={{ background: c.tokens.line }} /> shared messages
            </span>
            {hasDirect && (
              <span className={styles.legendItem}>
                <span className={styles.legendLine} style={{ background: c.tokens.patina }} /> direct link
              </span>
            )}
          </div>
          <div className={styles.legendNote}>line weight = strength · dot size = total links</div>
        </div>
      )}

      {tip && (
        <div className={styles.graphTip} style={{ left: Math.min(tip.x + 14, size.width - 190), top: Math.min(tip.y + 14, size.height - 58) }}>
          <strong>{tip.node.label || tip.node.kind}</strong>
          <span>{tip.node.kind} · {tip.node.weight.toLocaleString("en-US")} {tip.node.focus ? "links" : "shared"}</span>
        </div>
      )}
    </div>
  );
}

function clip(s: string, n = 22): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
