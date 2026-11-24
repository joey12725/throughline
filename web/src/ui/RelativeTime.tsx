import { type ReactNode } from "react";
import { usePrefs } from "@/store/prefs";
import { formatInTz, relative } from "@/store/scopes";

export interface RelativeTimeProps {
  value: string | number | Date | null | undefined;
  /** show the exact time instead of relative (hover always shows exact) */
  exact?: boolean;
  className?: string;
}

/** Relative time ("3h ago") with the exact, timezone-aware time on hover. */
export function RelativeTime({ value, exact, className }: RelativeTimeProps): ReactNode {
  const tz = usePrefs((s) => s.tz);
  if (value === null || value === undefined) return <span className={className}>—</span>;
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return <span className={className}>—</span>;
  const full = formatInTz(d, tz, { dateStyle: "full", timeStyle: "long" });
  const shown = exact ? formatInTz(d, tz, { dateStyle: "medium", timeStyle: "short" }) : relative(d);
  return (
    <time className={className} dateTime={d.toISOString()} title={full}>
      {shown}
    </time>
  );
}
