/*
 * The left rail: a collapsible column with four tabs — Schema (the catalogue),
 * Recipes, Saved and History. Collapsed, it is a thin strip of icons that expands
 * on click, so the editor can take the full width when wanted.
 */
import { useState, type ReactNode } from "react";
import { Database, BookOpen, Bookmark, Clock, PanelLeftClose, PanelLeft } from "lucide-react";
import { IconButton, Tooltip } from "@/ui";
import type { ViewSpec } from "@/lens";
import { SchemaTab } from "./SchemaTab";
import { RecipesTab } from "./RecipesTab";
import { SavedTab } from "./SavedTab";
import { HistoryTab } from "./HistoryTab";
import styles from "../query.module.css";

type RailTab = "schema" | "recipes" | "saved" | "history";

export interface SchemaRailProps {
  collapsed: boolean;
  onToggleCollapse: () => void;
  onInsert: (text: string) => void;
  onLoad: (statement: string, spec?: ViewSpec | null) => void;
  onSaveCurrent: () => void;
  canSave: boolean;
  historyVersion: number;
}

const TABS: { id: RailTab; label: string; icon: ReactNode }[] = [
  { id: "schema", label: "Schema", icon: <Database size={16} /> },
  { id: "recipes", label: "Recipes", icon: <BookOpen size={16} /> },
  { id: "saved", label: "Saved", icon: <Bookmark size={16} /> },
  { id: "history", label: "History", icon: <Clock size={16} /> },
];

export function SchemaRail(props: SchemaRailProps): ReactNode {
  const [tab, setTab] = useState<RailTab>("schema");

  if (props.collapsed) {
    return (
      <div className={styles.railCollapsed}>
        <Tooltip content="Open catalogue">
          <IconButton size="sm" variant="ghost" label="Open catalogue" onClick={props.onToggleCollapse}>
            <PanelLeft size={16} />
          </IconButton>
        </Tooltip>
        {TABS.map((t) => (
          <Tooltip key={t.id} content={t.label}>
            <IconButton
              size="sm"
              variant="ghost"
              label={t.label}
              onClick={() => {
                setTab(t.id);
                props.onToggleCollapse();
              }}
            >
              {t.icon}
            </IconButton>
          </Tooltip>
        ))}
      </div>
    );
  }

  return (
    <div className={styles.rail}>
      <div className={styles.railTabs} role="tablist" aria-label="Catalogue">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`${styles.railTab} ${tab === t.id ? styles.railTabOn : ""}`}
            onClick={() => setTab(t.id)}
          >
            <span aria-hidden>{t.icon}</span>
            <span className={styles.railTabLabel}>{t.label}</span>
          </button>
        ))}
        <div className={styles.railTabSpacer} />
        <IconButton size="sm" variant="ghost" label="Collapse catalogue" onClick={props.onToggleCollapse}>
          <PanelLeftClose size={16} />
        </IconButton>
      </div>
      <div className={styles.railContent}>
        {tab === "schema" && <SchemaTab onInsert={props.onInsert} />}
        {tab === "recipes" && <RecipesTab onInsert={props.onInsert} />}
        {tab === "saved" && <SavedTab onLoad={props.onLoad} onSaveCurrent={props.onSaveCurrent} canSave={props.canSave} />}
        {tab === "history" && <HistoryTab onLoad={(s) => props.onLoad(s)} version={props.historyVersion} />}
      </div>
    </div>
  );
}
