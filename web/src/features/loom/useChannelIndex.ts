/*
 * The channel index: everything the Loom and Live need to lay out channels by
 * plugin and render an arrival by its archetype, derived from the bootstrap
 * shape alone (no channel or plugin name is special-cased). The reader variant
 * comes from the lens engine's declared-only analysis, so a channel with zero
 * rows still gets a sensible card.
 *
 * This is a leaf module (it imports only @/api and @/lens) so both features can
 * share it without a circular import.
 */
import { useMemo } from "react";
import { useBootstrap } from "@/api";
import type { Bootstrap, ChannelRow, PluginRow } from "@/api/types";
import {
  analyzeChannel,
  declaredProfile,
  readerVariant,
  type ArchetypeId,
  type ReaderVariant,
} from "@/lens";
import { channelMeta } from "@/features/channel/meta";

export interface ChannelInfo {
  name: string;
  plugin: string;
  /** stable index of this channel within its plugin group (for the colour step) */
  channelIndex: number;
  archetype: ArchetypeId;
  variant: ReaderVariant;
  row: ChannelRow;
}

export interface PluginGroup {
  plugin: string;
  /** index into the bootstrap plugin order (stable hue seed) */
  order: number;
  channels: ChannelInfo[];
  rows: number;
  pluginRow: PluginRow | null;
}

export interface ChannelIndex {
  byName: Map<string, ChannelInfo>;
  groups: PluginGroup[];
  pluginByName: Map<string, PluginRow>;
  /** total records across visible channels (for shares) */
  totalRows: number;
}

/** Pure build so it can be unit-tested and reused off a bootstrap value. */
export function buildChannelIndex(boot: Bootstrap): ChannelIndex {
  const pluginByName = new Map<string, PluginRow>();
  boot.plugins.forEach((p) => pluginByName.set(p.name, p));

  const pluginOrder = new Map<string, number>();
  boot.plugins.forEach((p, i) => pluginOrder.set(p.name, i));

  // Group channels by plugin, keeping a stable per-plugin order (volume desc,
  // then name) so the colour lightness step is deterministic across renders.
  const byPlugin = new Map<string, ChannelRow[]>();
  for (const c of boot.channels) {
    const list = byPlugin.get(c.plugin) ?? [];
    list.push(c);
    byPlugin.set(c.plugin, list);
  }

  const byName = new Map<string, ChannelInfo>();
  const groups: PluginGroup[] = [];
  let totalRows = 0;

  const pluginNames = [...byPlugin.keys()].sort(
    (a, b) => (pluginOrder.get(a) ?? 999) - (pluginOrder.get(b) ?? 999) || a.localeCompare(b),
  );

  for (const plugin of pluginNames) {
    const chans = (byPlugin.get(plugin) ?? [])
      .slice()
      .sort((a, b) => (b.rows || 0) - (a.rows || 0) || a.name.localeCompare(b.name));
    const infos: ChannelInfo[] = chans.map((c, i) => {
      const meta = channelMeta(c);
      let archetype: ArchetypeId = "registry";
      try {
        archetype = analyzeChannel(meta, declaredProfile(meta)).archetypes.primary;
      } catch {
        /* keep fallback */
      }
      const info: ChannelInfo = {
        name: c.name,
        plugin,
        channelIndex: i,
        archetype,
        variant: readerVariant(archetype),
        row: c,
      };
      byName.set(c.name, info);
      return info;
    });
    const rows = infos.reduce((s, c) => s + (c.row.rows || 0), 0);
    totalRows += rows;
    groups.push({
      plugin,
      order: pluginOrder.get(plugin) ?? 999,
      channels: infos,
      rows,
      pluginRow: pluginByName.get(plugin) ?? null,
    });
  }

  // Heaviest plugins first — the eye lands on the densest sources.
  groups.sort((a, b) => b.rows - a.rows || a.order - b.order);

  return { byName, groups, pluginByName, totalRows };
}

export function useChannelIndex(): ChannelIndex | null {
  const { data: boot } = useBootstrap();
  return useMemo(() => (boot ? buildChannelIndex(boot) : null), [boot]);
}
