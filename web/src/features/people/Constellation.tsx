/*
 * /ui/people — the constellation. Every person is a star on a dark field around
 * the owner at the centre: distance is recency of contact, the angular sector is
 * the channel family they are mostly reached through, size is lifetime volume,
 * brightness and twinkle are recent activity. A canvas draws the dense marks; an
 * SVG-free accessible table beside it is the sortable alternative. Nothing here
 * names a real channel or plugin — colours come from the source-colour engine.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Search } from "lucide-react";
import { useBootstrap } from "@/api";
import { useSourceColor, Input, EmptyState, ErrorState, Skeleton, SourceDot } from "@/ui";
import { usePrefs } from "@/store/prefs";
import { useScopes, formatInTz, relative } from "@/store/scopes";
import { buildConstellation, recencyRings, familyTotals, type Star, type RecencyRing } from "./layout";
import { usePeopleInputs, usePeopleSparklines, channelIndexMap } from "./data";
import { Sparkline } from "./Sparkline";
import styles from "./Constellation.module.css";

interface Tokens {
  ink: string;
  ink2: string;
  ink3: string;
  brass: string;
  line: string;
  bg0: string;
}

function readTokens(el: HTMLElement): Tokens {
  const s = getComputedStyle(el);
  const g = (n: string, f: string) => s.getPropertyValue(n).trim() || f;
  return {
    ink: g("--ink", "#ECE6D8"),
    ink2: g("--ink-2", "#A6A392"),
    ink3: g("--ink-3", "#6B6E72"),
    brass: g("--brass", "#D9A64A"),
    line: g("--line", "#2A3040"),
    bg0: g("--bg-0", "#0A0C0F"),
  };
}

const LABEL_TOP = 14;

export function Constellation(): ReactNode {
  const navigate = useNavigate();
  const { data: boot } = useBootstrap();
  const ownerId = boot?.owner_person_id ?? null;
  const { people, loading, isError, error, refetch } = usePeopleInputs(ownerId);
  const sparks = usePeopleSparklines(ownerId);
  const { rgbFor } = useSourceColor();
  const theme = usePrefs((s) => s.theme);
  const tz = usePrefs((s) => s.tz);
  const { from, to } = useScopes();

  const channelIndex = useMemo(() => channelIndexMap(boot?.channels ?? []), [boot?.channels]);
  const colorFor = useCallback(
    (star: { family: string; topChannel: string }, alpha = 1) =>
      rgbFor(star.family, channelIndex.get(star.topChannel) ?? 0, alpha),
    [rgbFor, channelIndex],
  );

  // Filters
  const [search, setSearch] = useState("");
  const [minVol, setMinVol] = useState(0);
  const [mutedFamilies, setMutedFamilies] = useState<Set<string>>(() => new Set());

  const maxVol = useMemo(() => Math.max(1, ...people.map((p) => p.records)), [people]);
  const families = useMemo(() => familyTotals(people), [people]);

  const q = search.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      people.filter(
        (p) =>
          p.records >= minVol &&
          !mutedFamilies.has(p.family) &&
          (q === "" || p.name.toLowerCase().includes(q) || (p.handle ?? "").toLowerCase().includes(q)),
      ),
    [people, minVol, mutedFamilies, q],
  );

  const nowMs = boot?.now ? Date.parse(boot.now) : Date.now();
  const stars = useMemo(
    () => buildConstellation(filtered, { now: nowMs, sizeMin: 3, sizeMax: 12 }),
    [filtered, nowMs],
  );
  const rings = useMemo(() => recencyRings(filtered, { now: nowMs }), [filtered, nowMs]);
  const ringsRef = useRef<RecencyRing[]>(rings);
  ringsRef.current = rings;

  // View transform (zoom/pan) and interaction state.
  const skyRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const viewRef = useRef({ zoom: 1, panX: 0, panY: 0 });
  const [, bump] = useState(0);
  const redraw = useCallback(() => bump((v) => v + 1), []);
  const screenRef = useRef<{ star: Star; px: number; py: number; r: number }[]>([]);
  const [hover, setHover] = useState<{ star: Star; x: number; y: number } | null>(null);
  const dragRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  // The hovered hit, read by the RAF draw loop without forcing a React redraw.
  const hoverRef = useRef<{ star: Star; px: number; py: number; r: number } | null>(null);

  const reduceMotion = useRef(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    reduceMotion.current = mq.matches;
    const on = () => (reduceMotion.current = mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  // The animation / draw loop.
  useEffect(() => {
    const canvas = canvasRef.current;
    const sky = skyRef.current;
    if (!canvas || !sky) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let raf = 0;
    let running = true;

    const draw = (t: number) => {
      if (!running) return;
      const rect = sky.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = Math.max(1, Math.floor(rect.width));
      const h = Math.max(1, Math.floor(rect.height));
      if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
        canvas.width = w * dpr;
        canvas.height = h * dpr;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const tok = readTokens(sky);
      const { zoom, panX, panY } = viewRef.current;
      const cx = w / 2 + panX;
      const cy = h / 2 + panY;
      const base = (Math.min(w, h) / 2) * 0.9;
      const scale = base * zoom;

      // Recency guide rings — concentric circles at the time horizons the rank
      // transform maps onto the field (a day, a week, a month, a year).
      const ringList = ringsRef.current;
      ctx.save();
      ctx.strokeStyle = tok.line;
      ctx.lineWidth = 1;
      if (ringList.length === 0) {
        ctx.globalAlpha = 0.4;
        ctx.beginPath();
        ctx.arc(cx, cy, scale, 0, Math.PI * 2);
        ctx.stroke();
      } else {
        for (const ring of ringList) {
          ctx.globalAlpha = 0.42;
          ctx.setLineDash([2, 4]);
          ctx.beginPath();
          ctx.arc(cx, cy, ring.radius * scale, 0, Math.PI * 2);
          ctx.stroke();
        }
        ctx.setLineDash([]);
      }
      ctx.restore();

      const map = screenRef.current;
      map.length = 0;
      const twinkle = reduceMotion.current ? 0 : 1;

      // Stars.
      for (const s of stars) {
        const px = cx + s.x * scale;
        const py = cy + s.y * scale;
        if (px < -40 || px > w + 40 || py < -40 || py > h + 40) continue;
        const tw = 1 - twinkle * 0.18 * (0.5 + 0.5 * Math.sin(t / 900 + s.phase * Math.PI * 2));
        const bright = s.brightness * tw;
        const r = s.size * Math.sqrt(zoom);

        // glow
        const glow = ctx.createRadialGradient(px, py, 0, px, py, r * 3.4);
        glow.addColorStop(0, colorFor(s, 0.55 * bright));
        glow.addColorStop(1, colorFor(s, 0));
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(px, py, r * 3.4, 0, Math.PI * 2);
        ctx.fill();
        // core
        ctx.fillStyle = colorFor(s, Math.min(1, 0.5 + bright * 0.6));
        ctx.beginPath();
        ctx.arc(px, py, r, 0, Math.PI * 2);
        ctx.fill();

        map.push({ star: s, px, py, r: Math.max(r, 7) });
      }

      // Owner at the centre — the throughline everything orbits.
      const op = 6 + 2 * (reduceMotion.current ? 1 : 0.5 + 0.5 * Math.sin(t / 1400));
      const oglow = ctx.createRadialGradient(cx, cy, 0, cx, cy, 26);
      oglow.addColorStop(0, withAlpha(tok.brass, 0.5));
      oglow.addColorStop(1, withAlpha(tok.brass, 0));
      ctx.fillStyle = oglow;
      ctx.beginPath();
      ctx.arc(cx, cy, 26, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = tok.brass;
      ctx.beginPath();
      ctx.arc(cx, cy, op, 0, Math.PI * 2);
      ctx.fill();
      ctx.font = "600 11px var(--font-ui), system-ui";
      ctx.fillStyle = tok.ink2;
      ctx.textAlign = "center";
      ctx.fillText("you", cx, cy + 24);

      // Ring labels — a faint horizon mark at the top of each guide ring, so the
      // radial axis is legible without a key ("everything inside = newer than…").
      ctx.save();
      ctx.textAlign = "center";
      ctx.font = "500 10px var(--font-ui), system-ui";
      ctx.shadowColor = tok.bg0;
      ctx.shadowBlur = 4;
      for (const ring of ringList) {
        const ry = cy - ring.radius * scale;
        if (ry < 10 || ry > h - 6) continue;
        ctx.fillStyle = withAlpha(tok.ink3, 0.95);
        ctx.fillText(ring.label, cx, ry - 3);
      }
      ctx.restore();

      // Labels: the brightest/biggest, with greedy collision avoidance so a dense
      // cluster never piles its names on top of each other. The rest surface on
      // hover (hoverRef), so no name is ever lost — just deferred.
      const candidates = [...map].sort((a, b) => b.star.records - a.star.records);
      ctx.textAlign = "left";
      ctx.font = "500 11.5px var(--font-ui), system-ui";
      ctx.save();
      ctx.shadowColor = tok.bg0;
      ctx.shadowBlur = 5;
      const placed: { x: number; y: number; w: number; h: number }[] = [];
      let shown = 0;
      for (const m of candidates) {
        if (shown >= LABEL_TOP) break;
        const lx = m.px + m.r + 4;
        const ly = m.py + 4;
        const lw = ctx.measureText(m.star.name).width;
        const box = { x: lx, y: ly - 10, w: lw, h: 14 };
        if (placed.some((p) => box.x < p.x + p.w + 6 && box.x + box.w + 6 > p.x && box.y < p.y + p.h && box.y + box.h > p.y)) continue;
        placed.push(box);
        shown++;
        ctx.fillStyle = withAlpha(tok.ink, 0.82);
        ctx.fillText(m.star.name, lx, ly);
      }
      ctx.restore();
      const hv = hoverRef.current;
      if (hv) {
        ctx.save();
        ctx.shadowColor = tok.bg0;
        ctx.shadowBlur = 6;
        ctx.fillStyle = tok.ink;
        ctx.font = "600 12px var(--font-ui), system-ui";
        ctx.fillText(hv.star.name, hv.px + hv.r + 5, hv.py + 4);
        ctx.restore();
      }

      if (!reduceMotion.current) raf = requestAnimationFrame(draw);
    };

    raf = requestAnimationFrame(draw);
    const ro = new ResizeObserver(() => {
      if (reduceMotion.current) requestAnimationFrame(draw);
    });
    ro.observe(sky);
    return () => {
      running = false;
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stars, colorFor, theme]);

  const hitTest = useCallback((clientX: number, clientY: number) => {
    const sky = skyRef.current;
    if (!sky) return null;
    const rect = sky.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    let best: { star: Star; px: number; py: number; r: number } | null = null;
    let bestD = Infinity;
    for (const m of screenRef.current) {
      const d = Math.hypot(m.px - x, m.py - y);
      if (d < m.r + 6 && d < bestD) {
        bestD = d;
        best = m;
      }
    }
    return best;
  }, []);

  const onPointerMove = (e: React.PointerEvent) => {
    if (dragRef.current) {
      const dx = e.clientX - dragRef.current.x;
      const dy = e.clientY - dragRef.current.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) dragRef.current.moved = true;
      viewRef.current.panX += dx;
      viewRef.current.panY += dy;
      dragRef.current.x = e.clientX;
      dragRef.current.y = e.clientY;
      if (reduceMotion.current) redraw();
      return;
    }
    const hit = hitTest(e.clientX, e.clientY);
    hoverRef.current = hit;
    const sky = skyRef.current;
    if (hit && sky) {
      setHover({ star: hit.star, x: hit.px, y: hit.py });
    } else {
      setHover(null);
    }
    if (reduceMotion.current) redraw();
  };

  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    dragRef.current = { x: e.clientX, y: e.clientY, moved: false };
  };
  const onPointerUp = (e: React.PointerEvent) => {
    const drag = dragRef.current;
    dragRef.current = null;
    if (drag && !drag.moved) {
      const hit = hitTest(e.clientX, e.clientY);
      if (hit) navigate(`/people/${hit.star.id}`);
    }
  };
  const onWheel = (e: React.WheelEvent) => {
    const v = viewRef.current;
    const factor = Math.exp(-e.deltaY * 0.0015);
    v.zoom = Math.min(6, Math.max(0.5, v.zoom * factor));
    if (reduceMotion.current) redraw();
  };
  const resetView = () => {
    viewRef.current = { zoom: 1, panX: 0, panY: 0 };
    redraw();
  };

  const toggleFamily = (fam: string) => {
    setMutedFamilies((prev) => {
      const next = new Set(prev);
      if (next.has(fam)) next.delete(fam);
      else next.add(fam);
      return next;
    });
  };

  const scopeNote = from || to ? ` · ${from ? formatInTz(from, tz, { dateStyle: "medium" }) : "start"}–${to ? formatInTz(to, tz, { dateStyle: "medium" }) : "now"}` : "";

  return (
    <div className={styles.page}>
      <div className={styles.head}>
        <p className={styles.lede}>
          <b>{people.length.toLocaleString("en-US")}</b> people orbit you — nearer stars are more recent, their sector is
          where you reach them, their size is how much you{"’"}ve said{scopeNote}.
        </p>
        <div className={styles.controls}>
          <div className={styles.search}>
            <Input
              placeholder="Find a person…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="Search people"
              lead={<Search size={14} />}
            />
          </div>
          <label className={styles.minVol}>
            min {minVol > 0 ? minVol.toLocaleString("en-US") : "all"}
            <input
              type="range"
              min={0}
              max={Math.max(1, Math.round(maxVol * 0.5))}
              value={minVol}
              onChange={(e) => setMinVol(Number(e.target.value))}
              aria-label="Minimum volume"
            />
          </label>
        </div>
      </div>

      <div className={styles.body}>
        {loading ? (
          <div className={styles.loadWrap}>
            <Skeleton height={340} width="70%" radius={12} />
          </div>
        ) : isError ? (
          <div className={styles.loadWrap}>
            <ErrorState error={error} onRetry={refetch} />
          </div>
        ) : people.length === 0 ? (
          <div className={styles.emptyWrap}>
            <EmptyState
              title="No people yet"
              description="People appear here once records are attributed to them. A restricted token sees no identity data."
            />
          </div>
        ) : (
          <div className={styles.sky} ref={skyRef}>
            <canvas
              ref={canvasRef}
              className={styles.canvas}
              onPointerMove={onPointerMove}
              onPointerDown={onPointerDown}
              onPointerUp={onPointerUp}
              onPointerLeave={() => {
                hoverRef.current = null;
                setHover(null);
                dragRef.current = null;
              }}
              onWheel={onWheel}
            />
            <div className={styles.hint}>
              <span>scroll to zoom · drag to pan</span>
              <button type="button" className={styles.resetBtn} onClick={resetView}>
                reset
              </button>
            </div>
            {families.length > 1 && (
              <div className={styles.legend}>
                <span className={styles.legendTitle}>Reached through</span>
                {families.slice(0, 8).map((f) => (
                  <button
                    key={f.family}
                    type="button"
                    className={styles.legendRow}
                    data-off={mutedFamilies.has(f.family)}
                    onClick={() => toggleFamily(f.family)}
                    title="Toggle this family"
                  >
                    <span className={styles.swatch} style={{ color: rgbFor(f.family, 0), background: rgbFor(f.family, 0) }} />
                    <span className={styles.legendName}>{f.family}</span>
                    <span className={styles.legendCount}>{f.count}</span>
                  </button>
                ))}
              </div>
            )}
            {hover && (
              <div className={styles.tip} style={{ left: hover.x, top: hover.y }} role="status">
                <div className={styles.tipName}>
                  <span className={styles.dot} style={{ color: colorFor(hover.star), background: colorFor(hover.star) }} />
                  {hover.star.name}
                </div>
                <div className={styles.tipMeta}>
                  {hover.star.records.toLocaleString("en-US")} records · {hover.star.channels} channels
                </div>
                <div className={styles.tipMeta}>last {relative(hover.star.lastAt, boot?.now ? Date.parse(boot.now) : undefined)}</div>
                <div className={styles.tipChan}>{hover.star.topChannel}</div>
              </div>
            )}
          </div>
        )}

        <PeopleList
          people={filtered}
          sparks={sparks}
          colorFor={colorFor}
          tz={tz}
          now={boot?.now ? Date.parse(boot.now) : Date.now()}
          onOpen={(id) => navigate(`/people/${id}`)}
          hoverId={hover?.star.id ?? null}
        />
      </div>
    </div>
  );
}

/* --------------------------------------------------------- accessible table */

type SortKey = "name" | "records" | "channels" | "first" | "last";

function PeopleList({
  people,
  sparks,
  colorFor,
  tz,
  now,
  onOpen,
  hoverId,
}: {
  people: { id: number; name: string; family: string; topChannel: string; records: number; channels: number; firstAt: string | null; lastAt: string | null }[];
  sparks: Map<string, number[]>;
  colorFor: (s: { family: string; topChannel: string }, a?: number) => string;
  tz: string;
  now: number;
  onOpen: (id: number) => void;
  hoverId: number | null;
}): ReactNode {
  const [sort, setSort] = useState<SortKey>("records");
  const [dir, setDir] = useState<1 | -1>(-1);
  const sorted = useMemo(() => {
    const arr = [...people];
    arr.sort((a, b) => {
      let d = 0;
      switch (sort) {
        case "name":
          d = a.name.localeCompare(b.name);
          break;
        case "records":
          d = a.records - b.records;
          break;
        case "channels":
          d = a.channels - b.channels;
          break;
        case "first":
          d = (Date.parse(a.firstAt ?? "") || 0) - (Date.parse(b.firstAt ?? "") || 0);
          break;
        case "last":
          d = (Date.parse(a.lastAt ?? "") || 0) - (Date.parse(b.lastAt ?? "") || 0);
          break;
      }
      return d * dir || a.id - b.id;
    });
    return arr;
  }, [people, sort, dir]);

  const header = (key: SortKey, label: string, numCol = false) => (
    <th
      scope="col"
      data-num={numCol}
      onClick={() => {
        if (sort === key) setDir((v) => (v === 1 ? -1 : 1));
        else {
          setSort(key);
          setDir(key === "name" ? 1 : -1);
        }
      }}
      aria-sort={sort === key ? (dir === 1 ? "ascending" : "descending") : "none"}
    >
      {label}
      {sort === key && <span className={styles.sortCaret}>{dir === 1 ? "▲" : "▼"}</span>}
    </th>
  );

  return (
    <div className={styles.listWrap}>
      <div className={styles.listHead}>
        <span className={styles.listTitle}>Everyone</span>
        <span className={styles.listCount}>{people.length.toLocaleString("en-US")} shown</span>
      </div>
      <div className={styles.tableScroll}>
        <table className={styles.table}>
          <thead>
            <tr>
              {header("name", "Name")}
              {header("records", "Recs", true)}
              {header("channels", "Ch", true)}
              <th scope="col">Trend</th>
              {header("last", "Last", true)}
            </tr>
          </thead>
          <tbody>
            {sorted.map((p) => (
              <tr
                key={p.id}
                className={styles.row}
                data-hot={p.id === hoverId}
                onClick={() => onOpen(p.id)}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onOpen(p.id);
                  }
                }}
              >
                <td>
                  <div className={styles.nameCell}>
                    <span className={styles.dot} style={{ color: colorFor(p), background: colorFor(p) }} />
                    <span className={styles.nameText}>{p.name}</span>
                  </div>
                </td>
                <td className={styles.num}>{p.records.toLocaleString("en-US")}</td>
                <td className={styles.num}>{p.channels}</td>
                <td>
                  <Sparkline values={sparks.get(String(p.id)) ?? []} color={colorFor(p, 0.9)} width={62} height={16} />
                </td>
                <td className={styles.num} title={p.lastAt ? formatInTz(p.lastAt, tz) : ""}>
                  {relative(p.lastAt, now)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function withAlpha(color: string, alpha: number): string {
  // color is a hex token like #RRGGBB.
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return color;
  const n = parseInt(m[1] ?? "0", 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}
