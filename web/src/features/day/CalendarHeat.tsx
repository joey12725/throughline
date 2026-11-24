/*
 * A compact calendar heat for the months around the selected day — each cell
 * shaded by how many records landed that day on one sequential ramp (from the
 * chart tokens), the day number always legible, today ringed and the selected
 * day filled in brass. Click a day to jump. Weeks start Monday.
 */
import { useMemo, type ReactNode } from "react";
import { Panel } from "@/ui";
import { useChartColors } from "@/charts";
import { ymd } from "./episodes";
import styles from "./Day.module.css";

const DOW = ["M", "T", "W", "T", "F", "S", "S"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const LEVELS = 5;
const MONTHS_BACK = 5; // selected month + five before it

export function CalendarHeat({
  selected,
  today,
  heat,
  onJump,
}: {
  selected: string;
  today: string;
  heat: Map<string, number>;
  onJump: (date: string) => void;
}): ReactNode {
  const [y, m] = ymd(selected);
  const colors = useChartColors();
  const ramp = useMemo(() => colors.sequential(LEVELS), [colors]);

  const months = useMemo(() => {
    const out: { year: number; month: number }[] = [];
    for (let k = MONTHS_BACK; k >= 0; k--) {
      const d = new Date(Date.UTC(y, m - 1 - k, 1));
      out.push({ year: d.getUTCFullYear(), month: d.getUTCMonth() + 1 });
    }
    return out;
  }, [y, m]);

  const max = useMemo(() => Math.max(1, ...heat.values()), [heat]);

  return (
    <Panel title="Jump to a day" subtitle="Records per day">
      <div className={styles.calWrap}>
        {months.map((mo) => (
          <MonthGrid
            key={`${mo.year}-${mo.month}`}
            year={mo.year}
            month={mo.month}
            heat={heat}
            max={max}
            ramp={ramp}
            selected={selected}
            today={today}
            onJump={onJump}
          />
        ))}
      </div>
      <div className={styles.calLegend}>
        <span>less</span>
        <span className={styles.calLegendCells}>
          <span className={styles.calLegendCell} style={{ background: "var(--bg-2)" }} />
          {ramp.map((c, i) => (
            <span key={i} className={styles.calLegendCell} style={{ background: c }} />
          ))}
        </span>
        <span>more</span>
      </div>
    </Panel>
  );
}

function MonthGrid({
  year,
  month,
  heat,
  max,
  ramp,
  selected,
  today,
  onJump,
}: {
  year: number;
  month: number;
  heat: Map<string, number>;
  max: number;
  ramp: string[];
  selected: string;
  today: string;
  onJump: (date: string) => void;
}): ReactNode {
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const firstDow = (new Date(Date.UTC(year, month - 1, 1)).getUTCDay() + 6) % 7;
  const cells: (string | null)[] = [];
  for (let i = 0; i < firstDow; i++) cells.push(null);
  for (let d = 1; d <= days; d++) {
    cells.push(`${year}-${String(month).padStart(2, "0")}-${String(d).padStart(2, "0")}`);
  }

  return (
    <div className={styles.calMonth}>
      <div className={styles.calTitle}>
        {MONTHS[month - 1]} <span className={styles.calYear}>{year}</span>
      </div>
      <div className={styles.calGrid}>
        {DOW.map((d, i) => (
          <div key={i} className={styles.calDow}>
            {d}
          </div>
        ))}
        {cells.map((cell, i) => {
          if (!cell) return <div key={i} className={styles.calCell} data-empty="true" />;
          const n = heat.get(cell) ?? 0;
          const level = n > 0 ? Math.min(LEVELS - 1, Math.floor((n / max) * LEVELS)) : -1;
          const day = Number(cell.slice(8));
          const filled = level >= 0;
          return (
            <button
              key={i}
              type="button"
              className={styles.calCell}
              data-selected={cell === selected}
              data-today={cell === today}
              data-filled={filled}
              data-strong={level >= LEVELS - 2}
              onClick={() => onJump(cell)}
              aria-label={`${cell}: ${n} records`}
              aria-current={cell === selected ? "date" : undefined}
              title={`${cell} · ${n.toLocaleString("en-US")} records`}
              style={filled ? { background: ramp[level] } : undefined}
            >
              <span className={styles.calCellNum}>{day}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
