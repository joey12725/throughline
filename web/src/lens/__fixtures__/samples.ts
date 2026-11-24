// Invented sample rows with the structure the engine tests for: stage durations that add
// up, a level that climbs and resets, probabilities that move against each other.

import type { Row } from "../types";

export function sleepSample(n: number): Row[] {
  const rows: Row[] = [];
  for (let i = 0; i < n; i++) {
    const deep = 3000 + ((i * 977) % 3000);
    const light = 12000 + ((i * 613) % 6000);
    const rem = 4000 + ((i * 331) % 3000);
    const awake = 1200 + ((i * 449) % 2400);
    const total = deep + light + rem;
    rows.push({
      id: i + 1,
      happened_at: new Date(Date.UTC(2026, 5, 1 + i, 22, 30)).toISOString(),
      ended_at: new Date(Date.UTC(2026, 5, 2 + i, 6, 30)).toISOString(),
      deep_sleep_s: deep,
      light_sleep_s: light,
      rem_sleep_s: rem,
      awake_s: awake,
      total_sleep_s: total,
      time_in_bed_s: total + awake,
      latency_s: 300 + ((i * 71) % 900),
      efficiency: Math.round((total / (total + awake)) * 100),
      readiness_score: 55 + Math.round((total - 19000) / 400) + (i % 5),
      average_hrv: 30 + ((i * 17) % 40),
      average_heart_rate: 52 + ((i * 7) % 12),
      sleep_type: i % 9 === 0 ? "late_nap" : "long_sleep",
    });
  }
  return rows;
}

export function usageSample(n = 120): Row[] {
  const rows: Row[] = [];
  const windows = ["acct-a:weekly", "acct-a:five_hour", "acct-b:weekly"];
  for (let i = 0; i < n; i++) {
    const w = i % windows.length;
    const step = Math.floor(i / windows.length);
    const period = 9 + w * 3;
    const used = Math.round(((step % period) / (period - 1)) * 96);
    rows.push({
      id: i + 1,
      happened_at: new Date(Date.UTC(2026, 10, 1, 0, i * 45)).toISOString(),
      window_id: windows[w],
      account: w === 2 ? "acct-b" : "acct-a",
      used_percent: used,
      left_percent: 100 - used,
      window_elapsed_percent: Math.round(((step % period) / period) * 100),
      pace_points: used - Math.round(((step % period) / period) * 100),
      status: used > 90 ? "allowed_warning" : "allowed",
      event: step % period === 0 ? "reset" : "observed",
    });
  }
  return rows;
}

function noise(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function picksSample(n = 90): Row[] {
  const rows: Row[] = [];
  const rnd = noise(7);
  for (let i = 0; i < n; i++) {
    const watch = Math.round(rnd() * 100) / 100;
    const junk = Math.min(1, Math.max(0, 0.85 - watch * 0.7 + (rnd() - 0.5) / 5));
    rows.push({
      id: i + 1,
      happened_at: new Date(Date.UTC(2026, 10, 10, i)).toISOString(),
      watch_probability: watch,
      junk_food_probability: junk,
      watch_later_probability: Math.round(rnd() * 100) / 100,
      progress_if_watched: 20 + Math.round(rnd() * 70),
      duration_s: 120 + Math.round(rnd() * 3000),
      verdict: watch > 0.6 ? "pick" : "skip",
      picked: watch > 0.6,
    });
  }
  return rows;
}
