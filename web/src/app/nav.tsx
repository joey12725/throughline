/*
 * The navigation model — the single source of truth for the left rail, the
 * router and the command palette. Screens come straight from design.md's
 * Structure table. Channel, Day-by-date and Add-a-source are reachable but not
 * rail items.
 */
import type { LucideIcon } from "lucide-react";
import {
  LayoutGrid,
  Database,
  Radio,
  Terminal,
  Users,
  CalendarDays,
  MapPin,
  Share2,
  ScrollText,
  Settings,
  Palette,
  Tags,
} from "lucide-react";

export interface NavItem {
  id: string;
  path: string;
  label: string;
  /** document.title for this screen */
  title: string;
  icon: LucideIcon;
  /** show in the left rail */
  inRail: boolean;
  /** pin to the foot of the rail (Settings) */
  foot?: boolean;
  /** match as the active rail item for these path prefixes too */
  activePrefixes?: string[];
}

export const NAV: NavItem[] = [
  { id: "loom", path: "/", label: "Loom", title: "Loom", icon: LayoutGrid, inRail: true, activePrefixes: [] },
  { id: "sources", path: "/sources", label: "Sources", title: "Sources", icon: Database, inRail: true, activePrefixes: ["/sources", "/c/"] },
  { id: "live", path: "/live", label: "Live", title: "Live", icon: Radio, inRail: true },
  { id: "query", path: "/query", label: "Query", title: "Query", icon: Terminal, inRail: true },
  { id: "people", path: "/people", label: "People", title: "People", icon: Users, inRail: true, activePrefixes: ["/people"] },
  { id: "day", path: "/day", label: "Day", title: "Day", icon: CalendarDays, inRail: true, activePrefixes: ["/day"] },
  { id: "places", path: "/places", label: "Places", title: "Places", icon: MapPin, inRail: true },
  { id: "graph", path: "/graph", label: "Graph", title: "Graph", icon: Share2, inRail: true },
  { id: "ledger", path: "/ledger", label: "Ledger", title: "Ledger", icon: ScrollText, inRail: true },
  { id: "classifiers", path: "/classifiers", label: "Classifiers", title: "Classifiers", icon: Tags, inRail: true, activePrefixes: ["/classifiers"] },
  { id: "settings", path: "/settings", label: "Settings", title: "Settings", icon: Settings, inRail: true, foot: true },
  { id: "styleguide", path: "/styleguide", label: "Style guide", title: "Style guide", icon: Palette, inRail: false },
];

export const RAIL_ITEMS = NAV.filter((n) => n.inRail && !n.foot);
export const RAIL_FOOT = NAV.filter((n) => n.inRail && n.foot);

/** Find the active rail item for a pathname. */
export function activeNavId(pathname: string): string {
  // Longest-prefix wins, so /sources/add highlights Sources, /c/x too.
  let best: NavItem | null = null;
  for (const item of NAV) {
    const prefixes = [item.path, ...(item.activePrefixes ?? [])].filter(Boolean);
    for (const p of prefixes) {
      if (p === "/" ? pathname === "/" : pathname === p || pathname.startsWith(p + "/") || pathname.startsWith(p)) {
        if (!best || p.length > best.path.length) best = item;
      }
    }
  }
  return best?.id ?? "loom";
}

export function titleFor(pathname: string): string {
  const id = activeNavId(pathname);
  const item = NAV.find((n) => n.id === id);
  return item?.title ?? "Throughline";
}
