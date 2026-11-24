// Owner overrides: correct a field's role or unit, hide a field, pin or hide a lens.
// They are UI state keyed by channel name; plugins never see or supply them.

import { isPlumbing } from "./roles";
import type { ChannelOverrides, FieldOverride, LensCandidate, RoleId, RoleMap } from "./types";
import { LENS_IDS, ROLE_IDS } from "./types";

/** Coerce whatever the UI-state store returned into a well-formed override document. */
export function normalizeOverrides(raw: unknown): ChannelOverrides {
  const out: ChannelOverrides = {};
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return out;
  const doc = raw as Record<string, unknown>;
  if (doc.fields !== null && typeof doc.fields === "object" && !Array.isArray(doc.fields)) {
    const fields: Record<string, FieldOverride> = {};
    for (const [name, value] of Object.entries(doc.fields as Record<string, unknown>)) {
      if (value === null || typeof value !== "object") continue;
      const v = value as Record<string, unknown>;
      const o: FieldOverride = {};
      if (typeof v.role === "string" && (ROLE_IDS as readonly string[]).includes(v.role)) o.role = v.role as RoleId;
      if (typeof v.unit === "string" && v.unit.length <= 24) o.unit = v.unit;
      if (typeof v.hidden === "boolean") o.hidden = v.hidden;
      if (Object.keys(o).length > 0) fields[name] = o;
    }
    if (Object.keys(fields).length > 0) out.fields = fields;
  }
  const keys = (x: unknown) =>
    Array.isArray(x) ? [...new Set(x.filter((k): k is string => typeof k === "string" && k.length > 0 && k.length <= 200))] : [];
  const pinned = keys(doc.pinned);
  const hidden = keys(doc.hidden);
  if (pinned.length) out.pinned = pinned;
  if (hidden.length) out.hidden = hidden;
  return out;
}

/** Apply per-field overrides to inferred roles. Returns a new map; the input is not changed. */
export function applyRoleOverrides(roles: RoleMap, overrides?: ChannelOverrides | null): RoleMap {
  const fields = overrides?.fields;
  if (!fields) return roles;
  const out: RoleMap = {};
  for (const [name, role] of Object.entries(roles)) {
    const o = Object.prototype.hasOwnProperty.call(fields, name) ? fields[name] : undefined;
    if (!o) {
      out[name] = role;
      continue;
    }
    const next = { ...role, evidence: [...role.evidence] };
    if (o.role && o.role !== role.role) {
      next.evidence = [`set to ${o.role} by the owner (the engine read it as ${role.role})`];
      next.role = o.role;
      next.confidence = 1;
      next.plumbing = isPlumbing(o.role);
      next.overridden = true;
      delete next.family;
      if (!o.unit) delete next.unit;
      // A pair only survives when the new role is still one half of it.
      if (!/^(time\.(start|end|offset)|geo\.(lat|lon)|date\.text)$/.test(o.role)) delete next.pair;
    }
    if (o.unit !== undefined && o.unit !== role.unit) {
      if (o.unit === "") delete next.unit;
      else next.unit = o.unit;
      next.overridden = true;
      next.evidence.push(o.unit === "" ? "unit cleared by the owner" : `unit set to ${o.unit} by the owner`);
    }
    if (o.hidden !== undefined) {
      if (o.hidden) {
        next.hidden = true;
        next.evidence.push("hidden by the owner");
      } else delete next.hidden;
    }
    out[name] = next;
  }
  return out;
}

/** The keys a pin or a hide can name a candidate by: "lens", "lens:field" and "lens:field+field". */
export function candidateKeys(c: LensCandidate): string[] {
  const fields = c.spec.provenance.fields ?? [];
  const keys = [c.spec.lens as string];
  if (fields.length > 0) {
    keys.push(`${c.spec.lens}:${fields[0]}`);
    if (fields.length > 1) keys.push(`${c.spec.lens}:${fields.join("+")}`);
  }
  return keys;
}

/** The most specific key of a candidate: what a "pin this" button stores. */
export function candidateKey(c: LensCandidate): string {
  const keys = candidateKeys(c);
  return keys[keys.length - 1] as string;
}

/**
 * Apply pins and hides last: hidden candidates drop out (the spine and the table always
 * stay), pinned ones move to the front in the order the owner pinned them.
 */
export function applyLensOverrides(candidates: LensCandidate[], overrides?: ChannelOverrides | null): LensCandidate[] {
  if (!overrides) return candidates;
  const hidden = new Set(overrides.hidden ?? []);
  const pinned = overrides.pinned ?? [];
  let list = candidates;
  if (hidden.size > 0) {
    list = list.filter((c) => c.spec.lens === "spine" || c.spec.lens === "table" || !candidateKeys(c).some((k) => hidden.has(k)));
  }
  if (pinned.length === 0) return list;
  const front: LensCandidate[] = [];
  const taken = new Set<LensCandidate>();
  for (const key of pinned) {
    // A bare lens id pins that lens's best candidate; a longer key pins exactly that one.
    const hit = list.find((c) => !taken.has(c) && candidateKeys(c).includes(key));
    if (hit) {
      front.push(hit);
      taken.add(hit);
    }
  }
  return [...front, ...list.filter((c) => !taken.has(c))];
}

export function isLensId(value: unknown): boolean {
  return typeof value === "string" && (LENS_IDS as readonly string[]).includes(value);
}
