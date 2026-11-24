import { describe, expect, it } from "vitest";
import { fitTransform, layoutGraph, mergeGraph, type GEdge, type GNode } from "./layout";

function node(id: number, over: Partial<GNode> = {}): GNode {
  return { key: `entity:${id}`, nodeKind: "entity", id, kind: "place", label: `n${id}`, weight: 1, ...over };
}

describe("layoutGraph", () => {
  const nodes: GNode[] = [node(0, { focus: true, weight: 10 }), node(1), node(2), node(3), node(4)];
  const edges: GEdge[] = [
    { source: "entity:0", target: "entity:1", weight: 5 },
    { source: "entity:0", target: "entity:2", weight: 3 },
    { source: "entity:1", target: "entity:2", weight: 1 },
  ];

  it("pins the focus node at the centre", () => {
    const pos = layoutGraph(nodes, edges, { width: 400, height: 300 });
    expect(pos.get("entity:0")).toEqual({ x: 200, y: 150 });
  });
  it("fits every node into the padded box via fitTransform", () => {
    const size = { width: 400, height: 300 };
    const pos = layoutGraph(nodes, edges, size);
    const t = fitTransform(pos, size, 20);
    for (const n of nodes) {
      const p = pos.get(n.key)!;
      const sx = p.x * t.k + t.tx;
      const sy = p.y * t.k + t.ty;
      expect(sx).toBeGreaterThanOrEqual(-0.001);
      expect(sx).toBeLessThanOrEqual(400.001);
      expect(sy).toBeGreaterThanOrEqual(-0.001);
      expect(sy).toBeLessThanOrEqual(300.001);
    }
  });
  it("is deterministic for the same input", () => {
    const a = layoutGraph(nodes, edges, { width: 400, height: 300 });
    const b = layoutGraph(nodes, edges, { width: 400, height: 300 });
    for (const n of nodes) {
      expect(a.get(n.key)!.x).toBeCloseTo(b.get(n.key)!.x, 9);
      expect(a.get(n.key)!.y).toBeCloseTo(b.get(n.key)!.y, 9);
    }
  });
  it("separates two connected nodes to roughly the link distance, not on top of each other", () => {
    const pos = layoutGraph(nodes, edges, { width: 600, height: 600 });
    const p1 = pos.get("entity:1")!;
    const p2 = pos.get("entity:2")!;
    expect(Math.hypot(p1.x - p2.x, p1.y - p2.y)).toBeGreaterThan(5);
  });
});

describe("mergeGraph", () => {
  it("adds new nodes/edges and de-dups by key", () => {
    const base = { nodes: [node(0, { focus: true })], edges: [] as GEdge[] };
    const merged = mergeGraph(base, {
      nodes: [node(1), node(2)],
      edges: [{ source: "entity:0", target: "entity:1", weight: 2 }],
    });
    expect(merged.nodes).toHaveLength(3);
    expect(merged.edges).toHaveLength(1);
    const again = mergeGraph(merged, { nodes: [node(1)], edges: [{ source: "entity:1", target: "entity:0", weight: 9 }] });
    expect(again.nodes).toHaveLength(3); // no duplicate node
    expect(again.edges).toHaveLength(1); // same undirected edge
  });
  it("preserves the focus flag through a merge", () => {
    const base = { nodes: [node(0, { focus: true })], edges: [] as GEdge[] };
    const merged = mergeGraph(base, { nodes: [node(0)], edges: [] });
    expect(merged.nodes[0]!.focus).toBe(true);
  });
  it("respects the node cap", () => {
    const base = { nodes: [node(0)], edges: [] as GEdge[] };
    const add = { nodes: [...Array(50)].map((_, i) => node(i + 1)), edges: [] as GEdge[] };
    const merged = mergeGraph(base, add, 10);
    expect(merged.nodes.length).toBeLessThanOrEqual(10);
  });
});
