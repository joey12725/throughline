import { useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { Command } from "cmdk";
import { useQuery } from "@tanstack/react-query";
import { CornerDownLeft } from "lucide-react";
import clsx from "clsx";
import { useBootstrap, sql } from "@/api";
import type { ChannelRow } from "@/api";
import { SourceDot } from "@/ui";
import { usePrefs } from "@/store/prefs";
import { NAV } from "./nav";
import { listCommands, subscribeCommands, type Command as Cmd } from "./commands";
import styles from "./CommandPalette.module.css";

interface PersonRow {
  id: number;
  display_name: string | null;
  handle: string | null;
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }): ReactNode {
  const navigate = useNavigate();
  const { data: boot } = useBootstrap();
  const prefs = usePrefs();

  const commands = useSyncExternalStore(subscribeCommands, listCommands, listCommands);

  // People: loaded only while the palette is open, so no idle audit rows.
  const people = useQuery({
    queryKey: ["palette", "people"],
    enabled: open,
    staleTime: 5 * 60_000,
    retry: false,
    queryFn: async () => {
      const r = await sql<PersonRow>(
        "SELECT id, display_name, handle FROM api.people WHERE status = 'active' ORDER BY display_name NULLS LAST LIMIT 200",
        { priority: "visible", rowCap: 200 },
      );
      return r.rows;
    },
  });

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const channelsByPlugin = useMemo(() => {
    const map = new Map<string, ChannelRow[]>();
    for (const ch of boot?.channels ?? []) {
      const arr = map.get(ch.plugin) ?? [];
      arr.push(ch);
      map.set(ch.plugin, arr);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [boot]);

  // Built-in display actions, alongside feature-registered commands.
  const builtins: Cmd[] = useMemo<Cmd[]>(
    () => [
      { id: "act.veil", group: "Actions", label: prefs.veil ? "Turn privacy veil off" : "Turn privacy veil on", keywords: "privacy blur hide", perform: () => prefs.set("veil", !prefs.veil) },
      { id: "act.theme", group: "Actions", label: "Cycle theme", keywords: "dark light nocturne daybook", perform: () => prefs.set("theme", prefs.theme === "nocturne" ? "daybook" : prefs.theme === "daybook" ? "system" : "nocturne") },
      { id: "act.density", group: "Actions", label: "Toggle density", keywords: "observatory workbench compact", perform: () => prefs.set("density", prefs.density === "observatory" ? "workbench" : "observatory") },
      { id: "act.style", group: "Actions", label: "Open the style guide", keywords: "design tokens primitives", perform: ({ navigate: n }) => n("/styleguide") },
    ],
    [prefs],
  );

  if (!open) return null;

  function run(perform: Cmd["perform"]): void {
    perform({ navigate, close: onClose });
    onClose();
  }

  return createPortal(
    <div className={styles.scrim} onClick={onClose}>
      <div className={styles.palette} onClick={(e) => e.stopPropagation()}>
        <Command label="Command palette" className={styles.cmd} loop>
          <Command.Input autoFocus placeholder="Jump to a channel, person, screen, or run an action…" className={styles.input} />
          <Command.List className={styles.list}>
            <Command.Empty className={styles.empty}>No matches.</Command.Empty>

            <Command.Group heading="Screens" className={styles.group}>
              {NAV.map((n) => {
                const Icon = n.icon;
                return (
                  <Command.Item
                    key={n.id}
                    value={`screen ${n.label} ${n.path}`}
                    onSelect={() => { navigate(n.path); onClose(); }}
                    className={styles.item}
                  >
                    <Icon size={16} className={styles.itemIcon} />
                    <span>{n.label}</span>
                    <span className={styles.path}>{n.path}</span>
                  </Command.Item>
                );
              })}
            </Command.Group>

            {channelsByPlugin.map(([plugin, chans]) => (
              <Command.Group key={plugin} heading={plugin} className={styles.group}>
                {chans.map((ch, i) => (
                  <Command.Item
                    key={ch.name}
                    value={`channel ${ch.name} ${plugin} ${ch.description ?? ""}`}
                    onSelect={() => { navigate(`/c/${encodeURIComponent(ch.name)}`); onClose(); }}
                    className={styles.item}
                  >
                    <SourceDot plugin={plugin} channelIndex={i} />
                    <span className={styles.mono}>{ch.name}</span>
                    <span className={clsx(styles.count, "tnum")}>{ch.rows.toLocaleString()}</span>
                  </Command.Item>
                ))}
              </Command.Group>
            ))}

            {(people.data?.length ?? 0) > 0 && (
              <Command.Group heading="People" className={styles.group}>
                {people.data!.map((p) => (
                  <Command.Item
                    key={p.id}
                    value={`person ${p.display_name ?? ""} ${p.handle ?? ""}`}
                    onSelect={() => { navigate(`/people/${p.id}`); onClose(); }}
                    className={styles.item}
                  >
                    <span className={styles.avatar} aria-hidden>{(p.display_name ?? p.handle ?? "?").slice(0, 1).toUpperCase()}</span>
                    <span>{p.display_name ?? p.handle}</span>
                    {p.handle && <span className={styles.path}>{p.handle}</span>}
                  </Command.Item>
                ))}
              </Command.Group>
            )}

            <Command.Group heading="Actions" className={styles.group}>
              {[...builtins, ...commands].map((c) => (
                <Command.Item
                  key={c.id}
                  value={`action ${c.label} ${c.keywords ?? ""}`}
                  onSelect={() => run(c.perform)}
                  className={styles.item}
                >
                  <span className={styles.itemIcon}>{c.icon}</span>
                  <span>{c.label}</span>
                  {c.hint && <span className={styles.path}>{c.hint}</span>}
                </Command.Item>
              ))}
            </Command.Group>
          </Command.List>
          <div className={styles.footer}>
            <span>
              <kbd>↑</kbd> <kbd>↓</kbd> to move
            </span>
            <span>
              <CornerDownLeft size={12} /> to select · <kbd>esc</kbd> to close
            </span>
          </div>
        </Command>
      </div>
    </div>,
    document.body,
  );
}
