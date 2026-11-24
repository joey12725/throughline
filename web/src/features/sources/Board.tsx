/*
 * The source board: a dense health surface. A summary sentence, the freshness
 * matrix (channels x 60 days), then one expandable row per source, sorted by
 * severity then name. A restricted token sees no source rows (the host returns
 * none) — it still gets the freshness of the channels it can see.
 */
import { useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Plus } from "lucide-react";
import { useBootstrap } from "@/api";
import { EmptyState, ErrorState, Page, PageHeader, Panel, Skeleton } from "@/ui";
import { deriveSourceState, summarySentence } from "./status";
import { PluginRowCard } from "./PluginRowCard";
import { FreshnessMatrix, type MatrixChannel } from "./FreshnessMatrix";
import { channelIndexMap, channelsByPlugin } from "./channelIndex";
import { useFreshnessMatrix, useMatrixIndex, usePluginHistory, historyByPlugin } from "./data";
import { MatrixLegend } from "./MatrixLegend";
import styles from "./Sources.module.css";

export function Board(): ReactNode {
  const { data: boot, isLoading, error } = useBootstrap();
  const now = boot ? Date.parse(boot.now) : Date.now();

  const fresh = useFreshnessMatrix(!!boot);
  const matrixIndex = useMatrixIndex(fresh.data?.rows);
  const history = usePluginHistory(!!boot && (boot?.plugins.length ?? 0) > 0);
  const histByPlugin = useMemo(() => historyByPlugin(history.data?.rows), [history.data]);

  const channelIndex = useMemo(() => channelIndexMap(boot?.channels ?? []), [boot?.channels]);
  const byPlugin = useMemo(() => channelsByPlugin(boot?.channels ?? []), [boot?.channels]);

  const matrixChannels: MatrixChannel[] = useMemo(
    () =>
      (boot?.channels ?? [])
        .filter((c) => c.rows > 0)
        .map((c) => ({ name: c.name, plugin: c.plugin, channelIndex: channelIndex.get(c.name) ?? 0 }))
        .sort((a, b) => (a.plugin === b.plugin ? (a.name < b.name ? -1 : 1) : a.plugin < b.plugin ? -1 : 1)),
    [boot?.channels, channelIndex],
  );

  const sortedPlugins = useMemo(() => {
    const plugins = [...(boot?.plugins ?? [])];
    return plugins.sort((a, b) => {
      const sa = deriveSourceState(a, now).severity;
      const sb = deriveSourceState(b, now).severity;
      return sa === sb ? (a.name < b.name ? -1 : 1) : sa - sb;
    });
  }, [boot?.plugins, now]);

  if (isLoading && !boot) {
    return (
      <Page width="wide">
        <Skeleton height={32} />
        <Skeleton height={220} />
        <Skeleton height={64} />
        <Skeleton height={64} />
      </Page>
    );
  }
  if (error) return <ErrorState error={error} />;
  if (!boot) return <EmptyState title="No data" />;

  const restricted = boot.plugins.length === 0;

  return (
    <Page width="wide">
      <PageHeader
        title="Sources"
        lede={restricted ? `${matrixChannels.length} channels visible` : summarySentence(boot.plugins, now)}
        actions={
          <Link to="/sources/add" className={styles.addBtn}>
            <Plus size={16} aria-hidden /> Add a source
          </Link>
        }
      />

      <Panel
        title="Freshness"
        subtitle={`Each channel's last 60 days — colour is volume, outlines and hatching show what is missing`}
        actions={<MatrixLegend />}
      >
        {matrixChannels.length === 0 ? (
          <EmptyState title="No records yet" description="Channels will light up here as records arrive." />
        ) : (
          <FreshnessMatrix channels={matrixChannels} index={matrixIndex} />
        )}
      </Panel>

      {restricted ? (
        <Panel title="Channels">
          <div className={styles.channelGrid}>
            {boot.channels.map((c) => (
              <RestrictedChannel key={c.name} name={c.name} />
            ))}
          </div>
          {boot.channels.length === 0 ? (
            <EmptyState title="Nothing shared" description="This token has access to no channels." />
          ) : null}
        </Panel>
      ) : (
        <div className={styles.pluginList}>
          {sortedPlugins.map((p) => (
            <PluginRowCard
              key={p.name}
              plugin={p}
              channels={byPlugin.get(p.name) ?? []}
              channelIndex={channelIndex}
              history={histByPlugin.get(p.name) ?? []}
              now={now}
            />
          ))}
        </div>
      )}
    </Page>
  );
}

function RestrictedChannel({ name }: { name: string }): ReactNode {
  return (
    <Link to={`/c/${encodeURIComponent(name)}`} className={styles.restrictedChannel}>
      {name}
    </Link>
  );
}
