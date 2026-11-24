import { useEffect, useState, type ReactNode } from "react";
import { Outlet, useLocation, useNavigate, Link } from "react-router-dom";
import { Info, Wand2, ArrowRight, X } from "lucide-react";
import { Rail } from "./Rail";
import { BottomNav } from "./BottomNav";
import { Header } from "./Header";
import { CommandPalette } from "./CommandPalette";
import { UrlSync } from "./UrlSync";
import { RouteError } from "./RouteError";
import { DrawerHost, ScrollArea, ErrorState } from "@/ui";
import { useBootstrap } from "@/api";
import { useSetupState } from "@/api/setup";
import { registerCommand } from "./commands";
import { titleFor } from "./nav";
import styles from "./Shell.module.css";

/**
 * The app shell: rail, header, the routed screen, command palette and the drawer
 * stack. `error` renders the root-level error boundary full-page.
 */
export function Shell({ error }: { error?: boolean }): ReactNode {
  const { pathname } = useLocation();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const { data: boot, isError, error: bootError, refetch } = useBootstrap();

  // Global ⌘K / Ctrl-K opens the palette.
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Document title tracks the screen.
  useEffect(() => {
    document.title = `${titleFor(pathname)} · Throughline`;
  }, [pathname]);

  // The command palette can re-open the first-run wizard any time.
  useEffect(
    () =>
      registerCommand({
        id: "setup.run",
        group: "Actions",
        label: "Run setup again",
        keywords: "onboarding welcome wizard first run",
        icon: <Wand2 size={16} />,
        perform: ({ navigate, close }) => {
          close();
          navigate("/welcome");
        },
      }),
    [],
  );

  return (
    <div className={styles.shell}>
      <UrlSync />
      <Rail />
      <Header onOpenPalette={() => setPaletteOpen(true)} />
      <main className={styles.main} id="main">
        <SetupBanner />
        {boot?.degraded && (
          <div className={styles.degraded} role="status">
            <Info size={14} />
            <span>
              Composed view — this server does not yet serve <code>/v1/web</code>. Health, totals and
              arrivals are built from the classic routes.
            </span>
          </div>
        )}
        <ScrollArea className={styles.scroll}>
          <div className={styles.content}>
            {error ? (
              <RouteError />
            ) : isError ? (
              <div className={styles.bootError}>
                <ErrorState error={bootError} title="Could not load the installation" onRetry={() => refetch()} />
              </div>
            ) : (
              <Outlet />
            )}
          </div>
        </ScrollArea>
      </main>

      <BottomNav />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
      <DrawerHost />
    </div>
  );
}

const BANNER_KEY = "tl.setup.banner.dismissed";

/** A slim, dismissible nudge to finish setup, shown until setup is complete. */
function SetupBanner(): ReactNode {
  const navigate = useNavigate();
  const state = useSetupState();
  const [dismissed, setDismissed] = useState(() => {
    try {
      return sessionStorage.getItem(BANNER_KEY) === "1";
    } catch {
      return false;
    }
  });

  if (dismissed) return null;
  if (!state.data || state.data.status === "done") return null;

  const dismiss = (): void => {
    try {
      sessionStorage.setItem(BANNER_KEY, "1");
    } catch {
      /* ignore */
    }
    setDismissed(true);
  };

  const isRestore = state.data.mode === "restore";

  return (
    <div className={styles.setupBanner} role="status">
      <Wand2 size={14} aria-hidden />
      <span>{isRestore ? "Restored install — continue setup or add more sources." : "Your hub isn't fully set up yet."}</span>
      <Link to="/welcome" className={styles.setupBannerLink}>
        {isRestore ? "Continue setup" : "Finish setting up"} <ArrowRight size={13} aria-hidden />
      </Link>
      <span className={styles.setupBannerSpacer} />
      <button type="button" className={styles.setupBannerClose} onClick={dismiss} aria-label="Dismiss">
        <X size={14} />
      </button>
    </div>
  );
}
