/*
 * The channel page: /ui/c/:channel. One channel, automatically visualised by the
 * lens engine. Its layout is fixed (header, time spine, hero + lens grid, native
 * reader, field inspector); all content is generated from the channel's declared
 * shape and observed statistics. Nothing here names a channel or plugin.
 */
import { useMemo, useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { useBootstrap } from "@/api";
import { Drawer, EmptyState, Skeleton } from "@/ui";
import type { ChannelRow } from "@/api/types";
import {
  analyzeChannel,
  channelFamilies,
  type ChannelMeta,
  type LensCandidate,
} from "@/lens";
import { channelMeta, useChannelProfile } from "./meta";
import { useUiState } from "@/api";
import type { ChannelOverrides } from "@/lens";
import { ChannelHeader } from "./ChannelHeader";
import { TimeSpine } from "./TimeSpine";
import { FamilyStrip } from "./FamilyStrip";
import { LensGrid } from "./LensGrid";
import { Reader } from "./Reader";
import { FieldInspector } from "./FieldInspector";
import styles from "./Channel.module.css";

export { ChannelCard } from "./ChannelCard";
export type { ChannelCardProps } from "./ChannelCard";

export default function ChannelScreen(): ReactNode {
  const { channel } = useParams();
  const name = channel ?? "";
  const { data: boot, isLoading } = useBootstrap();

  const row = boot?.channels.find((c) => c.name === name);

  if (isLoading && !boot) {
    return (
      <div className={styles.page}>
        <Skeleton height={120} />
        <Skeleton height={140} />
        <Skeleton height={320} />
      </div>
    );
  }
  if (!row) {
    return (
      <div className={styles.page}>
        <EmptyState title="Unknown channel" description={`No channel named “${name}” is installed, or your token cannot see it.`} />
      </div>
    );
  }
  return <ChannelBody key={row.name} row={row} channels={boot!.channels} plugins={boot!.plugins} />;
}

function ChannelBody({
  row,
  channels,
  plugins,
}: {
  row: ChannelRow;
  channels: readonly ChannelRow[];
  plugins: readonly import("@/api/types").PluginRow[];
}): ReactNode {
  const cmeta = useMemo(() => channelMeta(row), [row]);
  const profileQ = useChannelProfile(cmeta);
  const ov = useUiState<ChannelOverrides>("overrides", row.name, {});
  const overrides = ov.value ?? {};
  const [inspectorOpen, setInspectorOpen] = useState(false);

  const plugin = plugins.find((p) => p.name === row.plugin) ?? null;

  // Colour index within the plugin, and the channel's family (same field signature).
  const { channelIndex, indexOf, siblings, sharedFields } = useMemo(() => {
    const metas = channels.map(channelMeta);
    const perPlugin = new Map<string, number>();
    const idxByName = new Map<string, number>();
    for (const c of channels) {
      const i = perPlugin.get(c.plugin) ?? 0;
      idxByName.set(c.name, i);
      perPlugin.set(c.plugin, i + 1);
    }
    const families = channelFamilies(metas);
    const fam = families.find((f) => f.channels.includes(row.name));
    const sibRows = fam ? channels.filter((c) => fam.channels.includes(c.name)) : [];
    return {
      channelIndex: idxByName.get(row.name) ?? 0,
      indexOf: (n: string) => idxByName.get(n) ?? 0,
      siblings: sibRows,
      sharedFields: fam?.shared_fields ?? [],
    };
  }, [channels, row.name]);

  const siblingMetas = useMemo(() => siblings.map(channelMeta), [siblings]);

  const analysis = useMemo(
    () => analyzeChannel(cmeta, profileQ.data ?? null, overrides, { siblings: siblingMetas }),
    [cmeta, profileQ.data, overrides, siblingMetas],
  );
  const analysisAll = useMemo(
    () => analyzeChannel(cmeta, profileQ.data ?? null, null, { siblings: siblingMetas }),
    [cmeta, profileQ.data, siblingMetas],
  );

  const spine = analysis.candidates.find((c) => c.spec.lens === "spine");
  const readerCandidate = analysis.candidates.find((c) => c.spec.lens === "reader");
  const gridCandidates: LensCandidate[] = analysis.candidates.filter((c) => c.spec.lens !== "spine" && c.spec.lens !== "reader");
  const predicted = analysis.archetypes.predicted;

  return (
    <div className={styles.page}>
      <ChannelHeader meta={row} channelIndex={channelIndex} archetypes={analysis.archetypes} plugin={plugin} onOpenInspector={() => setInspectorOpen(true)} />

      <TimeSpine spine={spine} />

      {siblings.length > 1 ? <FamilyStrip current={row.name} siblings={siblings as ChannelRow[]} sharedFields={sharedFields} indexOf={indexOf} /> : null}

      <section className={styles.lenses} aria-label="Lenses">
        <LensGrid channel={row.name} candidates={gridCandidates} all={analysisAll.candidates} predicted={predicted} />
      </section>

      <Reader meta={row} cmeta={cmeta} archetypes={analysis.archetypes} roles={analysis.roles} readerCandidate={readerCandidate} channelIndex={channelIndex} />

      <Drawer open={inspectorOpen} onClose={() => setInspectorOpen(false)} title="Fields" width={420}>
        <FieldInspector channel={row.name} profile={profileQ.data} roles={analysis.roles} order={(cmeta.fields ?? []).map((f) => f.name)} />
      </Drawer>
    </div>
  );
}
