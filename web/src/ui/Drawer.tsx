import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";
import { X } from "lucide-react";
import styles from "./Drawer.module.css";
import { IconButton } from "./Button";
import { getDrawerRenderer, useDrawers } from "./drawerStore";

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  /** 0 = frontmost; higher = deeper in the stack (offset to the left) */
  depth?: number;
  stackSize?: number;
  width?: number;
  toolbar?: ReactNode;
}

/** A single right-hand drawer panel. Used directly, or driven by DrawerHost. */
export function Drawer({ open, onClose, title, children, depth = 0, stackSize = 1, width, toolbar }: DrawerProps): ReactNode {
  const ref = useRef<HTMLDivElement | null>(null);
  const isTop = depth === 0;

  useEffect(() => {
    if (!open || !isTop) return;
    const el = ref.current;
    const prev = document.activeElement as HTMLElement | null;
    el?.querySelector<HTMLElement>("[data-autofocus], button, [href], input, select, textarea")?.focus();
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    }
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      prev?.focus?.();
    };
  }, [open, isTop, onClose]);

  if (!open) return null;
  const offset = depth * 28;
  return createPortal(
    <>
      {isTop && <div className={styles.scrim} onClick={onClose} aria-hidden />}
      <aside
        ref={ref}
        className={clsx(styles.drawer, !isTop && styles.behind)}
        role="dialog"
        aria-modal={isTop}
        aria-label={typeof title === "string" ? title : "Panel"}
        style={{
          width: width ?? "var(--drawer-w)",
          transform: `translateX(${-offset}px)`,
          zIndex: 1050 + (stackSize - depth),
          opacity: depth > 1 ? 0 : 1,
          pointerEvents: isTop ? "auto" : "none",
        }}
      >
        <header className={styles.header}>
          <div className={styles.titleRow}>
            {title && <h2 className={styles.title}>{title}</h2>}
            <div className={styles.toolbar}>
              {toolbar}
              <IconButton label="Close panel" onClick={onClose}>
                <X />
              </IconButton>
            </div>
          </div>
        </header>
        <div className={styles.body}>{children}</div>
      </aside>
    </>,
    document.body,
  );
}

/** Renders the whole drawer stack from the store + registry. Mount once. */
export function DrawerHost(): ReactNode {
  const stack = useDrawers((s) => s.stack);
  const closeTop = useDrawers((s) => s.closeTop);
  if (!stack.length) return null;
  return (
    <>
      {stack.map((spec, i) => {
        const depth = stack.length - 1 - i;
        const Renderer = getDrawerRenderer(spec.key);
        return (
          <Drawer
            key={`${spec.key}-${i}`}
            open
            depth={depth}
            stackSize={stack.length}
            onClose={depth === 0 ? closeTop : () => {}}
          >
            {Renderer ? (
              <Renderer params={spec.params} close={closeTop} depth={depth} />
            ) : (
              <div className={styles.missing}>No panel registered for “{spec.key}”.</div>
            )}
          </Drawer>
        );
      })}
    </>
  );
}
