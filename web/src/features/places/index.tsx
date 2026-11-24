/*
 * Places (/ui/places): a full-bleed instrument map of where this archive has
 * been — the track as its own map, detected visits as dwell blooms, named places
 * as labelled circles, and a density hex-heat — with a time strip that brushes
 * the global scope and a side panel that reads one place's rhythm and records.
 *
 * Location is derived from shape: the system's own cross-channel track and
 * visit views, and place entities with coordinates. No channel name is wired in;
 * the only "location plugin" we name is the hue we borrow for the track, taken
 * from whichever geo-bearing channel is largest in this installation.
 */
import { useMemo, useState } from "react";
import { Layers, Route, Flame, MapPin } from "lucide-react";
import { num, useBootstrap, useSql } from "@/api";
import { EmptyState, ErrorState, Segmented, Switch } from "@/ui";
import { useScopes } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";
import MapCanvas, { type MapLayers, type PlaceMark, type TrackColorMode, type VisitMark } from "./MapCanvas";
import PlacePanel from "./PlacePanel";
import TimelineStrip, { type DensityDay } from "./TimelineStrip";
import { densitySql, placesSql, trackSql, visitsSql } from "./queries";
import type { TrackPoint } from "./projection";
import styles from "./places.module.css";

export default function PlacesScreen() {
  const { data: boot } = useBootstrap();
  const { from, to, setRange } = useScopes();
  const tz = usePrefs((s) => s.tz);
  const now = boot ? Date.parse(boot.now) : Date.now();

  const [layers, setLayers] = useState<MapLayers>({ track: true, places: true, heat: false });
  const [trackColor, setTrackColor] = useState<TrackColorMode>("recency");
  const [selected, setSelected] = useState<PlaceMark | null>(null);
  const [focus, setFocus] = useState<{ lon: number; lat: number; nonce: number } | null>(null);

  // A pick from the side list recentres the map; a pick on the map does not.
  const selectFromList = (p: PlaceMark | null) => {
    setSelected(p);
    if (p && Number.isFinite(p.lon) && Number.isFinite(p.lat)) {
      setFocus((f) => ({ lon: p.lon, lat: p.lat, nonce: (f?.nonce ?? 0) + 1 }));
    }
  };

  // The hue we colour the track with: the largest geo-bearing channel's plugin.
  const plugin = useMemo(() => {
    const geo = (boot?.channels ?? []).filter((c) => c.with_geo).sort((a, b) => b.rows - a.rows);
    return geo[0]?.plugin ?? "";
  }, [boot]);

  const trackQ = useSql<{ at: string; lat: number; lon: number }>(trackSql(from, to), { priority: "visible", rowCap: 6000, keyParts: ["track", from, to] });
  const visitsQ = useSql<{ happened_at: string; lat: number; lon: number; dwell_s: number | string }>(visitsSql(from, to), { priority: "visible", keyParts: ["visits", from, to] });
  const placesQ = useSql<{ id: number; name: string; lat: number; lon: number; address: string | null; records: number | string }>(placesSql(), { priority: "visible", keyParts: ["places"] });
  const densityQ = useSql<{ day: string; n: number | string }>(densitySql(), { priority: "background", keyParts: ["geo-density"] });

  const track: TrackPoint[] = useMemo(() => (trackQ.data?.rows ?? []).map((r) => ({ lon: r.lon, lat: r.lat, t: Date.parse(r.at) })), [trackQ.data]);
  const visits: VisitMark[] = useMemo(() => (visitsQ.data?.rows ?? []).map((r) => ({ lon: r.lon, lat: r.lat, dwellS: num(r.dwell_s) || 0, at: Date.parse(r.happened_at) })), [visitsQ.data]);
  const places: PlaceMark[] = useMemo(() => (placesQ.data?.rows ?? []).map((r) => ({ id: r.id, name: r.name, lat: r.lat, lon: r.lon, records: num(r.records) || 0, address: r.address })), [placesQ.data]);
  const density: DensityDay[] = useMemo(() => (densityQ.data?.rows ?? []).map((r) => ({ t: Date.parse(r.day), n: num(r.n) || 0 })), [densityQ.data]);

  const anyError = trackQ.error ?? visitsQ.error ?? placesQ.error;
  const loading = trackQ.isLoading || visitsQ.isLoading || placesQ.isLoading;
  const empty = !loading && track.length === 0 && visits.length === 0 && places.length === 0;

  return (
    <div className={styles.screen}>
      <div className={styles.mapColumn}>
        <div className={styles.toolbar}>
          <span className={styles.toolTitle}><MapPin size={15} /> Places</span>
          <div className={styles.toolGroup}>
            <LayerToggle icon={<Route size={13} />} label="Track" on={layers.track} onChange={(v) => setLayers((s) => ({ ...s, track: v }))} />
            <LayerToggle icon={<MapPin size={13} />} label="Places" on={layers.places} onChange={(v) => setLayers((s) => ({ ...s, places: v }))} />
            <LayerToggle icon={<Flame size={13} />} label="Heat" on={layers.heat} onChange={(v) => setLayers((s) => ({ ...s, heat: v }))} />
          </div>
          {layers.track && (
            <Segmented
              aria-label="Colour the track by"
              size="sm"
              value={trackColor}
              onChange={(v) => setTrackColor(v)}
              items={[
                { value: "recency", label: "Recency" },
                { value: "source", label: "Source" },
              ]}
            />
          )}
          <span className={styles.toolMeta}>
            <Layers size={12} /> {track.length.toLocaleString("en-US")} points · {places.length} places
          </span>
        </div>

        <div className={styles.mapArea}>
          {anyError ? (
            <div className={styles.center}><ErrorState error={anyError} /></div>
          ) : empty ? (
            <div className={styles.center}><EmptyState icon={<MapPin />} title="No geography in scope" description="Nothing with a position falls in the current time scope. Clear the scope on the strip below, or no source carries location yet." /></div>
          ) : (
            <MapCanvas
              track={track}
              visits={visits}
              places={places}
              layers={layers}
              trackColor={trackColor}
              plugin={plugin}
              tz={tz}
              selectedId={selected?.id ?? null}
              focus={focus}
              onSelectPlace={setSelected}
            />
          )}
        </div>

        <TimelineStrip days={density} from={from} to={to} tz={tz} now={now} plugin={plugin} onBrush={setRange} />
      </div>

      <aside className={styles.side}>
        <PlacePanel from={from} to={to} tz={tz} plugin={plugin} selected={selected} onSelect={selectFromList} />
      </aside>
    </div>
  );
}

function LayerToggle({ icon, label, on, onChange }: { icon: React.ReactNode; label: string; on: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className={styles.layerToggle} data-on={on}>
      {icon}
      <span>{label}</span>
      <Switch checked={on} onChange={onChange} />
    </label>
  );
}
