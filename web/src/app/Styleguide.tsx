import { useRef, useState, type ReactNode } from "react";
import {
  Plus,
  Search,
  Trash2,
  Settings as SettingsIcon,
  ChevronDown,
  Database,
} from "lucide-react";
import {
  Button,
  IconButton,
  Chip,
  StaticChip,
  Badge,
  StatusGlyph,
  Tabs,
  Segmented,
  Tooltip,
  Menu,
  Dialog,
  Drawer,
  Stat,
  KeyValue,
  Kbd,
  Field,
  Input,
  Select,
  Switch,
  CodeBlock,
  RelativeTime,
  Sensitive,
  Skeleton,
  EmptyState,
  ErrorState,
  Split,
  SourceDot,
  SourceChip,
  Panel,
  toast,
  useSourceColor,
} from "@/ui";
import { ApiError } from "@/api";
import { useBootstrap } from "@/api";
import * as sc from "@/ui/sourceColor";
import styles from "./Styleguide.module.css";

const SECTIONS = [
  "Themes",
  "Source palette",
  "Type",
  "Buttons",
  "Chips & badges",
  "Status",
  "Tabs",
  "Forms",
  "Stats",
  "Overlays",
  "Feedback",
  "Code & time",
  "Panes",
];

export default function Styleguide(): ReactNode {
  const { data: boot } = useBootstrap();
  // Demo sources come from live data, never hard-coded plugin names.
  const demoPlugin = boot?.plugins[0]?.name ?? "source";
  const demoChannel = boot?.channels[0];
  return (
    <div className={styles.page}>
      <aside className={styles.toc}>
        <div className={styles.tocHead}>Style guide</div>
        <nav>
          {SECTIONS.map((s) => (
            <a key={s} href={`#sg-${slug(s)}`} className={styles.tocLink}>
              {s}
            </a>
          ))}
        </nav>
      </aside>

      <div className={styles.body}>
        <header className={styles.intro}>
          <h1 className={styles.h1}>The system</h1>
          <p className={styles.lede}>
            Every primitive and the source palette, in both themes. This is how the design is judged
            and kept honest. Nocturne is an instrument room at night; Daybook is ink on warm paper;
            brass is the only accent — the throughline.
          </p>
        </header>

        <Section id="themes" title="Themes">
          <div className={styles.themePair}>
            <ThemeSwatches theme="nocturne" />
            <ThemeSwatches theme="daybook" />
          </div>
        </Section>

        <Section id="source-palette" title="Source palette">
          <SourcePalette />
        </Section>

        <Section id="type" title="Type">
          <div className={styles.type}>
            <div><span className={styles.tLabel}>38 / 2xl</span><p className={styles.s2xl}>The whole archive at a glance</p></div>
            <div><span className={styles.tLabel}>28 / xl</span><p className={styles.sxl}>Sources, coverage, outages</p></div>
            <div><span className={styles.tLabel}>22 / lg</span><p className={styles.slg}>Watch records arrive</p></div>
            <div><span className={styles.tLabel}>18 / md</span><p className={styles.smd}>Answer with SQL and a helper</p></div>
            <div><span className={styles.tLabel}>15 / base</span><p className={styles.sbase}>Interface text in Instrument Sans.</p></div>
            <div><span className={styles.tLabel}>serif / read</span><p className={styles.sread}>Long text — pages, e-mail, transcript turns — is set in Newsreader for a calmer read.</p></div>
            <div><span className={styles.tLabel}>mono</span><p className={styles.smono}>SELECT count(*) FROM api.messages — 186,042</p></div>
          </div>
        </Section>

        <Section id="buttons" title="Buttons">
          <Row>
            <Button variant="primary" icon={<Plus size={15} />}>Primary</Button>
            <Button>Default</Button>
            <Button variant="ghost">Ghost</Button>
            <Button variant="danger" icon={<Trash2 size={15} />}>Danger</Button>
            <Button disabled>Disabled</Button>
          </Row>
          <Row>
            <Button size="sm" variant="primary">Small</Button>
            <Button size="sm">Small</Button>
            <IconButton label="Search"><Search /></IconButton>
            <IconButton label="Settings" variant="default"><SettingsIcon /></IconButton>
            <IconButton label="Active" active><Database /></IconButton>
          </Row>
        </Section>

        <Section id="chips-badges" title="Chips & badges">
          <Row>
            <ChipDemo />
            {demoChannel && (
              <StaticChip lead={<SourceDot plugin={demoChannel.plugin} />}>{demoChannel.name}</StaticChip>
            )}
            <SourceChip plugin={demoPlugin} channel={boot?.channels[1]?.name ?? demoPlugin} channelIndex={1} />
          </Row>
          <Row>
            <Badge>neutral</Badge>
            <Badge tone="ok">complete</Badge>
            <Badge tone="warn">partial</Badge>
            <Badge tone="bad">failing</Badge>
          </Row>
        </Section>

        <Section id="status" title="Status">
          <Row>
            <StatusGlyph state="ok" />
            <StatusGlyph state="late" />
            <StatusGlyph state="stale" />
            <StatusGlyph state="failing" />
            <StatusGlyph state="paused" />
            <StatusGlyph state="one_shot" />
            <StatusGlyph state="never_ran" />
          </Row>
        </Section>

        <Section id="tabs" title="Tabs & segmented">
          <TabsDemo />
        </Section>

        <Section id="forms" title="Forms">
          <div className={styles.forms}>
            <Field label="Text" htmlFor="sg-text" hint="A plain field">
              <Input id="sg-text" placeholder="Zibble Mosskite" />
            </Field>
            <Field label="Identifier" htmlFor="sg-mono">
              <Input id="sg-mono" mono lead={<Search size={14} />} placeholder="fake-person-0" />
            </Field>
            <Field label="Select" htmlFor="sg-select">
              <Select id="sg-select" defaultValue="month">
                <option value="week">week</option>
                <option value="month">month</option>
                <option value="year">year</option>
              </Select>
            </Field>
            <SwitchDemo />
            <Field label="Invalid" htmlFor="sg-invalid" error="relations must be explicitly in schema api">
              <Input id="sg-invalid" invalid defaultValue="SELECT * FROM tl.messages" mono />
            </Field>
          </div>
        </Section>

        <Section id="stats" title="Stats & key/value">
          <div className={styles.statRow}>
            <Stat label="Records" value="186,042" hint="across 6 channels" />
            <Stat label="People" value="12" />
            <Stat label="Threads" value="2,206" />
            <Stat label="Vectors" value="0" unit="none" />
          </div>
          <div style={{ maxWidth: 440, marginTop: 16 }}>
            <KeyValue
              rows={[
                { key: "channel", value: <span className="tnum">{demoChannel?.name ?? "example.channel"}</span> },
                { key: "record kind", value: "life_event" },
                { key: "first", value: <RelativeTime value="2022-01-01T12:00:00Z" /> },
                { key: "last ingest", value: <RelativeTime value={new Date(Date.now() - 6e5)} /> },
              ]}
            />
          </div>
        </Section>

        <Section id="overlays" title="Overlays">
          <OverlayDemo />
        </Section>

        <Section id="feedback" title="Feedback">
          <div className={styles.feedback}>
            <Panel title="Skeleton">
              <Skeleton lines={3} />
            </Panel>
            <Panel title="Empty">
              <EmptyState compact icon={<Database />} title="No rows" description="This channel has no records in the current scope." />
            </Panel>
            <div className={styles.span2}>
              <ErrorState
                error={new ApiError({ status: 400, message: "database operation rejected", code: "42703" })}
                title="Query rejected"
                onRetry={() => toast.info("Retried")}
              />
            </div>
          </div>
          <Row>
            <Button onClick={() => toast.ok("Saved", "Your query is on the server.")}>Toast ok</Button>
            <Button onClick={() => toast.warn("Heads up", "Two plugins are stale.")}>Toast warn</Button>
            <Button onClick={() => toast.error("Rejected", "SQLSTATE 42703: unknown column.")}>Toast error</Button>
          </Row>
        </Section>

        <Section id="code-time" title="Code, time & veil">
          <div className={styles.codeRow}>
            <CodeBlock lang="sql" code={"SELECT channel, count(*)\nFROM api.messages\nGROUP BY 1 ORDER BY 2 DESC"} />
            <div>
              <p>Keyboard: <Kbd>⌘</Kbd> <Kbd>K</Kbd> opens the palette.</p>
              <p>
                Under the veil:{" "}
                <Sensitive>a personal name, a place, a line of a message</Sensitive>
              </p>
              <p className={styles.dim}>(toggle the veil in the header to see the blur)</p>
            </div>
          </div>
        </Section>

        <Section id="panes" title="Resizable panes">
          <div className={styles.split}>
            <Split storageKey="sg" initial={220} min={120} max={360} first={<div className={styles.pane}>Schema rail</div>} second={<div className={styles.pane}>Results</div>} />
          </div>
        </Section>
      </div>
    </div>
  );
}

/* ---- helpers ------------------------------------------------------------- */

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }): ReactNode {
  return (
    <section id={`sg-${id}`} className={styles.section}>
      <h2 className={styles.h2}>{title}</h2>
      {children}
    </section>
  );
}

function Row({ children }: { children: ReactNode }): ReactNode {
  return <div className={styles.demoRow}>{children}</div>;
}

function ThemeSwatches({ theme }: { theme: "nocturne" | "daybook" }): ReactNode {
  const tokens = [
    ["--bg-0", "canvas"],
    ["--bg-1", "panel"],
    ["--bg-2", "raised"],
    ["--bg-3", "hover"],
    ["--line", "line"],
    ["--ink", "ink"],
    ["--ink-2", "ink-2"],
    ["--brass", "brass"],
    ["--patina", "patina"],
    ["--ok", "ok"],
    ["--warn", "warn"],
    ["--bad", "bad"],
  ];
  return (
    <div className={styles.themeCard} data-theme={theme}>
      <div className={styles.themeName}>{theme === "nocturne" ? "Nocturne" : "Daybook"}</div>
      <div className={styles.swatchGrid}>
        {tokens.map(([v, label]) => (
          <div key={v} className={styles.swatch}>
            <span className={styles.swatchChip} style={{ background: `var(${v})` }} />
            <span className={styles.swatchLabel}>{label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function SourcePalette(): ReactNode {
  const { data: boot } = useBootstrap();
  const color = useSourceColor();
  const plugins = boot?.plugins.map((p) => p.name) ?? [];
  const first = plugins[0] ?? "source";
  return (
    <div>
      <div className={styles.palRow}>
        {plugins.map((p) => (
          <div key={p} className={styles.palItem}>
            <SourceDot plugin={p} size={16} />
            <span className={styles.palName}>{p}</span>
            <span className={styles.palHue}>{Math.round(color.hue(p))}°</span>
          </div>
        ))}
      </div>
      <div className={styles.rampRow}>
        <Ramp title="Channel lightness steps" colors={[0, 1, 2, 3, 4].map((k) => color.cssFor(first, k))} />
        <Ramp title="Sequential (source hue)" colors={sc.sequential(color.assignments, first, 6, color.theme)} />
        <Ramp title="Categorical (within channel)" colors={sc.categorical(6, color.theme)} />
        <Ramp title="Diverging (signed)" colors={sc.diverging(7, color.theme)} />
      </div>
    </div>
  );
}

function Ramp({ title, colors }: { title: string; colors: string[] }): ReactNode {
  return (
    <div className={styles.ramp}>
      <div className={styles.rampTitle}>{title}</div>
      <div className={styles.rampBar}>
        {colors.map((c, i) => (
          <span key={i} style={{ background: c }} />
        ))}
      </div>
    </div>
  );
}

function ChipDemo(): ReactNode {
  const [on, setOn] = useState(true);
  return (
    <Chip active={on} onClick={() => setOn((v) => !v)} onRemove={() => setOn(false)}>
      filter: this week
    </Chip>
  );
}

function SwitchDemo(): ReactNode {
  const [on, setOn] = useState(true);
  return (
    <Field label="Switch" inline hint="An on/off control">
      <Switch checked={on} onChange={setOn} label="Demo switch" />
    </Field>
  );
}

function TabsDemo(): ReactNode {
  const [tab, setTab] = useState("table");
  const [seg, setSeg] = useState("loom");
  return (
    <div className={styles.tabsDemo}>
      <Tabs
        value={tab}
        onChange={setTab}
        items={[
          { value: "table", label: "Table" },
          { value: "chart", label: "Chart", count: 6 },
          { value: "profile", label: "Profile" },
          { value: "execution", label: "Execution" },
        ]}
      />
      <Segmented
        aria-label="Loom view"
        value={seg}
        onChange={setSeg}
        items={[
          { value: "loom", label: "Loom" },
          { value: "river", label: "River" },
        ]}
      />
    </div>
  );
}

function OverlayDemo(): ReactNode {
  const menuRef = useRef<HTMLButtonElement | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  return (
    <Row>
      <Tooltip content="Exact time on hover, relative at rest">
        <Button>Hover for a tooltip</Button>
      </Tooltip>
      <Button ref={menuRef} icon={<ChevronDown size={15} />} onClick={() => setMenuOpen((v) => !v)}>
        Menu
      </Button>
      <Menu
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
        anchorRef={menuRef}
        aria-label="Demo menu"
        entries={[
          { header: "Row" },
          { key: "open", label: "Open channel", onSelect: () => toast.info("Open") },
          { key: "sql", label: "Show SQL", onSelect: () => toast.info("SQL") },
          { separator: true },
          { key: "hide", label: "Hide field", danger: true, onSelect: () => toast.info("Hidden") },
        ]}
      />
      <Button onClick={() => setDialogOpen(true)}>Dialog</Button>
      <Button onClick={() => setDrawerOpen(true)}>Drawer</Button>
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        title="Save this query"
        description="Saved queries live with your installation."
        footer={
          <>
            <Button onClick={() => setDialogOpen(false)}>Cancel</Button>
            <Button variant="primary" onClick={() => setDialogOpen(false)}>Save</Button>
          </>
        }
      >
        <Field label="Name" htmlFor="sg-dialog-name">
          <Input id="sg-dialog-name" placeholder="Bedtime to wake, per night" data-autofocus />
        </Field>
      </Dialog>
      <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} title="Record">
        <KeyValue
          rows={[
            { key: "record_id", value: <span className="tnum">m:1549</span> },
            { key: "channel", value: "example.channel" },
            { key: "content", value: <Sensitive>A record's text, rendered as text — never as HTML.</Sensitive> },
          ]}
        />
      </Drawer>
    </Row>
  );
}

function slug(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-");
}
