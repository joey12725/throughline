/*
 * The Places side panel: top places by dwell, and — when one is selected — its
 * visit rhythm (weekday × hour), first/last visit and the records that happened
 * there (each opens the record drawer). Every number traces to a bounded query.
 */
import { useMemo } from "react";
import { MapPin, Clock, ArrowLeft } from "lucide-react";
import { num, useSql } from "@/api";
import { useChartColors } from "@/charts";
import { useCanvas } from "@/charts/runtime/hooks";
import { Button, EmptyState, ErrorState, Panel, Sensitive, Skeleton, SourceDot } from "@/ui";
import { formatInTz } from "@/store/scopes";
import { useOpenRecord } from "@/features/record";
import type { PlaceMark } from "./MapCanvas";
import { recordsAtPlaceSql, topPlacesSql, visitHistorySql } from "./queries";
import { gridMax, humanDuration, weekdayHourGrid } from "./placeStats";
import styles from "./places.module.css";

interface TopPlace {
  place: string;
  lat: number;
  lon: number;
  visits: string | number;
  dwell_s: string | number | null;
  first_at: string | null;
  last_at: string | null;
}

interface Props {
  from: string | null;
  to: string | null;
  tz: string;
  plugin: string;
  selected: PlaceMark | null;
  onSelect: (p: PlaceMark | null) => void;
}

const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function PlacePanel({ from, to, tz, plugin, selected, onSelect }: Props) {
  if (selected) return <PlaceDetail place={selected} tz={tz} plugin={plugin} onBack={() => onSelect(null)} />;
  return <TopPlaces from={from} to={to} tz={tz} plugin={plugin} onSelect={onSelect} />;
}

function TopPlaces({ from, to, tz, plugin, onSelect }: Omit<Props, "selected">) {
  const q = useSql<TopPlace>(topPlacesSql(from, to), { priority: "visible", keyParts: ["topplaces", from, to] });
  const rows = q.data?.rows ?? [];
  const maxDwell = rows.reduce((m, r) => Math.max(m, num(r.dwell_s) || 0), 1);

  return (
    <Panel title="Top places by dwell" subtitle="Each detected visit snapped to its nearest named place" className={styles.sidePanel}>
      {q.isLoading && <Skeleton height={240} />}
      {q.error && <ErrorState error={q.error} />}
      {!q.isLoading && !q.error && rows.length === 0 && <EmptyState icon={<MapPin />} title="No visits in scope" description="Widen the time scope, or no places are near these visits." compact />}
      <ul className={styles.placeList}>
        {rows.map((r) => {
          const dwell = num(r.dwell_s) || 0;
          return (
            <li key={r.place}>
              <button
                type="button"
                className={styles.placeRow}
                onClick={() => onSelect({ id: -1, name: r.place, lat: r.lat, lon: r.lon, records: 0 })}
              >
                <span className={styles.placeBar} style={{ width: `${(dwell / maxDwell) * 100}%` }} aria-hidden />
                <span className={styles.placeName}>{r.place}</span>
                <span className={styles.placeDwell}>{humanDuration(dwell)}</span>
                <span className={styles.placeVisits}>{num(r.visits)} visits</span>
              </button>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

interface HistRow {
  happened_at: string;
  dwell_s: string | number | null;
}
interface RecRow {
  id: number;
  channel: string;
  happened_at: string;
  content: string | null;
  display_name: string | null;
}

function PlaceDetail({ place, tz, plugin, onBack }: { place: PlaceMark; tz: string; plugin: string; onBack: () => void }) {
  const hist = useSql<HistRow>(visitHistorySql(place.lat, place.lon), { priority: "visible", keyParts: ["placehist", place.lat, place.lon] });
  const recs = place.id > 0 ? recordsAtPlaceSql(place.id) : null;
  const records = useSql<RecRow>(recs, { priority: "background", keyParts: ["placerecs", place.id], enabled: place.id > 0 });
  const open = useOpenRecord();

  const times = useMemo(() => (hist.data?.rows ?? []).map((r) => Date.parse(r.happened_at)).filter(Number.isFinite), [hist.data]);
  const totalDwell = (hist.data?.rows ?? []).reduce((s, r) => s + (num(r.dwell_s) || 0), 0);
  const first = times.length ? Math.min(...times) : null;
  const last = times.length ? Math.max(...times) : null;

  return (
    <Panel
      title={
        <span className={styles.detailTitle}>
          <SourceDot plugin={plugin} /> {place.name}
        </span>
      }
      subtitle={place.address ?? `${place.lat.toFixed(4)}, ${place.lon.toFixed(4)}`}
      actions={<Button size="sm" variant="ghost" onClick={onBack} icon={<ArrowLeft size={14} />}>Back</Button>}
      className={styles.sidePanel}
    >
      <div className={styles.detailStats}>
        <Stat label="Visits" value={String(times.length)} />
        <Stat label="Total dwell" value={humanDuration(totalDwell)} />
        <Stat label="First" value={first ? formatInTz(first, tz, { dateStyle: "medium" }) : "—"} />
        <Stat label="Last" value={last ? formatInTz(last, tz, { dateStyle: "medium" }) : "—"} />
      </div>

      <h3 className={styles.detailH}><Clock size={12} /> When you are here</h3>
      {hist.isLoading ? <Skeleton height={120} /> : times.length === 0 ? <p className={styles.muted}>No detected visits near this point.</p> : <WeekHourHeat times={times} tz={tz} plugin={plugin} />}

      {place.id > 0 && (
        <>
          <h3 className={styles.detailH}><MapPin size={12} /> Records here</h3>
          {records.isLoading && <Skeleton height={100} />}
          {records.error && <ErrorState error={records.error} />}
          {!records.isLoading && (records.data?.rows.length ?? 0) === 0 && <p className={styles.muted}>No records are linked to this place.</p>}
          <ul className={styles.recList}>
            {(records.data?.rows ?? []).map((r) => (
              <li key={r.id}>
                <button type="button" className={styles.recRow} onClick={() => open(r.id, r.channel)}>
                  <span className={styles.recMeta}>
                    <SourceDot plugin={plugin} /> {r.channel} · {formatInTz(r.happened_at, tz, { dateStyle: "medium", timeStyle: "short" })}
                  </span>
                  <Sensitive as="span" className={styles.recContent}>{r.content ?? "—"}</Sensitive>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.miniStat}>
      <span className={styles.miniStatV}>{value}</span>
      <span className={styles.miniStatL}>{label}</span>
    </div>
  );
}

function WeekHourHeat({ times, tz, plugin }: { times: number[]; tz: string; plugin: string }) {
  const c = useChartColors(plugin);
  const grid = useMemo(() => weekdayHourGrid(times, tz), [times, tz]);
  const max = gridMax(grid);
  const W = 7 * 26 + 30;
  const H = 24 * 6 + 16;
  const ramp = useMemo(() => c.sequential(7), [c]);

  const ref = useCanvas(
    (ctx) => {
      ctx.clearRect(0, 0, W, H);
      const cell = 24;
      const gap = 2;
      const x0 = 30;
      const y0 = 14;
      const rowH = 6;
      ctx.font = `9px ${c.tokens.fontUi}`;
      ctx.fillStyle = c.tokens.ink3;
      ctx.textBaseline = "middle";
      for (let h = 0; h < 24; h += 6) {
        ctx.textAlign = "right";
        ctx.fillText(`${h}:00`, x0 - 4, y0 + h * rowH + rowH / 2);
      }
      ctx.textAlign = "center";
      for (let d = 0; d < 7; d++) ctx.fillText(WD[d]!, x0 + d * (cell + gap) + cell / 2, 7);
      for (let d = 0; d < 7; d++) {
        for (let h = 0; h < 24; h++) {
          const v = grid[d]![h]!;
          const x = x0 + d * (cell + gap);
          const y = y0 + h * rowH;
          if (v === 0) {
            ctx.fillStyle = c.tokens.bg2;
          } else {
            const t = Math.min(0.999, Math.log1p(v) / Math.log1p(max));
            ctx.fillStyle = ramp[Math.floor(t * (ramp.length - 1))]!;
          }
          ctx.fillRect(x, y, cell, rowH - 0.5);
        }
      }
    },
    { width: W, height: H },
    [grid, max, c.tokens, c.theme],
  );

  return <canvas ref={ref} role="img" aria-label="Visits by weekday and hour" className={styles.heatCanvas} />;
}
