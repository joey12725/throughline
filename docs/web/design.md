# Web front end: design

The web interface is an observatory for a Throughline installation. Agents and
the CLI stay the primary entry points; the interface exists to *look*: to see
every source at a glance, tell whether it is healthy, watch records arrive,
answer a question with SQL and a helper agent, and enjoy a large, deeply
personal archive as something beautiful.

This file is the contract for everyone building it. Read it together with
`docs/architecture.md`. Where this file and the code disagree, fix one of them.

## Principles

1. **Sources owe the interface nothing.** A plugin declares typed fields and
   pushes records, exactly as today. Every picture is derived from shape: the
   declared fields (name, type, description), the channel flags, the message
   envelope and observed statistics. No plugin code, no view hints in the
   manifest, no per-source components in the front end. A test fails if the
   front end names a real channel or plugin outside fixtures.
2. **Every panel is a query.** A panel is a *view spec*: one read-only
   `SELECT` against schema `api` plus a lens and its column bindings. Every
   panel can show its SQL and open it in the editor. The helper agent emits the
   same spec the engine does, so there is no hidden answer path.
3. **The database aggregates; the browser draws.** No chart fetches raw rows.
   Raw rows appear only in readers, by keyset pagination, virtualised.
4. **Private by construction.** The browser talks to its own origin and to
   nothing else: fonts, scripts and map data are bundled; URLs and images found
   in data are never fetched automatically. Model calls happen in the API
   process behind the existing egress allow-list and cost gate.
5. **Honest about what it knows.** Truncation, sampling, inferred roles,
   coverage gaps, "not found in covered sources" and unsettled arrivals are
   drawn, not hidden. An inference shows its evidence and can be overridden by
   the owner.
6. **It degrades, it does not break.** No model configured, no embeddings, a
   restricted token, a channel with zero rows, a database behind on
   migrations: each has a designed state.

## Identity

Two themes, one system. **Nocturne** (dark, default when the system prefers
dark) is an instrument room at night: near-black with a warm cast, warm
paper-white ink, hairline rules. **Daybook** (light) is ink on warm paper. The
only accent is **brass**: focus, the active item, the time brush, the "now"
rule. It is the throughline. Chrome stays almost monochrome so that source
colours carry the wayfinding.

| Token | Nocturne | Daybook |
|---|---|---|
| `--bg-0` canvas | `#0A0C0F` | `#F5F1E7` |
| `--bg-1` panel | `#10131A` | `#FBF8F1` |
| `--bg-2` raised | `#171B24` | `#FFFFFF` |
| `--bg-3` hover | `#1F2430` | `#ECE6D7` |
| `--line` | `#2A3040` | `#E1D9C8` |
| `--line-strong` | `#3A4254` | `#CFC5B0` |
| `--ink` | `#ECE6D8` | `#1C1A16` |
| `--ink-2` | `#A6A392` | `#57534A` |
| `--ink-3` | `#6B6E72` | `#8A857A` |
| `--brass` | `#D9A64A` | `#A9772A` |
| `--patina` (links) | `#58B9A6` | `#2E8576` |
| `--ok` / `--warn` / `--bad` | `#6FBF8E` `#E0A93C` `#E0603C` | `#2E7D5B` `#A86A00` `#B3261E` |

**Source colour.** Hue belongs to the plugin, lightness to the channel inside
it. The front end assigns each plugin the next free hue from a fixed order
(`25, 150, 255, 45, 190, 330, 85, 215, 300, 12, 170, 120` degrees) the first
time it sees it and stores the assignment in UI state, so a new plugin never
recolours the others. Swatch `oklch(0.74 0.13 H)` on dark and
`oklch(0.52 0.15 H)` on light; channel *k* of a plugin shifts lightness by
`±0.055·⌈k/2⌉`. Status never borrows a source hue and always carries a glyph
as well as a colour. Values inside one channel use a separate categorical
ramp; ordered measures one sequential ramp through the panel's source hue;
signed measures one diverging ramp.

**Type**, all bundled: Instrument Sans (interface), Newsreader (long text
only: pages, e-mail, transcript turns), JetBrains Mono (SQL, ids, every
number; tabular figures). Scale 12 / 13 / 15 / 18 / 22 / 28 / 38.

**Shape and motion.** 4 px grid. Radii 10 (panels), 6 (controls). Depth is one
background step plus a hairline; no drop shadows. Charts have no gridlines by
default, hairline axes and direct labels. Motion explains causality only
(160 / 240 / 420 ms, `cubic-bezier(.2,.8,.2,1)`); `prefers-reduced-motion`
reduces everything to a short cross-fade and stops particle layers.

**Time grammar**, used by every time axis: a solid brass rule at "now"; the
future to its right dimmed and dashed; periods a source does not claim to
cover hatched; imprecise times (day, month, approximate) drawn soft-edged.

**Privacy veil.** One switch blurs record text, names and places everywhere
(charts and counts stay sharp) for use in front of other people.

## Structure

A slim left rail, a header and a command palette (Ctrl/Cmd-K: any channel,
person, place, recipe, saved query, or a question for the helper).

The header carries the **global scopes** every panel reads as parameters: the
time scope (`:from`, `:to`, set by brushing any time axis), the timezone
(`:tz`) and, for an owner token, the asker to act as. The URL holds screen,
scopes and panel state, so every view is a link.

| Route (`/ui/…`) | Screen | What it is for |
|---|---|---|
| `/` | **Loom** | The whole archive at a glance |
| `/sources`, `/sources/:plugin` | **Sources** | Health, coverage, outages |
| `/sources/add` | **Add a source** | File import, plugin checklist, HTTP push |
| `/c/:channel` | **Channel** | One channel, automatically visualised |
| `/live` | **Live** | Arrivals as they land, and what is being asked |
| `/query` | **Query** | Editor, results, charts, helper agent, recipes, saved |
| `/people`, `/people/:id` | **People** | Constellation and the person read model |
| `/day/:date` | **Day** | Everything in one local day |
| `/places` | **Places** | Track, visits, places |
| `/graph` | **Graph** | Entities, links, pages, neighbourhoods |
| `/ledger` | **Ledger** | Open loops, commitments, claims, proposals, approvals, spend, audit |
| `/settings` | **Settings** | Token, theme, veil, tiles, helper disclosure |

A record opens in a right-hand **drawer** over any screen: content rendered by
archetype, fields with roles, links, provenance, versions, raw payload.

### The Loom (first screen)

- One editorial sentence of vitals, each number a link to its query.
- **The Loom**: one lane per channel, grouped and coloured by plugin, across
  the installation's whole time range; ink density is records per week on a
  log scale. Brushing sets the global time scope; clicking opens the channel
  at that week. A toggle redraws the same data as a stacked **river**. Canvas,
  with an SVG overlay for labels, the now-rule and the brush.
- Below: **Today** (the Day swimlanes for the current day), **Moving now**
  (latest arrivals as archetype cards), **Needs attention** (stale or failing
  plugins, partial coverage, channels quiet for much longer than their own
  rhythm, pending approvals) and **Ask** (the helper box with profile-derived
  starters).

## The lens engine

A pure TypeScript module (`web/src/lens`, no DOM, no React) with JSON in and
JSON out at every stage:

`profile → roles → archetypes → ranked lenses → view spec → SQL`

It runs for a channel (profile gathered by SQL), for any SQL result (profile
computed from the returned rows in a worker) and for an answer from the
helper.

**Profile.** For a channel: envelope statistics, per-field presence, per-type
value statistics, cadence, thread sizes and a spread sample of rows. Above
50,000 rows value statistics run on an `id % k` sample and the profile says
"sampled"; counts stay exact. Cached in IndexedDB by channel, row count and
last ingest.

**Roles.** Each field gets one role with a confidence and its evidence, from
declared type, name tokens, description text and observed values:
`time.start/end`, `time.offset`, `time.bookkeeping`, `time.deadline`,
`date.text`, `duration`, `distance`, `mass`, `percent`, `probability`,
`score`, `count`, `currency`, `geo.lat/lon`, `geo.label`, `identifier`,
`group_key`, `bookkeeping`, `category`, `category.speaker`, `category.status`,
`title`, `body`, `url`, `actor.label`, `flag`, `flag.tombstone`, `json.tags`,
`json.items`, `json.object`, `measure`. Identifier and bookkeeping detection
runs before measure detection (a revision counter is never charted).
Descriptions are mined for units and category domains, which is what lets a
channel with no rows render. Fields sharing a prefix form a family; same-unit
siblings are tested for part-of-whole.

**Archetypes** are scored, not exclusive: conversation, transcript,
measurement, interval, geo track, document, feed item, gauge, snapshot,
registry. Channels with the same field signature form a family and get small
multiples.

**Lenses** declare the roles they need, a fitness function and a builder.
Score = affinity × fitness × information × (1 − cost), then diversified so the
top of the list is not six time series. `spine` and `table` always apply.

| Group | Lenses |
|---|---|
| Time | `spine`, `timeseries`, `small_multiples`, `calendar_heat`, `weekday_hour`, `interval_clock`, `gantt`, `sawtooth` |
| Distribution | `histogram`, `top_values`, `composition`, `scatter`, `strip` |
| Structure | `thread_tapestry`, `speaker_share`, `reply_latency`, `status_matrix`, `tags`, `json_tree` |
| Space | `track_map`, `pin_map`, `dwell_rank` |
| Relation | `ego_graph`, `link_matrix` |
| Reading | `reader` (bubbles, transcript, document, feed, cards by archetype), `table`, `stat` |

**View spec** (`throughline.view/1`): deliberately narrower than a chart
grammar. It names a lens and binds columns to that lens's channels; marks,
palette and axes belong to the renderer, so every panel looks like one system
and a model can emit it reliably.

```json
{
  "v": 1,
  "title": "Bedtime to wake, per night",
  "why": "Every record has a start and an end; time of day is the pattern",
  "lens": "interval_clock",
  "source": {
    "sql": "SELECT happened_at, ended_at, sleep_type FROM api.ch_health_oura_sleep WHERE happened_at >= :from AND happened_at < :to ORDER BY 1",
    "row_cap": 2000
  },
  "encode": { "start": "happened_at", "end": "ended_at", "color": "sleep_type" },
  "provenance": { "by": "engine", "channel": "health.oura.sleep", "sampled": false }
}
```

Parameters (`:from`, `:to`, `:tz`, `:person`, `:channel`, and panel-local
ones) are substituted in the browser as typed, escaped literals; the server's
validator and the read-only reader role remain the safety boundary. The
compiler chooses the time grain so a result stays under about 1,500 buckets.
Every statement the compiler can produce is run against a test server in CI.

**Overrides.** The owner can correct a field's role or unit, hide a field,
pin or hide a lens. Overrides are UI state keyed by channel name. Plugins
never see them.

## Query and the helper

**Editor**: CodeMirror 6 with completion from the catalogue (views, typed
channel columns, declared fields with their descriptions, allow-listed
functions with signatures and examples), lint through `validate_only`, a
schema rail, recipes with their adaptation notes, history on the device,
saved queries on the server, share-by-URL and "copy as CLI".

**Results**: Table (virtualised, cells rendered by role), Chart (ranked
lenses for this result), Profile, Execution (the server's own envelope:
stages, timings, truncation, warnings, trace), JSON. A truncated result
carries a banner onto every chart drawn from it. A semantic statement that
hits the cost gate surfaces the existing approval flow.

**Helper**: a side panel whose product is always a `(statement, view spec)`
pair the owner can see, edit and save. Three verbs: Ask, Fix (on any error),
Explain/Refine (on a selection). It runs in the API process as a bounded tool
loop with read-only tools (`describe`, `profile`, `find_recipes`, `recipe`,
`run_sql`, `recall`, `chart`, `final`), each executed under the request's
scope through the unchanged validator and gate.

- **Disclosure tier**, shown beside the model name: `schema` (the model sees
  catalogue, field names, types, descriptions, errors and result shapes; rows
  stay in the browser) or `samples` (clipped previews). Non-loopback
  endpoints default to `schema`.
- **Transparency**: every step shows tool, SQL, row count, what was sent,
  tokens and cost.
- **No model**: the panel stays and says so. Ask becomes a finder over
  recipes, describe subjects, channels, fields, people and full text;
  starters come from the profile; Fix uses a rule table keyed on validator
  messages and SQLSTATE.

## Sources, adding one, and Live

**Health** reads what exists (`/v1/status`, coverage, gaps, and source-health
outages where that migration is present): per plugin a status word and glyph
(ok, late, stale, failing, paused, one-shot, never ran), age against interval,
last error, arrivals sparkline against the source's own cadence band,
coverage bar with gaps cut out, and outage history. A freshness matrix
(channels × last 60 days) separates holes from quiet days. *Inferred*
quietness is styled apart from what a source *reported*.

**Add a source**, three doors:
1. **Drop a file** (CSV, TSV, JSON, NDJSON): parsed in the browser, columns
   mapped to the envelope with inferred defaults, the real channel page
   previewed from the sample, validated by `dry_run`, then registered and
   ingested in idempotent batches through the existing contract. The mapping
   is saved so next month's export is one click.
2. **Connect a plugin**: a checklist with the exact commands (tokens are
   minted only on the box) and a live row that turns green on the first
   manifest, heartbeat and records.
3. **Push over HTTP**: channel builder, curl/Python/Node snippets, a sample
   tester.

**Live**: long-polled arrivals (`GET /v1/web/arrivals?after=…&wait=…` answers
as soon as something lands; plain request and response, so the "no push
routes" rule of the core stands). Rate strip per plugin, the
arrival list rendered by archetype, the settle line (arrivals consumers
cannot see yet are lighter), backfills folded into one card, and a *Reads*
lane showing what tokens are asking. Pause, filter, replay the last minutes.

## Server additions

All under `src/throughline/web/`, `api/routes/web.py`, `assistant/` and one
migration; see `docs/web/backend.md`.

1. Static shell at `/ui/` with a strict content security policy.
2. Pure, bounded builtins added to the SQL allow-list (binning, quantiles,
   window offsets, JSON inspection).
3. `POST /v1/sql`: `columns` in the response and `validate_only`.
4. `tl.web_state` (saved queries, boards, overrides, colour assignment,
   import mappings) and its routes.
5. `GET /v1/web/bootstrap`, `GET /v1/web/arrivals` (long poll),
   `POST /v1/web/assistant` and `GET /v1/web/assistant/{run}` (a run and its
   long-polled events).
6. An `assistant` model kind (Anthropic, any OpenAI-compatible endpoint, or
   the deterministic local helper).

## Deliberately not here (yet)

- Token minting, purge and asker administration stay on the box.
- Identity merges, claims and annotations are read-only in the interface;
  approvals and proposals can be decided.
- The embedding atlas (a 2-D map of every vector) needs a projection job and
  is specified separately.
- Street-level map tiles are an explicit, named opt-in.
