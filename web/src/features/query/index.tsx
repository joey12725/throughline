/*
 * The Query screen: a catalogue rail, a CodeMirror editor with a live helper, and a
 * results surface that renders every result as a table, a chart, a profile, the
 * server's execution envelope and raw JSON. The empty state is a set of real
 * questions the lens engine derives from the archive — the "easy to answer questions"
 * path. URL state round-trips (?q=…&run=1), and ChartPanel's "Open in Query" deep
 * link is honoured exactly.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { Play, Sparkles, PanelRightOpen } from "lucide-react";
import { Button, IconButton, Split, Tooltip, toast } from "@/ui";
import { sql, useBootstrap, putState } from "@/api";
import { ApiError } from "@/api/errors";
import { useScopes } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import { registerCommand } from "@/app/commands";
import type { ViewSpec } from "@/lens";
import type { SqlResult } from "@/api/types";
import { SqlEditor, type SqlEditorHandle } from "./SqlEditor";
import { ResultsPanel } from "./ResultsPanel";
import { SchemaRail } from "./rail/SchemaRail";
import { HelperPanel, type HelperPanelHandle } from "./helper/HelperPanel";
import { Starters } from "./Starters";
import { ParametersBar } from "./ParametersBar";
import { useCatalogue } from "./useCatalogue";
import { resolveStatement, statementParams, parseBarValue, isGlobalParam } from "./params";
import type { ParamValue } from "@/lens";
import { readQueryUrl, shareQueryString } from "./urlState";
import { pushHistory } from "./history";
import styles from "./query.module.css";

const EDITOR_HINT = "SELECT … FROM api.ch_…  —  ⌘⏎ to run, ⌘I for the helper";

export default function QueryScreen(): ReactNode {
  const { data: boot } = useBootstrap();
  const { from, to } = useScopes();
  const tz = usePrefs((s) => s.tz);
  const location = useLocation();
  const { getCatalogue, catalogue, ensureColumns } = useCatalogue();

  const editorRef = useRef<SqlEditorHandle | null>(null);
  const helperRef = useRef<HelperPanelHandle | null>(null);

  const [statement, setStatement] = useState("");
  const [selection, setSelection] = useState("");
  const [paramText, setParamText] = useState<Record<string, string>>({});
  const [result, setResult] = useState<SqlResult | null>(null);
  const [ranStatement, setRanStatement] = useState("");
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<{ code: string | null; message: string } | null>(null);
  const [rawError, setRawError] = useState<unknown>(null);
  const [ms, setMs] = useState<number | null>(null);
  const [externalSpec, setExternalSpec] = useState<ViewSpec | null>(null);
  const [helperOpen, setHelperOpen] = useState(false);
  // Start the schema rail collapsed on phones so the editor gets the width.
  const [railCollapsed, setRailCollapsed] = useState(
    () => typeof window !== "undefined" && !!window.matchMedia?.("(max-width: 720px)").matches,
  );
  const [historyVersion, setHistoryVersion] = useState(0);

  const scope = useMemo(() => ({ from, to, tz }), [from, to, tz]);

  // Parameter overrides from the bar (typed best-effort).
  const overrides = useMemo<Record<string, ParamValue>>(() => {
    const out: Record<string, ParamValue> = {};
    for (const [name, raw] of Object.entries(paramText)) {
      if (raw.trim() !== "") out[name] = parseBarValue(raw);
    }
    return out;
  }, [paramText]);

  const resolved = useMemo(() => resolveStatement(statement, scope, overrides), [statement, scope, overrides]);
  const allParams = useMemo(() => statementParams(statement), [statement]);
  const resolvedParamNames = useMemo(
    () => new Set(allParams.filter((p) => !resolved.unresolved.includes(p))),
    [allParams, resolved.unresolved],
  );
  // The bar asks for every unresolved parameter (global ones the scope didn't fill, too).
  const barParams = resolved.unresolved;

  // The channel (and plugin) this result's records belong to, if the ran statement
  // references exactly one ch_* view.
  const { recordChannel, plugin } = useMemo(() => {
    const re = /api\.(ch_[a-z0-9_]+)/gi;
    const found = new Set<string>();
    let m: RegExpExecArray | null;
    while ((m = re.exec(ranStatement)) !== null) found.add(`api.${(m[1] as string).toLowerCase()}`);
    if (found.size !== 1) return { recordChannel: null as string | null, plugin: undefined as string | undefined };
    const view = [...found][0];
    const v = catalogue.views.find((x) => x.view.toLowerCase() === view);
    return { recordChannel: v?.channel ?? null, plugin: v?.plugin };
  }, [ranStatement, catalogue.views]);

  // Warm typed columns for views referenced in the editor.
  useEffect(() => {
    const re = /api\.(ch_[a-z0-9_]+)/gi;
    let m: RegExpExecArray | null;
    while ((m = re.exec(statement)) !== null) {
      const v = catalogue.views.find((x) => x.view.toLowerCase() === `api.${(m![1] as string).toLowerCase()}`);
      if (v) ensureColumns(v.view);
    }
  }, [statement, catalogue.views, ensureColumns]);

  const runStatement = useCallback(
    async (target: string): Promise<void> => {
      const r = resolveStatement(target, { from, to, tz }, overrides);
      if (!r.sql.trim()) return;
      if (r.unresolved.length) {
        toast.warn("Set parameters first", `Unresolved: ${r.unresolved.map((p) => `:${p}`).join(", ")}`);
        return;
      }
      setRunning(true);
      setRunError(null);
      setRawError(null);
      const t0 = performance.now();
      try {
        const res = await sql(r.sql, { priority: "visible" });
        const elapsed = performance.now() - t0;
        setResult(res);
        setRanStatement(target);
        setMs(elapsed);
        void pushHistory({ statement: target, rowCount: res.rowCount, ms: elapsed, ok: true });
        setHistoryVersion((v) => v + 1);
      } catch (err) {
        const elapsed = performance.now() - t0;
        const code = err instanceof ApiError ? err.code : null;
        const message = err instanceof Error ? err.message : String(err);
        setRunError({ code, message });
        setRawError(err);
        setResult(null);
        setRanStatement(target);
        setMs(elapsed);
        void pushHistory({ statement: target, rowCount: null, ms: elapsed, ok: false, error: message });
        setHistoryVersion((v) => v + 1);
      } finally {
        setRunning(false);
      }
    },
    [from, to, tz, overrides],
  );

  const run = useCallback(() => void runStatement(statement), [runStatement, statement]);
  const runSelection = useCallback(
    (sel: string) => void runStatement(sel || statement),
    [runStatement, statement],
  );

  const loadStatement = useCallback((next: string, spec?: ViewSpec | null, autoRun = false) => {
    setStatement(next);
    setExternalSpec(spec ?? null);
    if (autoRun) void runStatementRef.current(next);
  }, []);
  // Keep a stable ref so loadStatement can run the latest resolver.
  const runStatementRef = useRef(runStatement);
  runStatementRef.current = runStatement;

  const openHelper = useCallback(() => {
    setHelperOpen(true);
    requestAnimationFrame(() => helperRef.current?.focusInput());
  }, []);

  const onInsert = useCallback((text: string) => editorRef.current?.insert(text), []);

  const onStarter = useCallback(
    (stmt: string, spec: ViewSpec) => {
      setStatement(stmt);
      setExternalSpec(spec);
      void runStatementRef.current(stmt);
    },
    [],
  );

  const saveCurrent = useCallback(() => {
    const s = (editorRef.current?.getValue() ?? statement).trim();
    if (!s) return;
    const title = window.prompt("Name this query", s.slice(0, 40).replace(/\s+/g, " "));
    if (title === null) return;
    const key = `q_${Date.now()}`;
    void putState("saved_query", key, { title: title || "Untitled query", statement: s, spec: externalSpec, created_at: new Date().toISOString() })
      .then(() => toast.ok("Saved"))
      .catch(() => toast.error("Could not save"));
  }, [statement, externalSpec]);

  const share = useCallback(async () => {
    const s = statement.trim();
    if (!s) return;
    try {
      const qs = await shareQueryString(s, true);
      const url = `${location.pathname}?${qs}`;
      const full = `${window.location.origin}${url}`;
      await navigator.clipboard.writeText(full);
      window.history.replaceState(null, "", url);
      toast.ok("Link copied");
    } catch {
      toast.error("Could not build link");
    }
  }, [statement, location.pathname]);

  // --- deep link on first mount (ChartPanel "Open in Query" and ?q= share links) ---
  const loadedRef = useRef(false);
  useEffect(() => {
    if (loadedRef.current) return;
    loadedRef.current = true;
    void (async () => {
      const state = await readQueryUrl(window.location.search, window.location.hash);
      if (state.statement) {
        setStatement(state.statement);
        if (state.spec) setExternalSpec(state.spec);
        // A deep link is a request to see this result: run unless params are missing.
        const r = resolveStatement(state.statement, { from, to, tz }, {});
        if (!r.unresolved.length) void runStatementRef.current(state.statement);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Palette commands (Mod-K palette — never stolen by the editor).
  useEffect(() => {
    const offs = [
      registerCommand({ id: "query.run", group: "Query", label: "Run query", perform: () => run() }),
      registerCommand({ id: "query.helper", group: "Query", label: "Toggle query helper", perform: () => setHelperOpen((o) => !o) }),
      registerCommand({ id: "query.save", group: "Query", label: "Save current query", perform: () => saveCurrent() }),
      registerCommand({ id: "query.share", group: "Query", label: "Copy share link for query", perform: () => void share() }),
    ];
    return () => offs.forEach((off) => off());
  }, [run, saveCurrent, share]);

  const showStarters = !result && !running && !runError && statement.trim() === "";

  return (
    <div className={styles.screen}>
      <SchemaRail
        collapsed={railCollapsed}
        onToggleCollapse={() => setRailCollapsed((c) => !c)}
        onInsert={onInsert}
        onLoad={(s, spec) => loadStatement(s, spec)}
        onSaveCurrent={saveCurrent}
        canSave={statement.trim().length > 0}
        historyVersion={historyVersion}
      />

      <div className={styles.center}>
        <div className={styles.toolbar}>
          <Button size="sm" variant="primary" icon={<Play size={14} />} onClick={run} disabled={running || !statement.trim()}>
            {running ? "Running…" : "Run"}
          </Button>
          <span className={styles.toolbarHint}>⌘⏎ run · ⌘⇧⏎ run selection · ⌘/ comment</span>
          <div className={styles.toolbarSpacer} />
          <Tooltip content={helperOpen ? "Hide helper (⌘I)" : "Open helper (⌘I)"}>
            <IconButton
              size="sm"
              variant={helperOpen ? "default" : "ghost"}
              label="Toggle helper"
              onClick={() => (helperOpen ? setHelperOpen(false) : openHelper())}
            >
              {helperOpen ? <Sparkles size={16} /> : <PanelRightOpen size={16} />}
            </IconButton>
          </Tooltip>
        </div>

        <ParametersBar params={barParams} values={paramText} onChange={(n, v) => setParamText((p) => ({ ...p, [n]: v }))} />

        <div className={styles.workArea}>
          <Split
            direction="vertical"
            initial={260}
            min={120}
            max={640}
            storageKey="query-editor"
            first={
              <div className={styles.editorPane}>
                <SqlEditor
                  ref={editorRef}
                  value={statement}
                  onChange={(v) => {
                    setStatement(v);
                    setExternalSpec(null);
                  }}
                  onSelectionChange={setSelection}
                  getCatalogue={getCatalogue}
                  getResolved={() => ({ raw: statement, sql: resolved.sql, unresolved: resolved.unresolved })}
                  resolvedParams={resolvedParamNames}
                  onRun={run}
                  onRunSelection={runSelection}
                  onFocusHelper={openHelper}
                  placeholder={EDITOR_HINT}
                />
              </div>
            }
            second={
              showStarters ? (
                <div className={styles.startersPane}>
                  <Starters channels={boot?.channels ?? []} tz={tz} onPick={(s, spec) => onStarter(s, spec)} />
                </div>
              ) : (
                <ResultsPanel
                  result={result}
                  statement={statement}
                  ranStatement={ranStatement}
                  running={running}
                  error={rawError}
                  ms={ms}
                  plugin={plugin}
                  recordChannel={recordChannel}
                  externalSpec={externalSpec}
                  onShare={() => void share()}
                  onRetry={run}
                />
              )
            }
          />
        </div>
      </div>

      {helperOpen && (
        <HelperPanel
          ref={helperRef}
          statement={statement}
          selection={selection}
          scope={scope}
          channel={recordChannel}
          result={result}
          runError={runError}
          catalogue={catalogue}
          plugin={plugin}
          onApply={(s) => loadStatement(s)}
          onApplyAndRun={(s) => loadStatement(s, externalSpec, true)}
          onClose={() => setHelperOpen(false)}
        />
      )}
    </div>
  );
}
