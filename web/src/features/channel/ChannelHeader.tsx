/*
 * The channel page header: identity, description, record kind and flags, counts,
 * first/last, coverage status with its reasons, and the detected archetypes as
 * chips whose confidence is shown and whose evidence opens on click. Everything
 * is derived from shape; no channel or plugin name is special-cased.
 */
import { type ReactNode } from "react";
import { SlidersHorizontal } from "lucide-react";
import type { ChannelRow, PluginRow } from "@/api/types";
import { Badge, Button, Popover, StaticChip, StatusGlyph, SourceDot, usePopover } from "@/ui";
import { pluginState, type Tone } from "@/ui/status";
import { usePrefs } from "@/store/prefs";
import { formatInTz, relative } from "@/store/scopes";
import type { ArchetypeId, ArchetypeScore, Archetypes } from "@/lens";
import styles from "./ChannelHeader.module.css";

const COVERAGE_TONE: Record<string, Tone> = { complete: "ok", partial: "warn", best_effort: "neutral" };

function flagList(meta: ChannelRow): string[] {
  const out: string[] = [];
  if (meta.is_reference) out.push("reference");
  if (meta.is_outside) out.push("outside");
  if (meta.whole_thread) out.push("whole-thread");
  if (meta.embed) out.push("embedded");
  if (meta.with_geo) out.push("located");
  if (meta.with_ended_at) out.push("intervals");
  if (meta.with_images) out.push("images");
  return out;
}

export interface ChannelHeaderProps {
  meta: ChannelRow;
  channelIndex: number;
  archetypes: Archetypes;
  plugin?: PluginRow | null;
  onOpenInspector: () => void;
}

export function ChannelHeader({ meta, channelIndex, archetypes, plugin, onOpenInspector }: ChannelHeaderProps): ReactNode {
  const tz = usePrefs((s) => s.tz);
  const cov = meta.coverage;
  const covTone = COVERAGE_TONE[cov?.status ?? ""] ?? "neutral";
  const shown = archetypeOrder(archetypes);

  return (
    <header className={styles.header}>
      <div className={styles.topRow}>
        <div className={styles.identity}>
          <SourceDot plugin={meta.plugin} channelIndex={channelIndex} size={13} />
          <h1 className={styles.name}>{meta.name}</h1>
          <StaticChip>{meta.record_kind ?? "record"}</StaticChip>
          {plugin ? <StatusGlyph state={pluginState(plugin)} size={14} /> : null}
        </div>
        <Button variant="ghost" size="sm" onClick={onOpenInspector}>
          <SlidersHorizontal size={14} /> Fields
        </Button>
      </div>

      {meta.description ? <p className={styles.desc}>{meta.description}</p> : null}

      <div className={styles.flags}>
        {flagList(meta).map((f) => (
          <StaticChip key={f}>{f}</StaticChip>
        ))}
      </div>

      <div className={styles.facts}>
        <Fact label="records" value={meta.rows.toLocaleString("en-US")} />
        <Fact label="threads" value={meta.threads > 0 ? meta.threads.toLocaleString("en-US") : "—"} />
        <Fact label="first" value={meta.first_at ? formatInTz(meta.first_at, tz, { dateStyle: "medium" }) : "—"} />
        <Fact
          label="last"
          value={meta.last_at ? formatInTz(meta.last_at, tz, { dateStyle: "medium" }) : "—"}
          hint={meta.last_at ? relative(meta.last_at) : undefined}
        />
        <div className={styles.coverage}>
          <span className={styles.factLabel}>coverage</span>
          {cov ? (
            <CoverageBadge status={cov.status} tone={covTone} reasons={cov.reasons} />
          ) : (
            <span className={styles.factValue}>unknown</span>
          )}
        </div>
      </div>

      <div className={styles.archetypes} aria-label="Detected archetypes">
        {shown.length === 0 ? (
          <span className={styles.noArch}>No archetype detected</span>
        ) : (
          shown.map((s, i) => (
            <ArchetypeChip key={s.archetype} score={s} primary={i === 0} predicted={archetypes.predicted} />
          ))
        )}
      </div>
    </header>
  );
}

function Fact({ label, value, hint }: { label: string; value: string; hint?: string }): ReactNode {
  return (
    <div className={styles.fact}>
      <span className={styles.factLabel}>{label}</span>
      <span className={styles.factValue}>
        {value}
        {hint ? <span className={styles.factHint}>{hint}</span> : null}
      </span>
    </div>
  );
}

function CoverageBadge({ status, tone, reasons }: { status: string; tone: Tone; reasons: readonly string[] }): ReactNode {
  const pop = usePopover();
  const hasReasons = reasons.length > 0;
  return (
    <>
      <button
        type="button"
        ref={pop.anchorRef}
        className={styles.covButton}
        onClick={hasReasons ? pop.toggle : undefined}
        aria-expanded={hasReasons ? pop.open : undefined}
      >
        <Badge tone={tone}>{status.replace(/_/g, " ")}</Badge>
        {hasReasons ? <span className={styles.covWhy}>why</span> : null}
      </button>
      {hasReasons ? (
        <Popover open={pop.open} onClose={pop.close} anchorRef={pop.anchorRef} align="start" aria-label="Coverage reasons">
          <div className={styles.pop}>
            <div className={styles.popTitle}>Coverage: {status.replace(/_/g, " ")}</div>
            <ul className={styles.reasons}>
              {reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </div>
        </Popover>
      ) : null}
    </>
  );
}

function ArchetypeChip({ score, primary, predicted }: { score: ArchetypeScore; primary: boolean; predicted: boolean }): ReactNode {
  const pop = usePopover();
  const pct = Math.round(score.score * 100);
  return (
    <>
      <button
        type="button"
        ref={pop.anchorRef}
        className={`${styles.archChip} ${primary ? styles.archPrimary : ""}`}
        onClick={pop.toggle}
        aria-expanded={pop.open}
      >
        <span className={styles.archName}>{score.archetype.replace(/_/g, " ")}</span>
        <span className={styles.archPct}>{pct}%</span>
      </button>
      <Popover open={pop.open} onClose={pop.close} anchorRef={pop.anchorRef} align="start" aria-label={`${score.archetype} evidence`}>
        <div className={styles.pop}>
          <div className={styles.popTitle}>
            {score.archetype.replace(/_/g, " ")} · {pct}%{predicted ? " · predicted" : ""}
          </div>
          {score.evidence.length ? (
            <ul className={styles.reasons}>
              {score.evidence.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          ) : (
            <p className={styles.popEmpty}>No specific evidence recorded.</p>
          )}
        </div>
      </Popover>
    </>
  );
}

/** Primary first, then every secondary scoring above a floor, best first, capped. */
function archetypeOrder(a: Archetypes): ArchetypeScore[] {
  const byId = new Map(a.scores.map((s) => [s.archetype, s] as const));
  const out: ArchetypeScore[] = [];
  const primary = byId.get(a.primary);
  if (primary) out.push(primary);
  for (const id of a.secondary) {
    const s = byId.get(id as ArchetypeId);
    if (s && s.archetype !== a.primary) out.push(s);
  }
  return out.slice(0, 5);
}
