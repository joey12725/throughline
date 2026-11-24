// The pipeline: profile -> roles -> archetypes -> ranked lenses -> view specs.

import { scoreArchetypes } from "./archetypes";
import { type ChannelCtx, type LensDef, type ResultCtx, channelContext, channelSpec, resultContext, resultSpec } from "./lenses/common";
import { LENS_LIST } from "./lenses/index";
import { type Scored, diversify, scoreVariant, toCandidates } from "./lenses/rank";
import { applyLensOverrides, applyRoleOverrides } from "./overrides";
import { declaredProfile } from "./profile";
import { inferRoles } from "./roles";
import type { Capabilities } from "./sql";
import type {
  Archetypes,
  ChannelMeta,
  ChannelOverrides,
  ChannelProfile,
  FieldDecl,
  FieldFamily,
  LensCandidate,
  PartOfWhole,
  ResultProfile,
  RoleMap,
} from "./types";

export interface EngineOptions {
  /** Builtins the server allows; the extended allow-list by default. */
  caps?: Capabilities;
  /** The channels that share this channel's field signature (this one included), for family views. */
  siblings?: ChannelMeta[];
}

export interface ChannelAnalysis {
  roles: RoleMap;
  archetypes: Archetypes;
  families: FieldFamily[];
  wholes: PartOfWhole[];
  /** Ranked and diversified; `spine` and `table` are always among them. */
  candidates: LensCandidate[];
  /** Lenses whose builder failed. Empty in normal operation; kept so failures are not silent. */
  errors: { lens: string; message: string }[];
}

function sanitizeMeta(meta: ChannelMeta): ChannelMeta {
  const fields: FieldDecl[] = [];
  const seen = new Set<string>();
  for (const f of Array.isArray(meta.fields) ? meta.fields : []) {
    if (!f || typeof f.name !== "string" || f.name.length === 0 || seen.has(f.name)) continue;
    seen.add(f.name);
    fields.push({ name: f.name, type: typeof f.type === "string" ? f.type : "text", description: typeof f.description === "string" ? f.description : "" });
  }
  return {
    ...meta,
    name: String(meta.name ?? ""),
    plugin: String(meta.plugin ?? ""),
    description: typeof meta.description === "string" ? meta.description : "",
    record_kind: String(meta.record_kind ?? "life_event"),
    is_reference: !!meta.is_reference,
    is_outside: !!meta.is_outside,
    whole_thread: !!meta.whole_thread,
    fields,
    rows: Number.isFinite(meta.rows) ? Math.max(0, meta.rows) : 0,
    threads: Number.isFinite(meta.threads) ? Math.max(0, meta.threads) : 0,
    with_geo: Number.isFinite(meta.with_geo) ? meta.with_geo : 0,
    with_ended_at: Number.isFinite(meta.with_ended_at) ? meta.with_ended_at : 0,
    with_images: Number.isFinite(meta.with_images) ? meta.with_images : 0,
  };
}

/** Two fields can read the same once humanised (weight, weight_kg): name them apart. */
function distinctTitles(candidates: LensCandidate[]): void {
  const seen = new Map<string, LensCandidate[]>();
  for (const c of candidates) {
    const key = `${c.spec.lens}|${c.spec.title}`;
    seen.set(key, [...(seen.get(key) ?? []), c]);
  }
  for (const group of seen.values()) {
    if (group.length < 2) continue;
    for (const c of group) {
      const fields = c.spec.provenance.fields ?? [];
      if (fields.length > 0) c.spec.title = `${c.spec.title} (${fields.slice(0, 3).join(", ")})`;
    }
  }
}

function gather<C extends ChannelCtx | ResultCtx>(
  ctx: C,
  archetypes: Archetypes | null,
  errors: { lens: string; message: string }[],
  run: (lens: LensDef) => { fitness: number; variants: ReturnType<LensDef["build"]> } | null,
): Scored[] {
  void ctx;
  const scored: Scored[] = [];
  for (const lens of LENS_LIST) {
    try {
      const built = run(lens);
      if (!built) continue;
      for (const variant of built.variants) {
        const score = scoreVariant(lens, built.fitness, variant, archetypes);
        if (score > 0 || lens.id === "spine" || lens.id === "table") scored.push({ lens, variant, score });
      }
    } catch (err) {
      errors.push({ lens: lens.id, message: err instanceof Error ? err.message : String(err) });
    }
  }
  return scored;
}

/**
 * Everything the channel page needs from the engine: roles, archetypes, families and the
 * ranked lens candidates. Never throws: a lens that fails is reported in `errors`, and the
 * spine and the table are always present.
 */
export function analyzeChannel(
  metaIn: ChannelMeta,
  profileIn?: ChannelProfile | null,
  overrides?: ChannelOverrides | null,
  opts: EngineOptions = {},
): ChannelAnalysis {
  const meta = sanitizeMeta(metaIn);
  const profile = profileIn ?? declaredProfile(meta);
  const roles = applyRoleOverrides(inferRoles(profile, meta), overrides);
  const archetypes = scoreArchetypes(meta, profile, roles);
  const ctx = channelContext(meta, profile, roles, archetypes, opts);
  const errors: { lens: string; message: string }[] = [];
  const scored = gather(ctx, archetypes, errors, (lens) => {
    const fit = lens.fit(profile, roles, archetypes);
    if (!(fit.fitness > 0)) return null;
    return { fitness: fit.fitness, variants: lens.build(ctx) };
  });
  const ranked = diversify(scored);
  const candidates = toCandidates(ranked);
  distinctTitles(candidates);
  for (const id of ["spine", "table"] as const) {
    if (candidates.some((c) => c.spec.lens === id)) continue;
    // A builder failed: fall back to the plainest statement so the page still has its frame.
    const sql =
      id === "spine"
        ? `SELECT date_trunc(:grain, happened_at, :tz) AS bucket, count(*) AS n FROM ${ctx.view} GROUP BY 1 ORDER BY 1`
        : `SELECT id, happened_at, left(content, 200) AS content FROM ${ctx.view} WHERE happened_at >= :from AND happened_at < :to ORDER BY happened_at DESC, id DESC LIMIT 200`;
    const spec = channelSpec(ctx, id, {
      title: id === "spine" ? "Records over time" : "Records as a table",
      why: id === "spine" ? "Every record has a time" : "The latest records",
      sql,
      row_cap: id === "spine" ? 2000 : 200,
      encode: id === "spine" ? { x: "bucket", y: "n" } : { columns: ["id", "happened_at", "content"] },
    });
    candidates.push({ spec, score: 0, why: spec.why });
  }
  return {
    roles,
    archetypes,
    families: ctx.families,
    wholes: ctx.wholes,
    candidates: applyLensOverrides(candidates, overrides),
    errors,
  };
}

/** Ranked lens candidates for a channel. `profile` may be omitted for a channel with no rows. */
export function lensesForChannel(
  meta: ChannelMeta,
  profile?: ChannelProfile | null,
  overrides?: ChannelOverrides | null,
  opts: EngineOptions = {},
): LensCandidate[] {
  return analyzeChannel(meta, profile, overrides, opts).candidates;
}

export interface ResultAnalysis {
  roles: RoleMap;
  candidates: LensCandidate[];
  errors: { lens: string; message: string }[];
}

/**
 * Ranked lens candidates for an arbitrary SQL result. A lens that can draw the rows as
 * they are keeps the user's statement untouched (`provenance.statement = "as_is"`), so the
 * rows already in the browser can be reused and nothing about the answer changes. Only a
 * lens that has to aggregate raw rows wraps the statement as a subquery ("wrapped"), and
 * only when the text is a single statement.
 */
export function analyzeResult(profile: ResultProfile, statement: string, opts: { caps?: Capabilities } = {}): ResultAnalysis {
  const roles = inferRoles(profile);
  const text = typeof statement === "string" ? statement : "";
  const ctx = resultContext(profile, roles, text, opts.caps);
  const errors: { lens: string; message: string }[] = [];
  const scored = gather(ctx, null, errors, (lens) => {
    if (!lens.buildResult) return null;
    return { fitness: 1, variants: lens.buildResult(ctx) };
  });
  const candidates = toCandidates(diversify(scored));
  if (!candidates.some((c) => c.spec.lens === "table")) {
    const spec = resultSpec("table", "as_is", {
      title: "Result table",
      why: "The result as returned",
      sql: text.trim() ? text : "SELECT 1 AS empty_statement",
      row_cap: 10000,
      encode: { columns: profile.columns.length ? profile.columns.map((c) => c.name).slice(0, 24) : ["empty_statement"] },
    });
    candidates.push({ spec, score: 0, why: spec.why });
  }
  return { roles, candidates, errors };
}

export function lensesForResult(profile: ResultProfile, statement: string, opts: { caps?: Capabilities } = {}): LensCandidate[] {
  return analyzeResult(profile, statement, opts).candidates;
}
