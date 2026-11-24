# Web interface: server side

What the core gained for the web interface of [design.md](design.md). The
interface is a static single-page application; the server additions are a
public shell, three small route families under `/v1/web`, two additive changes
to `POST /v1/sql`, a list of extra SQL builtins and one table.

| Piece | Where |
|---|---|
| Static shell | `src/throughline/web/__init__.py`, assets in `src/throughline/web/static/` |
| Routes | `src/throughline/api/routes/web.py` |
| Stored documents, feature detection | `src/throughline/web/store.py` |
| Migration | `src/throughline/migrations/0060_web.sql` |
| SQL additions | `src/throughline/query.py`, `POST /v1/sql` in `src/throughline/api/__init__.py` |
| Tests | `tests/test_web_routes.py`, `tests/test_web_sql.py` |

## The shell

`GET /ui`, `/ui/` and every path below it answer `index.html` (client-side
routes are deep-linkable); `GET /` redirects to `/ui/`. `GET /ui/assets/...`
serves the hashed build output; a missing asset is a 404, never the shell. A
regular file directly in the static directory (a favicon) is served by name.

The shell is public by design: it contains no data and no token. It is made of
plain Starlette routes added after the API routers, so it is not in the
OpenAPI schema, has no authentication dependency, opens no database connection
and writes no audit row. Everything the page shows comes from `/v1`, either
with a session cookie set by signing in or with a bearer token the user pastes
(kept by the page, sent as `Authorization`); see [auth.md](auth.md).

Headers on every shell response:

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline';
    img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; worker-src 'none';
    base-uri 'none'; form-action 'none'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
X-Frame-Options: DENY
Cross-Origin-Opener-Policy: same-origin
```

- Scripts: this origin only. No inline script, no `eval`, no remote code.
- Styles allow inline ones because the editor and chart libraries inject
  style elements. The interface starts no workers (`worker-src 'none'`;
  frames fall back to `default-src 'none'`); a test checks that `web/src`
  creates none.
- `connect-src 'self'` lets the page call `/v1` and nothing else: no fonts,
  tiles, images or analytics from another host.
- `index.html` and top-level files are `Cache-Control: no-store`; assets are
  `public, max-age=31536000, immutable` (their names contain a content hash).

The build is looked up per request. Without one, `/ui/` answers a small
self-contained page that says to run `pnpm build` in `web/`; the API is
unaffected.

| Variable | Default | Effect |
|---|---|---|
| `THROUGHLINE_WEB` | `1` | `0` does not install the shell routes (`/ui/` is 404). The `/v1/web` routes stay. |
| `THROUGHLINE_WEB_TILE_ORIGIN` | unset | One `https://host[:port]` origin added to `img-src` and `connect-src` for map tiles (`http://` only for a loopback host). Anything else is ignored, with a warning at startup. |
| `THROUGHLINE_WEB_WAITERS` | `16` | Requests that may wait in `GET /v1/web/arrivals` at the same time. |

Packaging: `web/` builds into `src/throughline/web/static/` (never `dist/` or
`build/`). The directory is git-ignored and listed as a hatch `artifacts`
entry in `pyproject.toml`, so a wheel built after `pnpm build` contains it. The
`Dockerfile` builds it in a Node stage.

## Requests from the interface

The interface sends `X-Throughline-Client: web`. The audit middleware records
the header as `detail.client` when its value is exactly `web`, `cli` or `sdk`
and ignores anything else, so a row of the interface is told apart from an
agent's.

## GET /v1/web/bootstrap

Everything needed for the first screen, in one request. Owner and agent
tokens; a plugin token is 403.

```
{ version, now,
  token: {id, kind, name, asker, unrestricted, can_write},
  features: {web_state, source_health, embeddings: {available, model},
             assistant: {provider, model, loopback, default_tier}, egress: <api.describe('egress')>},
  owner_person_id,
  totals: {records, channels, plugins, people, entities, links, threads, vectors},
  plugins: [{name, version, mode, interval_s, last_success_at, last_error, messages_ingested,
             stale, updated_at, [last_attempt_at, last_error_at, consecutive_failures,
             paused_at, paused_reason,] open_outage}],
  channels: [{name, plugin, description, record_kind, is_reference, is_outside, whole_thread,
              embed, fields: [{name, type, description}], view, rows, threads, first_at, last_at,
              last_ingest_at, with_geo, with_ended_at, with_images,
              coverage: {status, reasons, covered_from, covered_to, open_gaps}}] }
```

- Channels, their counts, coverage, the people/entity/link totals and the
  egress description are read on the reader path under the caller's scope
  (`api.channels`, `api.messages`, `api.threads`, `api.coverage`,
  `api.coverage_gaps`, `api.people`, `api.entities`, `api.links`), so a
  restricted asker gets exactly what its grants show.
- What only the app role can read is added for an unrestricted scope and left
  out otherwise: `plugins` (`[]`), `channels[].embed` (`null`),
  `totals.vectors` (`null`). For a restricted asker `totals.plugins` counts
  the plugins of its visible channels.
- `view` is the channel's view, schema-qualified (`api.ch_chat_example`), or
  `null` when it does not exist. `rows` counts current records.
- `coverage` folds a channel's coverage rows: the weakest status
  (`partial` < `best_effort` < `complete`), every reason, the widest window;
  `unknown` when the channel has no coverage row.
- `plugins[].mode` is the manifest's `run_mode`. The source-health columns
  appear when the migration that adds them to `tl.plugins` is present
  (`features.source_health`), `open_outage` is the plugin's unrestored
  `tl.source_outages` row or `null`.
- `features.web_state` is false on a database without migration 0060 (the
  state routes then answer 400 with code `42P01`).
- `features.assistant` is `throughline.assistant.describe()`:
  `{provider, model, loopback, default_tier, tiers, configured}` (section
  "The query helper" below); `{provider: "none", ...}` when it is disabled.
- `token.can_write` is true for an unrestricted owner token: the only kind
  that registers channels and ingests records from the interface.

## The interface's documents: /v1/web/state

```
GET    /v1/web/state/{kind}          -> [{key, value, updated_at}]   (ordered by key)
GET    /v1/web/state/{kind}/{key}    -> {key, value, updated_at} | 404
PUT    /v1/web/state/{kind}/{key}    {value: <json>} -> {key, value, updated_at}
DELETE /v1/web/state/{kind}/{key}    -> {deleted: bool}
```

Stored in `tl.web_state(namespace, kind, key, value, updated_at, token_id)`.
Kinds: exactly the ones the interface uses, `prefs`, `colors`, `overrides`,
`saved_query`, `board`, `import_mapping` (`store.KINDS`; a test keeps it equal
to `UiStateKind` of `web/src/api/types.ts`). Any other kind is 400; a new
kind is added to both lists.

- Namespace, derived from the token and the request's scope, never from the
  request:
  - `''`: an **owner** token with an unrestricted scope. This is the owner's
    interface, the same on every device and with every owner token.
  - `token:<id>`: any other unrestricted token (an agent token with full
    read). It reads and writes only its own documents, so it can never
    replace a saved query or a board that later runs in the owner's browser.
    This matches bootstrap's `token.can_write` (unrestricted and kind owner).
  - `asker:<name>`: a restricted asker, whichever token names it; it never
    sees another namespace.
- Limits: key 1..200 characters without `/` or control characters; value not
  `null`, at most 512 KiB of JSON (413); at most 2000 documents per namespace
  and kind (400); at most 8 MiB of documents per namespace in all
  (`MAX_NAMESPACE_BYTES`, counted as the stored JSON text; a PUT that would
  pass it is 413). The total also bounds what `GET /v1/web/state/{kind}`
  returns, values included.
- These routes are the caller's own working state (`own_working_state`), like
  result sets; PUT and DELETE are in the `EXEMPT` list of
  `tests/test_v11_security_matrix.py`. A PUT replaces the document; there is
  no compare-and-set (last writer wins).
- Content. A document is what the user typed or arranged: a title, SQL text,
  a layout, a column mapping. The server never copies record content into a
  document, and the interface must not store result rows or excerpts there.
  That is why the table has **no purge hook**: it holds no text derived from
  messages and is not keyed by channel. A saved query naming a purged channel
  simply returns nothing. The audit row of a state request holds method and
  path only (the key is in the path; the value never is).
- The table is empty after migration, so the per-test truncate needs no
  keep-list entry and no seed hook.

## GET /v1/web/arrivals

What was ingested since a cursor. A plain request answered with JSON; the
server may hold it for up to 25 s (long poll). No server-sent events, no
WebSocket: the "no push routes" rule of the core (INT-11) stands.

```
GET /v1/web/arrivals?after=<cursor>&wait=<0..25>&limit=<1..500, default 200>&channels=<csv>
-> { cursor, rows: [{id, channel, thread_id, happened_at, ingested_at, author, person_id,
                     excerpt, version, settled}],
     counts: {<channel>: n}, sampled, plugins: [...], reads: [...], horizon, settle_s }
```

- Unrestricted scopes only (it reads `tl.messages` below the gate); a
  restricted asker and a plugin token get 403.
- Without `after` the answer is immediate, whatever `wait` says: the newest
  `limit` records (oldest first), every plugin row, the last 20 reads, an
  empty `counts`, and the cursor to continue from.
- With `after` the request is answered as soon as a record, a changed plugin
  row or a read appears, or after `wait` seconds with empty lists. Always use
  the returned cursor for the next request.
- `rows` are in arrival (id) order. `excerpt` is the first 280 characters of
  the content. A new version of a record is an arrival of its own (`version`).
- `counts` is exact for everything this answer covers. `sampled` is true when
  more records arrived than `limit`: `rows` is then an even sample of them
  that includes the newest. One answer covers at most 5000 records; a larger
  backlog continues with the next request, which returns at once.
- `channels` restricts `rows` and `counts` to those channels; the cursor
  still moves over everything.
- `plugins`: rows of `tl.plugins` changed since the cursor, in the bootstrap
  shape. `reads`: `{at, token, asker, statement, row_count, status}` from
  `tl.audit_log` for owner tokens only (an agent gets `[]`): requests that
  answered rows, with statement text cut to 300 characters; rows tagged
  `client=web`, all `/v1/web` requests and plugin traffic are left out.
- `horizon` is `api.consumer_horizon()` and `settle_s` its distance from now:
  a row is `settled` when `ingested_at <= horizon`, which is exactly what
  consumers (`api.news`) can already see. A client can flip a row to settled
  itself once `ingested_at + settle_s` has passed.
- At most `THROUGHLINE_WEB_WAITERS` requests wait at a time; one more is
  answered 429 with `Retry-After: 2`. A request that does not need to wait is
  never refused.
- No database connection is held while waiting: the handler is asynchronous,
  looks every 0.75 s in one short app-role transaction on a worker thread and
  sleeps in between.
- An invalid cursor is 400 `invalid cursor`. A cursor from another database
  (its watermark is beyond the newest record) starts over like a request
  without `after`.

### Why the cursor is exact

Transactions commit out of order: a record with a lower id, or an earlier
`ingested_at`, can become visible after a higher one was delivered. A cursor
that is one position would lose it. The cursor (opaque to the client) holds

- `w`, a watermark: every record with `id <= w` has been delivered;
- `seen`, the ids above `w` already delivered, as ranges (a bulk load is one
  range);
- the last audit row and plugin change looked at.

Each look reads the ids above `w` that are not in `seen` (the gaps), so a
late commit is delivered when it appears, exactly once. This is the sweep over
the unsettled region.

The watermark only moves over ids that can no longer appear. Ingest
transactions must commit within the settle window S of their start, and
`ingested_at` is that start (`docs/v11/consumers.md`). If a visible record R
has `ingested_at <= now - 2S`, every lower id was taken from the sequence
before R's, by a transaction that started before R committed (at most
`ingested_at(R) + S`) and committed within S itself: before now. So every id
at or below R's is final and `w` moves there. `seen` therefore spans at most
the last 2S (four minutes by default). It is capped at 128 ranges; if arrivals
are more fragmented than that, the oldest ranges fold into the watermark (the
gaps between them are given up), so nothing already delivered is forgotten or
delivered again. A look reads at most 5000 ids. The answer without a cursor
looks only at the newest 5000 ids above the watermark (a backward walk of the
primary key); when more are visible, the watermark starts just below that
window.

## POST /v1/sql additions

Both are additive; `query.query()` still returns exactly `{rows, truncated,
row_count}` and `query.query_ex()` its four keys.

- `columns: [{name, type}]` in the response: the output columns in order with
  PostgreSQL type names (`int8`, `text`, `timestamptz`, `jsonb`, `_int8` for
  an array). Present for a result without rows; duplicate output names are
  kept (in `rows`, which are objects, the last one wins).
- Request key `validate_only: true` answers
  `{valid, error, code, columns}` and produces no row. The statement passes
  the same validator, then the database plans it under the caller's scope
  with a zero-row limit, so unknown names and type errors are found and the
  columns are known. `error` is the validator's message, or
  `database operation rejected` with the SQLSTATE in `code` (database
  messages are never exposed). Statements with semantic calls use their own
  dry run. The request is audited with the statement and
  `detail.validate_only`.

Keys that do not belong to an answer are left out (a run has no `valid`; a
validation has no `rows`).

## SQL allow-list additions

Added to `query.BUILTINS`, unqualified names only. The rule for admission:
immutable or stable, no side effect, no access to files, the network,
settings or catalogues, and output bounded by the input. Everything remains
under the row cap, the 4 MiB response budget, the 8 MiB row limit and the
statement timeout.

| Class | Names | Why it is safe |
|---|---|---|
| Binning, arithmetic | `floor`, `ceil`, `ceiling`, `width_bucket`, `date_bin`, `ln`, `log`, `sqrt`, `power`, `exp`, `sign`, `mod` | Pure functions of their arguments; one value out. Overflow or a domain error is an ordinary error of the statement. |
| Aggregates | `percentile_cont`, `percentile_disc`, `mode`, `stddev_samp`, `stddev_pop`, `var_samp`, `corr`, `string_agg`, `bool_or`, `bool_and` | Fold rows the statement can already read. `string_agg` grows like the already allowed `array_agg` and `jsonb_agg` and meets the same row limit. |
| Window | `lag`, `lead`, `first_value`, `last_value`, `nth_value`, `ntile`, `percent_rank`, `cume_dist` | Read other rows of the same result only. |
| Inspection | `split_part`, `concat`, `char_length`, `cardinality`, `array_length`, `jsonb_typeof`, `jsonb_array_length` | Pure; output no larger than the input. |
| Time | `to_timestamp`, `age`, `justify_interval`, `make_interval`, `isfinite` | Pure or stable (time zone, current date). |
| Set-returning | `jsonb_array_elements`, `jsonb_array_elements_text`, `jsonb_object_keys`, `unnest` | One row per element of a value the statement already holds; bounded like `generate_series` by row cap and timeout. |

How the validator keeps them from carrying anything else:

- The walk visits every node of the parse tree. Arguments, `WITHIN GROUP
  (ORDER BY ...)`, `FILTER (WHERE ...)`, `OVER (...)` and named `WINDOW`
  definitions are ordinary sub-trees, so a call inside them is checked
  against the same list (`percentile_cont(0.5) WITHIN GROUP (ORDER BY
  pg_sleep(1))` is refused).
- Schema-qualified spellings (`pg_catalog.floor`) stay refused; only the
  SQL-syntax functions the parser itself qualifies are accepted that way.
- In FROM, PostgreSQL resolves `alias.name` to `name(alias)` when the alias
  has no such column, which would call any one-argument function. The
  existing rule covers the new set-returning functions: on such an item
  `alias.name` is accepted only for the item's own column. For
  `jsonb_array_elements` and `jsonb_array_elements_text` that column is
  `value` (their OUT parameter), unless the statement declares column
  aliases.
- `age(xid)` would read the transaction counter. No `api` column has that
  type, an untyped literal is ambiguous for `age`, and the type names `xid`
  and `xid8` are now refused in casts.

Deliberately not added: `regexp_*` (pattern matching with unbounded
backtracking), `repeat`, `lpad`, `rpad` (output unrelated to input size),
`current_setting`, `set_config`, `version` and every `pg_*` function.

## Indexes

No index was added. Measured on the synthetic development corpus (186k
records): keyset browsing of the 120k-record channel view
(`ORDER BY happened_at DESC, id DESC LIMIT 100`) takes about 240 ms without and
220 ms with `tl.messages(channel_id, happened_at DESC, id DESC) WHERE
superseded_by IS NULL`. The channel views join the channel by name, so the
planner cannot use the index order and keeps its top-N sort either way; the
gain did not justify the write cost. A 9k-record channel takes about 20 ms.
The arrival tail runs on the primary key and the existing `messages_arrival`
index. The cost of a page follows the number of records the predicate leaves,
so a page bounded to a narrow `happened_at` window (which `messages_happened`
serves) is cheap; an open-ended keyset page over a very large channel is not.

## The query helper

The contract is [assistant.md](assistant.md); this is how the server keeps it.

| Piece | Where |
|---|---|
| Routes | `src/throughline/api/routes/web_assistant.py` |
| Registry and loop | `src/throughline/assistant/runs.py` |
| Tools and disclosure tiers | `src/throughline/assistant/tools.py` |
| Catalogue under the scope | `src/throughline/assistant/catalog.py` |
| System prompt | `src/throughline/assistant/prompt.py`; lens catalogue in `assistant/lenses.py` |
| Local helper, rule table | `src/throughline/assistant/local.py`, `assistant/sqlfix.py` |
| Model providers | `src/throughline/providers/assistant.py` (kind `assistant`), `LocalAssistant` in `providers/local.py` |
| Migration | `src/throughline/migrations/0061_assistant.sql` |
| Tests | `tests/test_web_assistant.py` |

**A provider kind.** `assistant` is the fifth kind of `providers.KINDS`
(`THROUGHLINE_ASSISTANT_PROVIDER` = `local` | `anthropic` | `openai` (alias
`http`) | `none`; default `local`). Its interface is
`Assistant.turn(system, tools, messages) -> (reply, Usage)` in a
provider-neutral message form; the remote implementations send through
`providers.http.request` (egress allow-list first, no proxies, no
redirects, bodies never echoed). Migration 0061 adds the kind to
`tl.provider_state` (so `api.describe('egress')` shows whether the helper's
model leaves the host) and `assistant` to the origins of `tl.model_ledger`.
`sync_state` writes each kind in its own savepoint, so a database without
0061 still records the other four.

- `anthropic`: `POST {base}/v1/messages`, `anthropic-version: 2026-10-21`,
  `x-api-key`, client tools with `tool_choice: auto`, adaptive thinking,
  `output_config.effort` (default `medium`), prompt caching (a breakpoint
  after the system prompt, so tools and system are cached, and top-level
  automatic caching for the growing conversation). The assistant content of
  every turn, thinking blocks included, is passed back unchanged. On the
  models that accept it the server-side refusal fallback is on
  (`fallbacks: "default"`). Stop reasons: `tool_use` runs the tools, `refusal`
  ends the run with an answer that says so (and the category), `max_tokens`
  ends it with a note, `pause_turn` is resent (twice at most). Cost is
  computed from `usage` (input, cache writes at 1.25x, cache reads, output)
  with a built-in price table (`claude-opus-5-5`: 4 / 20 USD per million,
  cache reads 0.20); unknown models are priced like the most expensive.
- `openai`: `POST {base}/chat/completions` with function tools; `tool_calls`,
  `finish_reason` `length` and `content_filter` / `refusal` are mapped the
  same way. Prices from `THROUGHLINE_ASSISTANT_PRICE_IN/_OUT` (default 0).
- `local`: no model. `providers.get('assistant')` is `LocalAssistant`
  (`scripted`), and the loop answers with `assistant/local.py`.

**Who reads what.** Every tool runs under the scope of the request that
started the run (`scope_of(token)`, captured at POST time) and through
`deps.audited` on a copy of the token row: the same validator, reader role,
asker gate, caps, statement timeout and (for semantic SQL) cost gate as
`POST /v1/sql`. A restricted asker's helper sees its channels only, in the
prompt too (channel views of hidden channels are left out of the
catalogue). The final statement passes the `validate_only` path; an invalid
one is returned to the model once with the error, then delivered with the
error in `notes`.

**Disclosure tiers.** `schema`: names, types, counts and errors; run_sql
returns columns, row count and errors with `rows: "<n> rows withheld (schema
tier)"`; `profile` returns record and non-null counts per field only (no
time range, no ranges, no top values); `recall` returns hit counts per
channel; `describe('channel:...')` leaves out the time range and the
plugin's maintenance block. `samples`: additionally at most 20 rows with
cells clipped to 200 characters, numeric ranges, top text values clipped to
60 characters.

Sensitivity labels apply to every model that is not known to run on this
machine: the local helper is, and a loopback endpoint is only when the owner
sets `THROUGHLINE_ASSISTANT_MODEL_IS_LOCAL=1` (a loopback endpoint may be a
gateway that forwards to a vendor). When labels apply and any label keeps
content on the machine (`tl.labels.egress`), the rows of a `run_sql`
statement are withheld whole (`rows: []` with a note, also in `sent`: a
column the statement calls `id` proves nothing about the record), a profile
has counts and the time range only (no numeric ranges, no top values), and
`recall`, which builds its rows from real record ids, replaces the hits of
held messages by `{id, withheld: "egress_held"}`.

Database errors never carry a row value to the model (`tools.database_error`):
a class 42 message (syntax, names, privileges) is passed through `scrub`,
which keeps only a double-quoted plain name whose every part is a word of the
statement and turns everything from the first other quote character to the
last into `<value>` (any length, any number of lines); any other SQLSTATE
class (22 data exceptions, P0 raised messages, ...) is reduced to its code
and a fixed description of the class. The validator's own messages pass the
same scrub. `describe` forwards only subjects of the index (`functions`,
`function:<name>`, `recipe:<name>`, static sections, computed sections); at
the `schema` tier the computed sections other than `operators` and `recipes`
(`coverage`, `access`, `egress`: time ranges, windows, plugin errors, grants)
are refused, like a channel's time range.

Every step event's `sent` says what was sent; the local helper's say
`nothing: the local helper sends no data anywhere`. Default tier: `samples`
for the local helper (or a model declared local), `schema` for every other
model, loopback included. A loopback endpoint may be asked for `samples` per
run; a non-loopback one is refused 400 unless
`THROUGHLINE_ASSISTANT_TIER=samples` (which also makes `samples` the
default). A vendor's conventional key variable (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`) is used only for that vendor's own host over https.

**Bounds.** Tool steps (`THROUGHLINE_ASSISTANT_MAX_STEPS`, default 8), a
wall clock (`THROUGHLINE_ASSISTANT_DEADLINE_S`, default 300) and spend. A run
may spend at most the lower of `THROUGHLINE_ASSISTANT_MAX_USD` (default
0.50) and the token's `max_model_cost` (default 0.10): the token's limit is
per request, and a run is one request, so it holds for the whole run. A
token's runs may spend at most `THROUGHLINE_ASSISTANT_DAILY_USD` (default
5.00) in any 24 hours, summed from `tl.model_ledger` (origin `assistant`).
Before each turn the output cap (`max_tokens`) is lowered so that the turn's
worst case, its input estimate plus `max_tokens` output tokens at the output
price, fits what is left of both; a turn that cannot afford 1024 output
tokens is not sent. That worst case is reserved on the run's `BudgetTracker`
(the unspent part is released after the call) and passes `cost.authorize`
with the remaining budget as the caller limit. Reaching any bound ends the run
with `status: done` and a final answer that says which bound, carrying the
last statement that ran without an error.

**Runs.** In process memory (`runs._runs`), 30 minutes, keyed by token id
and asker; another token or asker gets 404. At most 4 active runs per token
and 24 in the process (429, `Retry-After: 5`); a run is active until its
worker thread returns, so a cancelled run keeps its slot until its current
model call or statement ends. Loops run on a pool of 8 worker threads (the
process bound keeps its queue at 16). Ended runs are kept at most 50 per token
and 500 in all (oldest dropped first), each for 30 minutes at most. Only the
worker writes a run's trace (cancel never does), so the two never race. Every
statement a tool runs leaves its own `tl.audit_log` row (the statement, its
row count, status 200/400/402, detail `client: web-assistant` and the run id),
like `POST /v1/sql`, so the reads lane of arrivals shows it. `GET`
is an `async` handler that waits on an `asyncio.Event` set from the worker
thread with `call_soon_threadsafe` (no busy loop, no thread and no database
connection held while waiting; at most 64 waiters per run, more answer at
once). Events: `step` (`tool`, `input` or `sql`/`row_count`/`columns`/
`error`, `summary`, `sent`, `ms`, `is_error` when a tool failed), `text`
(the model's text), and one terminal `final` | `cancelled` | `failed`. The
answer carries `trace_id` (additive): the run's statements and model calls
are in `tl.traces` like any multi-statement read; nothing else of a run is
stored. Audit rows hold `run`, `verb`, `tier` and event counts only.

**Routes and the security tests.** Both POST routes are in `EXEMPT` of
`tests/test_v11_security_matrix.py` (a reader-path query; cancelling one's
own run). Handlers call `require(token, "owner", "agent")` first (plugin
tokens 403) and open no app-role connection. `{}` is 422; `wait` above 25 is
422; path value `0` is 404.

**The view spec.** `final.spec` follows `VIEW_SPEC_SCHEMA` of
`web/src/lens/types.ts` exactly (`v: 1`, no `$schema` key: the schema has
`additionalProperties: false`), with `provenance: {by: "agent", sampled:
false, statement: "as_is"}`. Lenses and encode channels are checked against
`assistant/lenses.py`, which mirrors `LENS_CATALOG`; a test fails when the
front end's lens ids change.

