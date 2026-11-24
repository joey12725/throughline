/*
 * The record drawer feature. Importing this module registers the URL-addressable
 * "record" drawer (so ?d=record:id=…&channel=… reopens it) and exposes
 * useOpenRecord(), the one hook any screen uses to open a record over itself. The
 * drawer body is a lazy chunk, so react-markdown and the overlays load only when
 * a record is first opened.
 */
import { lazy, Suspense, useCallback, type ReactNode } from "react";
import { registerDrawer, useDrawers, Skeleton, type DrawerRenderProps } from "@/ui";

const LazyRecordDrawer = lazy(() => import("./RecordDrawer").then((m) => ({ default: m.RecordDrawer })));

function RecordDrawerHost(props: DrawerRenderProps): ReactNode {
  return (
    <Suspense fallback={<div style={{ padding: 16 }}><Skeleton height={60} lines={6} /></div>}>
      <LazyRecordDrawer params={props.params} close={props.close} />
    </Suspense>
  );
}

registerDrawer("record", RecordDrawerHost);

/** Open a record in the shared right-hand drawer (reflected in the URL's `d`). */
export function useOpenRecord(): (id: string | number, channel?: string) => void {
  return useCallback((id, channel) => {
    useDrawers.getState().open("record", { id: String(id), channel: channel ?? "" });
  }, []);
}
