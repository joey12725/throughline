# Evidence selection, grouping and the adaptive planner

Package w3-selection. Requirements: QRY-24, QRY-26, QRY-27, QRY-28, QRY-30
(grouping and manifests), QRY-15, QRY-21, QRY-17, CST-8; design priority P7
(episode grain, incident counts) and P8 (alias-pair dedup); scenarios
`episode_count` and `newest_relevant`.

Modules:

| Module | Role |
| --- | --- |
| `retrieval/selection.py` | Pure, deterministic rules: support keys, independent groups, dependencies, greedy selection, ordering, group keys, manifests |
| `retrieval/stages/dedup.py` (order 30) | Duplicates, contained spans, alias pairs, independent-group annotation; loads record facts for the later stages |
| `retrieval/stages/select.py` (order 40) | Budgeted joint selection with context dependencies; `context.fetch` implementations |
| `retrieval/stages/group.py` (order 50) | `group_by` grains and the manifest |
| `retrieval/planner.py` | `PLANNER` (replaces the default planner by presence) |
| `resultsets.py` | `transform(..., op='group', by='episode')` |

## Provenance-aware dedup (QRY-26)

A raw message (`life_event`, `reference`) is its own source. A generated
record (page revision, derived entry, passage, claim, link) rests on the
messages in its `support` (relation `self` excluded). Records whose source
sets overlap are one independent group, so a passage, the page it came from
and the raw message the page was built from count as one source.
`independent_sources` is the number of distinct raw source messages of the
group. A generated record without support forms its own group, is flagged
`group_unsupported`, and contributes 0 sources. The dedup stage writes
`meta.independent_group`, `meta.independent_sources` and
`meta.group_unsupported` on every candidate; `group_by: "support"` groups by
the same groups.

The dedup stage also drops repeated record ids, and a candidate whose span
lies inside the explicit span (from the spans stage) of a candidate kept
earlier in the same thread (reason `redundant`; its discovery entries move to
the span that holds it). Under `as_of`/`known_by` only explicit spans are
compared, because older versions keep stale thread numbers.

## Alias dedup (P8)

One statement over the gated `api.links` (or the link state at the cutoff
under `as_of`/`known_by`) finds active `same_as` links between message
candidates. Each connected set of candidates collapses into one candidate:

- kept: the record whose `fields.identity_quality` ranks highest in
  `THROUGHLINE_IDENTITY_QUALITY_ORDER` (comma-separated; default `native_id,
  beeper_id, fingerprint, unknown`; an unlisted value ranks after `unknown`);
  on a tie the lower record id;
- the kept candidate takes the best rank of the set and the discovery
  entries of every member, and its `meta.aliases` lists the record ids it
  absorbed;
- every other member goes to `dropped` with reason `duplicate_alias` and
  `kept` naming the survivor.

Without `same_as` links nothing changes. A `same_as` link between versions is
matched on the link's message ids, which follow the current version.

## Selection under a token budget (QRY-24, QRY-27, QRY-28)

The select stage runs when the request gives `budgets.tokens`, gives
`expand: true` explicitly, sets `hints.select`, or asks
`hints.order: "newest"`; never for `exhaustive` (an enumeration is not a
selection). Otherwise recall's default selection (top k, then the token
budget) applies, so existing orderings are unchanged.

Greedy marginal gain over the first 500 candidates, one candidate at a time:

    gain = relevance (score / best score)
         + coverage of query terms not yet covered
         + new linked entities, new conversations
         - redundancy (span overlap with a selected span, same independent group)
         - soft diversity penalties (conversation, thread, channel, day, entity)

Penalties grow with the number of already selected records sharing the value
(`w * n / (n + 1)`); there are no quotas, so one conversation can fill the
selection when nothing else is relevant. `hints.diversity: false` turns the
penalties off; `hints.selection_weights` overrides weights (`relevance,
coverage, entities, conversations, redundancy, conversation, thread,
channel, time, entity`). Ties go to the better ranked candidate, so the
result is deterministic.

`k` counts hits. A candidate and its unmet dependencies are taken jointly or
not at all, within the remaining token budget (tokens are counted exactly as
recall counts returned records). The output keeps rank order of the hits,
each preceded by the dependencies taken for it.

Dropped candidates are listed in `dropped` with a reason: `budget` (with
`with_dependencies` when the pair did not fit), `redundant` (inside a
selected span), `duplicate_group` (same independent group, no new source and
no new query term; a raw message is never dropped in favour of generated
records of its group), `rank` (beyond k or the pool). A budget drop sets
`execution.truncation = {reason: "token_budget", limit}`, and
`execution.selection` carries the counts (considered, selected,
dependencies, dropped by reason, budget and spent tokens).

### Context dependencies (QRY-27)

Heuristic triggers only:

- a short message (at most `THROUGHLINE_DEP_SHORT_TOKENS`, 12, tokens) or one
  that starts with an anaphor or acknowledgement (`yes`, `ok`, `that`,
  `they`, ...): the previous turn of its thread;
- an active `reply_to`/`quote` link from the message: the linked message;
- a generated record with support: its first cited source.

Neighbours are read through the logical operation `context.fetch`, with two
exact implementations: `context_many` (one `api.context_many` statement, the
reference) and `context_loop` (`api.context` per anchor).
`operators.choose` picks one from `tl.operator_profiles`; the result is the
same either way (`execution.context_fetch` names the one used). A dependency
row is added only when it passes the reduced filter (`api.scope_filter` with
kinds, as_of, known_by and include_outside, the other arguments NULL). It
gets role `dependency` and `depends_on` (the hit's record id) and is never a
hit: a neighbour written by another person can be context for a scoped hit,
never a hit itself. `expand: false` adds no dependencies.

## Newest relevant versus newest absolute

`hints.order: "newest"` orders the selected hits by record time, newest
first, among the candidates with query relevance: a score above zero from
the lexical, vector or passages leg, or entity or link discovery. When the
query has content tokens, a candidate found only by the structured leg (the
newest absolute message is often unrelated chatter) follows the relevant
ones and is never placed first. Without a content query every hit is
ordered by time. `execution.order` is `newest_relevant`. The default order
is unchanged.

## Grouping and manifests (QRY-30)

`group_by` grains: `conversation, thread, channel, day, month, year,
person, entity, support, episode`. Every hit gets its key in `groups`; a
record without a key (for example a link under `group_by: "thread"`) forms
its own singleton group keyed by its record id. The manifest, built from
the records recall finally returns:

    {by, unit, groups: [{key, count, record_ids, singleton?}],
     records, incidents, omitted}

`unit` is `record` for every grain except `episode`. `records` is the
number of hit records. `incidents` is the number of episode groups for the
episode grain and `null` otherwise. `omitted` counts grouped candidates that
did not reach the response (k or budget).

## Episodes (P7)

An episode is a gap-based session in one thread: consecutive current
messages, in thread order, whose time gaps are within the channel's
`conversation_gap_s` (default 1,800 s). A whole-thread channel
(`conversation_whole_thread`, for example a Wave recording) is one episode
per thread regardless of gaps. The id is `ep:<thread_id>:<first message
id>` (a message without a thread: `ep:m:<id>`), so it is stable across reads
until the thread changes. A late message that arrives inside the gap between
two sessions bridges them: the two sessions merge into one episode and the
later session's id disappears. Ids are therefore stable for a fixed corpus,
not across every ingest.

`group_by: "episode"` runs one `api.episodes_of` statement over the hit
message ids (migration 0040; it reads the gated current versions, so a
restricted asker's sessions are built from visible messages only).
Non-message records (pages' passages, claims, entities, links) form their
own singleton group keyed by record id (`singleton: true`); such a
singleton is not an incident and is not counted in `incidents`. Without
`api.episodes_of`, hits are grouped by thread with a 1,800 s gap between
consecutive hits and `execution.warnings` gets `episode_fallback`.

**A hit count is not an incident count.** Nine keyword hits in three
sessions are `records: 9, incidents: 3, unit: "episode"`. Count incidents
with the episode grain (or `count(DISTINCT episode_id)` over
`api.episodes_of` in SQL), never by counting hits; a record count is never
put in the `incidents` field.

Result sets: `transform(scope, "group", handle, by="episode")` applies the
same rule and writes `tl.result_set_groups` with `grp` = the episode id,
`certain` true (singletons keyed by record id). Without `api.episodes_of`
the thread-and-gap fallback writes tentative groups (`certain` false).
`ungroup` with `prefix: "ep:"` reverses it.

## Adaptive planner (QRY-15, QRY-21, CST-8)

`initial`: a request that names legs, a strategy other than `auto`, or
`exhaustive` gets exactly the default planner's legs (an override is never
second-guessed). For `auto`:

- only available legs are planned; an unavailable one is disclosed as
  `skipped` with its reason;
- vector is pruned for a query without content tokens and lexical for a
  query whose tsquery is empty (only stop words), status `pruned`, reason
  `no_content_tokens` / `empty_tsquery`;
- a leg whose p95 (latest profile in `tl.operator_profiles`, else 250 ms
  free, 1,500 ms embedding, 10,000 ms model) exceeds the remaining deadline
  is pruned (`p95_exceeds_deadline`); the fastest leg is kept if all would
  be pruned;
- the links leg runs when starting sets or message references exist;
- each leg's logical operation goes through `operators.choose`: another leg
  implementing the same operation replaces it only with a recorded quality
  not worse than the reference's and a lower measured p95; an approximate
  implementation is considered only with `hints.allow_approximate`
  (`execution.implementation_choices` lists swaps).

`next_round`: a second round only for `auto`, when fewer than k candidates
were found or a soft `hints.must_terms` term is not covered by the top
candidates, and only with legs not yet run (registered legs outside the
default set; structured only when the query has no content tokens). The hard
scope is never relaxed (no leg reads soft hints today, so there is nothing
soft to relax). The planner stage reports the stop reason: `single_round`,
`deadline`, `budget`, `no_new_candidates`, `enough_candidates`,
`low_expected_gain` (missing fraction of k below
`THROUGHLINE_MIN_ROUND_GAIN`, 0.1), `no_more_legs`, or recall's
`max_rounds`. It never emits a sufficiency flag: stopping says why retrieval
stopped, not that the evidence is enough.

`after_recall`: measured leg timings and `context.fetch` timings are
appended to `tl.operator_profiles` with source `runtime` (p50/p95 over the
samples since the last write, quality carried over from the latest recorded
quality), at most once per minute per implementation (checked in process and
in the table). `THROUGHLINE_RUNTIME_PROFILES=0` turns this off.
