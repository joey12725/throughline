import { useMemo, useRef, useState, type ReactNode } from "react";
import {
  Sun,
  Moon,
  Monitor,
  Eye,
  Gauge,
  KeyRound,
  Palette as PaletteIcon,
  Map as MapIcon,
  MessageSquare,
  Clock,
} from "lucide-react";
import {
  Panel,
  Field,
  Select,
  Switch,
  Segmented,
  Badge,
  StaticChip,
  SourceDot,
  Menu,
  Button,
  KeyValue,
  useSourceColor,
  CodeBlock,
  PageHeader,
  RelativeTime,
} from "@/ui";
import { useBootstrap } from "@/api";
import { usePrefs, type ThemePref, type Density } from "@/store/prefs";
import { useScopes } from "@/store/scopes";
import { useAuth } from "@/store/auth";
import { HUE_ORDER } from "@/ui/sourceColor";
import { BackupPanel } from "./BackupPanel";
import { PluginsPanel, SetupLinkPanel } from "./PluginsPanel";
import styles from "./Settings.module.css";

const TIMEZONES = Intl.supportedValuesOf
  ? Intl.supportedValuesOf("timeZone")
  : ["UTC", "America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Tokyo"];

export default function SettingsScreen(): ReactNode {
  const { data: boot } = useBootstrap();
  const prefs = usePrefs();
  const tz = usePrefs((s) => s.tz);
  const scopeAsker = useScopes((s) => s.asker);
  const signOut = useAuth((s) => s.signOut);
  const mode = useAuth((s) => s.mode);
  const account = useAuth((s) => s.account);
  const session = useAuth((s) => s.session);

  const token = boot?.token;
  const features = boot?.features;

  return (
    <div className={styles.page}>
      <PageHeader
        className={styles.pageHead}
        title="Settings"
        lede="Preferences live with your installation. Tokens, purge and asker administration stay on the box."
      />

      <div className={styles.grid}>
        <Panel
          title={mode === "session" ? "Account" : "Token"}
          subtitle="This browser session"
          className={styles.span2}
        >
          {token ? (
            <>
              <KeyValue
                rows={[
                  ...(mode === "session" && account
                    ? [
                        {
                          key: "Signed in as",
                          value: (
                            <span>
                              {account.display_name ?? account.handle}{" "}
                              <span className={styles.muted}>@{account.handle}</span>
                            </span>
                          ),
                        },
                        {
                          key: "Signed in with",
                          value: session?.method === "password" ? "Username and password" : <Badge tone="neutral">{session?.method}</Badge>,
                        },
                        {
                          key: "Session ends",
                          value: session ? <RelativeTime value={session.expires_at} /> : "—",
                        },
                      ]
                    : []),
                  ...(mode === "session" ? [] : [{ key: "Name", value: <span className="tnum">{token.name}</span> }]),
                  { key: "Kind", value: <Badge tone="neutral">{token.kind}</Badge> },
                  {
                    key: "Scope",
                    value: token.unrestricted ? (
                      <Badge tone="ok">unrestricted</Badge>
                    ) : (
                      <Badge tone="warn">restricted</Badge>
                    ),
                  },
                  { key: "Can write", value: token.can_write ? "yes" : "read-only" },
                  { key: "Acting as", value: scopeAsker ? <span className="tnum">{scopeAsker}</span> : "owner" },
                ]}
              />
              {boot?.degraded && (
                <p className={styles.note}>
                  Running against an older server: token details are inferred until{" "}
                  <code>/v1/web/bootstrap</code> is available.
                </p>
              )}
              {mode === "session" ? (
                <p className={styles.note}>
                  Accounts are managed on the box with{" "}
                  <CodeBlock inline code="throughline lib account" />. What an account can see is its
                  principal token&apos;s scope.
                </p>
              ) : (
                <p className={styles.note}>
                  Tokens are minted on the box with{" "}
                  <CodeBlock inline code="throughline token create" />. The browser never mints them.
                </p>
              )}
              <div className={styles.row}>
                <Button variant="danger" onClick={signOut}>
                  Sign out of this browser
                </Button>
              </div>
            </>
          ) : (
            <p className={styles.note}>No token information.</p>
          )}
        </Panel>

        <Panel title="Appearance" subtitle="Theme and density">
          <Field label="Theme" inline hint="Nocturne is the default when your system prefers dark">
            <Segmented<ThemePref>
              aria-label="Theme"
              value={prefs.theme}
              onChange={(v) => prefs.set("theme", v)}
              items={[
                { value: "system", label: "Auto", icon: <Monitor size={14} /> },
                { value: "nocturne", label: "Nocturne", icon: <Moon size={14} /> },
                { value: "daybook", label: "Daybook", icon: <Sun size={14} /> },
              ]}
            />
          </Field>
          <Field label="Density" inline hint="Observatory 15px · Workbench 13px">
            <Segmented<Density>
              aria-label="Density"
              value={prefs.density}
              onChange={(v) => prefs.set("density", v)}
              items={[
                { value: "observatory", label: "Observatory" },
                { value: "workbench", label: "Workbench" },
              ]}
            />
          </Field>
        </Panel>

        <Panel title="Privacy" subtitle="For use in front of other people">
          <Field
            label={
              <span className={styles.inlineIcon}>
                <Eye size={15} /> Privacy veil
              </span>
            }
            inline
            hint="Blurs record text, names and places. Charts and counts stay sharp."
          >
            <Switch checked={prefs.veil} onChange={(v) => prefs.set("veil", v)} label="Privacy veil" />
          </Field>
        </Panel>

        <Panel title="Time" subtitle="The :tz every panel reads">
          <Field
            label={
              <span className={styles.inlineIcon}>
                <Clock size={15} /> Timezone
              </span>
            }
          >
            <Select value={tz} onChange={(e) => prefs.set("tz", e.target.value)}>
              {TIMEZONES.map((z) => (
                <option key={z} value={z}>
                  {z}
                </option>
              ))}
            </Select>
          </Field>
        </Panel>

        <Panel title="Map tiles" subtitle="An explicit, named opt-in">
          <Field
            label={
              <span className={styles.inlineIcon}>
                <MapIcon size={15} /> Street-level tiles
              </span>
            }
            inline
            hint="Off by default. When on, Places may fetch map tiles from a tile server you name — the one network exception, and only on the Places screen."
          >
            <Switch
              checked={prefs.mapTiles}
              onChange={(v) => prefs.set("mapTiles", v)}
              label="Street-level map tiles"
            />
          </Field>
        </Panel>

        <Panel title="Helper" subtitle="What the assistant may see">
          <Field
            label={
              <span className={styles.inlineIcon}>
                <MessageSquare size={15} /> Disclosure tier
              </span>
            }
            inline
            hint={
              prefs.disclosure === "schema"
                ? "Schema only: the model sees the catalogue, field names, types and result shapes. Rows stay in the browser."
                : "Samples: the model may also see clipped previews of rows."
            }
          >
            <Segmented
              aria-label="Disclosure tier"
              value={prefs.disclosure}
              onChange={(v) => prefs.set("disclosure", v as "schema" | "samples")}
              items={[
                { value: "schema", label: "Schema" },
                { value: "samples", label: "Samples" },
              ]}
            />
          </Field>
          {features && (
            <div className={styles.row}>
              <StaticChip lead={<Gauge size={13} />}>
                {features.assistant.provider
                  ? `${features.assistant.provider} · ${features.assistant.model ?? "model"}`
                  : "no model configured"}
              </StaticChip>
              {features.embeddings.available ? (
                <Badge tone="ok">embeddings on</Badge>
              ) : (
                <Badge tone="neutral">no embeddings</Badge>
              )}
            </div>
          )}
        </Panel>

        <BackupPanel isOwner={token?.kind === "owner" && token.unrestricted} />

        <SetupLinkPanel />

        <PluginsPanel />

        <Panel
          title="Source colours"
          subtitle="Hue belongs to the plugin; reassign if two feel too close"
          className={styles.span2}
        >
          <SourceColorList />
        </Panel>
      </div>
    </div>
  );
}

function SourceColorList(): ReactNode {
  const { data: boot } = useBootstrap();
  const sc = useSourceColor();
  const plugins = useMemo(() => boot?.plugins.map((p) => p.name) ?? [], [boot]);
  if (!plugins.length) return <p className={styles.note}>No plugins yet.</p>;
  return (
    <div className={styles.colors}>
      {plugins.map((plugin) => (
        <PluginColorRow key={plugin} plugin={plugin} currentHue={sc.hue(plugin)} />
      ))}
      <p className={styles.note}>
        <PaletteIcon size={13} /> Assignments are stored with your installation, so a new plugin never
        recolours the others.
      </p>
    </div>
  );
}

function PluginColorRow({ plugin, currentHue }: { plugin: string; currentHue: number }): ReactNode {
  const sc = useSourceColor();
  const anchorRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  return (
    <div className={styles.colorRow}>
      <SourceDot plugin={plugin} size={12} />
      <span className={styles.colorName}>{plugin}</span>
      <span className={styles.colorHue}>{Math.round(currentHue)}°</span>
      <button ref={anchorRef} type="button" className={styles.reassign} onClick={() => setOpen((v) => !v)}>
        reassign
      </button>
      <Menu
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={anchorRef}
        aria-label={`Hue for ${plugin}`}
        align="end"
        entries={HUE_ORDER.map((hue, i) => ({
          key: String(i),
          label: (
            <span className={styles.hueOption}>
              <span className={styles.hueSwatch} style={{ background: `oklch(var(--src-l) var(--src-c) ${hue})` }} />
              {hue}°
            </span>
          ),
          checked: Math.round(hue) === Math.round(currentHue),
          onSelect: () => sc.reassign(plugin, i),
        }))}
      />
    </div>
  );
}
