/*
 * The no-model finder. When no assistant model is configured (provider "local") the
 * helper is still useful: it searches the catalogue (channels, fields, functions) and
 * offers a runnable statement, and Fix maps a validator error (SQLSTATE + message) to
 * a plain suggestion. Pure functions, lightly unit-tested.
 */
import type { Catalogue } from "../catalogue";
import { shortViewName } from "../catalogue";

export type FinderKind = "channel" | "field" | "function";

export interface FinderMatch {
  kind: FinderKind;
  label: string;
  detail: string;
  /** A ready-to-run statement, when one makes sense. */
  statement?: string;
  score: number;
}

function score(hay: string, needle: string): number {
  const h = hay.toLowerCase();
  const n = needle.toLowerCase();
  if (!n) return 0;
  if (h === n) return 100;
  if (h.startsWith(n)) return 70;
  const idx = h.indexOf(n);
  if (idx >= 0) return 40 - idx;
  return -1;
}

export function findMatches(cat: Catalogue, query: string, limit = 12): FinderMatch[] {
  const q = query.trim();
  const terms = q.split(/\s+/).filter(Boolean);
  const out: FinderMatch[] = [];

  for (const v of cat.views) {
    const s = Math.max(score(v.channel, q), ...terms.map((t) => score(v.channel, t)), score(v.record_kind ?? "", q));
    if (s > 0 || !q) {
      out.push({
        kind: "channel",
        label: v.channel,
        detail: `${v.rows.toLocaleString("en-US")} rows${v.record_kind ? ` · ${v.record_kind}` : ""}`,
        statement: `SELECT *\nFROM ${v.view}\nWHERE happened_at >= :from AND happened_at < :to\nORDER BY happened_at DESC\nLIMIT 100`,
        score: (q ? s : v.rows / 1e6) + 5,
      });
    }
    for (const f of v.fields) {
      const fs = Math.max(score(f.name, q), ...terms.map((t) => score(f.name, t)), score(f.description ?? "", q) - 20);
      if (fs > 0) {
        out.push({
          kind: "field",
          label: `${v.channel}.${f.name}`,
          detail: `${f.type}${f.description ? ` · ${f.description}` : ""}`,
          statement: `SELECT ${f.name}, count(*) AS n\nFROM ${v.view}\nGROUP BY 1\nORDER BY n DESC\nLIMIT 50`,
          score: fs,
        });
      }
    }
  }

  for (const fn of cat.functions) {
    const s = Math.max(score(fn.name, q), score(fn.description, q) - 10);
    if (s > 0) {
      out.push({
        kind: "function",
        label: `api.${fn.name}`,
        detail: fn.description || fn.signature,
        statement: fn.example,
        score: s,
      });
    }
  }

  return out.sort((a, b) => b.score - a.score).slice(0, limit);
}

export interface FixHint {
  title: string;
  detail: string;
}

/** Map a validator/run error to a plain suggestion. Keyed on SQLSTATE, then message. */
export function fixHints(code: string | null | undefined, message: string | null | undefined): FixHint[] {
  const m = (message ?? "").toLowerCase();
  const hints: FixHint[] = [];

  switch (code) {
    case "42703":
      hints.push({ title: "Unknown column", detail: "A column in the statement does not exist. Expand the view in the schema rail to see its real columns." });
      break;
    case "42P01":
      hints.push({ title: "Unknown table or view", detail: "Only schema api views and the ch_* channel views exist. Pick one from the schema rail." });
      break;
    case "42601":
      hints.push({ title: "Syntax error", detail: "Check commas, parentheses and keyword order near the reported token." });
      break;
    case "42883":
      hints.push({ title: "No such function", detail: "That function or operator is not on the allow-list. Use an api.* function or an allow-listed builtin." });
      break;
    case "22007":
    case "22008":
      hints.push({ title: "Bad date/time", detail: "A timestamp or interval literal is malformed. Use an ISO instant, or bind :from / :to from the scope." });
      break;
    case "42501":
      hints.push({ title: "Not permitted", detail: "This reader role cannot touch that object. Stay within schema api." });
      break;
    default:
      break;
  }

  if (m.includes("approval") || m.includes("cost")) {
    hints.push({ title: "Cost gate", detail: "This statement is expensive or semantic and needs approval. Run it from the box with the approvals flow." });
  }
  if (hints.length === 0) {
    hints.push({ title: "Could not run", detail: message || "The statement did not validate. Check it against the schema rail." });
  }
  return hints;
}
