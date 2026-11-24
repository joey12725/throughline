import { useEffect, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { useScopes } from "@/store/scopes";
import { useDrawers, encodeStack, decodeStack } from "@/ui";

/**
 * Keeps the URL and the global stores in step so every view is a link: the time
 * scope (:from/:to), the acting asker, and the open drawer stack all live in the
 * search params. Convergent: writes only when a value actually differs.
 */
export function UrlSync(): null {
  const [params, setParams] = useSearchParams();
  const scopes = useScopes();
  const drawers = useDrawers();
  const hydrated = useRef(false);

  // URL -> stores (initial load and back/forward).
  useEffect(() => {
    const from = params.get("from");
    const to = params.get("to");
    const asker = params.get("asker");
    const d = params.get("d");

    if (from !== scopes.from || to !== scopes.to) scopes.setRange(from, to);
    if ((asker ?? null) !== scopes.asker) scopes.setAsker(asker ?? null);

    const next = decodeStack(d);
    if (encodeStack(next) !== encodeStack(drawers.stack)) drawers.setStack(next);

    hydrated.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  // stores -> URL. Read fresh state (not the render closure): on the first commit
  // the URL->store effect above has already applied setStack synchronously, so the
  // closure's `drawers.stack` is stale ([]) while getState() is the hydrated stack.
  // Using the closure here would write d="" and clobber a deep-linked drawer.
  useEffect(() => {
    if (!hydrated.current) return;
    const next = new URLSearchParams(params);
    const sc = useScopes.getState();
    setOrDelete(next, "from", sc.from);
    setOrDelete(next, "to", sc.to);
    setOrDelete(next, "asker", sc.asker);
    const d = encodeStack(useDrawers.getState().stack);
    setOrDelete(next, "d", d || null);
    if (next.toString() !== params.toString()) setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopes.from, scopes.to, scopes.asker, drawers.stack]);

  return null;
}

function setOrDelete(p: URLSearchParams, key: string, value: string | null): void {
  if (value === null || value === "") p.delete(key);
  else p.set(key, value);
}
