import { cloneElement, useId, useRef, useState, type ReactElement, type ReactNode } from "react";
import { createPortal } from "react-dom";
import styles from "./Floating.module.css";
import { useFloating, type Align, type Side } from "./floating";

export interface TooltipProps {
  content: ReactNode;
  side?: Side;
  align?: Align;
  delay?: number;
  /** a single focusable element to attach the tooltip to */
  children: ReactElement<{
    ref?: React.Ref<HTMLElement>;
    onMouseEnter?: (e: React.MouseEvent) => void;
    onMouseLeave?: (e: React.MouseEvent) => void;
    onFocus?: (e: React.FocusEvent) => void;
    onBlur?: (e: React.FocusEvent) => void;
    "aria-describedby"?: string;
  }>;
}

/** Hover/focus tooltip. Appears on keyboard focus too, dismisses on Escape/blur. */
export function Tooltip({ content, side = "top", align = "center", delay = 350, children }: TooltipProps): ReactNode {
  const anchorRef = useRef<HTMLElement | null>(null);
  const [open, setOpen] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const id = useId();
  const { floatingRef, position } = useFloating(open, anchorRef, side, align);

  const show = (): void => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setOpen(true), delay);
  };
  const hide = (): void => {
    window.clearTimeout(timer.current);
    setOpen(false);
  };

  const child = children;
  const trigger = cloneElement(child, {
    ref: (node: HTMLElement | null) => {
      anchorRef.current = node;
    },
    onMouseEnter: (e: React.MouseEvent) => {
      child.props.onMouseEnter?.(e);
      show();
    },
    onMouseLeave: (e: React.MouseEvent) => {
      child.props.onMouseLeave?.(e);
      hide();
    },
    onFocus: (e: React.FocusEvent) => {
      child.props.onFocus?.(e);
      setOpen(true);
    },
    onBlur: (e: React.FocusEvent) => {
      child.props.onBlur?.(e);
      hide();
    },
    "aria-describedby": open ? id : undefined,
  });

  return (
    <>
      {trigger}
      {open &&
        createPortal(
          <div
            ref={floatingRef}
            id={id}
            role="tooltip"
            className={styles.tooltip}
            style={{
              top: position?.top ?? -9999,
              left: position?.left ?? -9999,
              visibility: position ? "visible" : "hidden",
            }}
          >
            {content}
          </div>,
          document.body,
        )}
    </>
  );
}
