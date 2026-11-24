/*
 * Colour lightness belongs to a channel's position within its plugin. Both the
 * board and the detail page need the same stable index, so it lives here: sort a
 * plugin's channels by name and number them from 0.
 */
import type { ChannelRow } from "@/api/types";

export function channelIndexMap(channels: readonly ChannelRow[]): Map<string, number> {
  const byPlugin = new Map<string, ChannelRow[]>();
  for (const c of channels) {
    const list = byPlugin.get(c.plugin) ?? [];
    list.push(c);
    byPlugin.set(c.plugin, list);
  }
  const idx = new Map<string, number>();
  for (const list of byPlugin.values()) {
    list.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    list.forEach((c, i) => idx.set(c.name, i));
  }
  return idx;
}

export function channelsByPlugin(channels: readonly ChannelRow[]): Map<string, ChannelRow[]> {
  const byPlugin = new Map<string, ChannelRow[]>();
  for (const c of channels) {
    const list = byPlugin.get(c.plugin) ?? [];
    list.push(c);
    byPlugin.set(c.plugin, list);
  }
  for (const list of byPlugin.values()) list.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  return byPlugin;
}
