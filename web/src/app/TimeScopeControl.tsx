import { useRef, useState, type ReactNode } from "react";
import { CalendarRange, ChevronDown, X } from "lucide-react";
import { Popover, Button, Field, Input } from "@/ui";
import { useScopes, formatDateInTz } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import styles from "./TimeScopeControl.module.css";

interface Preset {
  id: string;
  label: string;
  days: number;
}
const PRESETS: Preset[] = [
  { id: "7d", label: "7 days", days: 7 },
  { id: "30d", label: "30 days", days: 30 },
  { id: "90d", label: "90 days", days: 90 },
  { id: "365d", label: "1 year", days: 365 },
];

/**
 * The header's time-scope control: presets, a custom range and clear. It writes
 * the global :from/:to scope; a brush on any chart sets the same store, so the
 * two stay interchangeable.
 */
export function TimeScopeControl(): ReactNode {
  const { from, to, setRange, clearRange } = useScopes();
  const tz = usePrefs((s) => s.tz);
  const anchorRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const [draftFrom, setDraftFrom] = useState("");
  const [draftTo, setDraftTo] = useState("");

  const isAll = from === null && to === null;
  const label = isAll ? "All time" : `${from ? formatDateInTz(from, tz) : "…"} – ${to ? formatDateInTz(to, tz) : "now"}`;

  function applyPreset(days: number): void {
    const now = new Date();
    const start = new Date(now.getTime() - days * 86400_000);
    setRange(start.toISOString(), now.toISOString());
    setOpen(false);
  }

  function applyCustom(): void {
    const f = draftFrom ? new Date(draftFrom).toISOString() : null;
    const t = draftTo ? new Date(draftTo).toISOString() : null;
    setRange(f, t);
    setOpen(false);
  }

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        className={styles.trigger}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <CalendarRange size={15} />
        <span className={styles.label}>{label}</span>
        {!isAll && (
          <span
            className={styles.clear}
            role="button"
            aria-label="Clear time scope"
            tabIndex={-1}
            onClick={(e) => {
              e.stopPropagation();
              clearRange();
            }}
          >
            <X size={13} />
          </span>
        )}
        <ChevronDown size={14} className={styles.chevron} />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} anchorRef={anchorRef} align="start" aria-label="Time scope">
        <div className={styles.menu}>
          <div className={styles.presets}>
            {PRESETS.map((p) => (
              <button key={p.id} type="button" className={styles.preset} onClick={() => applyPreset(p.days)}>
                {p.label}
              </button>
            ))}
            <button type="button" className={styles.preset} onClick={() => { clearRange(); setOpen(false); }}>
              All time
            </button>
          </div>
          <div className={styles.custom}>
            <Field label="From" htmlFor="scope-from">
              <Input id="scope-from" type="date" value={draftFrom} onChange={(e) => setDraftFrom(e.target.value)} />
            </Field>
            <Field label="To" htmlFor="scope-to">
              <Input id="scope-to" type="date" value={draftTo} onChange={(e) => setDraftTo(e.target.value)} />
            </Field>
            <Button variant="primary" size="sm" block onClick={applyCustom}>
              Apply range
            </Button>
          </div>
        </div>
      </Popover>
    </>
  );
}
