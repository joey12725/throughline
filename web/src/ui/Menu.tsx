import { useEffect, useRef, type ReactNode } from "react";
import clsx from "clsx";
import { Check } from "lucide-react";
import styles from "./Floating.module.css";
import { Popover } from "./Popover";
import type { Align, Side } from "./floating";

export interface MenuItem {
  key: string;
  label: ReactNode;
  icon?: ReactNode;
  onSelect?: () => void;
  checked?: boolean;
  danger?: boolean;
  disabled?: boolean;
}

export type MenuEntry = MenuItem | { separator: true } | { header: ReactNode };

export interface MenuProps {
  open: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLElement | null>;
  entries: ReadonlyArray<MenuEntry>;
  side?: Side;
  align?: Align;
  "aria-label": string;
}

export function Menu({ open, onClose, anchorRef, entries, side, align = "start", ...rest }: MenuProps): ReactNode {
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const first = listRef.current?.querySelector<HTMLElement>("[role='menuitem']:not([aria-disabled='true'])");
    first?.focus();
  }, [open]);

  function onKey(e: React.KeyboardEvent): void {
    const items = Array.from(
      listRef.current?.querySelectorAll<HTMLElement>("[role='menuitem']:not([aria-disabled='true'])") ?? [],
    );
    const idx = items.indexOf(document.activeElement as HTMLElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      items[(idx + 1) % items.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      items[(idx - 1 + items.length) % items.length]?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      items[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      items[items.length - 1]?.focus();
    }
  }

  return (
    <Popover open={open} onClose={onClose} anchorRef={anchorRef} side={side} align={align} role="menu" aria-label={rest["aria-label"]} className={styles.menu}>
      <div ref={listRef} onKeyDown={onKey}>
        {entries.map((entry, i) => {
          if ("separator" in entry) return <div key={i} className={styles.menuSep} role="separator" />;
          if ("header" in entry) return <div key={i} className={styles.menuHeader}>{entry.header}</div>;
          return (
            <button
              key={entry.key}
              role="menuitem"
              type="button"
              aria-disabled={entry.disabled || undefined}
              className={clsx(styles.menuItem, entry.danger && styles.menuDanger)}
              onClick={() => {
                if (entry.disabled) return;
                entry.onSelect?.();
                onClose();
              }}
            >
              <span className={styles.menuItemIcon} aria-hidden>
                {entry.checked ? <Check size={15} /> : entry.icon}
              </span>
              <span className={styles.menuItemLabel}>{entry.label}</span>
            </button>
          );
        })}
      </div>
    </Popover>
  );
}
