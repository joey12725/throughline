/*
 * Minimal floating helpers — anchored positioning and dismiss — so Tooltip,
 * Popover and Menu share one small, dependency-free implementation. Positions
 * are computed with getBoundingClientRect and clamped to the viewport.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

export type Side = "top" | "bottom" | "left" | "right";
export type Align = "start" | "center" | "end";

export interface FloatingPosition {
  top: number;
  left: number;
  side: Side;
}

const GAP = 6;
const MARGIN = 8;

export function computePosition(
  anchor: DOMRect,
  floating: { width: number; height: number },
  side: Side,
  align: Align,
): FloatingPosition {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let top = 0;
  let left = 0;
  let resolved = side;

  // Flip vertically if there is not enough room.
  if (side === "bottom" && anchor.bottom + GAP + floating.height > vh - MARGIN && anchor.top - GAP - floating.height > MARGIN) {
    resolved = "top";
  } else if (side === "top" && anchor.top - GAP - floating.height < MARGIN && anchor.bottom + GAP + floating.height < vh - MARGIN) {
    resolved = "bottom";
  }

  if (resolved === "bottom" || resolved === "top") {
    top = resolved === "bottom" ? anchor.bottom + GAP : anchor.top - GAP - floating.height;
    if (align === "start") left = anchor.left;
    else if (align === "end") left = anchor.right - floating.width;
    else left = anchor.left + anchor.width / 2 - floating.width / 2;
  } else {
    left = resolved === "right" ? anchor.right + GAP : anchor.left - GAP - floating.width;
    if (align === "start") top = anchor.top;
    else if (align === "end") top = anchor.bottom - floating.height;
    else top = anchor.top + anchor.height / 2 - floating.height / 2;
  }

  left = Math.max(MARGIN, Math.min(left, vw - floating.width - MARGIN));
  top = Math.max(MARGIN, Math.min(top, vh - floating.height - MARGIN));
  return { top, left, side: resolved };
}

export function useFloating(
  open: boolean,
  anchorRef: React.RefObject<HTMLElement | null>,
  side: Side = "bottom",
  align: Align = "center",
): { floatingRef: React.RefObject<HTMLDivElement | null>; position: FloatingPosition | null } {
  const floatingRef = useRef<HTMLDivElement | null>(null);
  const [position, setPosition] = useState<FloatingPosition | null>(null);

  const update = useCallback(() => {
    const a = anchorRef.current;
    const f = floatingRef.current;
    if (!a || !f) return;
    const rect = a.getBoundingClientRect();
    setPosition(computePosition(rect, { width: f.offsetWidth, height: f.offsetHeight }, side, align));
  }, [anchorRef, side, align]);

  useLayoutEffect(() => {
    if (!open) {
      setPosition(null);
      return;
    }
    update();
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [open, update]);

  return { floatingRef, position };
}

/** Close on outside pointerdown and on Escape. */
export function useDismiss(
  open: boolean,
  onClose: () => void,
  refs: ReadonlyArray<React.RefObject<HTMLElement | null>>,
): void {
  useEffect(() => {
    if (!open) return;
    function onPointer(e: PointerEvent): void {
      const target = e.target as Node;
      if (refs.some((r) => r.current && r.current.contains(target))) return;
      onClose();
    }
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    }
    document.addEventListener("pointerdown", onPointer, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onPointer, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open, onClose, refs]);
}
