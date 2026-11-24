import { useCallback, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";
import styles from "./Floating.module.css";
import { useDismiss, useFloating, type Align, type Side } from "./floating";

export interface PopoverProps {
  open: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLElement | null>;
  side?: Side;
  align?: Align;
  className?: string;
  "aria-label"?: string;
  role?: "dialog" | "menu" | "listbox";
  children: ReactNode;
}

/** A floating surface anchored to a trigger, dismissed on outside click/Escape. */
export function Popover({
  open,
  onClose,
  anchorRef,
  side = "bottom",
  align = "start",
  className,
  role = "dialog",
  children,
  ...rest
}: PopoverProps): ReactNode {
  const { floatingRef, position } = useFloating(open, anchorRef, side, align);
  useDismiss(open, onClose, [floatingRef, anchorRef]);
  if (!open) return null;
  return createPortal(
    <div
      ref={floatingRef}
      role={role}
      aria-label={rest["aria-label"]}
      className={clsx(styles.popover, className)}
      style={{
        top: position?.top ?? -9999,
        left: position?.left ?? -9999,
        visibility: position ? "visible" : "hidden",
      }}
    >
      {children}
    </div>,
    document.body,
  );
}

export interface PopoverControl {
  open: boolean;
  toggle: () => void;
  close: () => void;
  anchorRef: React.RefObject<HTMLButtonElement | null>;
}

/** Convenience state for a trigger+popover pair. */
export function usePopover(): PopoverControl {
  const anchorRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const toggle = useCallback(() => setOpen((v) => !v), []);
  const close = useCallback(() => setOpen(false), []);
  return { open, toggle, close, anchorRef };
}
