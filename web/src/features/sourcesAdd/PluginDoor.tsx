/*
 * Connect a plugin: the exact commands to run on the box, and a live row that
 * turns green the moment the source posts its first manifest, heartbeat and
 * records. We poll the bootstrap every few seconds while this door is open,
 * keyed by the plugin name the person types.
 */
import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Check, Circle, Loader2, ArrowRight } from "lucide-react";
import { loadBootstrap } from "@/api";
import { CodeBlock, Field, Input } from "@/ui";
import { CHANNEL_NAME_RE } from "./envelope";
import styles from "./Add.module.css";

interface WatchState {
  manifest: boolean;
  heartbeat: boolean;
  records: boolean;
  channels: string[];
}

export function PluginDoor(): ReactNode {
  const [name, setName] = useState("");
  const clean = name.trim().toLowerCase();
  const valid = clean.length > 0 && /^[a-z][a-z0-9_]*$/.test(clean);

  const watch = useQuery({
    queryKey: ["plugin-watch", clean],
    enabled: valid,
    refetchInterval: valid ? 5000 : false,
    queryFn: async (): Promise<WatchState> => {
      const boot = await loadBootstrap();
      const p = boot.plugins.find((x) => x.name === clean) ?? null;
      const channels = boot.channels.filter((c) => c.plugin === clean).map((c) => c.name);
      const records = channels.some((n) => (boot.channels.find((c) => c.name === n)?.rows ?? 0) > 0);
      return {
        manifest: !!p,
        heartbeat: !!p?.last_success_at || (p?.messages_ingested ?? 0) > 0,
        records: records || (p?.messages_ingested ?? 0) > 0,
        channels,
      };
    },
  });

  const state = watch.data;

  return (
    <div className={styles.pluginDoor}>
      <Field label="What will the source be called?" hint="lower_snake_case, e.g. strava or my_journal.">
        <Input
          value={name}
          spellCheck={false}
          placeholder="my_source"
          onChange={(e) => setName(e.target.value)}
          aria-label="Plugin name"
          mono
        />
      </Field>

      <ol className={styles.checklist}>
        <li>
          <p className={styles.stepTitle}>Install the collector on the box</p>
          <p className={styles.stepDetail}>A plugin is a small program that fetches and posts records. Run it where Throughline runs.</p>
        </li>
        <li>
          <p className={styles.stepTitle}>Mint a plugin token — only ever on the box</p>
          <CodeBlock code={`throughline token create ${clean || "my_source"} --kind plugin --plugin ${clean || "my_source"}`} lang="bash" />
        </li>
        <li>
          <p className={styles.stepTitle}>Point the collector at this server and run it</p>
          <CodeBlock
            code={`export THROUGHLINE_URL=${location.origin}\nexport THROUGHLINE_TOKEN=<the token you just minted>\nthroughline run ${clean || "my_source"}`}
            lang="bash"
          />
        </li>
      </ol>

      <div className={styles.liveRow} data-valid={valid}>
        <div className={styles.liveHead}>
          <span className={styles.liveTitle}>
            {valid ? (
              watch.isFetching ? (
                <>
                  <Loader2 size={14} className={styles.spin} aria-hidden /> watching for “{clean}”…
                </>
              ) : (
                <>watching for “{clean}”</>
              )
            ) : (
              "Type a name above to start watching"
            )}
          </span>
        </div>
        <ul className={styles.liveChecks}>
          <LiveCheck done={!!state?.manifest} label="manifest posted" />
          <LiveCheck done={!!state?.heartbeat} label="first heartbeat" />
          <LiveCheck done={!!state?.records} label="records arriving" />
        </ul>
        {state && state.channels.length > 0 ? (
          <div className={styles.liveChannels}>
            {state.channels.map((c) => (
              <Link key={c} to={`/c/${encodeURIComponent(c)}`} className={styles.liveChannelLink}>
                {c} <ArrowRight size={12} aria-hidden />
              </Link>
            ))}
          </div>
        ) : null}
        {state?.manifest && state.records ? (
          <Link to={`/sources/${encodeURIComponent(clean)}`} className={styles.doneLink}>
            {clean} is live — open it <ArrowRight size={15} aria-hidden />
          </Link>
        ) : null}
      </div>

      {valid && !CHANNEL_NAME_RE.test(clean) ? (
        <p className={styles.mapNote}>The name must start with a letter and use only lower_snake_case.</p>
      ) : null}
    </div>
  );
}

function LiveCheck({ done, label }: { done: boolean; label: string }): ReactNode {
  return (
    <li className={styles.liveCheck} data-done={done}>
      {done ? <Check size={15} aria-hidden /> : <Circle size={15} aria-hidden />}
      <span>{label}</span>
    </li>
  );
}
