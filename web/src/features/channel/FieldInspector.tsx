/*
 * The field inspector: every declared field with its inferred role, unit, fill
 * rate, a mini distribution, the evidence for the inference, and owner overrides
 * (role / unit / hidden) persisted as UI state of kind "overrides", keyed by
 * channel. Identifiers and bookkeeping collapse into a "plumbing" group. The
 * parent reads the same override state, so a change re-ranks the page's lenses.
 */
import { useMemo, useState, type ReactNode } from "react";
import { ChevronRight, RotateCcw } from "lucide-react";
import { Badge, Sensitive, Switch } from "@/ui";
import { useUiState } from "@/api";
import { ROLE_IDS, type ChannelOverrides, type ChannelProfile, type FieldProfile, type RoleId, type RoleMap } from "@/lens";
import styles from "./FieldInspector.module.css";

export interface FieldInspectorProps {
  channel: string;
  profile: ChannelProfile | undefined;
  roles: RoleMap;
  /** declared field order from the channel meta */
  order: readonly string[];
}

export function FieldInspector({ channel, profile, roles, order }: FieldInspectorProps): ReactNode {
  const ov = useUiState<ChannelOverrides>("overrides", channel, {});
  const overrides = ov.value ?? {};

  const byName = useMemo(() => new Map((profile?.fields ?? []).map((f) => [f.name, f] as const)), [profile]);
  const fields = order.filter((n) => n in roles || byName.has(n));
  const primary = fields.filter((n) => !roles[n]?.plumbing);
  const plumbing = fields.filter((n) => roles[n]?.plumbing);
  const [showPlumbing, setShowPlumbing] = useState(false);

  const setField = async (name: string, patch: Partial<NonNullable<ChannelOverrides["fields"]>[string]>): Promise<void> => {
    const next: ChannelOverrides = { ...overrides, fields: { ...(overrides.fields ?? {}) } };
    const cur = { ...(next.fields![name] ?? {}) };
    for (const [k, val] of Object.entries(patch)) {
      if (val === undefined || val === "") delete (cur as Record<string, unknown>)[k];
      else (cur as Record<string, unknown>)[k] = val;
    }
    if (Object.keys(cur).length === 0) delete next.fields![name];
    else next.fields![name] = cur;
    await ov.set(next);
  };

  if (!profile) {
    return <div className={styles.loading}>Profiling the channel…</div>;
  }

  return (
    <div className={styles.inspector}>
      <div className={styles.group}>
        {primary.map((name) => (
          <FieldRow key={name} name={name} fp={byName.get(name)} roles={roles} override={overrides.fields?.[name]} onChange={(p) => void setField(name, p)} />
        ))}
      </div>

      {plumbing.length > 0 ? (
        <div className={styles.plumbing}>
          <button type="button" className={styles.plumbingToggle} onClick={() => setShowPlumbing((v) => !v)} aria-expanded={showPlumbing}>
            <ChevronRight size={14} className={`${styles.chevron} ${showPlumbing ? styles.open : ""}`} />
            Plumbing — identifiers and bookkeeping ({plumbing.length})
          </button>
          {showPlumbing ? (
            <div className={styles.group}>
              {plumbing.map((name) => (
                <FieldRow key={name} name={name} fp={byName.get(name)} roles={roles} override={overrides.fields?.[name]} onChange={(p) => void setField(name, p)} />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

type FieldOverride = NonNullable<ChannelOverrides["fields"]>[string];

function FieldRow({
  name,
  fp,
  roles,
  override,
  onChange,
}: {
  name: string;
  fp: FieldProfile | undefined;
  roles: RoleMap;
  override: FieldOverride | undefined;
  onChange: (patch: Partial<FieldOverride>) => void;
}): ReactNode {
  const role = roles[name];
  const [expanded, setExpanded] = useState(false);
  const fill = fp?.fill ?? null;
  const hidden = override?.hidden ?? role?.hidden ?? false;
  const effectiveRole = (override?.role ?? role?.role ?? "measure") as RoleId;
  const overridden = role?.overridden || !!override?.role || !!override?.unit;

  return (
    <div className={`${styles.field} ${hidden ? styles.hiddenField : ""}`}>
      <button type="button" className={styles.fieldHead} onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
        <ChevronRight size={13} className={`${styles.chevron} ${expanded ? styles.open : ""}`} />
        <span className={styles.fieldName}>{name}</span>
        <span className={styles.fieldRole}>
          {effectiveRole.replace(/\./g, " · ")}
          {overridden ? <span className={styles.editedDot} title="overridden" /> : null}
        </span>
        {fill !== null ? <span className={styles.fill}>{Math.round(fill * 100)}%</span> : null}
      </button>

      <div className={styles.miniRow}>
        <MiniDistribution fp={fp} />
      </div>

      {expanded ? (
        <div className={styles.detail}>
          <div className={styles.metaGrid}>
            <Meta k="type" v={fp?.pg_type ?? fp?.kind ?? "—"} />
            <Meta k="confidence" v={role ? `${Math.round(role.confidence * 100)}%` : "—"} />
            <Meta k="distinct" v={fp?.distinct != null ? fp.distinct.toLocaleString("en-US") : "—"} />
            <Meta k="unit" v={override?.unit ?? role?.unit ?? "—"} />
          </div>
          {role?.evidence?.length ? (
            <ul className={styles.evidence}>
              {role.evidence.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          ) : null}

          <div className={styles.controls}>
            <label className={styles.control}>
              <span className={styles.controlLabel}>Role</span>
              <select className={styles.select} value={override?.role ?? ""} onChange={(e) => onChange({ role: (e.currentTarget.value || undefined) as RoleId | undefined })}>
                <option value="">auto ({role?.role ?? "—"})</option>
                {ROLE_IDS.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.control}>
              <span className={styles.controlLabel}>Unit</span>
              <input
                className={styles.unitInput}
                type="text"
                defaultValue={override?.unit ?? ""}
                placeholder={role?.unit ?? "none"}
                onBlur={(e) => onChange({ unit: e.currentTarget.value.trim() || undefined })}
              />
            </label>
            <label className={styles.controlInline}>
              <Switch checked={hidden} onChange={(v) => onChange({ hidden: v || undefined })} />
              <span className={styles.controlLabel}>Hidden from charts</span>
            </label>
            {overridden || hidden ? (
              <button type="button" className={styles.reset} onClick={() => onChange({ role: undefined, unit: undefined, hidden: undefined })}>
                <RotateCcw size={12} /> reset
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function Meta({ k, v }: { k: string; v: ReactNode }): ReactNode {
  return (
    <div className={styles.metaItem}>
      <span className={styles.metaK}>{k}</span>
      <span className={styles.metaV}>{v}</span>
    </div>
  );
}

function MiniDistribution({ fp }: { fp: FieldProfile | undefined }): ReactNode {
  if (!fp) return <div className={styles.miniEmpty} />;
  if (fp.numeric && Number.isFinite(fp.numeric.min)) {
    const { min, max, avg } = fp.numeric;
    const span = max - min || 1;
    const pct = ((avg - min) / span) * 100;
    return (
      <div className={styles.rangeBar} title={`min ${min} · avg ${avg} · max ${max}`}>
        <div className={styles.rangeTrack} />
        <div className={styles.rangeAvg} style={{ left: `${Math.max(0, Math.min(100, pct))}%` }} />
        <span className={styles.rangeMin}>{compact(min)}</span>
        <span className={styles.rangeMax}>{compact(max)}</span>
      </div>
    );
  }
  if (fp.boolean) {
    const t = Math.round(fp.boolean.true_share * 100);
    return (
      <div className={styles.boolBar} title={`${t}% true`}>
        <div className={styles.boolTrue} style={{ width: `${t}%` }} />
        <span className={styles.boolLabel}>{t}% true</span>
      </div>
    );
  }
  if (fp.text?.top?.length) {
    const top = fp.text.top.slice(0, 4);
    const max = Math.max(...top.map((t) => t.n), 1);
    return (
      <div className={styles.topList}>
        {top.map((t, i) => (
          <div key={i} className={styles.topRow}>
            <Sensitive>
              <span className={styles.topVal}>{t.value || "∅"}</span>
            </Sensitive>
            <span className={styles.topBarWrap}>
              <span className={styles.topBar} style={{ width: `${(t.n / max) * 100}%` }} />
            </span>
          </div>
        ))}
      </div>
    );
  }
  if (fp.json?.keys?.length) {
    return <div className={styles.keys}>{fp.json.keys.slice(0, 6).map((k) => <Badge key={k} tone="neutral">{k}</Badge>)}</div>;
  }
  if (fp.time?.min) {
    return (
      <div className={styles.timeRange}>
        {String(fp.time.min).slice(0, 10)} → {String(fp.time.max).slice(0, 10)}
      </div>
    );
  }
  return <div className={styles.miniEmpty} />;
}

function compact(x: number): string {
  const a = Math.abs(x);
  if (a >= 1e6) return `${(x / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${(x / 1e3).toFixed(1)}k`;
  if (Number.isInteger(x)) return String(x);
  return a >= 1 ? x.toFixed(1) : x.toPrecision(2);
}
