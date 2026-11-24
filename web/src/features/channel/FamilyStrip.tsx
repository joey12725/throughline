/*
 * When several channels share a field signature they form a family. The strip
 * links the siblings and names the shared fields; the family's small-multiples
 * comparison rides in the lens grid (the engine builds it from the siblings).
 */
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Boxes } from "lucide-react";
import type { ChannelRow } from "@/api/types";
import { SourceChip } from "@/ui";
import styles from "./FamilyStrip.module.css";

export interface FamilyStripProps {
  current: string;
  siblings: ChannelRow[];
  sharedFields: readonly string[];
  indexOf: (channel: string) => number;
}

export function FamilyStrip({ current, siblings, sharedFields, indexOf }: FamilyStripProps): ReactNode {
  const navigate = useNavigate();
  const others = siblings.filter((s) => s.name !== current);
  if (others.length === 0) return null;
  return (
    <div className={styles.strip}>
      <div className={styles.head}>
        <Boxes size={14} />
        <span className={styles.title}>Channel family</span>
        <span className={styles.note}>
          {others.length + 1} channels share {sharedFields.length} field{sharedFields.length === 1 ? "" : "s"}
          {sharedFields.length ? `: ${sharedFields.slice(0, 5).join(", ")}${sharedFields.length > 5 ? "…" : ""}` : ""}
        </span>
      </div>
      <div className={styles.siblings}>
        {others.map((s) => (
          <SourceChip key={s.name} channel={s.name} plugin={s.plugin} channelIndex={indexOf(s.name)} onClick={() => navigate(`/c/${encodeURIComponent(s.name)}`)} />
        ))}
      </div>
    </div>
  );
}
