/*
 * Source-colour provider. Holds the plugin -> hue assignment, seeds it from the
 * bootstrap plugin order (so the fixed hue sequence is stable), and persists it
 * through UI state (kind "colors") with a localStorage fallback. Exposes helpers
 * that resolve to the active theme, plus a canvas-ready rgb() form for charts.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  assignPlugins,
  css as cssColor,
  emptyAssignments,
  HUE_ORDER,
  pluginHue,
  reassignPlugin,
  rgb as rgbColor,
  type SourceAssignments,
  type ThemeName,
} from "./sourceColor";
import { getState, putState } from "@/api/uiState";
import { usePrefs, resolveTheme } from "@/store/prefs";

interface SourceColorValue {
  assignments: SourceAssignments;
  theme: ThemeName;
  hueOrder: readonly number[];
  hue: (plugin: string) => number;
  /** CSS oklch string resolved to the active theme. */
  cssFor: (plugin: string, channelIndex?: number, alpha?: number) => string;
  /** rgb() string for <canvas>, resolved to the active theme. */
  rgbFor: (plugin: string, channelIndex?: number, alpha?: number) => string;
  reassign: (plugin: string, hueIndex: number) => void;
}

const Ctx = createContext<SourceColorValue | null>(null);
const COLORS_KEY = "assignments";

export function SourceColorProvider({
  pluginOrder,
  children,
}: {
  pluginOrder: readonly string[];
  children: ReactNode;
}): ReactNode {
  const [assignments, setAssignments] = useState<SourceAssignments>(emptyAssignments);
  const themePref = usePrefs((s) => s.theme);
  const theme = resolveTheme(themePref);

  // Load persisted assignments once, then fold in any new plugins.
  useEffect(() => {
    let alive = true;
    void (async () => {
      const stored = await getState<SourceAssignments>("colors", COLORS_KEY).catch(() => null);
      if (!alive) return;
      const seed = stored?.value?.plugins ? stored.value : emptyAssignments;
      const next = assignPlugins(seed, pluginOrder);
      setAssignments(next);
      if (JSON.stringify(next) !== JSON.stringify(seed)) {
        void putState("colors", COLORS_KEY, next);
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pluginOrder.join(",")]);

  const reassign = useCallback((plugin: string, hueIndex: number) => {
    setAssignments((prev) => {
      const next = reassignPlugin(prev, plugin, hueIndex);
      void putState("colors", COLORS_KEY, next);
      return next;
    });
  }, []);

  const value = useMemo<SourceColorValue>(
    () => ({
      assignments,
      theme,
      hueOrder: HUE_ORDER,
      hue: (plugin) => pluginHue(assignments, plugin),
      cssFor: (plugin, channelIndex = 0, alpha = 1) =>
        cssColor(assignments, plugin, channelIndex, theme, alpha),
      rgbFor: (plugin, channelIndex = 0, alpha = 1) =>
        rgbColor(assignments, plugin, channelIndex, theme, alpha),
      reassign,
    }),
    [assignments, theme, reassign],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSourceColor(): SourceColorValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSourceColor must be used within SourceColorProvider");
  return v;
}
