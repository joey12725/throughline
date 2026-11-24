# Agent retrieval guide

Throughline separates **storage** from **presentation**. Programs and pipelines use the full record contract (`/v1/recall`, `/v1/sets`, `/v1/plans`, `/v1/packets`), while AI agents reading inside a finite context window use three high-level retrieval endpoints — **`/v1/find`**, **`/v1/timeline`**, and **`/v1/orient`** — alongside person functions and read-only SQL.

All of them run under the same caller token, asker gate, and `throughline_reader` database boundary. Nothing bypasses access control.

## Choosing the right endpoint

| Question shape | Start with | Why |
|---|---|---|
| "What did we decide about X?" / topic lookup | `POST /v1/find` | Hybrid keyword + vector search shaped into compact episodes with surrounding chat context |
| "Read what happened before/after this hit" | `POST /v1/sql` with `api.context(id, before, after)` | Exact thread neighbours to catch reversals, jokes, or outcomes |
| "Tell me about Sam" / "When did we last talk?" | `api.person('Sam')` → `api.person_brief(id)` | Unified person summary across channels, DMs, open loops and arc |
| "How many times has X happened?" / "Over the years..." | `POST /v1/timeline` | Exhaustive match across synonyms at episode grain with yearly/channel counts and coverage |
| "How's my sleep / schedule / recent week?" | `POST /v1/orient` | Single-call snapshot of channels, latest telemetry, future calendar, voice notes, active threads |
| Exact date filters, telemetry series, aggregates | `POST /v1/sql` | AST-validated read-only PostgreSQL over `api.messages`, `api.threads`, and `api.ch_<channel>` |
| Schema discovery or worked examples | `GET /v1/describe` / `GET /v1/recipes` | Live channel field schemas, SQL function docs, and tested recipes |

The host-neutral skill file at [`skill/throughline/SKILL.md`](../skill/throughline/SKILL.md) packages this workflow in under 1,500 tokens so you can drop it into any agent host.

---

## `POST /v1/find` — context-window-shaped search

`/v1/recall` returns one bare record per hit with the full record contract, which is right for code and wasteful for an LLM reader. `POST /v1/find` runs the same recall pipeline (legs, fusion, ranker, hard scope, asker gate) and reshapes the output into compact, readable episodes:

- **Merged thread windows.** Chat and transcript hits arrive with surrounding messages (`window` turns before and after; transcripts widen automatically). Nearby hits in the same thread merge into one episode.
- **Tail awareness.** Each episode includes `later_in_thread` when newer messages exist beyond the returned window, and `searched.newer_in_pool` highlights newer matches that ranked outside the top `k`.
- **Cross-channel diversity.** Unscoped searches apply a soft per-channel cap so one high-volume channel cannot drown out other sources, and report `searched.other_matching_channels`.
- **Automatic person leads.** When the query mentions known people (unrestricted scope, no explicit `persons` filter), `matched_people` returns their `person_id`, `display_name` and `handle` for immediate follow-up with `api.person_brief`.
- **Long-record excerpting.** Emails, pages and transcripts are trimmed around the best-matching passages (with a second distant excerpt when relevant) and annotated with how to read the rest via `substr(...)` or `api.context`.
- **Owner-local timestamps & citations.** Every line carries owner-local time with weekday (controlled by `THROUGHLINE_TIMEZONE`, default UTC), `(owner)` markers, `channel`, `external_id` and `message_id`.

### Request

```json
{
  "query": "natural words about the topic",
  "k": 15,
  "sort": "relevance",
  "window": 4,
  "max_chars": 42000,
  "channels": ["chat.sms"],
  "persons": [123],
  "person_role": "involved",
  "after": "2024-01-01",
  "before": "2024-06-30"
}
```

| Field | Default | Notes |
|---|---|---|
| `query` | required | Natural-language query (aliases: `q`, `text`, `search`) |
| `k` | `15` | 1–100 (aliases: `limit`, `top_k`) |
| `sort` | `"relevance"` | `"relevance"` or `"recent"` (newest episodes first) |
| `window` | `4` | Surrounding turns before/after each hit (0–40) |
| `max_chars` | `42000` | Character budget (2,000–400,000); trailing episodes dropped with a warning |
| `strategy` | `"auto"` | Recall strategy override |
| `ranker` | unset | Ranker override (`fused`, `lexical`, `vector`, `cross_encoder`) |
| `scope` / top-level aliases | `include_reference: true` | `channels`, `persons`, `person_role`, `after`, `before`, `as_of`, `known_by`, `kinds`, `place`, `threads`, `include_ignored`, `include_reference` |

`/v1/find` is always a **ranked sample**, never a complete count. Its response explicitly notes this and includes `searched` (`legs`, `retrieved`, `ranker`, `status`, `coverage`, `channels_hit`, `other_matching_channels`, `newer_in_pool`, `date_range`).

---

## `POST /v1/timeline` — exhaustive episode history

Counting ("how many times did X happen?") and longitudinal pattern questions cannot rely on a top-`k` sample, and raw message counts overcount chatty conversations. `POST /v1/timeline` solves both:

1. Runs an exhaustive full-text search over gated `api.messages` using PostgreSQL `websearch_to_tsquery` across either a single `query` or a `queries` list of up to 12 synonyms/wordings.
2. When `semantic: true` (default), unions high-similarity meaning matches from the vector leg so paraphrased episodes without exact keywords still surface — every episode is tagged `match: "keyword" | "semantic" | "both"`. Set `semantic: false` for strict keyword-only counts.
3. Groups every match into sessions with `api.episodes_of` (gap-based per thread; whole-thread channels stay one episode) and returns chronological episodes, `episodes_by_year`, `episodes_by_channel`, `date_range`, `exact` flag, and per-channel `coverage`.

### Request

```json
{
  "queries": ["dentist", "orthodontist", "toothache", "dental"],
  "semantic": true,
  "max_episodes": 150,
  "order": "asc",
  "sample": "spread",
  "text_chars": 240
}
```

| Field | Default | Notes |
|---|---|---|
| `query` / `queries` | one required | Single string or up to 12 websearch queries (`"exact phrase"`, `a or b`, `-exclude`) |
| `semantic` | `true` | Include high-similarity semantic episodes; set `false` for strict keyword counts |
| `max_episodes` | `150` | 1–2000 (alias: `limit`) |
| `order` | `"asc"` | `"asc"` (oldest first) or `"desc"` |
| `sample` | `"head"` | `"head"` or `"spread"` (samples evenly across all eras when `total_episodes > max_episodes`) |
| `text_chars` | `240` | Snippet length per episode's first/last match (40–2000) |
| `scope` / top-level aliases | — | Same hard scope filters as `/v1/find` |

---

## `POST /v1/orient` — working-memory snapshot

Planning, advice, and recent check-ins depend on ambient state that rarely matches search keywords (sleep scores, weight, weather/finance digests, upcoming calendar events, recent voice notes, active chats). `POST /v1/orient` gathers all of it in one gated read:

- `channels`: visible channels with record counts and latest dates.
- `recent_telemetry`: newest items across visible channels whose plugin manifest declares a telemetry-like role (health, location, digests), with `age_days`.
- `upcoming_calendar`: strictly future calendar events at or after `as_of`.
- `recent_voice`: recent voice/transcript summaries.
- `recent_threads`: most recently active chat threads.
- `people` (optional): when `people: ["Name1", "Name2"]` is passed (up to 6 names), resolves each person and returns their channel breakdown and 6 most recent cross-channel messages.

### Request

```json
{
  "days": 14,
  "as_of": "2026-11-21",
  "people": ["Zibble"],
  "max_chars": 16000
}
```

---

## People tools

Always resolve people first with `api.person('name')` (multiple people can share a first name) and start with `api.person_brief(id)`.

| Function / route | What it answers |
|---|---|
| `api.person(text)` / `GET /v1/person?q=` | Resolve names, handles and identifiers to `person_id` |
| `api.person_brief(id)` / `GET /v1/people/{id}/brief` | Default first call: identifiers, facts, relations, channels, last contact, open loops, recent arc |
| `api.dms_with(id, after, before, limit)` | Both sides of direct conversations with a person (uses `is_me`, even if the owner's account isn't attached) |
| `api.last_contact(id)` | Most recent real interaction |
| `api.open_loops(id)` | Unanswered asks in either direction (closed by replies, closing phrases, or `horizon_days`) |
| `api.between(ARRAY[id1, id2], ...)` | Shared threads and exchanges between specific people |
| `api.circle(...)` / `api.ties(id)` | Closest contacts ranked by exchanges and shared conversations |
| `api.person_arc(id)` / `api.rhythm(id)` | First, peak and recent periods; time-of-day/week patterns |
| `api.day(date, tz, channels, limit)` / `api.around(ts, window_s, ...)` | Everything that happened on a date or around a timestamp |
| `api.attribution(ARRAY[msg_ids])` | Returns `is_me`, `direction`, `attribution_status`, `identity_quality`, `time_precision` |

See [docs/v11/people.md](v11/people.md) for the complete reference.

---

## Images

Messages with images carry `images[]` in `/v1/find` lines, `/v1/timeline` episode entries, `/v1/orient` items and `/v1/recall` records (`null` there when a message has none; elsewhere the key is absent): `{asset_id, kind, tags, people:[{person_id,name}], place, taken_at, ocr, caption, width, height, thumb_url, content_url}`. `people` is empty for restricted askers. nsfw images are left out unless the request passes `include_nsfw: true` (find, timeline, recall). Recall's `auto` strategy adds two legs: `visual` (the query embedded into the image space, Gemini first, local SigLIP2 when Gemini is unavailable) and `image_lexical` (keywords over each image's kind, tags, caption, place, people, OCR and file name). See [docs/images.md](images.md).

| Route | What it answers |
|---|---|
| `GET /v1/assets/{id}` | Metadata, linked messages and (unrestricted) people of a visible asset |
| `GET /v1/assets/{id}/thumb`, `GET /v1/assets/{id}/content?max_px=1536` | The image bytes, proxied from Immich (404 for hidden and missing alike) |
| `POST /v1/assets/inline {asset_ids, max_px}` | Up to 8 images as base64 data URIs (about 4 MB in total) for multimodal tool calls |
| `POST /v1/assets/search {query?, kind?, tags?, persons?, place?, after?, before?, channels?, include_nsfw?, limit}` | Images only: vector legs plus keywords, fused with RRF, within the filters; newest first without a query |
| `GET /v1/people/{id}/photographed-with` | People in the same frame, with counts and example asset ids (unrestricted only) |
| `GET /v1/people/{id}/nearby?hours=2&meters=1000` | People photographed near this person's photos in time and place without sharing the frame (unrestricted only) |

SQL: `api.assets`, `api.message_assets`, `api.asset_faces` (unrestricted only), `api.assets_of(ARRAY[message ids])`, `api.visual_records(...)`, `api.image_search_records(query, k, ...)`, `api.visual_assets(...)` and `api.image_search_assets(query, k)`.

---

## Read-only SQL and response shaping

`POST /v1/sql` accepts a single read-only `SELECT` statement validated by PostgreSQL's own parser (`pglast`) and executed as `throughline_reader` inside a `READ ONLY` transaction (30 s timeout, 1,000 default / 10,000 max rows, 4 MiB serialized cap).

By default, `throughline.agent_view.shape_rows` keeps responses readable for agents:

- Drops reader-useless internal columns (`tsv`, `payload_hash`).
- Cuts long text cells at 4,000 characters with a note showing the exact `substr(col, ...)` call to read the rest.
- Caps total JSON output around 45,000 characters and reports `rows_left_out` and `cells_cut`.
- Pass `"raw": true` to disable shaping when a program needs every byte verbatim.

Key views:

- `api.messages` — current versions of all visible messages (`id`, `channel`, `external_id`, `thread_id`, `thread_no`, `happened_at`, `ended_at`, `display_name`, `author_handle`, `person_id`, `content`, `thread_title`, `fields`).
- `api.threads` — thread records including `metadata` (for example `metadata->>'kind'` for `dm`, `group_dm`, `channel`).
- `api.ch_<channel>` — per-channel typed views with declared fields promoted to typed columns (dots replaced by underscores, e.g. `api.ch_health_sleep` for a `health.sleep` channel).

---

## Local cross-encoder reranking

Throughline ships [`tools/rerank_server.py`](../tools/rerank_server.py), a tiny standalone FastAPI wrapper around `sentence-transformers` `CrossEncoder` (default `BAAI/bge-reranker-v2-m3`, 1024-token max length) speaking the `POST /rerank` contract. Nothing leaves loopback, and `torch` stays out of Throughline's own dependencies:

```sh
uv venv ~/.throughline/rerank/.venv
uv pip install --python ~/.throughline/rerank/.venv/bin/python torch sentence-transformers fastapi uvicorn
~/.throughline/rerank/.venv/bin/python tools/rerank_server.py --port 8795
```

Configure Throughline to use it:

```sh
THROUGHLINE_RERANK_PROVIDER=http
THROUGHLINE_RERANK_BASE_URL=http://127.0.0.1:8795
THROUGHLINE_RERANK_MODEL=BAAI/bge-reranker-v2-m3
THROUGHLINE_DEFAULT_RANKER=cross_encoder
```

If the reranker is down or times out, recall and `/v1/find` degrade gracefully to the fused order and disclose the reason in `execution.warnings`.

---

## Lower-level library, recipes and librarian

When you need programmatic control, multi-step DAGs, or structured evidence packets:

- **Composable operators:** `POST /v1/recall`, `POST /v1/expand`, `POST /v1/match`, scoped result sets (`GET /v1/sets/{handle}`), plan DAGs (`POST /v1/plans`), and verified evidence packets (`POST /v1/packets`). All share the same record contract and execution envelope ([docs/v11/recall.md](v11/recall.md), [docs/v11/semantic_sql.md](v11/semantic_sql.md)).
- **Versioned recipes:** Tested SQL and semantic patterns in `src/throughline/recipes/` (listed by `GET /v1/recipes` or `api.describe('recipes')`, executed by `throughline lib recipe run <name>`).
- **Optional librarian:** `POST /v1/librarian` runs a bounded controller loop over four library tools (describe, plans, set inspection, packets) with baseline recall fallback, no recursion, and no writes beyond traces and working sets ([docs/v11/librarian.md](v11/librarian.md)).
