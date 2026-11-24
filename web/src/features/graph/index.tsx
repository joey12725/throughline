/*
 * Graph (/ui/graph): the whole link graph at a glance — the most-connected
 * entities and people laid out and fit to the frame, edges weighted by how many
 * messages two nodes share. Click a node to read it; Focus grows a depth-2
 * neighbourhood around it and fits again. An overview rail counts entities by
 * type and links by kind and lists the hubs; a search box jumps the focus to any
 * node. Everything is bounded (≤ 120 overview nodes, depth ≤ 2 on focus) and
 * derived from shape — no name is wired in.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Search, Network, ChevronLeft } from "lucide-react";
import { num, sql, useSql } from "@/api";
import { useChartColors } from "@/charts";
import { oklchStringToRgb } from "@/charts/primitives/colors";
import { EmptyState, ErrorState, Input, Panel, Skeleton } from "@/ui";
import GraphCanvas from "./GraphCanvas";
import NodePanel from "./NodePanel";
import { mergeGraph, type GEdge, type GNode } from "./layout";
import {
  entityKindsSql,
  linkKindsSql,
  mostConnectedSql,
  neighboursSql,
  overviewEdgesSql,
  overviewNodesSql,
  searchSql,
} from "./queries";
import styles from "./graph.module.css";

// Keep a stable kind order so colours never reshuffle as the graph grows.
const KIND_ORDER = ["person", "place", "organization", "work", "web_page", "event"];

interface NeighbourRow {
  node_kind: "entity" | "person";
  id: number;
  shared: number | string;
  label: string;
  kind: string;
}
interface ConnRow {
  kind: string;
  id: number;
  name: string;
  deg: number | string;
}
interface OverviewNodeRow {
  node_kind: "entity" | "person";
  id: number;
  label: string;
  kind: string;
  deg: number | string;
}
interface OverviewEdgeRow {
  ak: "entity" | "person";
  aid: number;
  bk: "entity" | "person";
  bid: number;
  w: number | string;
  rel: string;
}

/** Fetch a node's shared-message neighbours as graph nodes and star edges. */
async function fetchNeighbours(node: GNode): Promise<{ addNodes: GNode[]; addEdges: GEdge[] }> {
  const res = await sql<NeighbourRow>(neighboursSql(node.nodeKind, node.id));
  const addNodes: GNode[] = res.rows.map((r) => ({
    key: `${r.node_kind}:${r.id}`,
    nodeKind: r.node_kind,
    id: r.id,
    kind: r.kind,
    label: r.label,
    weight: num(r.shared) || 1,
  }));
  const addEdges: GEdge[] = res.rows.map((r) => ({ source: node.key, target: `${r.node_kind}:${r.id}`, weight: num(r.shared) || 1 }));
  return { addNodes, addEdges };
}

export default function GraphScreen() {
  const c = useChartColors("");
  const entityKinds = useSql<{ kind: string; n: number | string }>(entityKindsSql(), { priority: "visible", keyParts: ["gkinds"] });
  const linkKinds = useSql<{ type: string; n: number | string }>(linkKindsSql(), { priority: "background", keyParts: ["glinkkinds"] });
  const connected = useSql<ConnRow>(mostConnectedSql(), { priority: "visible", keyParts: ["gconn"] });

  // Overview: the whole network at a glance (nodes first, then the edges among
  // exactly those nodes).
  const ovNodes = useSql<OverviewNodeRow>(overviewNodesSql(), { priority: "visible", keyParts: ["govnodes"] });
  const ovIds = useMemo(() => {
    const rows = ovNodes.data?.rows ?? [];
    return {
      ent: rows.filter((r) => r.node_kind === "entity").map((r) => r.id),
      per: rows.filter((r) => r.node_kind === "person").map((r) => r.id),
    };
  }, [ovNodes.data]);
  const ovEdgeStmt = ovNodes.data?.rows.length ? overviewEdgesSql(ovIds.ent, ovIds.per) : null;
  const ovEdges = useSql<OverviewEdgeRow>(ovEdgeStmt, { priority: "visible", keyParts: ["govedges"], enabled: ovEdgeStmt != null });

  const [mode, setMode] = useState<"overview" | "focus">("overview");
  const [focusGraph, setFocusGraph] = useState<{ nodes: GNode[]; edges: GEdge[] }>({ nodes: [], edges: [] });
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [term, setTerm] = useState("");

  // Stable kind → colour map (categorical, not a source hue — these are types).
  const kindColor = useMemo(() => {
    const seen = new Set<string>(KIND_ORDER);
    for (const r of entityKinds.data?.rows ?? []) seen.add(r.kind);
    const kinds = [...KIND_ORDER, ...[...seen].filter((k) => !KIND_ORDER.includes(k))];
    const ramp = c.categoricalRgb(kinds.length);
    const map = new Map(kinds.map((k, i) => [k, ramp[i % ramp.length]!]));
    return (kind: string) => map.get(kind) ?? oklchStringToRgb(c.source(0));
  }, [entityKinds.data, c]);

  // The overview graph: keep nodes that share at least one edge (plus a floor of
  // hubs) so the canvas is a connected network, not a scatter of lone dots.
  const overviewGraph = useMemo<{ nodes: GNode[]; edges: GEdge[] }>(() => {
    const rows = ovNodes.data?.rows ?? [];
    const all: GNode[] = rows.map((r) => ({
      key: `${r.node_kind}:${r.id}`,
      nodeKind: r.node_kind,
      id: r.id,
      kind: r.kind,
      label: r.label,
      weight: num(r.deg) || 1,
    }));
    const present = new Set(all.map((n) => n.key));
    const edges: GEdge[] = (ovEdges.data?.rows ?? [])
      .map((e) => ({ source: `${e.ak}:${e.aid}`, target: `${e.bk}:${e.bid}`, weight: num(e.w) || 1, kind: e.rel }))
      .filter((e) => present.has(e.source) && present.has(e.target));
    const linked = new Set<string>();
    for (const e of edges) {
      linked.add(e.source);
      linked.add(e.target);
    }
    const connectedNodes = all.filter((n) => linked.has(n.key));
    const nodes = connectedNodes.length >= 40 ? connectedNodes : all;
    return { nodes, edges };
  }, [ovNodes.data, ovEdges.data]);

  const activeGraph = mode === "overview" ? overviewGraph : focusGraph;

  const legendKinds = useMemo(() => {
    const seen = new Set(activeGraph.nodes.map((n) => n.kind));
    return [...KIND_ORDER.filter((k) => seen.has(k)), ...[...seen].filter((k) => !KIND_ORDER.includes(k))];
  }, [activeGraph.nodes]);

  // Expand a node's neighbourhood in focus mode (click a node to grow it).
  const expandNode = useCallback(
    async (node: GNode, force = false) => {
      setSelectedKey(node.key);
      if (!force && expanded.has(node.key)) return;
      setBusy(true);
      try {
        const { addNodes, addEdges } = await fetchNeighbours(node);
        setFocusGraph((g) => mergeGraph({ nodes: g.nodes.map((n) => (n.key === node.key ? { ...n, expanded: true } : n)), edges: g.edges }, { nodes: addNodes, edges: addEdges }));
      } catch {
        /* leave the node unexpanded; marking it done avoids a retry loop */
      } finally {
        setExpanded((s) => new Set(s).add(node.key));
        setBusy(false);
      }
    },
    [expanded],
  );

  // Enter focus mode on a seed node and grow a depth-2 neighbourhood around it.
  const focusOn = useCallback((seed: GNode) => {
    setMode("focus");
    setBusy(true);
    setSelectedKey(seed.key);
    (async () => {
      const focus: GNode = { ...seed, focus: true };
      let g: { nodes: GNode[]; edges: GEdge[] } = { nodes: [focus], edges: [] };
      const exp = new Set<string>();
      const hop = async (n: GNode): Promise<GNode[]> => {
        try {
          const { addNodes, addEdges } = await fetchNeighbours(n);
          g = mergeGraph({ nodes: g.nodes.map((x) => (x.key === n.key ? { ...x, expanded: true } : x)), edges: g.edges }, { nodes: addNodes, edges: addEdges });
          exp.add(n.key);
          return addNodes;
        } catch {
          exp.add(n.key);
          return [];
        }
      };
      await hop(focus);
      const strongest = g.nodes.filter((x) => !x.focus).sort((a, b) => b.weight - a.weight).slice(0, 2);
      for (const s of strongest) await hop(s);
      setFocusGraph(g);
      setExpanded(exp);
      setBusy(false);
    })();
  }, []);

  const backToOverview = useCallback(() => {
    setMode("overview");
  }, []);

  // On the overview a click selects (reads) a node; in focus mode it expands.
  const onNodeClick = useCallback(
    (node: GNode) => {
      if (mode === "overview") setSelectedKey(node.key);
      else void expandNode(node);
    },
    [mode, expandNode],
  );

  const selected = activeGraph.nodes.find((n) => n.key === selectedKey) ?? null;

  // Search (debounced; names from data).
  const search = useSql<{ node_kind: "entity" | "person"; id: number; label: string; kind: string; deg: number | string }>(
    term.trim().length >= 2 ? searchSql(term.trim()) : null,
    { priority: "visible", keyParts: ["gsearch", term.trim()], enabled: term.trim().length >= 2 },
  );

  const ekMax = (entityKinds.data?.rows ?? []).reduce((m, r) => Math.max(m, num(r.n)), 1);
  const lkMax = (linkKinds.data?.rows ?? []).reduce((m, r) => Math.max(m, num(r.n)), 1);

  const loadingGraph = ovNodes.isLoading || (ovEdgeStmt != null && ovEdges.isLoading);

  // Resolve the primary action for the side panel by mode.
  const primary =
    selected == null
      ? null
      : mode === "overview"
        ? { label: "Focus", disabled: busy, onClick: () => focusOn(selected) }
        : { label: expanded.has(selected.key) ? "Expanded" : busy ? "…" : "Expand", disabled: expanded.has(selected.key) || busy, onClick: () => void expandNode(selected) };

  return (
    <div className={styles.screen}>
      <aside className={styles.overview}>
        <Panel title={<span className={styles.ovTitle}><Network size={14} /> Graph</span>} subtitle="Entities, people and the links between them" className={styles.ovPanel}>
          <div className={styles.searchWrap}>
            <Input lead={<Search size={14} />} value={term} onChange={(e) => setTerm(e.target.value)} placeholder="Find an entity or person…" aria-label="Search the graph" />
          </div>
          {term.trim().length >= 2 && (
            <ul className={styles.searchResults}>
              {search.isLoading && <li className={styles.muted}>Searching…</li>}
              {!search.isLoading && (search.data?.rows.length ?? 0) === 0 && <li className={styles.muted}>No matches.</li>}
              {(search.data?.rows ?? []).map((r) => (
                <li key={`${r.node_kind}:${r.id}`}>
                  <button type="button" className={styles.searchRow} onClick={() => { focusOn({ key: `${r.node_kind}:${r.id}`, nodeKind: r.node_kind, id: r.id, kind: r.kind, label: r.label, weight: num(r.deg) || 1, focus: true }); setTerm(""); }}>
                    <span className={styles.nodeSwatch} style={{ background: kindColor(r.kind) }} aria-hidden />
                    <span className={styles.searchLabel}>{r.label}</span>
                    <span className={styles.searchKind}>{r.kind}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}

          <h3 className={styles.sectionH}>Entities by type</h3>
          {entityKinds.isLoading && <Skeleton height={90} />}
          {entityKinds.error && <ErrorState error={entityKinds.error} />}
          <ul className={styles.barList}>
            {(entityKinds.data?.rows ?? []).map((r) => (
              <li key={r.kind} className={styles.barRow}>
                <span className={styles.nodeSwatch} style={{ background: kindColor(r.kind) }} aria-hidden />
                <span className={styles.barLabel}>{r.kind}</span>
                <span className={styles.barTrack}><span className={styles.barFill} style={{ width: `${(num(r.n) / ekMax) * 100}%`, background: kindColor(r.kind) }} /></span>
                <span className={styles.barN}>{num(r.n)}</span>
              </li>
            ))}
          </ul>

          <h3 className={styles.sectionH}>Links by kind</h3>
          {linkKinds.isLoading && <Skeleton height={90} />}
          <ul className={styles.barList}>
            {(linkKinds.data?.rows ?? []).slice(0, 10).map((r) => (
              <li key={r.type} className={styles.barRow}>
                <span className={styles.barLabel}>{r.type}</span>
                <span className={styles.barTrack}><span className={styles.barFill} style={{ width: `${(num(r.n) / lkMax) * 100}%`, background: c.tokens.patina }} /></span>
                <span className={styles.barN}>{num(r.n)}</span>
              </li>
            ))}
          </ul>

          <h3 className={styles.sectionH}>Most connected</h3>
          {connected.isLoading && <Skeleton height={120} />}
          <ul className={styles.connList}>
            {(connected.data?.rows ?? []).map((r) => (
              <li key={r.id}>
                <button type="button" className={styles.connRow} data-active={selectedKey === `entity:${r.id}`} onClick={() => focusOn({ key: `entity:${r.id}`, nodeKind: "entity", id: r.id, kind: r.kind, label: r.name, weight: num(r.deg) || 1, focus: true })}>
                  <span className={styles.nodeSwatch} style={{ background: kindColor(r.kind) }} aria-hidden />
                  <span className={styles.connName}>{r.name}</span>
                  <span className={styles.connDeg}>{num(r.deg)}</span>
                </button>
              </li>
            ))}
          </ul>
        </Panel>
      </aside>

      <div className={styles.graphArea}>
        <div className={styles.graphBar}>
          {mode === "focus" ? (
            <button type="button" className={styles.backBtn} onClick={backToOverview}>
              <ChevronLeft size={14} /> Overview
            </button>
          ) : (
            <span className={styles.barTitle}>Overview</span>
          )}
          <span className={styles.barCount}>{activeGraph.nodes.length} nodes · {activeGraph.edges.length} links</span>
        </div>

        {ovNodes.error ? (
          <div className={styles.center}><ErrorState error={ovNodes.error} /></div>
        ) : activeGraph.nodes.length === 0 ? (
          <div className={styles.center}>{loadingGraph ? <Skeleton height={200} /> : <EmptyState icon={<Network />} title="No entities to graph" description="This installation has no linked entities yet." />}</div>
        ) : (
          <GraphCanvas nodes={activeGraph.nodes} edges={activeGraph.edges} kindColor={kindColor} legendKinds={legendKinds} selectedKey={selectedKey} onSelect={onNodeClick} />
        )}
        {mode === "focus" && activeGraph.nodes.length === 1 && expanded.has(activeGraph.nodes[0]!.key) && (
          <div className={styles.loneHint}>This node shares no messages with another entity or person.</div>
        )}
      </div>

      <aside className={styles.side}>
        {selected ? (
          <NodePanel node={selected} kindColor={kindColor} primaryLabel={primary!.label} primaryDisabled={primary!.disabled} onPrimary={primary!.onClick} />
        ) : (
          <Panel title="No node selected" className={styles.sidePanel}>
            <EmptyState icon={<Network />} title="Pick a node" description="Click a node in the graph, or choose one from the overview." compact />
          </Panel>
        )}
      </aside>
    </div>
  );
}
