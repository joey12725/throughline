---
name: throughline
description: Retrieve and verify evidence from Throughline: ALWAYS search with POST /v1/find (never /v1/recall), read context, use person brief for people, /v1/timeline for counts/patterns, /v1/orient for telemetry/schedule, and exact SQL.
---

# Throughline

Throughline holds one person's life record (chats across apps, voice transcripts, email, calendar, curated notes/pages, health metrics, location history, morning digests, media, work logs) behind a read-only API. It returns evidence and provenance metadata; you find it, verify it, and draw the conclusions. Connect with `THROUGHLINE_URL` and a bearer token.

## Tools (in order of use)

1. **`POST /v1/find`** — **ALWAYS use `POST /v1/find` as your search endpoint (do NOT call `/v1/recall` for searches, even if a CLI example shows `/v1/recall`).** Hybrid keyword + semantic search shaped as compact *episodes* with surrounding thread context, owner-local times with weekday, `(owner)` markers, `channel` + `external_id`, `matched_people` IDs, `later_in_thread` hints when newer messages exist beyond the window, and `channels_hit` / `other_matching_channels` / `newer_in_pool` / `date_range`.
   ```json
   {"query": "natural words about the topic", "k": 15}
   ```
   Optional top-level scope (applied before ranking): `"channels": ["<channel>"]` (names from `orient` or the catalogue), `"after": "2024-01-01"`, `"before": "2024-06-30"`, `"persons": [123]`, `"person_role": "involved"`, `"sort": "recent"` (default `"relevance"`), `"window": 8` (default 4; channels that declare a minimum window, such as transcripts, widen automatically unless you pass `window`). Ranked sample, never a complete list.
   *(`/v1/recall` is a low-level programmatic endpoint that omits thread context, reference pages by default, `matched_people`, `later_in_thread`, and `newer_in_pool` — always use `POST /v1/find` instead.)*
2. **Read around every key hit: `SELECT * FROM api.context(<message_id>, <before>, <after>)`** via `POST /v1/sql {"statement": "..."}`. Use it on every hit you rely on — and follow `later_in_thread` and `newer_in_pool` when newer messages exist — to catch corrections, reversals, jokes/hypotheticals, or outcomes.
3. **People: `SELECT * FROM api.person('name')`** resolves person IDs (several people can share a first name — verify by context). Then `api.person_brief(id)` (first call for any person question), `api.dms_with(id, after, before, max_results)`, `api.last_contact(id)`, `api.open_loops(id)`, `api.between(ARRAY[id1,id2], after, before, max_results)`. Then the person's page (`matched_people[].page`), if any.
4. **`POST /v1/timeline`** — **USE THIS for any "how many times", "every time", "over the years", "why do I keep...", recurring issues, "patterns", "how has X changed", or "first/last time" question.** Exhaustive keyword search across `query` or a `queries` list of 3-6 synonyms (websearch syntax) plus high-similarity semantic episodes (`semantic: true` default, tagged `match: "keyword"|"semantic"|"both"`; set `"semantic": false` for strict keyword-only counts; set `"sample": "spread"` to sample evenly across all eras when `total_episodes > max_episodes`), grouped into episodes in time order with `episodes_by_year`, `episodes_by_channel`, `date_range`, first/last match per episode, and channel `coverage`.
   ```json
   {"queries": ["dentist", "orthodontist", "toothache", "dental"], "max_episodes": 150}
   ```
5. **`POST /v1/orient`** — compact one-call snapshot of visible `channels` (counts + latest dates), latest `recent_telemetry` (health, sleep, weight, weather, finance, tasks, location with `age_days`), strictly future `upcoming_calendar` (at or after `as_of`), latest `recent_voice` summaries, `recent_threads` (most recently active chat threads), and optional `people: ["Name1", "Name2"]` (returns each person's `person_brief` plus their latest cross-channel messages). Call this when a question involves current telemetry, upcoming schedule, recent-life check-ins, or multi-person catch-ups — skip it for specific past-chat or factual lookups.
   ```json
   {"days": 14}
   ```
6. **Exact SQL** (`POST /v1/sql`, **one `SELECT` statement per call** — never chain multiple statements with `;`) over `api.messages` (`id, channel, external_id, thread_id, thread_no, happened_at, ended_at, display_name, author_handle, person_id, content, thread_title, fields` — note: `is_me` comes from `api.attribution(ARRAY[id])`, NOT a column on `api.messages`), `api.threads` (`id, channel_id, external_id, title, metadata` — `metadata->>'kind'` gives thread type such as `dm`, `group_dm`, `channel`, `unknown`), and per-channel views `api.ch_<channel with _>` (channel `a.b_c` → `api.ch_a_b_c`; list channels with `orient` or `GET /v1/describe`). Use SQL for exact date ranges, telemetry time series (`ORDER BY happened_at DESC`), counts, thread-kind breakdowns, full threads, or `api.day('YYYY-MM-DD', NULL, NULL, 200)` (NULL zone = the owner's configured zone) / `api.around(ts, 7200, NULL, 100)`. Select only needed columns and `left(content, 400)`; convert UTC `happened_at` with `AT TIME ZONE '<owner IANA zone>'` (the offset shown in orient's `as_of` tells you the owner's local time).
7. **`GET /v1/describe`** lists every channel, function, and recipe (`api.describe('channel:<name>')` explains one channel's fields). Only needed when you need exact channel field schemas.

## Research discipline

Never stop after a single search. Run 3-5 targeted lookups across varied wordings, people, sources, and dates before concluding:

1. **Resolve relative times and check surrounding context.** Turn "recently / this week / last month" into concrete dates. When a question involves plans, decisions, or current state, check recent records (last 7-30 days) for schedule conflicts, telemetry, open loops, standing instructions, or another person's stated preferences and constraints.
2. **Search broadly across wordings and channels.** Run 2-4 `POST /v1/find` calls with varied natural wordings (casual texting words, synonyms, names, places). Follow `matched_people` into `api.person_brief` / `api.dms_with` / `persons` scope, and follow `other_matching_channels` when relevant sources sit outside the top sample. Curated notes and digests are summaries — verify details in primary records.
3. **Read full exchanges and check for newer updates.** Call `api.context` around key hits. Check who actually spoke (`(owner)` vs other), literal facts vs jokes/hypotheticals/hearsay, and whether newer records (`later_in_thread`, `after` searches, or channel `latest` dates) superseded an earlier status or plan.
4. **Breadth, pattern, and counting questions:** enumerate exhaustively with `POST /v1/timeline` (`queries: [...]`) and/or SQL across all relevant channels and the full date span. Count episodes rather than hits, spot-check representative episodes with `api.context` across the span to verify they qualify, and inspect `coverage`.
5. **Before concluding something is absent**, try alternative wordings, people, channels, and SQL `ILIKE`, and verify `api.coverage_for` / latest channel dates.

## Pages (maintained summaries)

Pages are the Librarian's cited synthesis over the records: people, days, events, places, ideas, loops, and other kinds. `POST /v1/find` returns related `pages` beside its episodes (ranked over the page graph, each with `linked_pages`, `recent` timeline lines and cited `evidence` ids); `GET /v1/pages/<slug>` reads one, `GET /v1/pages/timeline?on=YYYY-MM-DD` lists every page's lines for a day. Use pages to orient fast, never as final evidence.

- **Known entity → page first.** For "who is X", "what's going on with X", "what did I promise X": resolve the person, read their page and its Open threads, then verify the claims you rely on with `api.context` on the cited `[m:ID]`s.
- **Top vs Timeline.** The top is the current picture; `## Timeline` is the dated log. "What's the state of X" → top; "what happened / when" → Timeline, then the records.
- **Staleness.** A page is a summary as of its revision; newer records win. Compare the revision date with `/v1/find` `sort: recent` results and say when a page looks stale. No page means the Librarian has not written one, not that nothing happened.
- **Labels and conflicts.** `(inferred)` / `(observed)` are the Librarian's reading, not facts. Report both sides of a `## Conflicts` section with citations; never pick one silently.
- **Source precedence.** The owner's own words > what a person says about themselves > what others say > pages and summaries > your inference. A suggestion (including an AI assistant's) is not the owner's decision unless accepted; a later correction overrides earlier text.
- **Producer ≠ verifier.** Verify a page claim by opening its cited records, not by re-reading the page or re-running the search that surfaced it. Being in a thread, at an event or mentioned together is not authorship.
- **"When did I first discuss X":** search with `sort` by date and `before:` the earliest hit until nothing earlier survives (retry older wordings), read the earliest hit, answer with date, verbatim quote and message id, then the evolution as dated lines.
- **"That's wrong":** acknowledge, quote the wrong claim, state the correction, find its source (a page line and revision, a misread record, or an unsourced inference), and record the correction as an annotation or proposal so the Librarian picks it up.

## Interpreting returned metadata

Every response carries `execution`: `status`, `completeness`, `truncation`, `warnings`, `incomplete`, and `coverage`.

- **Ranked samples vs totals:** a ranked or truncated result is a sample; never treat its count as a total.
- **Coverage before absence:** a gap is not absence. Absence holds only within covered sources and covered date ranges; stale or partial channels mean newer or missing records may exist.
- **Attribution and identity:** `api.attribution(ARRAY[id])` returns `is_me`, `direction`, and `attribution_status`. Never merge two people with the same name or misattribute speakers (`(owner)` vs other). Suspected accounts count for nobody until confirmed.
- **`evidence_class`:** `primary` (source record), `normalized`, `derived` (pages, passages, links), or `inference` (model output). Generated text is never its own evidence; follow `support` and verify against primary records (`channel` + `external_id`).

## Tested recipes

`api.describe('recipe:<name>')` shows one; adapt literals. People: `person_brief_first`, `last_contact_pitfall`, `open_loops_both_ways`. Counting: `incidents_by_episode`, `exhaustive_versus_ranked`. Absence: `coverage_first`. New arrivals: `feed_loop`. Writes: `write_with_receipt`.

## New arrivals and writes

- Process only what is new with a named feed: read `GET /v1/feeds/{name}/news`, do the work, then mark.
- Send an `Idempotency-Key` header with every write and trust the receipt (`stored_hash`, `version`, `replayed`): a retry with the same key is a no-op, the same key with another body is 409. Do not read back.

## Classifiers (writing annotations automatically)

To tag messages or whole conversations automatically, read `GET /v1/classifiers/guide` (workflow, Python contract, examples), then `POST /v1/classifiers/check`, `POST /v1/classifiers/try` (writes nothing; spends cents) and `POST /v1/classifiers`. Prefer kind `message` (one Jev/LLM question per message, no code); use kind `python` only for conversation → participant cascades. Your definitions stay `proposed` until the owner approves them.

## Rules

- Access is enforced by the server: a hidden channel looks like an unknown one.
- **Restricted askers (someone other than the owner):** disclose only what the owner authorized for that person; withhold private third-party names, unshared owner plans, relationship/health/financial details, and standing-instruction exclusions.
- Never repeat secrets (passwords, tokens, API keys, account numbers) found in records.
- You cannot change messages; corrections are annotations or proposals the owner decides. Handles belong to your token and asker and expire; recall again after 409.
