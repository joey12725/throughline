/*
 * Live — the fire hose as it moves. A per-plugin rate strip, the arrival list
 * rendered by archetype (virtualised, newest first, with the settle line and
 * backfill folding), and, for an owner token, a Reads lane of what tokens are
 * asking. Controls: pause/resume, plugin filter chips and replay the last 15
 * minutes. Everything derives from the arrivals route and the bootstrap shape.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Pause, Play, Rewind, Eye, EyeOff } from "lucide-react";
import { useBootstrap } from "@/api";
import { Button, Chip, Panel, EmptyState, ErrorState, Skeleton, SourceDot } from "@/ui";
import { useChannelIndex } from "@/features/loom/useChannelIndex";
import { useLiveStream } from "./liveStream";
import { RateStrip } from "./RateStrip";
import { ArrivalList } from "./ArrivalList";
import { ReadsLane } from "./ReadsLane";
import styles from "./Live.module.css";

export default function LiveScreen(): ReactNode {
  const boot = useBootstrap();
  const index = useChannelIndex();
  const pluginOf = useCallback(
    (channel: string) => index?.byName.get(channel)?.plugin ?? channel.split(".")[0] ?? channel,
    [index],
  );
  const live = useLiveStream(pluginOf);
  const [activePlugins, setActivePlugins] = useState<ReadonlySet<string>>(new Set());
  const [showReads, setShowReads] = useState(true);
  const [now, setNow] = useState(() => Date.now());

  // Keep relative times fresh.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const togglePlugin = useCallback((plugin: string) => {
    setActivePlugins((prev) => {
      const next = new Set(prev);
      if (next.has(plugin)) next.delete(plugin);
      else next.add(plugin);
      return next;
    });
  }, []);

  const owner = !!boot.data?.token.unrestricted;

  const pluginList = useMemo(() => index?.groups.map((g) => g.plugin) ?? [], [index]);

  if (boot.isLoading || !index) {
    return (
      <div className={styles.page}>
        <Skeleton height={80} />
        <Skeleton height={360} />
      </div>
    );
  }
  if (boot.isError || !boot.data) {
    return (
      <div className={styles.page}>
        <ErrorState error={boot.error} title="Could not connect to the archive" />
      </div>
    );
  }

  const unsupported = live.error?.status === 404;
  const forbidden = live.error?.status === 403;

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>Live</h1>
          <span className={styles.conn} data-on={live.connected} data-paused={live.paused || live.hidden}>
            {live.hidden ? "paused · tab hidden" : live.paused ? "paused" : live.connected ? "live" : "connecting…"}
          </span>
        </div>
        <div className={styles.controls}>
          <Button
            size="sm"
            variant={live.paused ? "primary" : "default"}
            icon={live.paused ? <Play size={15} /> : <Pause size={15} />}
            onClick={() => live.setPaused(!live.paused)}
          >
            {live.paused ? "Resume" : "Pause"}
          </Button>
          <Button
            size="sm"
            variant="default"
            icon={<Rewind size={15} />}
            onClick={live.replay}
            disabled={live.replaying}
          >
            {live.replaying ? `Replaying ${Math.round(live.replayProgress * 100)}%` : "Replay 15m"}
          </Button>
          {owner && (
            <Button
              size="sm"
              variant="ghost"
              icon={showReads ? <EyeOff size={15} /> : <Eye size={15} />}
              onClick={() => setShowReads((s) => !s)}
            >
              Reads
            </Button>
          )}
        </div>
      </header>

      <RateStrip plugins={live.ratePlugins} totalPerMin={live.totalPerMin} now={now} />

      {pluginList.length > 0 && (
        <div className={styles.chips}>
          <Chip active={activePlugins.size === 0} onClick={() => setActivePlugins(new Set())}>
            All sources
          </Chip>
          {pluginList.map((p) => (
            <Chip
              key={p}
              active={activePlugins.has(p)}
              lead={<SourceDot plugin={p} channelIndex={0} size={8} />}
              onClick={() => togglePlugin(p)}
            >
              {p}
            </Chip>
          ))}
        </div>
      )}

      <div className={styles.body} data-reads={owner && showReads}>
        <Panel title="Arrivals" className={styles.arrivalsPanel} bodyClassName={styles.panelBody}>
          {forbidden ? (
            <EmptyState
              title="Arrivals are not available for this token"
              description="A restricted token cannot watch the live feed. Sign in as the owner to see arrivals."
            />
          ) : unsupported ? (
            <EmptyState
              title="Arrivals need a current server"
              description="This build of the API has no long-poll arrivals route yet."
            />
          ) : live.replaying && live.rows.length === 0 ? (
            <EmptyState title="Replaying…" description="Playing back the last 15 minutes of ingestion." />
          ) : (
            <ArrivalList
              rows={live.rows}
              index={index}
              activePlugins={activePlugins}
              settleHorizon={live.settleHorizon}
              settleS={live.settleS}
              now={now}
            />
          )}
        </Panel>

        {owner && showReads && (
          <Panel title="Reads" subtitle="what tokens are asking" className={styles.readsPanel} bodyClassName={styles.panelBody}>
            <ReadsLane reads={live.reads} now={now} />
          </Panel>
        )}
      </div>
    </div>
  );
}
