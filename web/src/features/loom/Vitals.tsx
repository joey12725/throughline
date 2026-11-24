/*
 * The vitals sentence: one editorial line in Newsreader serif, where every
 * number is a link to the query (or screen) that produced it. Reads from the
 * bootstrap totals plus one tiny "arrived today" count in the scope timezone.
 */
import { useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { num, useSql } from "@/api";
import type { Bootstrap } from "@/api/types";
import { usePrefs } from "@/store/prefs";
import styles from "./Vitals.module.css";

function queryLink(sql: string): string {
  return `/query?sql=${encodeURIComponent(sql)}&run=1`;
}

function NumLink({ to, children, title }: { to: string; children: ReactNode; title: string }): ReactNode {
  return (
    <Link className={styles.num} to={to} title={title}>
      {children}
    </Link>
  );
}

export function Vitals({ boot }: { boot: Bootstrap }): ReactNode {
  const tz = usePrefs((s) => s.tz);
  const t = boot.totals;

  const todayQ = useSql<{ n: string }>(
    `SELECT count(*)::text AS n FROM api.messages ` +
      `WHERE ingested_at >= date_trunc('day', now() AT TIME ZONE '${sqlTz(tz)}') AT TIME ZONE '${sqlTz(tz)}'`,
    { priority: "background" },
  );
  const today = todayQ.data ? num(todayQ.data.rows[0]?.n ?? "0") : null;

  const since = useMemo(() => {
    let min = Infinity;
    for (const c of boot.channels) {
      const ms = c.first_at ? Date.parse(c.first_at) : NaN;
      if (!Number.isNaN(ms) && ms < min) min = ms;
    }
    if (!Number.isFinite(min)) return null;
    return new Date(min).toLocaleDateString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
  }, [boot.channels]);

  const fmt = (n: number): string => n.toLocaleString("en-US");

  return (
    <p className={styles.sentence}>
      <NumLink to={queryLink("SELECT count(*) AS records FROM api.messages")} title="Count every record">
        {fmt(t.records)}
      </NumLink>{" "}
      records from{" "}
      <NumLink
        to={queryLink("SELECT plugin, count(*) AS channels FROM api.channels GROUP BY 1 ORDER BY 2 DESC")}
        title="Records by source"
      >
        {fmt(t.plugins)}
      </NumLink>{" "}
      {t.plugins === 1 ? "source" : "sources"} across{" "}
      <NumLink to={queryLink("SELECT name, plugin, record_kind FROM api.channels ORDER BY 1")} title="Every channel">
        {fmt(t.channels)}
      </NumLink>{" "}
      channels{since ? <>, since <span className={styles.since}>{since}</span></> : null};{" "}
      {today === null ? (
        <span className={styles.pending}>…</span>
      ) : (
        <NumLink
          to={queryLink(
            `SELECT channel, happened_at, left(content, 160) AS excerpt FROM api.messages ` +
              `WHERE ingested_at >= date_trunc('day', now() AT TIME ZONE '${sqlTz(tz)}') AT TIME ZONE '${sqlTz(tz)}' ` +
              `ORDER BY ingested_at DESC`,
          )}
          title="What arrived today"
        >
          {fmt(today)}
        </NumLink>
      )}{" "}
      arrived today.
      <span className={styles.secondary}>
        {" "}
        <NumLink to={queryLink("SELECT handle, display_name, is_owner FROM api.people ORDER BY 1")} title="People">
          {fmt(t.people)}
        </NumLink>{" "}
        people
        {t.threads ? (
          <>
            ,{" "}
            <NumLink to="/live" title="Watch arrivals live">
              {fmt(t.threads)}
            </NumLink>{" "}
            threads
          </>
        ) : null}
        {t.entities ? (
          <>
            ,{" "}
            <NumLink to="/graph" title="Entities and links">
              {fmt(t.entities)}
            </NumLink>{" "}
            entities
          </>
        ) : null}
        {t.links ? (
          <>
            {" and "}
            <NumLink to="/graph" title="Links">
              {fmt(t.links)}
            </NumLink>{" "}
            links
          </>
        ) : null}
        .
      </span>
    </p>
  );
}

/** A timezone name is an identifier from prefs; strip anything but the safe set. */
function sqlTz(tz: string): string {
  return tz.replace(/[^A-Za-z0-9_+\-/]/g, "") || "UTC";
}
