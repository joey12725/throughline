/*
 * React runtime for charts: element size (ResizeObserver), reduced-motion, and
 * a device-pixel-ratio aware canvas that re-paints on size or theme change.
 */
import { useCallback, useEffect, useRef, useState } from "react";

export interface Size {
  width: number;
  height: number;
}

/** Observe an element's content box. Returns a ref and its current size. */
export function useElementSize<T extends HTMLElement = HTMLDivElement>(): [React.RefObject<T | null>, Size] {
  const ref = useRef<T>(null);
  const [size, setSize] = useState<Size>({ width: 0, height: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      if (!e) return;
      const box = e.contentRect;
      setSize((prev) =>
        Math.abs(prev.width - box.width) < 0.5 && Math.abs(prev.height - box.height) < 0.5
          ? prev
          : { width: Math.round(box.width), height: Math.round(box.height) },
      );
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size];
}

/** Whether the viewer asked for reduced motion. Re-reads on change. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch {
      return false;
    }
  });
  useEffect(() => {
    let mql: MediaQueryList;
    try {
      mql = window.matchMedia("(prefers-reduced-motion: reduce)");
    } catch {
      return;
    }
    const on = () => setReduced(mql.matches);
    mql.addEventListener("change", on);
    return () => mql.removeEventListener("change", on);
  }, []);
  return reduced;
}

/**
 * A DPR-aware canvas painter. Pass a draw(ctx, size) that reads tokens for the
 * current theme; it re-runs on size change, on theme change (via `deps`) and on
 * DPR change. The canvas backing store is scaled so 1 unit == 1 CSS pixel.
 */
export function useCanvas(
  draw: (ctx: CanvasRenderingContext2D, size: Size) => void,
  size: Size,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  deps: ReadonlyArray<any>,
): React.RefObject<HTMLCanvasElement | null> {
  const ref = useRef<HTMLCanvasElement>(null);
  const drawRef = useRef(draw);
  drawRef.current = draw;

  const paint = useCallback(() => {
    const canvas = ref.current;
    if (!canvas || size.width <= 0 || size.height <= 0) return;
    const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
    canvas.width = Math.round(size.width * dpr);
    canvas.height = Math.round(size.height * dpr);
    canvas.style.width = `${size.width}px`;
    canvas.style.height = `${size.height}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.width, size.height);
    drawRef.current(ctx, size);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size.width, size.height, ...deps]);

  useEffect(() => {
    paint();
  }, [paint]);

  // Repaint when DPR changes (e.g. moving to another monitor).
  useEffect(() => {
    let mql: MediaQueryList | null = null;
    try {
      mql = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
      mql.addEventListener("change", paint);
    } catch {
      /* ignore */
    }
    return () => mql?.removeEventListener("change", paint);
  }, [paint]);

  return ref;
}
