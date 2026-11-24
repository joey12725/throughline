/*
 * The hero lens and the ranked lens grid. Each panel is a live view spec drawn by
 * the chart layer; a per-panel menu lets the owner pin a lens to the top or hide
 * it, both persisted as UI-state overrides keyed by channel so the ranking the
 * engine produces is the owner's to shape. Panels mount lazily as they scroll in.
 */
import { useMemo, type ReactNode } from "react";
import { MoreHorizontal, Pin, PinOff, EyeOff, Eye } from "lucide-react";
import { ChartPanel, LazyMount } from "@/charts";
import { Menu, usePopover } from "@/ui";
import { useUiState } from "@/api";
import { candidateKey, type ChannelOverrides, type LensCandidate } from "@/lens";
import styles from "./LensGrid.module.css";

export interface LensGridProps {
  channel: string;
  /** Visible, ranked candidates (spine and the native reader already removed). */
  candidates: readonly LensCandidate[];
  /** Full candidate set (no overrides), so hidden lenses can be surfaced to unhide. */
  all: readonly LensCandidate[];
  hero?: boolean;
  predicted?: boolean;
}

export function LensGrid({ channel, candidates, all, hero = true, predicted }: LensGridProps): ReactNode {
  const ov = useUiState<ChannelOverrides>("overrides", channel, {});
  const overrides = ov.value ?? {};
  const pinned = new Set(overrides.pinned ?? []);
  const hidden = overrides.hidden ?? [];

  const setPinned = async (key: string, on: boolean): Promise<void> => {
    const next = (overrides.pinned ?? []).filter((k) => k !== key);
    if (on) next.unshift(key);
    await ov.set({ ...overrides, pinned: next });
  };
  const setHidden = async (key: string, on: boolean): Promise<void> => {
    const next = (overrides.hidden ?? []).filter((k) => k !== key);
    if (on) next.push(key);
    await ov.set({ ...overrides, hidden: next, pinned: (overrides.pinned ?? []).filter((k) => k !== key) });
  };

  const hiddenTitles = useMemo(() => {
    const map = new Map<string, string>();
    for (const c of all) map.set(candidateKey(c), c.spec.title);
    return hidden.map((k) => ({ key: k, title: map.get(k) ?? k }));
  }, [all, hidden]);

  if (candidates.length === 0) {
    return <div className={styles.empty}>No pictures ranked for this channel yet.</div>;
  }

  const [first, ...rest] = candidates;
  const grid = hero ? rest : candidates;

  return (
    <div className={styles.wrap}>
      {hero && first ? (
        <LazyMount className={styles.hero} minHeight={380}>
          <LensCell candidate={first} channel={channel} pinned={pinned} onPin={setPinned} onHide={setHidden} height={360} predicted={predicted} />
        </LazyMount>
      ) : null}
      <div className={styles.grid}>
        {grid.map((c, i) => (
          <LazyMount key={`${c.spec.lens}-${i}`} minHeight={300} className={styles.gcell}>
            <LensCell candidate={c} channel={channel} pinned={pinned} onPin={setPinned} onHide={setHidden} height={250} predicted={predicted} />
          </LazyMount>
        ))}
      </div>

      {hiddenTitles.length > 0 ? (
        <div className={styles.hiddenBar}>
          <span className={styles.hiddenLabel}>Hidden lenses</span>
          {hiddenTitles.map((h) => (
            <button key={h.key} type="button" className={styles.unhide} onClick={() => void setHidden(h.key, false)}>
              <Eye size={12} /> {h.title}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function LensCell({
  candidate,
  channel,
  pinned,
  onPin,
  onHide,
  height,
  predicted,
}: {
  candidate: LensCandidate;
  channel: string;
  pinned: Set<string>;
  onPin: (key: string, on: boolean) => void;
  onHide: (key: string, on: boolean) => void;
  height: number;
  predicted?: boolean;
}): ReactNode {
  const key = candidateKey(candidate);
  const isPinned = pinned.has(key);
  const menu = usePopover();

  return (
    <div className={styles.cell}>
      <button type="button" ref={menu.anchorRef} className={styles.kebab} onClick={menu.toggle} aria-label="Lens options">
        {isPinned ? <Pin size={14} className={styles.pinned} /> : <MoreHorizontal size={15} />}
      </button>
      <Menu
        open={menu.open}
        onClose={menu.close}
        anchorRef={menu.anchorRef}
        aria-label="Lens options"
        entries={[
          { key: "pin", label: isPinned ? "Unpin" : "Pin to top", icon: isPinned ? <PinOff size={14} /> : <Pin size={14} />, onSelect: () => onPin(key, !isPinned) },
          { key: "hide", label: "Hide lens", icon: <EyeOff size={14} />, danger: true, onSelect: () => onHide(key, true) },
        ]}
      />
      {predicted ? <div className={styles.predicted}>awaiting first record</div> : null}
      <ChartPanel spec={candidate.spec} height={height} board={`channel:${channel}`} priority="normal" className={styles.panel} />
    </div>
  );
}
