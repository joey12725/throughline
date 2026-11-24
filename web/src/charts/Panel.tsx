/*
 * <Panel spec /> — a view spec made live. It resolves the global scopes into the
 * statement (compileView), runs it through the API scheduler (abortable, cached),
 * shows the title, the "why" caption and a provenance badge, and carries the
 * three corner controls on every panel: SQL (the resolved statement, copy, open
 * in the editor), Spec (the JSON) and Pin (save to a board in UI state). The lens
 * itself is drawn by LensChart.
 */
import { useMemo, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Braces, Code2, Pin } from "lucide-react";
import { Panel as Surface, IconButton, Popover, usePopover, CodeBlock, toast, Skeleton, ErrorState } from "@/ui";
import { useBootstrap, useSql } from "@/api";
import { compileView, inferRoles, profileResult, type Scope, type ViewSpec, type RoleMap } from "@/lens";
import { useScopes } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import { getState, putState } from "@/api/uiState";
import { LensChart } from "./LensChart";
import { ProvenanceBadge } from "./ProvenanceBadge";
import styles from "./Panel.module.css";

export interface ChartPanelProps {
  spec: ViewSpec;
  /** Fixed body height; otherwise the panel grows with its content area. */
  height?: number;
  /** Extra scope beyond the global one (e.g. a fixed :person on the People screen). */
  scope?: Partial<Scope>;
  /** Board this panel pins into (UI state key). */
  board?: string;
  className?: string;
  priority?: "visible" | "normal" | "background";
}

export function ChartPanel({ spec, height = 280, scope, board = "default", className, priority = "visible" }: ChartPanelProps): ReactNode {
  const navigate = useNavigate();
  const { from, to } = useScopes();
  const tz = usePrefs((s) => s.tz);
  const { data: boot } = useBootstrap();

  const channel = spec.provenance.channel ? boot?.channels.find((c) => c.name === spec.provenance.channel) : undefined;
  const plugin = channel?.plugin;
  const coverage = channel
    ? { from: channel.coverage?.covered_from ?? channel.first_at, to: channel.coverage?.covered_to ?? channel.last_at }
    : null;

  const compiled = useMemo(() => {
    try {
      return compileView(spec, { from, to, tz, channel: spec.provenance.channel ?? null, ...scope });
    } catch (e) {
      return { sql: "", row_cap: spec.source.row_cap, grain: null as string | null, unresolved: [], error: e instanceof Error ? e.message : String(e) };
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec, from, to, tz, JSON.stringify(scope)]);

  const query = useSql(compiled.sql || null, { rowCap: compiled.row_cap, priority, keyParts: [from ?? "", to ?? "", tz, compiled.grain ?? ""] });

  const rows = query.data?.rows ?? [];
  const columns = query.data?.columns ?? [];
  const truncated = query.data?.truncated ?? false;
  const approval = query.data?.execution?.status === "approval_required";

  const roles: RoleMap | undefined = useMemo(() => {
    if (rows.length === 0 || columns.length === 0) return undefined;
    try {
      return inferRoles(profileResult(rows.slice(0, 1500) as never, columns as never, truncated));
    } catch {
      return undefined;
    }
  }, [rows, columns, truncated]);

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
      const existing = await getState<ViewSpec[]>("board", board).catch(() => null);
      const list = Array.isArray(existing?.value) ? existing!.value : [];
      await putState("board", board, [...list, spec]);
      toast.ok(`Pinned to “${board}”`);
    } catch {
      toast.error("Could not pin");
    }
  };

  const actions = (
    <div className={styles.controls}>
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
    <Surface
      as="article"
      className={className}
      title={spec.title}
      subtitle={<span className={styles.why}>{spec.why}</span>}
      actions={actions}
      footer={
        <div className={styles.footer}>
          <ProvenanceBadge spec={spec} sampled={spec.provenance.sampled} truncated={truncated} />
          <span className={styles.count}>
            {query.isLoading ? "…" : `${rows.length.toLocaleString("en-US")}${truncated ? "+" : ""} rows`}
            {compiled.grain ? ` · ${compiled.grain}` : ""}
          </span>
        </div>
      }
    >
      <div className={styles.body} style={{ height }}>
        {"error" in compiled && compiled.error ? (
          <ErrorState title="This panel could not be compiled" error={new Error(String(compiled.error))} />
        ) : approval ? (
          <div className={styles.gate}>This statement hit the cost gate. Approve it in the query editor to run it.</div>
        ) : query.isLoading ? (
          <Skeleton height="100%" radius={8} />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : (
          <LensChart spec={spec} rows={rows} columns={columns} truncated={truncated} plugin={plugin} grain={compiled.grain} roles={roles} coverage={coverage} height={height} />
        )}
      </div>

      <Popover open={sqlPop.open} onClose={sqlPop.close} anchorRef={sqlPop.anchorRef} align="end" aria-label="Resolved statement">
        <div className={styles.pop}>
          <div className={styles.popTitle}>Resolved statement</div>
          <CodeBlock code={compiled.sql || "— could not compile —"} lang="sql" wrap />
          <div className={styles.popActions}>
            <button type="button" className={styles.popBtn} onClick={openInQuery}>Open in Query</button>
          </div>
        </div>
      </Popover>
      <Popover open={specPop.open} onClose={specPop.close} anchorRef={specPop.anchorRef} align="end" aria-label="View spec">
        <div className={styles.pop}>
          <div className={styles.popTitle}>View spec</div>
          <CodeBlock code={JSON.stringify(spec, null, 2)} lang="json" wrap />
        </div>
      </Popover>
    </Surface>
  );
}
