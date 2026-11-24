import type { ReactNode } from "react";
import clsx from "clsx";
import styles from "./Kbd.module.css";

/** A keyboard key cap. Pass several children for a chord (e.g. ⌘ K). */
export function Kbd({ children, className }: { children: ReactNode; className?: string }): ReactNode {
  return <kbd className={clsx(styles.kbd, className)}>{children}</kbd>;
}

/** Platform-aware ⌘ / Ctrl symbol. */
export function cmdKey(): string {
  if (typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform)) return "⌘";
  return "Ctrl";
}
