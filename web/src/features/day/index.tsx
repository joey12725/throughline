/*
 * Day: /ui/day (today in the scope zone) and /ui/day/:date (a local calendar
 * day). Swimlanes per channel, the day told as episodes, the day's vitals, a
 * calendar heat to jump, and keyboard navigation (←/→, j/k). Day boundaries
 * honour the timezone; everything is derived from shape.
 */
import { useCallback, useEffect, useMemo, useRef, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ChevronLeft, ChevronRight, CalendarCheck } from "lucide-react";
import { useBootstrap } from "@/api";
import { Panel, Stat, Skeleton, EmptyState, ErrorState } from "@/ui";
import { usePrefs } from "@/store/prefs";
import {
  analyzeChannel,
  archetypeScore,
  type ArchetypeId,
} from "@/lens";
import { channelMeta } from "@/features/channel/meta";
import { useOpenRecord } from "@/features/record";
import { channelPluginMap, channelIndexMap } from "@/features/people/data";
import { useDayRecords, useCalendarHeat } from "./data";
import { parseDate, addDays, todayInZone, groupEpisodes, dayVitals } from "./episodes";
import { Swimlanes } from "./Swimlanes";
import { Narrative } from "./Narrative";
import { CalendarHeat } from "./CalendarHeat";
import styles from "./Day.module.css";

const LONG_TEXT: ArchetypeId[] = ["document", "transcript", "feed_item"];

export default function DayScreen(): ReactNode {
  const navigate = useNavigate();
  const { date: param } = useParams();
  const { data: boot } = useBootstrap();
  const tz = usePrefs((s) => s.tz);

  const today = todayInZone(tz);
  const date = parseDate(param) ?? today;
  const isToday = date === today;

  const pluginOf = useMemo(() => channelPluginMap(boot?.channels ?? []), [boot?.channels]);
  const chIndex = useMemo(() => channelIndexMap(boot?.channels ?? []), [boot?.channels]);
  const ownerId = boot?.owner_person_id ?? null;
  const openRecord = useOpenRecord();

  // Per-channel archetype hints, derived from shape: which channels read as long
  // text (Newsreader in the story), and which are ambient geo tracks (a location
  // ping every few minutes) that should not drive the day's episodes or flood the
  // narrative — they belong to the swimlanes.
  const { longText, ambient } = useMemo(() => {
    const longSet = new Set<string>();
    const ambientSet = new Set<string>();
    for (const c of boot?.channels ?? []) {
      try {
        const { archetypes } = analyzeChannel(channelMeta(c));
        for (const a of LONG_TEXT) {
          if (archetypeScore(archetypes, a) >= 0.6) {
            longSet.add(c.name);
            break;
          }
        }
        if (archetypeScore(archetypes, "geo_track") >= 0.8) ambientSet.add(c.name);
      } catch {
        /* a channel we cannot profile simply reads as a short-text event */
      }
    }
    return { longText: longSet, ambient: ambientSet };
  }, [boot?.channels]);

  const day = useDayRecords(date, tz);
  const heat = useCalendarHeat(date, tz);

  const go = useCallback((d: string) => navigate(`/day/${d}`), [navigate]);
  const prev = useCallback(() => go(addDays(date, -1)), [go, date]);
  const next = useCallback(() => go(addDays(date, 1)), [go, date]);

  // The key handler reads the live date/today through a ref so one listener is
  // registered for the screen's life — no stale closures, no double navigation.
  const todayRef = useRef(today);
  todayRef.current = today;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (!["ArrowLeft", "ArrowRight", "j", "k", "t", "T"].includes(e.key)) return;
      e.preventDefault();
      // Read the live date from the URL (always in sync) rather than React state,
      // which can lag a just-fired navigation by a render. StrictMode may run this
      // twice, but both reads hit the same URL and resolve to the same target.
      const m = /\/day\/(\d{4}-\d{2}-\d{2})/.exec(window.location.pathname);
      const d = (m && m[1]) || todayRef.current;
      if (e.key === "ArrowLeft" || e.key === "k") go(addDays(d, -1));
      else if (e.key === "ArrowRight" || e.key === "j") go(addDays(d, 1));
      else go(todayRef.current);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go]);

  // Events (everything that isn't an ambient geo track) drive the day's story.
  const foreground = useMemo(() => day.records.filter((r) => !ambient.has(r.channel)), [day.records, ambient]);
  const episodes = useMemo(() => groupEpisodes(foreground, 40), [foreground]);
  const vitals = useMemo(() => dayVitals(day.records, episodes, ownerId), [day.records, episodes, ownerId]);
  const nowHour = useMemo(() => nowHourInZone(tz), [tz]);

  const weekday = new Intl.DateTimeFormat(undefined, { weekday: "long", timeZone: "UTC" }).format(
    new Date(`${date}T12:00:00Z`),
  );
  const nice = new Intl.DateTimeFormat(undefined, { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" }).format(
    new Date(`${date}T12:00:00Z`),
  );

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.dateWrap}>
          <div>
            <div className={styles.weekday}>{weekday}</div>
            <h1 className={styles.date}>{nice}</h1>
          </div>
          {isToday && <span className={styles.rel}>today</span>}
        </div>
        <div className={styles.nav}>
          <button type="button" className={styles.navBtn} onClick={prev} aria-label="Previous day (←)" title="Previous day (← or k)">
            <ChevronLeft size={16} />
          </button>
          <button
            type="button"
            className={`${styles.navBtn} ${styles.todayBtn}`}
            onClick={() => go(today)}
            aria-label="Jump to today"
            disabled={isToday}
          >
            <CalendarCheck size={14} /> Today
          </button>
          <button type="button" className={styles.navBtn} onClick={next} aria-label="Next day (→)" title="Next day (→ or j)">
            <ChevronRight size={16} />
          </button>
        </div>
      </header>

      <div className={styles.vitals}>
        <Stat label="Records" value={vitals.records.toLocaleString("en-US")} />
        <Stat label="Episodes" value={vitals.episodes.toLocaleString("en-US")} />
        <Stat label="People met" value={vitals.people.toLocaleString("en-US")} />
        <Stat label="Places" value={vitals.places.toLocaleString("en-US")} />
        <Stat label="Channels" value={vitals.channels.toLocaleString("en-US")} />
      </div>

      <div className={styles.main}>
        <div className={styles.leftCol}>
          <Panel title="Across the day" subtitle="00:00 – 24:00, local" flush>
            {day.loading ? (
              <div className={styles.loadWrap}>
                <Skeleton height={260} width="90%" />
              </div>
            ) : day.isError ? (
              <div className={styles.loadWrap}>
                <ErrorState error={day.error} onRetry={day.refetch} />
              </div>
            ) : day.records.length === 0 ? (
              <div className={styles.loadWrap}>
                <EmptyState title="A quiet day" description="Nothing landed in this day. Try an adjacent one." />
              </div>
            ) : (
              <Swimlanes
                records={day.records}
                date={date}
                nowHour={nowHour}
                isToday={isToday}
                pluginOf={pluginOf}
                chIndex={chIndex}
                onOpen={openRecord}
              />
            )}
          </Panel>

          {day.records.length > 0 && (
            <Narrative
              records={foreground}
              ownerPersonId={ownerId}
              longText={longText}
              pluginOf={pluginOf}
              chIndex={chIndex}
              onOpen={openRecord}
            />
          )}
        </div>

        <div className={styles.rightCol}>
          <CalendarHeat selected={date} today={today} heat={heat} onJump={go} />
        </div>
      </div>
    </div>
  );
}

/** Current time as a 0..24 hour fraction in a zone. */
function nowHourInZone(tz: string): number {
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(new Date());
    const h = Number(parts.find((p) => p.type === "hour")?.value ?? "0");
    const m = Number(parts.find((p) => p.type === "minute")?.value ?? "0");
    return (h % 24) + m / 60;
  } catch {
    const d = new Date();
    return d.getHours() + d.getMinutes() / 60;
  }
}
