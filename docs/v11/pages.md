# Pages: a graph the Librarian writes on top of the evidence

Package `throughline.pages` (`store`, `librarian`, `graph`, `health`,
`sinks`), migration `0070_page_lenses.sql`, CLI `throughline lib pages ...`,
routes `/v1/pages...`, the `pages` section of `POST /v1/find`.

Records are the ground truth. Pages are the synthesis on top of them, ported
from GBrain (github.com/garrytan/gbrain, MIT License, Copyright (c) 2026 Garry
Tan): its page anatomy (compiled truth over an append-only timeline), page
kinds, typed links, aliases and merges, its maintenance jobs and its writing
rules and skills, native in Throughline's database and enforced in code where
GBrain relies on prose: a page for each thing worth a page (a person, a day, an
event, a trip, a deal, a place, a project, a group's dynamics, a topic),
cited to the records it rests on and linked to the pages it relates to.
Nothing about pages is fixed to people or days: what pages exist, what they
say and how they connect is decided by **pages lenses** and the Librarian.

## Pages lenses

A pages lens is a lens (docs on lenses: query, prompt, schedule) whose output
is page actions instead of table rows:

```toml
name = "event_pages"
description = "A page for every gathering, trip or event."
output = "pages"                   # worker = "librarian" (implied)
grain = "day"                      # message | conversation | thread | day, as for any lens
channels = ["calendar.events", "transcripts.wave", "chat.sms", "visits"]
schedule = "0 */4 * * *"
# context_query = "SELECT ..."     # optional, as for any lens
max_items_per_run = 3
max_cost_per_run = 0.50

prompt = """Write or update an events/<YYYY-MM-DD>-<name> page for every real event in this window
the owner attended. Set when. Link attendees, the day page and the place page."""

[worker_config]
kinds = ["events"]                 # optional: the page kinds this lens may write
allow = ["write", "edit", "link"]  # optional: the actions it may take (default: all)
max_turns = 4                      # tool-use turns per work item (<= 12)
max_actions = 40
max_new_pages = 3                  # per work item
candidate_pages = 12
max_page_chars = 20000
max_chars_per_record = 1500        # each evidence line is cut here
require_citations = true
verify_quotes = true               # quoted words must appear in a record the Librarian was shown
exclude_annotations = ["media_noise"]
exclude_patterns = ["(?i)therapy"] # privacy wall: matching records never reach the model
exclude_people = [123]             # nor these people's records
input = "records"                  # or "flagged": the work items are pages health flags (upkeep)
input_kinds = ["people"]           # pages in the evidence count only of these kinds
lookback_days = 7                  # lens runner keys work as for any lens
max_chars_per_item = 120000        # (lens runner) a busy day is split into parts of this size
model = "gpt-6-luna"               # any OpenAI-compatible endpoint
base_url = "http://127.0.0.1:8317/v1"
api_key_env = "THROUGHLINE_CLIPROXY_KEY"
price_per_million = 0
```

Define it like any lens (`throughline lib lens define FILE`); the scheduler
runs it (`throughline lib lens tick`) with everything lenses have: cursors,
day windows re-read when they change, busy days split into parts, resume
after an outage, per-run and monthly budgets, the egress gate. A pages lens
has no columns, no natural key and no `api.lens_<name>` view; it never shares
model calls with table lenses. `throughline lib pages init` copies the
examples into `~/.throughline/lenses/` (each adapted from a GBrain skill):

| Lens | Reads | Writes | From GBrain |
| --- | --- | --- | --- |
| `daily_pages` | the day's records | `days/`, `events/`, timeline lines on touched pages | filing rules, cold-start calendar |
| `people_pages` | the day's conversations | `people/` (compiled truth, typed relationships) | enrich, resolve-before-asking |
| `event_pages` | calendar, transcripts, chats, visits | `events/` (presence, true order, verbatim quotes) | meeting-ingestion |
| `thoughts_pages` | transcripts and chats | `ideas/`, `reflections/` | signal-detector, voice-note ingest, dream synthesis |
| `open_loops` | conversations | `loops/<person>` (both directions, verbatim) | google-loops |
| `places_pages` | visits, calendar, chats | `places/` | typed links (`at_place`, `lives_in`) |
| `patterns` | reflection and idea pages (weekly) | `patterns/` (3+ instances) | dream patterns phase |
| `page_linker` | every new page revision | typed links, merges | auto-link, backlinks |
| `topic_consolidate` | idea, topic and pattern pages | merges, links | concept-synthesis |
| `conflicts` | changed entity pages | `## Conflicts`, corrections | contradiction judge, edge contradictions |
| `page_upkeep` | health flags (`input = "flagged"`) | refreshed truth, cited claims, links, archives | maintain, enrich_thin, citation-fixer |

Broad or narrow is the prompt's choice: "write today's page and link it",
"keep a profile of everyone who matters", "track every deal in email",
"summarize what happened at each event and who was there", "find 30
additions and 45 links among existing pages and draft pages for new people,
places and events".

## What the Librarian does with a work item

It is shown:

- `instructions` (the lens prompt), `window` (`key`, `grain`, `day`, `part`);
- `evidence`: the window's records grouped by conversation, one citable line
  each (`[m:ID] MM-DD Dow HH:MM Author: text`), pages one by one;
- `people_directory`: everyone who wrote, was annotated as speaker, or is
  named in the evidence (display names and recorded names), with their page;
- `candidate_pages`: pages about those people, pages whose title or alias
  appears in the evidence, the page of the same window;
- `context_query_results`, `date_context`, its limits.

Each turn it either calls tools or finishes with actions:

| Tool | Returns |
| --- | --- |
| `search_pages {query, kind?, limit?}` | pages by title, alias, summary, text |
| `read_page {slug}` | body (compiled truth and timeline), summary, aliases, links in and out, revision |
| `get_timeline {slug?, on?, since?, until?}` | dated timeline lines across pages (a day's view) |
| `search_messages {query, after?, before?, channels?, limit?}` | matching records as citable lines |
| `read_context {message_id, before?, after?}` | the conversation around a record |

| Action | Effect |
| --- | --- |
| `write {slug, kind?, title, body, summary?, aliases?, person_id?, when?, reason?}` | create a page, or rewrite one it read (the timeline is kept) |
| `edit {slug, section, mode: append\|replace, text, summary?, aliases_add?, reason?}` | change one `## ` section, keep the rest (an edit of `Timeline` is a timeline append) |
| `timeline {slug, entries: [{date, text}], reason?}` | append dated lines (exact duplicates collapse) |
| `merge {from, into, reason}` | fold a duplicate into a page: aliases, timeline and links move; `from` redirects |
| `link {from, to, type?, evidence: [ids], note?}` | page ↔ page or page ↔ `person:ID`, with evidence |
| `unlink {from, to, type?}` | remove a link a lens asserted |
| `archive {slug, reason}` | retire a page (after reading it) |

The runtime checks every action before applying it:

- citations must be ids the Librarian was shown in this work item (evidence,
  tool results, citations of pages it read); others are removed and counted;
  a page or an edit that cites nothing it was shown is refused;
- an existing page is read (`read_page`) before `write`, `edit`, `merge` or
  `archive` changes it, and the action is refused when the page changed since
  (GBrain's `expected_revision`); `timeline` needs only an existing page;
- quoted spans (three words or more) must appear in a record the Librarian
  was shown or already be on the page, else they lose their quotation marks
  (GBrain's synthesize-verify, counted as `unverified_quotes`);
- records matching `exclude_patterns` or written by `exclude_people` never
  reach the model;
- links need evidence it was shown, an active registered link type, and at
  least one page end;
- `kinds`, `allow`, `max_actions` and `max_new_pages` hold.

Applied and refused actions are logged in `tl.page_actions` (`lib pages
actions`, `GET /v1/pages/actions`). A pages lens never sees its own page
revisions as evidence, so a lens that reads `pages` does not loop on itself.

## Page anatomy (GBrain)

- **Compiled truth on top**: a first paragraph that says on its own what or
  who this is and why it matters, then the kind's `## ` sections. It is
  rewritten when the picture changes; `tl.pages.truth_updated_at` records
  when.
- **Timeline at the bottom**: `## Timeline` with one line per dated event,
  `- **YYYY-MM-DD** — what happened [m:ID]`, newest first. The lines are rows
  in `tl.page_timeline` (deduplicated on page, date and text, with their
  cited ids); every revision renders all rows, so the timeline is
  append-only by construction: a rewrite that leaves it out keeps it, a
  correction is a new line. `lib pages timeline --on DATE` and `GET
  /v1/pages/timeline` give the cross-page view of a day or a span.
- **Kinds** come from `tl.page_kinds` (description, recommended sections,
  guidance, recency half-life for search). Seeded: people, days, events,
  places, organizations, projects, topics, ideas, reflections, patterns,
  loops, media, trips, questions. A kind a lens invents is stored as a
  `candidate` (health lists them); `lib pages kinds --declare KIND` declares
  or promotes one, `--retire` retires one (writes of it are refused). The
  Librarian sees each relevant kind's sections and guidance.
- **Typed links** for people and places are seeded in the link-type
  registry: family_of, partner_of, friend_of, lives_in, works_at, member_of,
  attended, hosted, owes_to, awaiting_reply_from, discusses, supersedes (plus
  part_of, about, mentions, related_to, at_place).
- **Merges and redirects**: `merge` (action, `lib pages merge`, `POST
  /v1/pages/merge`) moves aliases, timeline rows and links to the kept page and
  stores a redirect, so the old slug, old `[[links]]` and `read_page` of it
  resolve to the kept page. Each revision records its `reason`.
- **Link context**: body links keep the text around them (`evidence.context`).

## Pages and the graph

- **Slugs** name pages: `<kind>/<name>` (lowercase, up to four segments).
  Kinds are free-form. `tl.pages` is the registry (kind, title, aliases,
  summary, person, status, current revision, stats, sink state).
- **Revisions** are `page_revision` records on channel `pages` (external id =
  slug): versioned, gated, split into passages, embedded and found by recall.
  `when` sets the record's subject period (an event's dates). Cited records
  become provenance (`api.sources`), so a page is visible to a restricted
  asker only when all of its sources are.
- **Body links**: `[[slug]]`, `[[slug|label]]`, `[[person:ID|Name]]` in the
  text become links (`mentions`; `about` for the page's own person), kept in
  step with the text on every revision. A link to a page that does not exist
  yet is unresolved until that page is created, then it resolves.
- **Asserted links** come from `link` actions (job `librarian`, method
  `model`, `evidence.message_ids`) and stay until an `unlink`. Both kinds move
  to each new revision of their page.

## Search over the graph

`POST /v1/find` still answers with ranked episodes of primary records in
their thread context. For unrestricted askers it adds `pages` (up to
`pages`, default 5; `0` turns it off):

1. seeds: page records the search ranked (any page store: `pages`,
   `gbrain.pages`, `notion.pages`) and pages whose title or alias is in the
   query;
2. the graph: each seed brings its linked pages; a page gains a small,
   logarithmic bonus for the pages linking to it, and a neighbour inherits
   part of its seed's score (`why`: `search hit`, `title match`, `linked
   from <title>`);
3. recency: each kind's half-life (days fade in two weeks, people and ideas
   never) scales a page's score by at most 30%;
4. ground truth: each page lists its most recent cited records (`evidence`:
   message id, channel, time, preview) for `api.context`, and its last three
   timeline lines (`recent`).

Each entry is marked `generated: true`. `matched_people[].page` names the
person's page.

## Health

`throughline lib pages health` (`GET /v1/pages/health`):

| Part | Reports |
| --- | --- |
| graph | pages by kind, links between pages, average degree, connected components, orphan pages, pages citing nothing, unresolved links and the most-wanted missing pages, timeline lines, stale truth (timeline newer than the top), stale evidence (a cited conversation has newer records), thin entity pages, candidate kinds, active people with no page, pages awaiting upkeep |
| lenses | per pages lens: last runs, actions in the last 7 days by op and status, refusal reasons |
| stores | other page channels (GBrain, Notion): pages, revised in 7 / 30 days, untouched 90 days, no links, no sources, oversized |

Each part gets `ok`, `warn` or `bad`; the command exits 1 on `bad`. The same
per-page flags (`stale_truth`, `stale_evidence`, `thin`, `orphan`,
`uncited`, `unresolved`) are the work items of an upkeep lens
(`input = "flagged"`); a page is offered again only when its flags change.

## Moving GBrain in

GBrain does not stay a separate store: `throughline lib pages import-gbrain`
(module `throughline.pages.gbrain_import`, migration `0072`) turns what the
GBrain plugin imports (`gbrain.pages`, `gbrain.legacy`, `gbrain.timeline` and
the links between them) into native pages:

| GBrain | Pages |
| --- | --- |
| live page | page; slug normalized (lowercase, up to eight segments), the original slug and GBrain slug aliases redirect to it |
| legacy page | archived page |
| type | kind: person→people, event→events, place→places, organization→organizations, project→projects, media→media, concept→concepts, commitment→commitments, conversation→conversations, report→reports, document→documents, dossier→dossiers, index→indexes, routine→routines, note→notes, pet→pets |
| compiled truth | the top, unchanged (so its passage vectors are reused) |
| timeline text and `gbrain.timeline` rows | timeline rows, each citing its imported record, plus a dated line citing the GBrain page record it came from |
| aliases, page aliases, tags | aliases, tags |
| frontmatter, id, source uri, type | the revision's `source` metadata; `tl.pages.source_ref` names the record |
| person page | `person_id` from the confirmed `gbrain` identifier; a people page a lens wrote for the same person is merged in |
| link | link between the native pages; GBrain's one-off types (origin `legacy` in the registry) become `related_to` with `evidence.gbrain_type` |

Re-runnable (it runs after each GBrain sync until GBrain is retired): a page
is rewritten only when its GBrain source changed and nobody edited it in
Throughline since; an edited page keeps its text and only gains new timeline
lines. One import runs at a time. In `/v1/find`, a hit on a GBrain record
stands for its native page in the page map. Once GBrain is retired, stop the
GBrain sync; the imported records stay as the archive the pages cite.

## What was not ported

GBrain's storage mechanics (Markdown files and git, `[Source: …]` strings,
frontmatter tooling, schema-pack YAML, fence tables) are replaced by records,
`[m:ID]` citations and the registries above. Takes, grading and calibration,
concept scoring, paid external enrichment, founder and investor page types,
query expansion (its own docs report it cut recall) and GBrain's
infrastructure and skill tooling are left out. The owner's GBrain stays
moved into Pages (above).

## Sinks (GBrain, folders)

`~/.throughline/pages.toml` (or `THROUGHLINE_PAGES_CONFIG`):

```toml
[[sinks]]
kind = "markdown"                  # YAML front matter + Markdown, written atomically
path = "~/brain/{slug}.md"

[[sinks]]
kind = "command"                   # page Markdown on stdin; exit 0 = delivered
name = "gbrain"
argv = ["ssh", "brain-host", "gbrain", "put", "{slug}"]
kinds = ["people", "events"]
```

Pages lenses export the pages they touch; `lib pages export` sends anything
pending. Each sink gets each revision once. Exported pages carry
`throughline_page: <slug>` in their front matter: filter on it when the same
pages are imported back (the GBrain importer would otherwise read them as
`gbrain.pages`).

## By hand

`lib pages list|show|write|link`, `POST /v1/pages` and `POST /v1/pages/links`
(owner, Idempotency-Key) write and link pages as the owner (`updated_by
owner`). `GET /v1/pages?kind=&q=` searches, `GET /v1/pages/{slug}` returns a
page with its links.
