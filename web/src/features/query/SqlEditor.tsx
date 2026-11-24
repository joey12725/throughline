/*
 * The CodeMirror 6 editor. PostgreSQL highlighting, catalogue completion, server
 * linting, parameter decoration and the app's own theme in both colour schemes.
 * Keys: Mod-Enter runs, Mod-Shift-Enter runs the selection, Mod-/ comments,
 * Mod-I focuses the helper. Mod-K is deliberately left to the global palette.
 */
import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { EditorState, Prec, type Extension } from "@codemirror/state";
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  drawSelection,
  placeholder as cmPlaceholder,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { toggleComment } from "@codemirror/commands";
import { sql, PostgreSQL } from "@codemirror/lang-sql";
import {
  autocompletion,
  completionKeymap,
  closeBrackets,
  closeBracketsKeymap,
} from "@codemirror/autocomplete";
import { bracketMatching, indentOnInput } from "@codemirror/language";
import { lintKeymap } from "@codemirror/lint";
import { editorTheme } from "./editorTheme";
import { paramHighlight, setResolvedParams } from "./paramHighlight";
import { sqlLinter, type ResolvedForLint } from "./lint";
import { sqlCompletionSource } from "./completion";
import type { Catalogue } from "./catalogue";

export interface SqlEditorHandle {
  focus(): void;
  insert(text: string): void;
  getSelection(): string;
  getValue(): string;
}

export interface SqlEditorProps {
  value: string;
  onChange: (value: string) => void;
  getCatalogue: () => Catalogue;
  getResolved: () => ResolvedForLint;
  resolvedParams: ReadonlySet<string>;
  onRun: () => void;
  onRunSelection: (selection: string) => void;
  onFocusHelper: () => void;
  onSelectionChange?: (selection: string) => void;
  placeholder?: string;
}

export const SqlEditor = forwardRef<SqlEditorHandle, SqlEditorProps>(function SqlEditor(props, ref) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  // Keep the latest callbacks reachable from the (stable) keymap closures.
  const cbs = useRef(props);
  cbs.current = props;

  useImperativeHandle(ref, () => ({
    focus: () => viewRef.current?.focus(),
    getValue: () => viewRef.current?.state.doc.toString() ?? "",
    getSelection: () => {
      const v = viewRef.current;
      if (!v) return "";
      const { from, to } = v.state.selection.main;
      return v.state.sliceDoc(from, to);
    },
    insert: (text: string) => {
      const v = viewRef.current;
      if (!v) return;
      const { from, to } = v.state.selection.main;
      v.dispatch({
        changes: { from, to, insert: text },
        selection: { anchor: from + text.length },
      });
      v.focus();
    },
  }), []);

  useEffect(() => {
    if (!hostRef.current) return;

    const runKeys: Extension = Prec.highest(
      keymap.of([
        { key: "Mod-Enter", preventDefault: true, run: () => (cbs.current.onRun(), true) },
        {
          key: "Mod-Shift-Enter",
          preventDefault: true,
          run: (view) => {
            const { from, to } = view.state.selection.main;
            const sel = from === to ? view.state.doc.toString() : view.state.sliceDoc(from, to);
            cbs.current.onRunSelection(sel);
            return true;
          },
        },
        { key: "Mod-i", preventDefault: true, run: () => (cbs.current.onFocusHelper(), true) },
        { key: "Mod-/", preventDefault: true, run: toggleComment },
      ]),
    );

    const updateListener = EditorView.updateListener.of((u) => {
      if (u.docChanged) cbs.current.onChange(u.state.doc.toString());
      if (u.selectionSet && cbs.current.onSelectionChange) {
        const { from, to } = u.state.selection.main;
        cbs.current.onSelectionChange(u.state.sliceDoc(from, to));
      }
    });

    const state = EditorState.create({
      doc: props.value,
      extensions: [
        lineNumbers(),
        highlightActiveLine(),
        highlightActiveLineGutter(),
        history(),
        drawSelection(),
        indentOnInput(),
        bracketMatching(),
        closeBrackets(),
        sql({ dialect: PostgreSQL, upperCaseKeywords: false }),
        autocompletion({ override: [sqlCompletionSource(() => cbs.current.getCatalogue())], icons: true, closeOnBlur: true }),
        paramHighlight(),
        sqlLinter(() => cbs.current.getResolved()),
        editorTheme(),
        props.placeholder ? cmPlaceholder(props.placeholder) : [],
        runKeys,
        keymap.of([
          ...closeBracketsKeymap,
          ...completionKeymap,
          ...historyKeymap,
          ...lintKeymap,
          indentWithTab,
          ...defaultKeymap,
        ]),
        updateListener,
        EditorView.lineWrapping,
      ],
    });

    const view = new EditorView({ state, parent: hostRef.current });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // Mount once; external value & params are synced by the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync external value (loading a saved query, a starter, a deep link).
  useEffect(() => {
    const v = viewRef.current;
    if (!v) return;
    const current = v.state.doc.toString();
    if (props.value !== current) {
      v.dispatch({ changes: { from: 0, to: current.length, insert: props.value } });
    }
  }, [props.value]);

  // Push the resolved-parameter set so decorations recolour on a scope change.
  useEffect(() => {
    const v = viewRef.current;
    if (!v) return;
    v.dispatch({ effects: setResolvedParams.of(new Set(props.resolvedParams)) });
  }, [props.resolvedParams]);

  return <div ref={hostRef} className="tl-query-editor" style={{ height: "100%" }} />;
});
