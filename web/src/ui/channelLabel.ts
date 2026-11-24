/**
 * A channel's short label inside its plugin's group: the part of the name the
 * plugin does not already say. `chat.sms` under `beeper` becomes `sms` unless
 * a sibling would also become `sms`; `transcripts.wave` under `wave`
 * becomes `transcripts`, `youtube.picks` under `youtube_feed` becomes `picks`.
 * Never shortens to something empty, and falls back to the full name.
 */
export function channelLabel(
  name: string,
  plugin: string | null | undefined,
  siblings: readonly string[] = [],
): string {
  if (!plugin) return name;
  const parts = name.split(".");
  if (parts.length < 2) return name;
  const first = parts[0]!;
  const last = parts[parts.length - 1]!;
  const related = (seg: string) => seg === plugin || plugin.startsWith(`${seg}_`) || seg.startsWith(`${plugin}_`);
  if (related(first)) return parts.slice(1).join(".");
  if (related(last)) return parts.slice(0, -1).join(".");
  // Otherwise drop the shared root (chat.sms -> sms) when that stays unique
  // among the group's channels.
  const rest = parts.slice(1).join(".");
  const clash = siblings.some((s) => s !== name && s.split(".").slice(1).join(".") === rest);
  return clash ? name : rest;
}
