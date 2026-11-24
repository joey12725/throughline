import { lazy, Suspense, type ComponentType, type ReactNode } from "react";
import { createBrowserRouter, Navigate } from "react-router-dom";
import { Shell } from "./Shell";
import { RouteError } from "./RouteError";
import { RouteFallback } from "./RouteFallback";
import { SetupRedirectGate } from "./SetupRedirectGate";

// Route-level code splitting: every feature is a lazy chunk.
const Loom = lazy(() => import("@/features/loom"));
const Sources = lazy(() => import("@/features/sources"));
const AddSource = lazy(() => import("@/features/sourcesAdd"));
const Channel = lazy(() => import("@/features/channel"));
const Live = lazy(() => import("@/features/live"));
const Query = lazy(() => import("@/features/query"));
const People = lazy(() => import("@/features/people"));
const Day = lazy(() => import("@/features/day"));
const Places = lazy(() => import("@/features/places"));
const Graph = lazy(() => import("@/features/graph"));
const Ledger = lazy(() => import("@/features/ledger"));
const Settings = lazy(() => import("@/features/settings"));
const Onboarding = lazy(() => import("@/features/onboarding"));
const Classifiers = lazy(() => import("@/features/classifiers"));
const Styleguide = lazy(() => import("./Styleguide"));
const ChartsGallery = lazy(() => import("@/charts/ChartsGallery"));

function page(Component: ComponentType): ReactNode {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Component />
    </Suspense>
  );
}

export const router = createBrowserRouter(
  [
    // The first-run wizard lives OUTSIDE the shell (no rail/header): a full-screen
    // experience. Owner-only; the router only mounts once signed in.
    { path: "/welcome", element: page(Onboarding), errorElement: <RouteError /> },
    { path: "/welcome/:step", element: page(Onboarding), errorElement: <RouteError /> },
    {
      path: "/",
      element: <Shell />,
      errorElement: <Shell error />,
      children: [
        { index: true, element: <SetupRedirectGate>{page(Loom)}</SetupRedirectGate>, errorElement: <RouteError /> },
        { path: "sources", element: page(Sources), errorElement: <RouteError /> },
        { path: "sources/add", element: page(AddSource), errorElement: <RouteError /> },
        { path: "sources/:plugin", element: page(Sources), errorElement: <RouteError /> },
        { path: "c/:channel", element: page(Channel), errorElement: <RouteError /> },
        { path: "live", element: page(Live), errorElement: <RouteError /> },
        { path: "query", element: page(Query), errorElement: <RouteError /> },
        { path: "people", element: page(People), errorElement: <RouteError /> },
        { path: "people/:id", element: page(People), errorElement: <RouteError /> },
        { path: "day", element: page(Day), errorElement: <RouteError /> },
        { path: "day/:date", element: page(Day), errorElement: <RouteError /> },
        { path: "places", element: page(Places), errorElement: <RouteError /> },
        { path: "graph", element: page(Graph), errorElement: <RouteError /> },
        { path: "ledger", element: page(Ledger), errorElement: <RouteError /> },
        { path: "classifiers", element: page(Classifiers), errorElement: <RouteError /> },
        { path: "classifiers/new", element: page(Classifiers), errorElement: <RouteError /> },
        { path: "classifiers/:name", element: page(Classifiers), errorElement: <RouteError /> },
        { path: "classifiers/:name/edit", element: page(Classifiers), errorElement: <RouteError /> },
        { path: "classifiers/:name/threads/:threadId", element: page(Classifiers), errorElement: <RouteError /> },
        { path: "settings", element: page(Settings), errorElement: <RouteError /> },
        { path: "styleguide", element: page(Styleguide), errorElement: <RouteError /> },
        { path: "styleguide/charts", element: page(ChartsGallery), errorElement: <RouteError /> },
        { path: "*", element: <Navigate to="/" replace /> },
      ],
    },
  ],
  { basename: "/ui" },
);
