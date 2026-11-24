import { useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Search,
  Eye,
  EyeOff,
  Sun,
  Moon,
  Monitor,
  Rows3,
  AlertTriangle,
  Gauge,
  UserCog,
  ChevronDown,
  MoreHorizontal,
} from "lucide-react";
import { IconButton, Menu, Kbd, cmdKey, Tooltip, Popover, Button, Input, Field, type MenuEntry } from "@/ui";
import { TimeScopeControl } from "./TimeScopeControl";
import { useBootstrap, request } from "@/api";
import { usePrefs } from "@/store/prefs";
import { useScopes } from "@/store/scopes";
import { titleFor } from "./nav";
import { useLocation } from "react-router-dom";
import styles from "./Header.module.css";

/** Track a media query (SSR-safe, updates on change). */
function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== "undefined" && window.matchMedia ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(query);
    const on = (): void => setMatches(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return matches;
}

export function Header({ onOpenPalette }: { onOpenPalette: () => void }): ReactNode {
  const { pathname } = useLocation();
  const prefs = usePrefs();
  const veil = usePrefs((s) => s.veil);
  const theme = usePrefs((s) => s.theme);
  const navigate = useNavigate();
  const { data: boot } = useBootstrap();
  const themeRef = useRef<HTMLButtonElement | null>(null);
  const [themeOpen, setThemeOpen] = useState(false);
  const phone = useMediaQuery("(max-width: 720px)");

  const stalePlugins = boot?.plugins.filter((p) => p.stale).length ?? 0;
  const openGaps = boot?.channels.reduce((a, c) => a + (c.coverage.open_gaps ?? 0), 0) ?? 0;

  const approvals = useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: async () => {
      const rows = await request<unknown[]>("/approvals?status=pending");
      return rows.length;
    },
    staleTime: 60_000,
    retry: false,
  });
  const pending = approvals.data ?? 0;

  const ThemeIcon = theme === "nocturne" ? Moon : theme === "daybook" ? Sun : Monitor;

  return (
    <header className={styles.header}>
      <div className={styles.left}>
        <h1 className={styles.screen}>{titleFor(pathname)}</h1>
        <button type="button" className={styles.search} onClick={onOpenPalette} aria-label="Open command palette">
          <Search size={15} />
          <span className={styles.searchLabel}>Search channels, people, actions…</span>
          {!phone && (
            <Kbd>
              {cmdKey()} K
            </Kbd>
          )}
        </button>
      </div>

      <div className={styles.center}>
        <TimeScopeControl />
        {!phone && <TzChip />}
        {!phone && boot?.token.kind === "owner" && <AskerControl />}
      </div>

      {phone ? (
        <div className={styles.right}>
          <OverflowMenu
            veil={veil}
            theme={theme}
            density={prefs.density}
            stalePlugins={stalePlugins}
            openGaps={openGaps}
            pending={pending}
            onVeil={() => prefs.set("veil", !veil)}
            onDensity={() => prefs.set("density", prefs.density === "observatory" ? "workbench" : "observatory")}
            onTheme={(t) => prefs.set("theme", t)}
            navigate={navigate}
          />
        </div>
      ) : (
        <div className={styles.right}>
          {stalePlugins > 0 && (
            <HealthChip tone="warn" icon={<Gauge size={13} />} count={stalePlugins} label="stale plugins" onClick={() => navigate("/sources")} />
          )}
          {openGaps > 0 && (
            <HealthChip tone="warn" icon={<AlertTriangle size={13} />} count={openGaps} label="open gaps" onClick={() => navigate("/sources")} />
          )}
          {pending > 0 && (
            <HealthChip tone="bad" icon={<Gauge size={13} />} count={pending} label="pending approvals" onClick={() => navigate("/ledger")} />
          )}

          <span className={styles.divider} aria-hidden />

          <Tooltip content={veil ? "Privacy veil on" : "Privacy veil off"} side="bottom">
            <IconButton label="Toggle privacy veil" active={veil} onClick={() => prefs.set("veil", !veil)}>
              {veil ? <EyeOff /> : <Eye />}
            </IconButton>
          </Tooltip>

          <Tooltip content="Density" side="bottom">
            <IconButton
              label="Toggle density"
              active={prefs.density === "workbench"}
              onClick={() => prefs.set("density", prefs.density === "observatory" ? "workbench" : "observatory")}
            >
              <Rows3 />
            </IconButton>
          </Tooltip>

          <IconButton ref={themeRef} label="Theme" onClick={() => setThemeOpen((v) => !v)}>
            <ThemeIcon />
          </IconButton>
          <Menu
            open={themeOpen}
            onClose={() => setThemeOpen(false)}
            anchorRef={themeRef}
            align="end"
            aria-label="Theme"
            entries={[
              { key: "system", label: "Auto (system)", icon: <Monitor size={15} />, checked: theme === "system", onSelect: () => prefs.set("theme", "system") },
              { key: "nocturne", label: "Nocturne", icon: <Moon size={15} />, checked: theme === "nocturne", onSelect: () => prefs.set("theme", "nocturne") },
              { key: "daybook", label: "Daybook", icon: <Sun size={15} />, checked: theme === "daybook", onSelect: () => prefs.set("theme", "daybook") },
            ]}
          />
        </div>
      )}
    </header>
  );
}

/** Phone header overflow: veil, density, theme and health shortcuts in one menu. */
function OverflowMenu({
  veil,
  theme,
  density,
  stalePlugins,
  openGaps,
  pending,
  onVeil,
  onDensity,
  onTheme,
  navigate,
}: {
  veil: boolean;
  theme: "system" | "nocturne" | "daybook";
  density: string;
  stalePlugins: number;
  openGaps: number;
  pending: number;
  tz?: undefined;
  onVeil: () => void;
  onDensity: () => void;
  onTheme: (t: "system" | "nocturne" | "daybook") => void;
  navigate: (to: string) => void;
}): ReactNode {
  const ref = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const hasHealth = stalePlugins > 0 || openGaps > 0 || pending > 0;
  const entries: MenuEntry[] = [];
  if (hasHealth) {
    entries.push({ header: "Needs attention" });
    if (stalePlugins > 0)
      entries.push({ key: "stale", label: `${stalePlugins} stale plugins`, icon: <Gauge size={15} />, onSelect: () => navigate("/sources") });
    if (openGaps > 0)
      entries.push({ key: "gaps", label: `${openGaps} open gaps`, icon: <AlertTriangle size={15} />, onSelect: () => navigate("/sources") });
    if (pending > 0)
      entries.push({ key: "pending", label: `${pending} pending approvals`, icon: <Gauge size={15} />, onSelect: () => navigate("/ledger") });
    entries.push({ separator: true });
  }
  entries.push({ header: "Display" });
  entries.push({ key: "veil", label: "Privacy veil", icon: veil ? <EyeOff size={15} /> : <Eye size={15} />, checked: veil, onSelect: onVeil });
  entries.push({ key: "density", label: "Compact density", icon: <Rows3 size={15} />, checked: density === "workbench", onSelect: onDensity });
  entries.push({ separator: true });
  entries.push({ header: "Theme" });
  entries.push({ key: "system", label: "Auto (system)", icon: <Monitor size={15} />, checked: theme === "system", onSelect: () => onTheme("system") });
  entries.push({ key: "nocturne", label: "Nocturne", icon: <Moon size={15} />, checked: theme === "nocturne", onSelect: () => onTheme("nocturne") });
  entries.push({ key: "daybook", label: "Daybook", icon: <Sun size={15} />, checked: theme === "daybook", onSelect: () => onTheme("daybook") });
  return (
    <>
      <IconButton
        ref={ref}
        label="More options"
        active={hasHealth}
        onClick={() => setOpen((v) => !v)}
      >
        <MoreHorizontal />
      </IconButton>
      <Menu open={open} onClose={() => setOpen(false)} anchorRef={ref} align="end" aria-label="Options" entries={entries} />
    </>
  );
}

function TzChip(): ReactNode {
  const tz = usePrefs((s) => s.tz);
  const short = tz.split("/").pop()?.replace(/_/g, " ") ?? tz;
  return (
    <Tooltip content={`Timezone — ${tz}`} side="bottom">
      <span className={styles.tz}>{short}</span>
    </Tooltip>
  );
}

function AskerControl(): ReactNode {
  const { asker, setAsker } = useScopes();
  const ref = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(asker ?? "");
  return (
    <>
      <button ref={ref} type="button" className={styles.asker} onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <UserCog size={14} />
        <span>{asker ? `as ${asker}` : "as owner"}</span>
        <ChevronDown size={13} className={styles.chevron} />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} anchorRef={ref} align="start" aria-label="Act as asker">
        <div className={styles.askerMenu}>
          <p className={styles.askerNote}>
            Preview what a registered asker can read. Identity-backed panels become empty and app-role
            routes refuse.
          </p>
          <Field label="Asker name" htmlFor="asker-name">
            <Input
              id="asker-name"
              mono
              placeholder="asker name"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  setAsker(draft.trim() || null);
                  setOpen(false);
                }
              }}
            />
          </Field>
          <div className={styles.askerActions}>
            <Button size="sm" onClick={() => { setAsker(null); setDraft(""); setOpen(false); }}>
              Act as owner
            </Button>
            <Button size="sm" variant="primary" onClick={() => { setAsker(draft.trim() || null); setOpen(false); }}>
              Act as asker
            </Button>
          </div>
        </div>
      </Popover>
    </>
  );
}

function HealthChip({
  tone,
  icon,
  count,
  label,
  onClick,
}: {
  tone: "warn" | "bad";
  icon: ReactNode;
  count: number;
  label: string;
  onClick: () => void;
}): ReactNode {
  return (
    <Tooltip content={label} side="bottom">
      <button type="button" className={`${styles.healthChip} ${styles[tone]}`} onClick={onClick} aria-label={`${count} ${label}`}>
        {icon}
        <span className="tnum">{count}</span>
      </button>
    </Tooltip>
  );
}
