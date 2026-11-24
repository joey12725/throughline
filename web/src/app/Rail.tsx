import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import clsx from "clsx";
import { Tooltip } from "@/ui";
import { RAIL_ITEMS, RAIL_FOOT, activeNavId, type NavItem } from "./nav";
import { usePrefs } from "@/store/prefs";
import styles from "./Rail.module.css";

export function Rail(): ReactNode {
  const { pathname } = useLocation();
  const active = activeNavId(pathname);
  const tz = usePrefs((s) => s.tz);
  return (
    <nav className={styles.rail} aria-label="Primary">
      <NavLink to="/" className={styles.brand} aria-label="Throughline — Loom">
        <img src="/ui/icon.svg" alt="" width={26} height={26} draggable={false} />
      </NavLink>
      <ul className={styles.list}>
        {RAIL_ITEMS.map((item) => (
          <RailLink key={item.id} item={item} active={active === item.id} tz={tz} />
        ))}
      </ul>
      <ul className={clsx(styles.list, styles.foot)}>
        {RAIL_FOOT.map((item) => (
          <RailLink key={item.id} item={item} active={active === item.id} tz={tz} />
        ))}
      </ul>
    </nav>
  );
}

function RailLink({ item, active, tz }: { item: NavItem; active: boolean; tz: string }): ReactNode {
  const Icon = item.icon;
  // Day's rail entry opens today's local date.
  const to = item.id === "day" ? `/day/${todayISO(tz)}` : item.path;
  return (
    <li>
      <Tooltip content={item.label} side="right">
        <NavLink
          to={to}
          end={item.path === "/"}
          className={clsx(styles.link, active && styles.active)}
          aria-current={active ? "page" : undefined}
        >
          <span className={styles.thread} aria-hidden />
          <Icon size={19} strokeWidth={active ? 2.2 : 1.9} aria-hidden />
          <span className="sr-only">{item.label}</span>
        </NavLink>
      </Tooltip>
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
