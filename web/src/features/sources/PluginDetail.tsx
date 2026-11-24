/*
 * /sources/:plugin — one source in full. Its manifest facts, every channel it
 * owns, its arrivals history, any open outage, its last error, and the exact
 * checklist to reconnect it (tokens are minted only on the box — never here).
 * Nothing is special-cased per source; the name is just a parameter.
 */
import { useMemo, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { useBootstrap } from "@/api";
import { EmptyState, ErrorState, KeyValue, Page, Panel, Skeleton, StatusGlyph, SourceDot, CodeBlock, useSourceColor } from "@/ui";
import { relative } from "@/store/scopes";
import { ChannelCard } from "@/features/channel";
import { deriveSourceState, formatCadence } from "./status";
import { Sparkline } from "./Sparkline";
import { FreshnessMatrix, type MatrixChannel } from "./FreshnessMatrix";
import { MatrixLegend } from "./MatrixLegend";
import { channelIndexMap } from "./channelIndex";
import { useFreshnessMatrix, useMatrixIndex, usePluginHistory, historyByPlugin } from "./data";
import { reconnectSteps } from "./reconnect";
import styles from "./Sources.module.css";

export function PluginDetail({ plugin: pluginName }: { plugin: string }): ReactNode {
  const { data: boot, isLoading, error } = useBootstrap();
  const { cssFor } = useSourceColor();
  const now = boot ? Date.parse(boot.now) : Date.now();

  const fresh = useFreshnessMatrix(!!boot);
  const matrixIndex = useMatrixIndex(fresh.data?.rows);
  const history = usePluginHistory(!!boot);
  const histByPlugin = useMemo(() => historyByPlugin(history.data?.rows), [history.data]);

  const channelIndex = useMemo(() => channelIndexMap(boot?.channels ?? []), [boot?.channels]);
  const plugin = boot?.plugins.find((p) => p.name === pluginName) ?? null;
  const channels = useMemo(
    () => (boot?.channels ?? []).filter((c) => c.plugin === pluginName).sort((a, b) => (a.name < b.name ? -1 : 1)),
    [boot?.channels, pluginName],
  );

  const matrixChannels: MatrixChannel[] = useMemo(
    () =>
      channels
        .filter((c) => c.rows > 0)
        .map((c) => ({ name: c.name, plugin: c.plugin, channelIndex: channelIndex.get(c.name) ?? 0 })),
    [channels, channelIndex],
  );

  if (isLoading && !boot) return <Page width="wide"><Skeleton height={32} /><Skeleton height={200} /></Page>;
  if (error) return <ErrorState error={error} />;
  if (!boot) return <EmptyState title="No data" />;

  // A restricted token gets no plugin rows; show whatever channels it can see.
  if (!plugin && channels.length === 0) {
    return (
      <Page width="wide">
        <BackLink />
        <EmptyState title="Source not found" description={`No source named "${pluginName}" is visible to this token.`} />
      </Page>
    );
  }

  const health = plugin ? deriveSourceState(plugin, now) : null;
  const records = channels.reduce((s, c) => s + (c.rows || 0), 0);
  const steps = plugin ? reconnectSteps(plugin) : [];

  return (
    <Page width="wide" style={{ ["--src" as string]: cssFor(pluginName, 0) }}>
      <BackLink />
      <header className={styles.detailHead}>
        <SourceDot plugin={pluginName} channelIndex={0} size={18} />
        <h1 className={styles.h1}>{pluginName}</h1>
        {health ? <StatusGlyph state={health.state} size={16} /> : null}
      </header>

      <div className={styles.detailGrid}>
        <Panel title="Source">
          <KeyValue
            rows={[
              ...(plugin?.version ? [{ key: "Version", value: plugin.version }] : []),
              { key: "Mode", value: plugin?.mode ?? "—" },
              { key: "Cadence", value: formatCadence(plugin?.interval_s) },
              {
                key: "Last success",
                value: plugin?.last_success_at ? `${relative(plugin.last_success_at, now)}` : "never",
              },
              { key: "Records", value: records.toLocaleString() },
              { key: "Channels", value: String(channels.length) },
            ]}
          />
        </Panel>

        <Panel title="Arrivals" subtitle="Weekly, against this source's own band">
          <div className={styles.detailSpark}>
            <Sparkline
              counts={histByPlugin.get(pluginName) ?? []}
              color="var(--src, var(--brass))"
              width={360}
              height={72}
              ariaLabel={`${pluginName} weekly arrivals`}
            />
          </div>
        </Panel>
      </div>

      {plugin?.last_error ? (
        <Panel title="Last error" subtitle="What the source last reported">
          <pre className={styles.errorPre}>{plugin.last_error}</pre>
        </Panel>
      ) : null}

      {plugin?.open_outage ? (
        <Panel title="Open outage">
          <pre className={styles.errorPre}>{JSON.stringify(plugin.open_outage, null, 2)}</pre>
        </Panel>
      ) : null}

      {matrixChannels.length > 0 ? (
        <Panel title="Freshness" subtitle="Last 60 days" actions={<MatrixLegend />}>
          <FreshnessMatrix channels={matrixChannels} index={matrixIndex} />
        </Panel>
      ) : null}

      {steps.length > 0 ? (
        <Panel title="Reconnect" subtitle="Run these on the box — tokens are minted there, never from the web">
          <ol className={styles.checklist}>
            {steps.map((s, i) => (
              <li key={i}>
                <p className={styles.stepTitle}>{s.title}</p>
                {s.detail ? <p className={styles.stepDetail}>{s.detail}</p> : null}
                {s.command ? <CodeBlock code={s.command} lang="bash" /> : null}
              </li>
            ))}
          </ol>
        </Panel>
      ) : null}

      <Panel title={`Channels (${channels.length})`}>
        {channels.length === 0 ? (
          <EmptyState title="No channels" description="This source has registered no channels yet." />
        ) : (
          <div className={styles.channelGrid}>
            {channels.map((c) => (
              <ChannelCard key={c.name} meta={c} channelIndex={channelIndex.get(c.name) ?? 0} plugin={plugin} />
            ))}
          </div>
        )}
      </Panel>
    </Page>
  );
}

function BackLink(): ReactNode {
  return (
    <Link to="/sources" className={styles.backLink}>
      <ArrowLeft size={15} aria-hidden /> All sources
    </Link>
  );
}
