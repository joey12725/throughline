import { useEffect, useMemo, useState, type ReactNode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";
import { createQueryClient, useBootstrap, setUnauthorizedHandler, listState, putState } from "@/api";
import { SourceColorProvider, Toaster, toast } from "@/ui";
import { getPublicSetup, type PublicSetup } from "@/api/setup";
import { AccountGate } from "@/features/onboarding/AccountGate";
import { router } from "./router";
import { SignInScreen } from "./SignInScreen";
import { RouteFallback } from "./RouteFallback";
import { useAuth } from "@/store/auth";
import { usePrefs, initPrefsDocument, type Prefs } from "@/store/prefs";
// Register URL-addressable drawers (record, …) eagerly so cold deep-links resolve.
import "./eagerDrawers";

const queryClient = createQueryClient();

export function App(): ReactNode {
  // Apply theme/density and watch OS theme for the whole session.
  useEffect(() => initPrefsDocument(), []);

  // Any 401 signs the browser out (AppInner then shows the sign-in screen).
  useEffect(() => {
    setUnauthorizedHandler(() => toast.warn("Session ended", "Sign in again to continue."));
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <AppInner />
      <Toaster />
    </QueryClientProvider>
  );
}

function AppInner(): ReactNode {
  const status = useAuth((s) => s.status);
  const checkSession = useAuth((s) => s.checkSession);
  // Without a stored token, ask once whether a session cookie is live.
  useEffect(() => {
    if (status === "checking") void checkSession();
    // Nothing fetched for one principal may be shown to the next.
    if (status === "signedOut") queryClient.clear();
  }, [status, checkSession]);
  if (status === "checking") return <RouteFallback />;
  if (status === "signedOut") return <SignedOut />;
  return <Authed />;
}

/**
 * Signed out: if no owner account exists yet, lead with the first-run account
 * creation (the wizard's account step) instead of the plain sign-in card.
 */
function SignedOut(): ReactNode {
  // A plain fetch, not a query: AppInner clears the query cache when the
  // status turns signedOut, which would orphan a query started here.
  const [publicSetup, setPublicSetup] = useState<PublicSetup | null | undefined>(undefined);
  useEffect(() => {
    let live = true;
    getPublicSetup()
      .then((value) => { if (live) setPublicSetup(value); })
      .catch(() => { if (live) setPublicSetup(null); });
    return () => { live = false; };
  }, []);
  if (publicSetup === undefined) return <RouteFallback />;
  if (publicSetup?.needs_account) return <AccountGate publicSetup={publicSetup} />;
  return <SignInScreen />;
}

function Authed(): ReactNode {
  const { data: boot, isLoading } = useBootstrap();
  const hydrate = usePrefs((s) => s.hydrate);

  const pluginOrder = useMemo(() => boot?.plugins.map((p) => p.name) ?? [], [boot]);

  // Merge server-side prefs once the installation is known, then mirror local
  // changes back to the server (debounced) so prefs follow the token, not the
  // browser. When web_state is false the localStorage fallback already holds.
  useEffect(() => {
    if (!boot?.features.web_state) return;
    let alive = true;
    // Read through the collection endpoint (200 with []) rather than the item
    // endpoint, so a first load with no saved prefs doc is an empty list, not a
    // logged 404.
    void listState<Partial<Prefs>>("prefs").then((entries) => {
      const value = entries.find((e) => e.key === "app")?.value;
      if (alive && value) hydrate(value);
    });
    let timer: number | undefined;
    const unsub = usePrefs.subscribe((state) => {
      window.clearTimeout(timer);
      const { theme, density, veil, mapTiles, disclosure, tz } = state;
      timer = window.setTimeout(() => {
        void putState("prefs", "app", { theme, density, veil, mapTiles, disclosure, tz });
      }, 400);
    });
    return () => {
      alive = false;
      window.clearTimeout(timer);
      unsub();
    };
  }, [boot?.features.web_state, hydrate]);

  if (isLoading && !boot) return <RouteFallback />;

  return (
    <SourceColorProvider pluginOrder={pluginOrder}>
      <RouterProvider router={router} />
    </SourceColorProvider>
  );
}
