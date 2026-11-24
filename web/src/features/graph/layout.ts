/*
 * A small deterministic force-directed layout, run to a fixed tick budget on the
 * main thread and then frozen. The shell's CSP allows workers, but a bounded
 * graph (depth ≤ 2, ≤ ~300 nodes) settles in a few milliseconds synchronously,
 * so there is no worker and no animation loop — we compute once and paint. Pure
 * (no DOM); unit-tested for determinism and convergence.
 */

export interface GNode {
  key: string; // `${nodeKind}:${id}`
  nodeKind: "entity" | "person"; // which table the id is in
  id: number;
  kind: string; // display type: entity kind ("place", …) or "person"
  label: string;
  weight: number; // degree / shared count, for node size
  focus?: boolean;
  expanded?: boolean;
}
export interface GEdge {
  source: string;
  target: string;
  weight: number;
  kind?: string; // link type, for colour
}
export interface Positioned {
  x: number;
  y: number;
}

/** A stable pseudo-random in [0,1) from a string, so layouts are repeatable. */
function hash01(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  // xorshift the avalanche a little
  h ^= h >>> 13;
  h = Math.imul(h, 0x5bd1e995);
  h ^= h >>> 15;
  return ((h >>> 0) % 100000) / 100000;
}

export interface LayoutOptions {
  width: number;
  height: number;
  iterations?: number;
  /** repulsion strength */
  charge?: number;
  /** ideal edge length */
  linkDistance?: number;
}

/**
 * Lay out nodes and edges in width×height. The focus node is pinned at the
 * centre; others start on a seeded ring and relax under repulsion, edge springs
 * and a gentle gravity. Returns a Map of key → {x,y}, all inside the box.
 */
export function layoutGraph(nodes: readonly GNode[], edges: readonly GEdge[], opts: LayoutOptions): Map<string, Positioned> {
  const { width, height } = opts;
  const n = nodes.length;
  // Larger sets need more relaxation to untangle; cap the budget for cost.
  const iterations = opts.iterations ?? Math.min(360, Math.max(180, Math.round(n * 2.2)));
  const charge = opts.charge ?? 1;
  const cx = width / 2;
  const cy = height / 2;
  const pos = new Map<string, { x: number; y: number; vx: number; vy: number }>();
  const hasFocus = nodes.some((nd) => nd.focus);
  const R = Math.min(width, height) * (hasFocus ? 0.36 : 0.46);

  nodes.forEach((node) => {
    if (node.focus) {
      pos.set(node.key, { x: cx, y: cy, vx: 0, vy: 0 });
      return;
    }
    // Area-uniform disc seeding (sqrt) spreads an overview evenly; a focused
    // neighbourhood keeps its ring look but still fills the disc.
    const a = hash01(node.key) * Math.PI * 2;
    const r = R * Math.sqrt(0.08 + 0.92 * hash01(node.key + "r"));
    pos.set(node.key, { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r, vx: 0, vy: 0 });
  });

  const idx = new Map(nodes.map((nd, i) => [nd.key, i]));
  const edgeList = edges.filter((e) => idx.has(e.source) && idx.has(e.target));
  const maxEdgeW = edgeList.reduce((m, e) => Math.max(m, e.weight), 1);

  const linkDist = opts.linkDistance ?? Math.max(44, Math.min(130, (R * 1.5) / Math.sqrt(Math.max(1, n))));
  // More nodes → stronger repulsion so hubs do not pile up.
  const repel = (hasFocus ? 2600 : 3400) * charge;

  for (let it = 0; it < iterations; it++) {
    const cool = 1 - it / iterations; // simulated annealing damping
    // Repulsion (O(n²); fine for ≤ ~300 nodes).
    for (let i = 0; i < n; i++) {
      const a = pos.get(nodes[i]!.key)!;
      for (let j = i + 1; j < n; j++) {
        const b = pos.get(nodes[j]!.key)!;
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) {
          // Deterministic jitter to break exact overlaps.
          dx = (hash01(nodes[i]!.key + nodes[j]!.key) - 0.5) * 2;
          dy = (hash01(nodes[j]!.key + nodes[i]!.key) - 0.5) * 2;
          d2 = dx * dx + dy * dy + 0.01;
        }
        const f = repel / d2;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }
    // Edge springs.
    for (const e of edgeList) {
      const a = pos.get(e.source)!;
      const b = pos.get(e.target)!;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const strength = 0.03 + 0.07 * (e.weight / maxEdgeW);
      const force = (d - linkDist) * strength;
      const fx = (dx / d) * force;
      const fy = (dy / d) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }
    // Gravity to centre + integrate.
    for (const node of nodes) {
      const p = pos.get(node.key)!;
      if (node.focus) {
        p.x = cx;
        p.y = cy;
        p.vx = 0;
        p.vy = 0;
        continue;
      }
      p.vx += (cx - p.x) * 0.012;
      p.vy += (cy - p.y) * 0.012;
      p.vx *= 0.82;
      p.vy *= 0.82;
      p.x += p.vx * cool;
      p.y += p.vy * cool;
      // Loose safety bound only — the canvas fits the natural extent to the box,
      // so nodes may settle outside the frame without being squashed.
      const lo = -width;
      p.x = Math.max(lo, Math.min(width * 2, p.x));
      p.y = Math.max(-height, Math.min(height * 2, p.y));
    }
  }

  const out = new Map<string, Positioned>();
  for (const [k, p] of pos) out.set(k, { x: p.x, y: p.y });
  return out;
}

/**
 * A transform that fits every laid-out position into the box with padding.
 * `k` is capped so a tiny graph (a lone node and one neighbour) is not blown up
 * to absurd size. Returned as the canvas's initial pan/zoom after each relayout.
 */
export function fitTransform(
  positions: Map<string, Positioned>,
  size: { width: number; height: number },
  pad = 56,
  maxK = 2.2,
): { k: number; tx: number; ty: number } {
  if (positions.size === 0 || size.width < 2 || size.height < 2) return { k: 1, tx: 0, ty: 0 };
  let minx = Infinity;
  let miny = Infinity;
  let maxx = -Infinity;
  let maxy = -Infinity;
  for (const p of positions.values()) {
    if (p.x < minx) minx = p.x;
    if (p.y < miny) miny = p.y;
    if (p.x > maxx) maxx = p.x;
    if (p.y > maxy) maxy = p.y;
  }
  const w = Math.max(1, maxx - minx);
  const h = Math.max(1, maxy - miny);
  const k = Math.max(0.05, Math.min((size.width - pad * 2) / w, (size.height - pad * 2) / h, maxK));
  const mx = (minx + maxx) / 2;
  const my = (miny + maxy) / 2;
  return { k, tx: size.width / 2 - mx * k, ty: size.height / 2 - my * k };
}

/** Merge new neighbour nodes/edges into an existing graph (expand on click). */
export function mergeGraph(
  base: { nodes: GNode[]; edges: GEdge[] },
  add: { nodes: GNode[]; edges: GEdge[] },
  cap = 300,
): { nodes: GNode[]; edges: GEdge[] } {
  const nodes = new Map(base.nodes.map((n) => [n.key, n]));
  for (const nd of add.nodes) {
    const prev = nodes.get(nd.key);
    if (prev) nodes.set(nd.key, { ...prev, ...nd, focus: prev.focus || nd.focus, expanded: prev.expanded || nd.expanded });
    else if (nodes.size < cap) nodes.set(nd.key, nd);
  }
  const edgeKey = (e: GEdge) => (e.source < e.target ? `${e.source}|${e.target}` : `${e.target}|${e.source}`);
  const edges = new Map(base.edges.map((e) => [edgeKey(e), e]));
  for (const e of add.edges) {
    if (!nodes.has(e.source) || !nodes.has(e.target)) continue;
    edges.set(edgeKey(e), e);
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}
