import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import clsx from "clsx";
import { RAIL_ITEMS, RAIL_FOOT, activeNavId, type NavItem } from "./nav";
import { usePrefs } from "@/store/prefs";
import styles from "./BottomNav.module.css";

/**
 * The phone navigation: the rail laid along the bottom of the screen as a tab
 * bar. Shown only at narrow widths (the rail is hidden there); every primary
 * screen is one icon-sized target. Icon-only with an accessible label, mirroring
 * the rail so wayfinding is identical.
 */
export function BottomNav(): ReactNode {
  const { pathname } = useLocation();
  const active = activeNavId(pathname);
  const tz = usePrefs((s) => s.tz);
  const items = [...RAIL_ITEMS, ...RAIL_FOOT];
  return (
    <nav className={styles.bar} aria-label="Primary">
      <ul className={styles.list}>
        {items.map((item) => (
          <BottomLink key={item.id} item={item} active={active === item.id} tz={tz} />
        ))}
      </ul>
    </nav>
  );
}

function BottomLink({ item, active, tz }: { item: NavItem; active: boolean; tz: string }): ReactNode {
  const Icon = item.icon;
  const to = item.id === "day" ? `/day/${todayISO(tz)}` : item.path;
  return (
    <li className={styles.item}>
      <NavLink
        to={to}
        end={item.path === "/"}
        className={clsx(styles.link, active && styles.active)}
        aria-current={active ? "page" : undefined}
        aria-label={item.label}
      >
        <span className={styles.thread} aria-hidden />
        <Icon size={20} strokeWidth={active ? 2.2 : 1.9} aria-hidden />
      </NavLink>
    </li>
  );
}

function todayISO(tz: string): string {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(
      new Date(),
    );
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}
