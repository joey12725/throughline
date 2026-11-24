# Feeds

Migration `0046_coverage.sql`, module `throughline.feeds`, routes in
`api/routes/feeds.py`, command `throughline lib feed`.

A feed is a named cursor over arrivals for your token and asker. It is
your own working state: restricted askers may use feeds, and what they
read passes the database gate (rows and counts of granted channels only).

## Arrival-keyed

The cursor is `(ingested_at, message_id)`, never message time. `ingested_at`
is the start of the ingesting transaction. A late backfill of old messages
therefore arrives after the cursor and is delivered once.

- `api.news(name, max_results)` returns a page of arrivals in arrival order,
  not a population: rows of `api.record_versions` after the cursor and at or
  before the horizon, with `is_new_version` and a `cursor` per row (at most
  5,000). It reads `tl.messages` through the index `messages_arrival`.
- `api.pending(name)` counts what is settled and not marked.
- An unknown name, and another token's or asker's feed, return nothing
  in SQL and the same 404 over HTTP.

Scope keys: `channels`, `kinds` (any record kind; default `life_event` and
`derived`), `thread_ids`, `persons` (attributed author), `include_ignored`
(default false: records annotations mark ignored are passed over) and
`include_versions` (default true: a new version of an earlier record is
delivered with `is_new_version`).

## Horizon and settle window

`api.consumer_horizon()` is `now() - settle_s`; news delivers only rows with
`ingested_at` at or before it.

The settle window is a database guarantee: a deferred constraint trigger on
`tl.messages` refuses at commit any transaction that inserts messages and has
run longer than `settle_s - margin_s` ("ingest transaction exceeded the
consumer settle window; retry with a smaller batch", SQLSTATE 54000). It
applies to every role: the app role, the owner CLI, registry imports and
reparse. So when a reader at time r sees no row with `ingested_at <= r -
settle_s`, none will ever commit, and a cursor that stops at the horizon never
skips a late commit. `margin_s` covers the time between the check and the
commit becoming visible.

Settings live in `tl.consumer_settings` (`settle_s` default 120, `margin_s`
default 15; `margin_s >= 0`, `settle_s > margin_s`, `settle_s >= 2`).
`GET /v1/feeds/settings` shows them; `PUT` is for owner tokens only.

### Exemption: bulk restore

The only exemption is `throughline.bulk_restore = on`. `throughline restore`
runs `pg_restore` with `PGOPTIONS=-c throughline.bulk_restore=on`: restored
rows keep their original `ingested_at` and feed state is restored with
them, so cursors stay consistent. A migration that inserts messages must
`SET LOCAL throughline.bulk_restore = on`.

### Why reparse batches

`reparse` re-maps a whole channel. In one transaction a large channel would
exceed the settle window and be refused, so it commits every
`THROUGHLINE_REPARSE_BATCH` (default 500) records in its own transaction, with
the same mapping and per-item results.

## Mark after work

1. `GET /v1/feeds/{name}/news?max=` (or `SELECT * FROM api.news(...)`).
2. Do the work.
3. `POST /v1/feeds/{name}/mark {to: <last row's cursor>, expected?:
   <previous cursor>}`.

Delivery is at least once: a crash before marking redelivers the same rows.
Marking is exactly once: mark is one transaction with compare-and-set on the
previous position (`expected`, when given, must match: else 409); a `to` at or
before the current position is a no-op that returns the same answer
`{seen_to, marked, pending_after}`; a `to` beyond the horizon is 409.

## HTTP and CLI

`POST /v1/feeds {name, scope?, start: now | beginning | timestamp}`,
`GET /v1/feeds`, `GET /v1/feeds/{name}/news?max=`,
`GET /v1/feeds/{name}/pending`, `POST /v1/feeds/{name}/mark`,
`DELETE /v1/feeds/{name}`, `GET|PUT /v1/feeds/settings`. News also
returns `execution.coverage` for the feed's channels.

`throughline lib feed create|list|news|pending|mark|drop|settings`.

## Former name: consumers

Feeds were called consumers. Every `/v1/consumers...` route is kept as a
deprecated alias with identical behaviour (hidden from the OpenAPI document),
`throughline lib consumer ...` is a hidden alias of `throughline lib feed`,
and `throughline.consumers` is an alias of `throughline.feeds`. Responses keep
their keys and add the new one beside them: news and pending carry both
`consumer` and `feed`, the list carries both `consumers` and `feeds`. Storage
and SQL keep the old word: `tl.consumers`, `tl.consumer_settings`,
`api.consumer_horizon()`, the `consumer` column of `api.pending`, receipt
kinds `consumer`, `consumer_cursor` and `consumer_settings`, and the
settle-window error message.
