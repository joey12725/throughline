/*
 * CodeMirror theme + syntax highlighting built from the app's design tokens, so
 * the editor is the same material as the rest of the surface in both Nocturne and
 * Daybook. Nothing here is a stock CodeMirror theme; every colour is a token var,
 * so a theme switch (which flips the CSS variables) re-colours the editor for free.
 */
import { EditorView } from "@codemirror/view";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";
import type { Extension } from "@codemirror/state";

const base = EditorView.theme({
  "&": {
    color: "var(--ink)",
    backgroundColor: "transparent",
    fontSize: "13px",
    height: "100%",
  },
  ".cm-scroller": {
    fontFamily: "var(--font-mono)",
    lineHeight: "1.6",
    overflow: "auto",
  },
  ".cm-content": {
    caretColor: "var(--brass)",
    padding: "var(--s-3) 0",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--brass)", borderLeftWidth: "2px" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection": {
    backgroundColor: "var(--selection)",
  },
  ".cm-gutters": {
    backgroundColor: "transparent",
    color: "var(--ink-3)",
    border: "none",
    borderRight: "1px solid var(--line)",
    fontVariantNumeric: "tabular-nums",
  },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--ink-2)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--bg-3) 45%, transparent)" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 var(--s-3) 0 var(--s-4)" },
  ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": {
    backgroundColor: "var(--brass-soft)",
    color: "var(--ink)",
    outline: "1px solid color-mix(in srgb, var(--brass) 50%, transparent)",
  },
  // Autocomplete popup.
  ".cm-tooltip": {
    backgroundColor: "var(--bg-2)",
    border: "1px solid var(--line-strong)",
    borderRadius: "var(--r-control)",
    boxShadow: "0 8px 28px color-mix(in srgb, #000 28%, transparent)",
    color: "var(--ink)",
  },
  ".cm-tooltip.cm-tooltip-autocomplete > ul": {
    fontFamily: "var(--font-mono)",
    fontSize: "12.5px",
    maxHeight: "16rem",
  },
  ".cm-tooltip-autocomplete ul li": { padding: "2px var(--s-3)" },
  ".cm-tooltip-autocomplete ul li[aria-selected]": {
    backgroundColor: "var(--brass-soft)",
    color: "var(--ink)",
  },
  ".cm-completionLabel": { color: "var(--ink)" },
  ".cm-completionDetail": { color: "var(--ink-3)", fontStyle: "normal", marginLeft: "var(--s-3)" },
  ".cm-completionIcon": { opacity: "0.7", paddingRight: "var(--s-3)" },
  ".cm-completionInfo": {
    backgroundColor: "var(--bg-2)",
    border: "1px solid var(--line-strong)",
    borderRadius: "var(--r-control)",
    color: "var(--ink-2)",
    fontFamily: "var(--font-ui)",
    fontSize: "12px",
    lineHeight: "1.5",
    maxWidth: "22rem",
    padding: "var(--s-3) var(--s-4)",
    whiteSpace: "pre-wrap",
  },
  // Diagnostics.
  ".cm-diagnostic": {
    fontFamily: "var(--font-ui)",
    fontSize: "12px",
    padding: "var(--s-2) var(--s-3)",
    borderLeftWidth: "3px",
  },
  ".cm-diagnostic-error": { borderLeftColor: "var(--bad)" },
  ".cm-lintRange-error": {
    backgroundImage: "none",
    textDecoration: "underline wavy var(--bad)",
    textUnderlineOffset: "3px",
  },
  ".cm-tooltip.cm-tooltip-lint": { backgroundColor: "var(--bg-2)", borderColor: "var(--line-strong)" },
  ".cm-panels": { backgroundColor: "var(--bg-2)", color: "var(--ink)" },
});

const highlight = HighlightStyle.define([
  { tag: t.keyword, color: "var(--brass)", fontWeight: "600" },
  { tag: [t.operator, t.derefOperator, t.compareOperator, t.arithmeticOperator, t.logicOperator], color: "var(--ink-2)" },
  { tag: [t.string, t.special(t.string)], color: "var(--ok)" },
  { tag: [t.number, t.bool, t.null], color: "var(--warn)" },
  { tag: [t.lineComment, t.blockComment], color: "var(--ink-3)", fontStyle: "italic" },
  { tag: [t.function(t.variableName), t.function(t.propertyName)], color: "var(--patina)" },
  { tag: t.typeName, color: "var(--patina)" },
  { tag: t.propertyName, color: "var(--ink)" },
  { tag: t.variableName, color: "var(--ink)" },
  { tag: [t.punctuation, t.bracket, t.paren, t.squareBracket, t.brace], color: "var(--ink-2)" },
  { tag: t.labelName, color: "var(--ink-2)" },
  { tag: t.invalid, color: "var(--bad)" },
]);

export function editorTheme(): Extension {
  return [base, syntaxHighlighting(highlight)];
}
