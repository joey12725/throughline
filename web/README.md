# Throughline web front end

An observatory for a Throughline installation: see every source at a glance, tell
whether it is healthy, watch records arrive, answer a question with SQL and a
helper agent, and enjoy a large, deeply personal archive as something beautiful.

This package is the **app shell and design system**. Feature screens
(`src/features/*`), the lens engine (`src/lens`) and charts (`src/charts`) are
built on top of it by other engineers.

The design contract is `docs/web/design.md`. Read it first; it governs tokens,
naming, the two themes, the source-colour rules and the structure.

## Run

```sh
pnpm install
pnpm dev            # Vite on :5173, proxies /v1 to the API (THROUGHLINE_URL, default http://127.0.0.1:8801)
```

Open `http://127.0.0.1:5173/ui/`. You need an access token, minted on the box:

```sh
throughline token create --kind owner --name web
```

Paste it on the token screen, or open `…/ui/#token=<token>` (the fragment is
scrubbed from history on read). The app is served under `/ui/`; the production
build is emitted to `src/throughline/web/static` (never `dist`/`build`, which the
wheel drops).

## Build & test

```sh
pnpm typecheck      # tsc --noEmit
pnpm build          # tsc + vite build -> src/throughline/web/static
pnpm test           # vitest unit tests (pure modules)
pnpm e2e            # Playwright: token screen, shell, every rail item, no-egress
```

`pnpm e2e` drives the **system Chromium** (`/usr/bin/chromium`, override with
`CHROMIUM_PATH`). Authenticated specs skip unless `THROUGHLINE_TOKEN` is set.

## What's here

| Area | Path | What it is |
|---|---|---|
| Design tokens | `src/ui/tokens.css` | Both themes, type scale, spacing, radii, motion, grain, focus ring |
| Primitives | `src/ui/*` | Button, Chip, Badge/StatusGlyph, Tabs/Segmented, Tooltip, Popover, Menu, Dialog, Drawer, Toast, Skeleton, EmptyState, ErrorState, Stat, KeyValue, Kbd, Field/Input/Select/Switch, ScrollArea, Split, CodeBlock, RelativeTime, Sensitive, SourceDot/SourceChip, Panel |
| Source colour | `src/ui/sourceColor.ts`, `SourceColorContext.tsx` | Plugin→hue assignment, channel lightness steps, chart ramps, canvas rgb |
| API | `src/api/*` | Fetch client, SQL + scheduler, bootstrap (degrades to classic routes), UI state, arrivals, TanStack Query, hooks |
| Stores | `src/store/*` | `auth`, `prefs` (theme/density/veil/tz), `scopes` (`:from`/`:to`/`:asker`) |
| Shell | `src/app/*` | Rail, header, command palette, drawers, routing, error boundaries, token screen |
| Real screens | `src/features/settings`, `src/app/Styleguide.tsx` | Settings and the living style guide at `/ui/styleguide` |

All styling is CSS Modules or plain CSS next to the component — no CSS-in-JS
runtime, no inline `<script>`. Components theme **only** through tokens.

## Hard rules (enforced by review and tests)

- **No third-party network requests.** Fonts are bundled (`@fontsource-variable/*`),
  there are no CDN links, tiles or analytics. `e2e/no-egress.spec.ts` fails if any
  request leaves the app origin.
- **Record text is text, never HTML.** Render it as a string; never
  `dangerouslySetInnerHTML`. URLs found in data are inert until clicked and carry
  `rel="noreferrer noopener"`.
- **Derive from shape, never hard-code a channel or plugin name** outside test
  fixtures. Read them from `useBootstrap()`.
- Every chart ships both themes; honour `prefers-reduced-motion`.

## Conventions for feature authors

Your screen is one file: `src/features/<name>/index.tsx`, a default export. The
shell lazy-loads it, wraps it in an error boundary and a `<Suspense>`, and sets
the document title. Keep data access in hooks from `@/api`.

### Add a route / rail item

Edit `src/app/nav.tsx` (one `NavItem`: `path`, `label`, `title`, `icon`,
`inRail`) and add the lazy route in `src/app/router.tsx`. The rail, command
palette and active-item logic all read `nav.tsx`, so nothing else changes.

### Fetch data

```tsx
import { useBootstrap, useSql, useUiState } from "@/api";

const { data: boot } = useBootstrap();                  // token, features, totals, plugins, channels
const q = useSql<{ channel: string; n: string }>(       // scheduled, typed, abortable
  "SELECT channel, count(*)::text AS n FROM api.messages GROUP BY 1",
  { priority: "visible", keyParts: [scope.from, scope.to] },
);
const tab = useUiState<"table" | "chart">("board", "results-tab", "table"); // server state, localStorage fallback
```

Numeric columns arrive as strings (Postgres `numeric`); use `num(cell)` from
`@/api` when you want a JS number. `ErrorState` shows the server's detail and
SQLSTATE — pass it `query.error`.

### Read the global scopes

```tsx
import { useScopes, formatInTz } from "@/store/scopes";
import { usePrefs } from "@/store/prefs";

const { from, to, setRange } = useScopes();   // :from / :to; brushing a chart calls setRange
const tz = usePrefs((s) => s.tz);             // :tz
formatInTz(record.happened_at, tz);           // timezone-aware formatting
```

Bind `:from`, `:to`, `:tz`, `:person`, `:channel` and panel-local params in your
view spec; the lens compiler substitutes them.

### Compose a panel

```tsx
import { Panel, Sensitive } from "@/ui";

<Panel title="Top channels" actions={<Segmented .../>} footer="showing 200 of 1,460">
  <Sensitive>{record.content}</Sensitive>   {/* blurred under the privacy veil */}
</Panel>
```

### Add a command

```tsx
import { registerCommand } from "@/app/commands";

useEffect(() => registerCommand({
  id: "query.save", group: "Query", label: "Save this query",
  perform: ({ navigate }) => navigate("/query?save=1"),
}), []);
```

### Open a drawer (URL-addressable, stackable)

```tsx
import { registerDrawer, useDrawers } from "@/ui";

registerDrawer("record", ({ params, close }) => <RecordView id={params.id} onClose={close} />);
useDrawers.getState().open("record", { id: "m:1549" });   // reflected in ?d=…
```

### Use the source colour

```tsx
import { SourceDot, SourceChip, useSourceColor } from "@/ui";

<SourceDot plugin={ch.plugin} channelIndex={i} />
const { rgbFor } = useSourceColor();            // rgb() string for <canvas>
```

Hue belongs to the plugin, lightness to the channel; both themes are handled for
you. Reassign a hue only through Settings.
