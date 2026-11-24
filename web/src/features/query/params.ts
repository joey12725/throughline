/*
 * Parameter resolution for a free statement. `:from :to :tz :grain :person :channel`
 * come from the global scopes; anything else the owner types is resolved from a
 * small parameters bar. All substitution goes through the lens SQL utilities so
 * every value is a correctly-typed, escaped literal.
 */
import {
  scanParams,
  substituteParams,
  paramOf,
  chooseGrain,
  type ParamValue,
} from "@/lens";

export interface ParamScope {
  from: string | null;
  to: string | null;
  tz: string;
  person?: number | string | null;
  channel?: string | null;
  grain?: string | null;
}

/** Global parameter values from the current scope (undefined when unset). */
export function globalParamValues(scope: ParamScope): Record<string, ParamValue | undefined> {
  const values: Record<string, ParamValue | undefined> = {};
  if (scope.from) values.from = paramOf(scope.from, "timestamp");
  if (scope.to) values.to = paramOf(scope.to, "timestamp");
  if (scope.tz) values.tz = paramOf(scope.tz, "tz");
  if (scope.person != null) values.person = paramOf(scope.person, "int");
  if (scope.channel) values.channel = paramOf(scope.channel, "text");
  const grain = scope.grain ?? chooseGrain(scope.from ?? undefined, scope.to ?? undefined);
  values.grain = paramOf(grain, "grain");
  return values;
}

export interface ResolvedStatement {
  sql: string;
  used: string[];
  unresolved: string[];
}

/**
 * Substitute parameters in a statement. `overrides` (from the parameters bar) win
 * over global scope values, so the owner can pin a value the scope did not supply.
 */
export function resolveStatement(
  statement: string,
  scope: ParamScope,
  overrides: Record<string, ParamValue> = {},
): ResolvedStatement {
  const values = { ...globalParamValues(scope), ...overrides };
  const sub = substituteParams(statement, values);
  return { sql: sub.sql, used: sub.used, unresolved: sub.unresolved };
}

/** Distinct `:param` names referenced in a statement, in order of first use. */
export function statementParams(statement: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const ref of scanParams(statement)) {
    if (!seen.has(ref.name)) {
      seen.add(ref.name);
      out.push(ref.name);
    }
  }
  return out;
}

const GLOBAL = new Set(["from", "to", "tz", "person", "channel", "grain"]);

/** Parameters a statement needs that the scope cannot supply on its own. */
export function unresolvedParams(statement: string, scope: ParamScope, overrides: Record<string, ParamValue> = {}): string[] {
  return resolveStatement(statement, scope, overrides).unresolved;
}

export function isGlobalParam(name: string): boolean {
  return GLOBAL.has(name);
}

/** Coerce a bar's free-text value into a typed ParamValue (best guess). */
export function parseBarValue(raw: string): ParamValue {
  const t = raw.trim();
  if (t === "") return paramOf("", "text");
  if (/^-?\d+$/.test(t)) return paramOf(Number(t), "int");
  if (/^-?\d*\.\d+$/.test(t)) return paramOf(Number(t), "number");
  if (t === "true" || t === "false") return paramOf(t === "true", "bool");
  if (/^\d{4}-\d{2}-\d{2}T/.test(t)) return paramOf(t, "timestamp");
  if (/^\d{4}-\d{2}-\d{2}$/.test(t)) return paramOf(t, "date");
  return paramOf(t, "text");
}
