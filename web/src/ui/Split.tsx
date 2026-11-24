import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import clsx from "clsx";
import styles from "./Split.module.css";

export interface SplitProps {
  direction?: "horizontal" | "vertical";
  /** initial size of the first pane, in px */
  initial?: number;
  min?: number;
  max?: number;
  /** persist the size under this key (localStorage) */
  storageKey?: string;
  first: ReactNode;
  second: ReactNode;
  className?: string;
}

/** Two resizable panes with a draggable, keyboard-operable divider. */
export function Split({
  direction = "horizontal",
  initial = 320,
  min = 160,
  max = 720,
  storageKey,
  first,
  second,
  className,
}: SplitProps): ReactNode {
  const horizontal = direction === "horizontal";
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<number>(() => {
    if (storageKey) {
      try {
        const v = localStorage.getItem(`tl.split.${storageKey}`);
        if (v) return clamp(Number(v), min, max);
      } catch {
        /* ignore */
      }
    }
    return initial;
  });
  const dragging = useRef(false);

  const persist = useCallback(
    (v: number) => {
      if (!storageKey) return;
      try {
        localStorage.setItem(`tl.split.${storageKey}`, String(Math.round(v)));
      } catch {
        /* ignore */
      }
    },
    [storageKey],
  );

  useEffect(() => {
    function onMove(e: PointerEvent): void {
      if (!dragging.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const raw = horizontal ? e.clientX - rect.left : e.clientY - rect.top;
      setSize(clamp(raw, min, max));
    }
    function onUp(): void {
      if (dragging.current) {
        dragging.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        setSize((s) => {
          persist(s);
          return s;
        });
      }
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [horizontal, min, max, persist]);

  function startDrag(): void {
    dragging.current = true;
    document.body.style.cursor = horizontal ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";
  }

  function onKey(e: React.KeyboardEvent): void {
    const step = e.shiftKey ? 32 : 8;
    const dec = horizontal ? "ArrowLeft" : "ArrowUp";
    const inc = horizontal ? "ArrowRight" : "ArrowDown";
    if (e.key === dec || e.key === inc) {
      e.preventDefault();
      setSize((s) => {
        const next = clamp(s + (e.key === inc ? step : -step), min, max);
        persist(next);
        return next;
      });
    }
  }

  return (
    <div ref={containerRef} className={clsx(styles.split, horizontal ? styles.h : styles.v, className)}>
      <div className={styles.pane} style={horizontal ? { width: size } : { height: size }}>
        {first}
      </div>
      <div
        role="separator"
        aria-orientation={horizontal ? "vertical" : "horizontal"}
        aria-valuenow={Math.round(size)}
        aria-valuemin={min}
        aria-valuemax={max}
        tabIndex={0}
        className={styles.divider}
        onPointerDown={startDrag}
        onKeyDown={onKey}
      >
        <span className={styles.grip} />
      </div>
      <div className={clsx(styles.pane, styles.grow)}>{second}</div>
    </div>
  );
}

function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}
