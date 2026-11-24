/*
 * ego_graph on hand-written Canvas 2D, lazily loaded. The owner sits at the
 * centre; each person of the channel is a node sized by how much they appear and
 * placed on a ring by how recently (recent == close in). Edges run to the owner,
 * their weight the threads shared. Colour is the source hue. Clicking a node
 * drills to that person.
 *
 * (The brief named sigma + graphology; its WebGL canvas renders blank under the
 * software-GL review environment and pulls in ~440 KB, so a dependency-free
 * canvas graph is both verifiable and far lighter for this one-off relation
 * view. link_matrix — the other "relation" lens — is a canvas matrix already.)
 */
import { useRef, useState, type ReactNode } from "react";
import { col1, numAt, strAt, timeMsAt } from "../runtime/accessors";
import { clip, exactNumber, relativeShort } from "../runtime/format";
import { useCanvas } from "../runtime/hooks";
import { useChartColors } from "../primitives/colors";
import { ChartBox, ChartEmpty } from "../primitives/parts";
import { ChartTooltip, type TipState } from "../primitives/Tooltip";
import type { LensRenderProps } from "../types";

interface Person {
  id: string;
  label: string;
  weight: number;
  threads: number;
  recency: number;
}

export default function EgoGraph(p: LensRenderProps): ReactNode {
  const c = useChartColors(p.plugin);
  const nodeCol = col1(p.spec, "node");
  const labelCol = col1(p.spec, "label");
  const weightCol = col1(p.spec, "weight");
  const threadsCol = col1(p.spec, "threads");
  const recencyCol = col1(p.spec, "recency");
  const [tip, setTip] = useState<TipState | null>(null);
  const nodesRef = useRef<{ x: number; y: number; r: number; person: Person }[]>([]);

  const people: Person[] = p.rows
    .map((r) => ({
      id: strAt(r, nodeCol),
      label: labelCol ? strAt(r, labelCol) : strAt(r, nodeCol),
      weight: weightCol ? numAt(r, weightCol) : 1,
      threads: threadsCol ? numAt(r, threadsCol) : 0,
      recency: recencyCol ? timeMsAt(r, recencyCol) : NaN,
    }))
    .filter((d) => d.id)
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 60);
  if (people.length === 0) return <ChartEmpty message="No people attributed here" />;

  const maxW = Math.max(...people.map((d) => d.weight), 1);
  const now = p.now ?? Date.now();
  const recencies = people.map((d) => d.recency).filter(Number.isFinite);
  const oldest = recencies.length ? Math.min(...recencies) : now;

  const canvasRef = useCanvas(
    (ctx, size) => {
      const cx = size.width / 2;
      const cy = size.height / 2;
      const maxR = Math.min(size.width, size.height) / 2 - 34;
      const nodes: typeof nodesRef.current = [];
      // Edges first, under the nodes. Angle spreads people evenly; radius is
      // mostly the outer ring, pulled in a little for the most recent.
      ctx.lineCap = "round";
      people.forEach((d, i) => {
        const angle = (i / people.length) * Math.PI * 2 - Math.PI / 2;
        const rec = Number.isFinite(d.recency) ? (now - d.recency) / Math.max(1, now - oldest) : 0.6;
        const radius = maxR * (0.58 + 0.42 * rec);
        const x = cx + Math.cos(angle) * radius;
        const y = cy + Math.sin(angle) * radius;
        ctx.strokeStyle = c.tokens.line;
        ctx.globalAlpha = 0.5;
        ctx.lineWidth = Math.max(0.4, Math.min(2.4, d.threads / 3));
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(x, y);
        ctx.stroke();
        ctx.globalAlpha = 1;
        nodes.push({ x, y, r: 3 + 9 * Math.sqrt(d.weight / maxW), person: d });
      });
      // Person nodes.
      for (const nd of nodes) {
        ctx.fillStyle = c.sourceRgb(0, 0.9);
        ctx.strokeStyle = c.tokens.bg0;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(nd.x, nd.y, nd.r, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }
      // Owner at the centre, in brass.
      ctx.fillStyle = c.tokens.brass;
      ctx.beginPath();
      ctx.arc(cx, cy, 8, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = c.tokens.bg0;
      ctx.font = `600 9px ${c.tokens.fontUi}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("you", cx, cy);
      // Labels for the most active, where they fit.
      ctx.fillStyle = c.tokens.ink2;
      ctx.font = `10px ${c.tokens.fontUi}`;
      ctx.textBaseline = "middle";
      const strong = [...nodes].sort((a, b) => b.person.weight - a.person.weight).slice(0, 8);
      const placedY: Record<"l" | "r", number[]> = { l: [], r: [] };
      for (const nd of strong) {
        if (!nd.person.label) continue;
        const left = nd.x < cx;
        const side = left ? "l" : "r";
        if (placedY[side].some((y) => Math.abs(y - nd.y) < 12)) continue;
        placedY[side].push(nd.y);
        ctx.textAlign = left ? "right" : "left";
        ctx.fillText(clip(nd.person.label, 14), nd.x + (left ? -nd.r - 3 : nd.r + 3), nd.y);
      }
      nodesRef.current = nodes;
    },
    { width: p.width, height: p.height },
    [c.tokens, people, maxW, oldest],
  );

  const onMove = (e: React.PointerEvent): void => {
    const host = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const px = e.clientX - host.left;
    const py = e.clientY - host.top;
    let best: (typeof nodesRef.current)[number] | null = null;
    let bestD = Infinity;
    for (const nd of nodesRef.current) {
      const d = Math.hypot(px - nd.x, py - nd.y);
      if (d < nd.r + 6 && d < bestD) { bestD = d; best = nd; }
    }
    setTip(
      best
        ? {
            x: px,
            y: py,
            title: best.person.label || "person",
            rows: [
              { label: "records", value: exactNumber(best.person.weight), swatch: c.source(0) },
              { label: "threads", value: exactNumber(best.person.threads) },
              ...(Number.isFinite(best.person.recency) ? [{ label: "last seen", value: relativeShort(best.person.recency, now) }] : []),
            ],
          }
        : null,
    );
  };

  const onClick = (): void => {
    if (!tip) return;
    const hit = nodesRef.current.find((nd) => (nd.person.label || "person") === String(tip.title));
    if (hit) p.onDrill?.({ kind: "person", id: hit.person.id, label: hit.person.label });
  };

  return (
    <ChartBox width={p.width} height={p.height}>
      <div style={{ position: "relative", width: p.width, height: p.height }} onPointerMove={onMove} onPointerLeave={() => setTip(null)} onClick={onClick}>
        <canvas ref={canvasRef} role="img" aria-label="People around the owner, by volume and recency" style={{ cursor: "pointer" }} />
        <ChartTooltip tip={tip} width={p.width} height={p.height} />
      </div>
    </ChartBox>
  );
}
