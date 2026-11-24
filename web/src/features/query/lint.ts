/*
 * Linting through the server's own validator. Debounced ~400 ms; the resolved SQL
 * (parameters substituted) is what gets validated, so a statement that is only
 * valid once `:from`/`:to` are bound still lints cleanly. When a parameter is
 * unresolved we skip rather than flag a spurious error. A validator error with no
 * position underlines the whole statement, which is honest about what we know.
 */
import { linter, type Diagnostic } from "@codemirror/lint";
import type { Extension } from "@codemirror/state";
import { validateStatement } from "./validateClient";

export interface ResolvedForLint {
  /** The statement as typed (used for offsets). */
  raw: string;
  /** The statement with parameters substituted (what we validate). */
  sql: string;
  /** Parameters still missing a value — lint is skipped while any remain. */
  unresolved: string[];
}

function friendly(error: string | null, code: string | null): string {
  const base = error && error !== "database operation rejected" ? error : "This statement is not valid.";
  return code ? `${base}\n(SQLSTATE ${code})` : base;
}

export function sqlLinter(getResolved: () => ResolvedForLint): Extension {
  return linter(
    async (view): Promise<Diagnostic[]> => {
      const { raw, sql, unresolved } = getResolved();
      if (!raw.trim() || unresolved.length) return [];
      const res = await validateStatement(sql);
      if (res.valid) return [];
      const to = view.state.doc.length;
      return [
        {
          from: 0,
          to,
          severity: "error",
          message: friendly(res.error, res.code),
        },
      ];
    },
    { delay: 400 },
  );
}
