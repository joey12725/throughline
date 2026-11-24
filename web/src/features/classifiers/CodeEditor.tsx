/*
 * A light code editor for python classifiers: CodeMirror (already a dependency
 * for the SQL editor) with line numbers, history, 4-space indent and the app's
 * theme. /check messages that name a line are drawn as lint diagnostics in the
 * gutter and under the line. No language package is added for python.
 */
import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { EditorState } from "@codemirror/state";
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  drawSelection,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { bracketMatching, indentOnInput, indentUnit } from "@codemirror/language";
import { lintGutter, setDiagnostics, type Diagnostic } from "@codemirror/lint";
import { editorTheme } from "@/features/query/editorTheme";
import type { LineMessage } from "./model";

export interface CodeEditorHandle {
  jumpTo(line: number): void;
}

export interface CodeEditorProps {
  value: string;
  onChange: (value: string) => void;
  diagnostics: readonly LineMessage[];
  ariaLabel: string;
}

export const CodeEditor = forwardRef<CodeEditorHandle, CodeEditorProps>(function CodeEditor(props, ref) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const cbs = useRef(props);
  cbs.current = props;

  useImperativeHandle(ref, () => ({
    jumpTo: (line: number) => {
      const v = viewRef.current;
      if (!v) return;
      const n = Math.min(Math.max(1, line), v.state.doc.lines);
      const pos = v.state.doc.line(n).from;
      v.dispatch({ selection: { anchor: pos }, scrollIntoView: true });
      v.focus();
    },
  }), []);

  useEffect(() => {
    if (!hostRef.current) return;
    const state = EditorState.create({
      doc: props.value,
      extensions: [
        lineNumbers(),
        lintGutter(),
        highlightActiveLine(),
        highlightActiveLineGutter(),
        history(),
        drawSelection(),
        indentOnInput(),
        bracketMatching(),
        indentUnit.of("    "),
        EditorState.tabSize.of(4),
        editorTheme(),
        keymap.of([...historyKeymap, indentWithTab, ...defaultKeymap]),
        EditorView.contentAttributes.of({ "aria-label": props.ariaLabel, spellcheck: "false" }),
        EditorView.updateListener.of((u) => {
          if (u.docChanged) cbs.current.onChange(u.state.doc.toString());
        }),
      ],
    });
    const view = new EditorView({ state, parent: hostRef.current });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // External value (a template, an AI draft) replaces the document.
  useEffect(() => {
    const v = viewRef.current;
    if (!v) return;
    const current = v.state.doc.toString();
    if (props.value !== current) v.dispatch({ changes: { from: 0, to: current.length, insert: props.value } });
  }, [props.value]);

  // Line-numbered check messages become diagnostics on that line.
  useEffect(() => {
    const v = viewRef.current;
    if (!v) return;
    const doc = v.state.doc;
    const diags: Diagnostic[] = [];
    for (const d of props.diagnostics) {
      if (d.line === null || d.line > doc.lines) continue;
      const line = doc.line(d.line);
      diags.push({ from: line.from, to: line.to, severity: d.severity, message: d.message });
    }
    v.dispatch(setDiagnostics(v.state, diags));
  }, [props.diagnostics]);

  return <div ref={hostRef} style={{ height: "100%", minHeight: 320 }} />;
});
