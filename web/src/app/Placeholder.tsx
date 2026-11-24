import type { ReactNode } from "react";
import { Hammer } from "lucide-react";
import { NAV } from "./nav";
import { Badge, EmptyState } from "@/ui";
import styles from "./Placeholder.module.css";

const BLURB: Record<string, string> = {
  loom: "The whole archive at a glance: one lane per channel across the full time range, with Today, Moving now, Needs attention and the Ask box.",
  sources: "Per-plugin health and glyph, freshness against each source's own rhythm, coverage bars with gaps cut out, and outage history.",
  live: "Arrivals as they land by archetype, a rate strip per plugin, the settle line, folded backfills, and a Reads lane of what tokens are asking.",
  query: "A CodeMirror editor with catalogue completion and lint, ranked chart lenses, the helper agent, recipes and saved queries.",
  people: "The constellation and the person read model: arc, circle, rhythm, last contact and open loops.",
  day: "Everything in one local day, drawn as swimlanes.",
  places: "The track, visits and places — opt-in street tiles stay off until asked.",
  graph: "Entities, links, pages and neighbourhoods.",
  ledger: "Proposals, approvals, spend and the audit trail.",
};

/**
 * A designed placeholder for a screen another engineer owns. Thin by intent:
 * feature files render only this until the real screen replaces them.
 */
export function Placeholder({ navId, children }: { navId: string; children?: ReactNode }): ReactNode {
  const item = NAV.find((n) => n.id === navId);
  const Icon = item?.icon ?? Hammer;
  return (
    <div className={styles.wrap}>
      <EmptyState
        icon={<Icon />}
        title={item?.label ?? navId}
        description={BLURB[navId] ?? "This screen is being built."}
        action={
          <Badge tone="neutral">
            <Hammer size={12} /> under construction
          </Badge>
        }
      />
      {children}
    </div>
  );
}
