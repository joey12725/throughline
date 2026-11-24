/*
 * Add a source — three doors. Drop a file (parsed, mapped and previewed in the
 * browser, then validated and ingested through the real contract), connect a
 * plugin (a checklist plus a live row that turns green), or push over HTTP
 * (a channel builder with real snippets and a dry-run tester). Nothing here
 * names a source; everything derives from the contract and what you type.
 */
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, FileUp, Plug, Code2 } from "lucide-react";
import { Page, PageHeader } from "@/ui";
import { FileDoor } from "./FileDoor";
import { PluginDoor } from "./PluginDoor";
import { HttpDoor } from "./HttpDoor";
import styles from "./Add.module.css";

type Door = "file" | "plugin" | "http";

const DOORS: Array<{ id: Door; label: string; icon: typeof FileUp; blurb: string }> = [
  { id: "file", label: "Drop a file", icon: FileUp, blurb: "CSV, TSV, JSON or NDJSON — mapped and previewed here, then imported." },
  { id: "plugin", label: "Connect a plugin", icon: Plug, blurb: "Run a collector on the box; watch it turn green as it lands." },
  { id: "http", label: "Push over HTTP", icon: Code2, blurb: "Build a channel and get curl, Python and Node you can paste." },
];

export default function AddSourceScreen(): ReactNode {
  const [door, setDoor] = useState<Door>("file");

  return (
    <Page width="wide">
      <PageHeader
        eyebrow={
          <Link to="/sources" className={styles.backLink}>
            <ArrowLeft size={15} aria-hidden /> Sources
          </Link>
        }
        title="Add a source"
        lede="A source never has to draw its own data. Declare when a record happened, its text and an id — the lens engine builds the channel page for you."
      />

      <div className={styles.doors} role="tablist" aria-label="Ways to add a source">
        {DOORS.map((d) => {
          const Icon = d.icon;
          const active = door === d.id;
          return (
            <button
              key={d.id}
              role="tab"
              aria-selected={active}
              className={styles.door}
              data-active={active}
              onClick={() => setDoor(d.id)}
            >
              <Icon size={20} aria-hidden className={styles.doorIcon} />
              <span className={styles.doorLabel}>{d.label}</span>
              <span className={styles.doorBlurb}>{d.blurb}</span>
            </button>
          );
        })}
      </div>

      <div className={styles.doorBody}>
        {door === "file" ? <FileDoor /> : door === "plugin" ? <PluginDoor /> : <HttpDoor />}
      </div>
    </Page>
  );
}
