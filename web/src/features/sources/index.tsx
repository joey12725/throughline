/*
 * Sources: the health board (/ui/sources) and one source in full
 * (/ui/sources/:plugin). The same lazy chunk serves both — the route parameter
 * decides which. "Tell the health of every source at a glance; open one to see
 * why and how to fix it." Nothing here names a source.
 */
import type { ReactNode } from "react";
import { useParams } from "react-router-dom";
import { Board } from "./Board";
import { PluginDetail } from "./PluginDetail";

export default function SourcesScreen(): ReactNode {
  const { plugin } = useParams();
  if (plugin) return <PluginDetail plugin={plugin} />;
  return <Board />;
}
