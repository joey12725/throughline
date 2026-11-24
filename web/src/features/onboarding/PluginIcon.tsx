import { type ReactNode } from "react";
import { DynamicIcon, iconNames } from "lucide-react/dynamic";
import { Plug } from "lucide-react";

const NAMES = new Set<string>(iconNames);

/** PascalCase or spaced names → kebab-case, which DynamicIcon expects. */
function toKebab(name: string): string {
  return name
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .replace(/[\s_]+/g, "-")
    .toLowerCase();
}

/**
 * Render a plugin/provider icon by the lucide name the catalog gives us (a
 * string). Unknown names fall back to a plug. The name map is loaded lazily, so
 * only the icons actually shown are fetched.
 */
export function PluginIcon({ name, size = 18 }: { name?: string | null; size?: number }): ReactNode {
  const kebab = name ? toKebab(name) : "";
  if (!kebab || !NAMES.has(kebab)) return <Plug size={size} aria-hidden />;
  return <DynamicIcon name={kebab as never} size={size} fallback={() => <Plug size={size} aria-hidden />} />;
}
