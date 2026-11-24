/*
 * The schema tree: every channel grouped by its plugin, with a source colour dot and
 * a row count, expandable to its declared fields (type + description). A click
 * inserts the identifier into the editor — the view for a channel, the field name for
 * a field. A search box filters channels and fields together. Nothing is hard-coded;
 * plugins and channels come from the bootstrap.
 */
import { useMemo, useState, type ReactNode } from "react";
import { ChevronRight, Search } from "lucide-react";
import { Input, SourceDot } from "@/ui";
import { useBootstrap } from "@/api";
import type { ChannelRow } from "@/api/types";
import styles from "../query.module.css";

export interface SchemaTabProps {
  onInsert: (text: string) => void;
}

interface Group {
  plugin: string;
  channels: { row: ChannelRow; index: number }[];
}

export function SchemaTab({ onInsert }: SchemaTabProps): ReactNode {
  const { data: boot } = useBootstrap();
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Set<string>>(() => new Set());

  const needle = q.trim().toLowerCase();

  const groups: Group[] = useMemo(() => {
    const channels = boot?.channels ?? [];
    const byPlugin = new Map<string, { row: ChannelRow; index: number }[]>();
    channels.forEach((row, index) => {
      const matchChannel = !needle || row.name.toLowerCase().includes(needle) || (row.description ?? "").toLowerCase().includes(needle);
      const matchField = !needle ? false : (row.fields ?? []).some((f) => f.name.toLowerCase().includes(needle) || (f.description ?? "").toLowerCase().includes(needle));
      if (needle && !matchChannel && !matchField) return;
      const list = byPlugin.get(row.plugin) ?? [];
      list.push({ row, index });
      byPlugin.set(row.plugin, list);
    });
    return [...byPlugin.entries()]
      .map(([plugin, cs]) => ({ plugin, channels: cs }))
      .sort((a, b) => a.plugin.localeCompare(b.plugin));
  }, [boot, needle]);

  const toggle = (name: string): void =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  return (
    <div className={styles.railPane}>
      <div className={styles.railSearch}>
        <Input lead={<Search size={14} />} placeholder="Search channels & fields" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search schema" />
      </div>
      <div className={styles.railScroll}>
        {groups.length === 0 ? (
          <div className={styles.railEmpty}>No channel matches “{q}”.</div>
        ) : (
          groups.map((g) => (
            <div key={g.plugin} className={styles.railGroup}>
              <div className={styles.railGroupHead}>{g.plugin}</div>
              {g.channels.map(({ row, index }) => {
                const expanded = open.has(row.name) || (needle.length > 0 && (row.fields ?? []).some((f) => f.name.toLowerCase().includes(needle)));
                return (
                  <div key={row.name} className={styles.channelItem}>
                    <div className={styles.channelRow}>
                      <button type="button" className={styles.channelToggle} onClick={() => toggle(row.name)} aria-expanded={expanded} aria-label={`Fields of ${row.name}`}>
                        <ChevronRight size={13} className={expanded ? styles.rotOpen : undefined} aria-hidden />
                      </button>
                      <SourceDot plugin={row.plugin} channelIndex={index} />
                      <button type="button" className={styles.channelName} onClick={() => onInsert(row.view)} title={`Insert ${row.view}`}>
                        {row.name}
                      </button>
                      <span className={`tnum ${styles.channelCount}`}>{Number(row.rows).toLocaleString("en-US")}</span>
                    </div>
                    {expanded && (
                      <ul className={styles.fieldList}>
                        {(row.fields ?? []).map((f) => (
                          <li key={f.name}>
                            <button type="button" className={styles.fieldBtn} onClick={() => onInsert(f.name)} title={f.description ?? undefined}>
                              <span className={styles.fieldName}>{f.name}</span>
                              <span className={styles.fieldType}>{f.type}</span>
                            </button>
                            {f.description ? <p className={styles.fieldDesc}>{f.description}</p> : null}
                          </li>
                        ))}
                        {(row.fields ?? []).length === 0 && <li className={styles.fieldDesc}>No declared fields — envelope columns only.</li>}
                      </ul>
                    )}
                  </div>
                );
              })}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
