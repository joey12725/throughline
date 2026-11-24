/*
 * Decorate `:param` references in the editor. Resolved parameters (the scope or the
 * parameters bar supplies a value) read in brass; unresolved ones get a dashed
 * underline so the owner sees at a glance what the parameters bar is asking for.
 * The set of resolved names is pushed in through a state field so a scope change
 * recolours without retyping.
 */
import { Decoration, type DecorationSet, EditorView, ViewPlugin, type ViewUpdate } from "@codemirror/view";
import { StateEffect, StateField, type Extension } from "@codemirror/state";
import { scanParams } from "@/lens";

export const setResolvedParams = StateEffect.define<Set<string>>();

const resolvedField = StateField.define<Set<string>>({
  create: () => new Set(),
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setResolvedParams)) return e.value;
    return value;
  },
});

const resolvedMark = Decoration.mark({ class: "tl-param tl-param-ok" });
const unresolvedMark = Decoration.mark({ class: "tl-param tl-param-missing" });

function buildDecorations(view: EditorView): DecorationSet {
  const resolved = view.state.field(resolvedField);
  const text = view.state.doc.toString();
  const refs = scanParams(text);
  const ranges = refs.map((r) =>
    (resolved.has(r.name) ? resolvedMark : unresolvedMark).range(r.start, r.end),
  );
  return Decoration.set(ranges, true);
}

const plugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = buildDecorations(view);
    }
    update(u: ViewUpdate): void {
      if (u.docChanged || u.state.field(resolvedField) !== u.startState.field(resolvedField)) {
        this.decorations = buildDecorations(u.view);
      }
    }
  },
  { decorations: (v) => v.decorations },
);

const paramTheme = EditorView.baseTheme({
  ".tl-param": { borderRadius: "3px", padding: "0 1px" },
  ".tl-param-ok": { color: "var(--brass)", fontWeight: "600" },
  ".tl-param-missing": {
    color: "var(--warn)",
    fontWeight: "600",
    textDecoration: "underline dashed var(--warn)",
    textUnderlineOffset: "3px",
  },
});

export function paramHighlight(): Extension {
  return [resolvedField, plugin, paramTheme];
}
