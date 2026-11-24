import { type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useBootstrap } from "@/api";
import { useSetupState } from "@/api/setup";

/**
 * Wraps the home route. On a fresh install (no records) whose setup isn't marked
 * done, it redirects to the wizard — once, and never as a trap: a "Set up later"
 * link sets a session flag, and any error or uncertainty renders home as normal.
 */
export function SetupRedirectGate({ children }: { children: ReactNode }): ReactNode {
  const { data: boot } = useBootstrap();
  const state = useSetupState();

  let deferred = false;
  try {
    deferred = sessionStorage.getItem("tl.setup.deferred") === "1";
  } catch {
    /* storage blocked */
  }

  const fresh = boot != null && boot.totals.records === 0;
  const notDone = state.data != null && state.data.status !== "done";

  if (!deferred && fresh && notDone) {
    return <Navigate to="/welcome" replace />;
  }
  return <>{children}</>;
}
