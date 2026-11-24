/*
 * Development gallery at /ui/styleguide/charts: every lens id in the engine drawn
 * against the dev server's real channels. Channels are chosen by ARCHETYPE and
 * fit (the engine's own ranking), never by name — so this exercises the same path
 * the product uses. A theme control flips Nocturne/Daybook for review.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useBootstrap, sql } from "@/api";
import {
  analyzeChannel,
  profileChannel,
  LENS_CATALOG,
  LENS_IDS,
  type ChannelMeta,
  type ChannelProfile,
  type LensCandidate,
  type LensGroup,
  type LensId,
  type Row,
  type RunSql,
  type SqlColumn,
} from "@/lens";
import { Segmented } from "@/ui";
import { usePrefs, type ThemePref } from "@/store/prefs";
import { ChartPanel } from "./Panel";
import styles from "./ChartsGallery.module.css";

// A plain mapping from the UI channel row to the engine's ChannelMeta.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function channelMeta(c: any): ChannelMeta {
  return {
    name: c.name,
    plugin: c.plugin,
    description: c.description,
    record_kind: c.record_kind ?? "life_event",
    is_reference: !!c.is_reference,
    is_outside: !!c.is_outside,
    whole_thread: !!c.whole_thread,
    embed: !!c.embed,
    fields: Array.isArray(c.fields) ? c.fields : [],
    view: c.view,
    rows: Number(c.rows) || 0,
    threads: Number(c.threads) || 0,
    first_at: c.first_at ?? null,
    last_at: c.last_at ?? null,
    last_ingest_at: c.last_ingest_at ?? null,
    with_geo: c.with_geo ? Math.max(1, Number(c.rows) || 1) : 0,
    with_ended_at: c.with_ended_at ? Math.max(1, Number(c.rows) || 1) : 0,
    with_images: c.with_images ? Math.max(1, Number(c.rows) || 1) : 0,
    coverage: c.coverage ?? null,
  };
}

interface Pick {
  spec: LensCandidate["spec"];
  channel: string;
  score: number;
  hasRows: boolean;
}

const GROUP_ORDER: LensGroup[] = ["time", "distribution", "structure", "space", "relation", "reading"];
const GROUP_OF: Record<LensId, LensGroup> = Object.fromEntries(LENS_CATALOG.map((e) => [e.id, e.group])) as Record<LensId, LensGroup>;

export default function ChartsGallery(): ReactNode {
  const { data: boot, isLoading } = useBootstrap();
  const theme = usePrefs((s) => s.theme);
  const setPref = usePrefs((s) => s.set);
  const [profiles, setProfiles] = useState<Map<string, ChannelProfile>>(new Map());

  // Profile the largest channels with live SQL so lenses that need observed
  // statistics (ego_graph, link_matrix, reply_latency, scatter…) attach to a
  // channel that actually has the data, not a sparse one with a short candidate
  // list. The rest fall back to the declared profile.
  useEffect(() => {
    if (!boot) return;
    let alive = true;
    const runSql: RunSql = (statement, opts) =>
      sql<Row>(statement, { rowCap: opts?.row_cap, deadlineMs: opts?.deadline_ms as number | undefined, priority: "background" }).then((r) => ({
        rows: r.rows as Row[],
        columns: r.columns as SqlColumn[],
        truncated: r.truncated,
      }));
    const metas = boot.channels
      .map(channelMeta)
      .filter((m) => m.rows > 0)
      .sort((a, b) => b.rows - a.rows)
      .slice(0, 16);
    void (async () => {
      const m = new Map<string, ChannelProfile>();
      await Promise.all(
        metas.map(async (meta) => {
          try {
            m.set(meta.name, await profileChannel(meta, runSql));
          } catch {
            /* leave it to the declared profile */
          }
        }),
      );
      if (alive) setProfiles(m);
    })();
    return () => {
      alive = false;
    };
  }, [boot]);

  const picks = useMemo(() => {
    const out = new Map<LensId, Pick>();
    if (!boot) return out;
    for (const row of boot.channels) {
      let analysis;
      try {
        analysis = analyzeChannel(channelMeta(row), profiles.get(row.name) ?? null);
      } catch {
        continue;
      }
      const hasRows = row.rows > 0;
      for (const c of analysis.candidates) {
        const id = c.spec.lens;
        const prev = out.get(id);
        // A channel with rows always beats an empty one for the same lens, so a
        // picture is drawn against real data; within a row-class, score wins.
        const wins = !prev || (hasRows && !prev.hasRows) || (hasRows === prev.hasRows && c.score > prev.score);
        if (wins) out.set(id, { spec: c.spec, channel: row.name, score: c.score, hasRows });
      }
    }
    return out;
  }, [boot, profiles]);

  if (isLoading) return <div className={styles.note}>Loading the installation…</div>;
  if (!boot) return <div className={styles.note}>No bootstrap.</div>;

  const covered = LENS_IDS.filter((id) => picks.has(id)).length;

  return (
    <div className={styles.page} id="charts-gallery">
      <header className={styles.head}>
        <div>
          <h1 className={styles.title}>Chart gallery</h1>
          <p className={styles.sub}>
            {covered} of {LENS_IDS.length} lenses drawn against {boot.channels.length} channels of the dev installation. Channels chosen by archetype and fit.
          </p>
        </div>
        <Segmented
          aria-label="Theme"
          value={theme}
          onChange={(v) => setPref("theme", v as ThemePref)}
          items={[
            { value: "nocturne", label: "Nocturne" },
            { value: "daybook", label: "Daybook" },
            { value: "system", label: "System" },
          ]}
        />
      </header>

      {GROUP_ORDER.map((group) => {
        const ids = LENS_IDS.filter((id) => GROUP_OF[id] === group);
        return (
          <section key={group} className={styles.group}>
            <h2 className={styles.groupTitle}>{group}</h2>
            <div className={styles.grid}>
              {ids.map((id) => {
                const pick = picks.get(id);
                return (
                  <div key={id} className={styles.item}>
                    <div className={styles.lensLabel}>
                      <code>{id}</code>
                      {pick && <span className={styles.chan}>{pick.channel}</span>}
                    </div>
                    {pick ? (
                      <ChartPanel spec={pick.spec} height={260} board="gallery" priority="normal" />
                    ) : (
                      <div className={styles.missing}>No channel ranked this lens.</div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
  );
}
