# Core architecture and HTTP contract

Throughline is a Python 3.12 uv project with a `src/throughline` package.
FastAPI serves `/v1`; Typer provides `throughline`. PostgreSQL is the only store.
No core module imports a plugin or SDK. Independent packages live in `plugins/`
and `sdk/`, and communicate exclusively through HTTP. Neither directory is
required to build or test the core. Models are optional; without configured
model providers nothing leaves the host.

> **Contents:** [Configuration and roles](#configuration-and-roles) · [Tables](#tables) · [HTTP shapes](#http-shapes) · [CLI and backups](#cli-and-backups) · [Scope and remaining work](#scope-and-remaining-work) · [M3 conversations and optional embeddings](#m3-conversations-and-optional-embeddings) · [v1.1 agent retrieval (waves 1-4)](#v11-agent-retrieval-waves-1-4) · [v1.1 waves 5-6](#v11-waves-5-6-people-writes-transcripts-commitments-disclosure-ops) · [Agent retrieval, embedding vendors and source health](#agent-retrieval-embedding-vendors-and-source-health-migrations-0054-0061)
>
> For a quick overview see [README.md](../README.md), [docs/README.md](README.md), [docs/agent-retrieval.md](agent-retrieval.md) and [docs/configuration.md](configuration.md).

## Configuration and roles

| Variable | Meaning |
|---|---|
| THROUGHLINE_DATABASE_URL | Owner connection for migrations, tokens, purge and backups; connection coordinates for runtime roles |
| THROUGHLINE_APP_PASSWORD | App role password. Required; no default exists. Unset only with `THROUGHLINE_DEV=1` against a trust-authenticated local server |
| THROUGHLINE_READER_PASSWORD | Reader role password. Required; no default exists. Unset only with `THROUGHLINE_DEV=1` against a trust-authenticated local server |
| THROUGHLINE_URL | CLI HTTP base URL |
| THROUGHLINE_TOKEN | CLI bearer token |
| THROUGHLINE_PG_BIN | Optional directory containing PostgreSQL backup executables |
| THROUGHLINE_TEST_DATABASE_URL | Local test administrative `postgres` connection |

Migrations are packaged SQL files sorted by filename, with each file and its
`tl.schema_migrations` record committed together. An advisory lock serializes
migration runners. Applied files are skipped. Roles are cluster-wide; use a
separate PostgreSQL cluster per independent production deployment.

The API connects as `throughline_app`, never the owner. It may insert ground
truth and update only message bookkeeping columns. It has no DELETE grants.
Triggers additionally protect messages, payloads and track points, including
when the owner connects without explicitly enabling purge. Purge requires both
a session setting and an owner role: app and reader sessions cannot use the
setting to bypass triggers. A narrowly scoped security-definer function creates
channel views, quoting every identifier and literal and fixing its search path.

SQL requests use a separate `throughline_reader` connection with read-only
transactions and a 30-second statement timeout. A PostgreSQL AST validator
allows one SELECT, explicitly qualified `api` relations and an allow-list of
query/aggregate functions. This deliberately supports a subset of PostgreSQL:
unqualified relations other than the statement's own WITH names, arbitrary functions, schema-qualified
operators, regclass casts, row locks and SELECT INTO are rejected. Results are
capped at 1,000 rows by default (maximum 10,000), with `truncated` reported.
Every accepted or rejected SQL execution attempt by an authorized query token
records token, asker, original statement and returned row count; failed queries
record zero. Database errors return a SQLSTATE without leaking row contents.

**Asker access control** is enforced in PostgreSQL through security-barrier
views keyed to transaction-local settings (not PostgreSQL row-level security),
combining channel and thread grants with sensitivity labels and expiring
grants; see [Access gate and scope modes](#access-gate-and-scope-modes) and
[Disclosure](#disclosure). See [Scope and remaining work](#scope-and-remaining-work)
for the hardening items still deferred before exposing the API to untrusted
public users.

## Tables

All internal tables are in `tl`; IDs are bigint identities unless noted.

| Table | Principal columns and constraints |
|---|---|
| schema_migrations | version filename primary key, applied_at |
| channels | name unique, plugin, description, fields JSON, is_reference, is_outside, conversation_gap_s, location_tolerance_s, created_at |
| threads | channel_id, external_id (unique together), title, metadata |
| messages | channel_id, external_id, version (unique together), superseded_by, thread_id, thread_no, happened_at, happened_offset_min, ended_at, ingested_at, author_handle, author_identifier_id, person_id, content, images, fields, lat, lon, loc_accuracy_m, loc_method, loc_reason, access_label, payload_hash, generated tsv |
| raw_payloads | message_id primary key, untouched JSON payload |
| location_track | at, lat, lon, accuracy_m, source; unique(source,at) |
| people | handle unique, display_name, is_owner (partial unique), status, note, merged_into, created_at |
| person_names | person_id, name |
| identifiers | person_id nullable, system, kind, value, value_norm, status, label, asserted_by, evidence, created_at; unique(system,kind,value_norm) |
| identifier_schemes | system, kind primary key; same_system, same_kind, normalizer |
| fact_keys | key primary key |
| facts | person_id, key foreign key to allow-list, value JSON, asserted_by |
| relation_kinds | kind primary key, inverse |
| relations | from_id, to_id, kind, asserted_by |
| identity_events | operation, asserted_by, detail JSON, at |
| entities | kind, name, external_ref unique within kind, is_placeholder, merged_into, metadata |
| links | type, from_kind/from_id, to_kind/to_id, confidence, method, evidence, job, job_version, status, created_at |
| plugins | name primary key, manifest, interval_s, last_success_at, last_error, messages_ingested, updated_at; since 0057 last_attempt_at, last_error_at, consecutive_failures, paused_at, paused_reason, alerted_state (docs/v11/sources.md) |
| source_outages | plugin, kind (failing, silent, stalled), last_good_at, detected_at, first/last error, failures, restored_at, caught_up_messages, caught_up_to, catch_up, catch_up_reasons; one open row per plugin |
| channel_pauses | channel_id primary key, reason, paused_at: channels whose silence never stalls their source |
| plugin_cursors | plugin/key primary key, cursor JSON, updated_at |
| tokens | name, token_hash, kind, plugin, askers, revoked_at |
| audit_log | token_id, asker, statement, row_count, http_status, detail, at |

Message content, payload and timestamps are immutable. A changed canonical
normalized message (including raw payload) creates a new version and points the
old version's `superseded_by` at it. The hash is SHA-256 of sorted, compact UTF-8
JSON. Source offsets remain in `happened_offset_min`; PostgreSQL stores instants
as timestamptz. Bookkeeping updates include thread numbers, identity resolution
and location attachment. Current versions alone participate in numbering.

Ingest validates every item independently before writing. Invalid items are
reported and omitted; all valid items and batch links commit in one transaction.
An infrastructure or link failure rolls that entire transaction back. A COPY
staging table and set-based inserts keep large imports fast. Channel locks are
acquired in ID order, then per-thread locks in ID order. Numbering uses one
window-function UPDATE across affected threads, appending after existing numbers
when every new message sorts after the current maximum; backfills and edits
renumber affected threads, including a prior thread when an edit moves threads. This stronger serialization
also protects concurrent versions of the same external ID.

Known identifiers resolve in code. Unknown authors create unclaimed identifiers.
The hot message table intentionally omits identity foreign-key triggers: both
identity references are assigned exclusively from registry lookup results, and
HTTP cannot delete registry rows. This keeps cached identity resolution within
the 10% ingest overhead target; registry-internal foreign keys remain enforced.
Direct owner SQL must preserve those two references.
Phone normalization removes nondigits, email normalization lowercases, and
schemes can remap (system,kind) to another namespace. Attaching an identifier
bulk-resolves earlier messages. Attach refuses reassignment across people.
Owner assertions override agent assertions. Merge stores complete snapshots;
split restores the registry rows and identifiers' message associations. Audit
history intentionally grows during undo. Split should be used before unrelated
subsequent edits to the merged people: conflict-aware undo is not implemented.

Source locations win; otherwise nearest track time within channel tolerance
wins, with deterministic ID tie-breaking. Reference channels never take
locations. Track ingestion fills empty locations and replaces a track-derived
location when a nearer point arrives. Stop detection partitions by source,
groups consecutive points within a radius of the group's running centroid, and
breaks a stay when the next point is outside the radius or follows a silence
longer than the visit maximum gap (`THROUGHLINE_VISIT_MAX_GAP_S`, default 43200
seconds, or `max_gap_s` on the request). The minimum stay is only the shortest
duration that counts as a visit; it does not limit the interval between samples.
Visits are idempotent and carry the centroid; see "Visits and withdrawn track
points" below. Place entities use metadata `{lat,lon}`; nearby visits receive
`at_place` links. The geographic index uses a fixed-search-path earth wrapper
so pg_restore can rebuild it under its restricted search path.
Migration 0007 uses positional arguments in geographic queries to prevent
column names from shadowing the requested center coordinates.

## HTTP shapes

Every route requires `Authorization: Bearer <token>`, including OpenAPI.
`X-Throughline-Asker` is optional; non-owner values must be allowed by the token.
Errors use `{detail: ...}`; database rejections additionally carry `code`.
Missing/invalid request structure returns 422. Scope failures return 403 and
missing/revoked tokens return 401. Tokens are created/revoked only on the box.

Permissions: O = owner, A = agent, P = plugin scoped to its own plugin name.
All token kinds may read the channel registry and create derived entities/links.
Agents can edit identity and run queries, but cannot ingest ground truth.

### Channels and messages

`POST /v1/channels` (O/P) registers or refreshes:

```json
{"name":"synthetic.chat","plugin":"synthetic","description":"Synthetic chat","fields":[{"name":"rating","type":"integer","description":"Synthetic score"}],"is_reference":false,"is_outside":false,"conversation_gap_s":1800,"location_tolerance_s":600}
```

Returns the normalized channel object. `GET /v1/channels` (O/A/P) returns an
array including IDs and creation times. Channel names follow
`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`; typed views replace dots with underscores.
Colliding view names are rejected. Fields allow text, integer, number, boolean,
timestamp and json. Missing and null fields are allowed, undeclared fields and
incorrect non-null types are rejected.

`POST /v1/ingest/messages` (O/P):

```json
{"plugin":"synthetic","dry_run":false,"messages":[{"channel":"synthetic.chat","external_id":"fake-1","thread":{"external_id":"fake-thread","title":"Synthetic thread"},"happened_at":"2020-01-01T12:00:00+02:00","ended_at":null,"author":{"system":"fake","kind":"account","value":"fake-account","display_name":"Synthetic author"},"content":"Synthetic orchard","fields":{"rating":1},"images":[],"location":{"lat":1,"lon":2,"accuracy_m":5},"access_label":null,"payload":{"synthetic":true}}],"links":[]}
```

Maximum 5,000 items. Thread, channel, external ID, timestamp, content and payload
are required. Timestamp must carry an offset, content must remain nonempty after
trimming; end cannot precede start. Author, images, fields and location are
optional. Returns
`{received,inserted,new_versions,unchanged,rejected:[{index,external_id,error}]}`.
Dry runs execute the same path then roll back data writes (sequence values may
advance). IDs and ingestion times are not dry-run promises.

`POST /v1/ingest/track` (O/P) accepts
`{points:[{at,lat,lon,accuracy_m?,source}]}`, maximum 10,000 points, returns
`{received,attached}`. Plugin track sources must equal token plugin scope.

`POST /v1/track/detect-visits?radius_m=100&min_stay_s=600&max_gap_s=43200` (O/A)
returns ingest counts for the native `visits` channel plus `retired`. All three
parameters are optional and must be positive; `max_gap_s` defaults to
`THROUGHLINE_VISIT_MAX_GAP_S`, else 43200.

`POST /v1/reparse` (O) accepts `{channel}`, returns ingest counts. Server code
registers a local callable using `throughline.reparse.register_mapper(channel,
mapper)`. It maps a stored payload to normalized message overrides; reparsing
creates immutable versions without source calls. No arbitrary import path or
network parser is accepted over HTTP. No mappers ship in this core milestone.

### Links and entities

`POST /v1/links` (O/A/P), also usable as an ingest-batch link:

```json
{"type":"part_of","from":{"channel":"synthetic.chat","external_id":"fake-1"},"to":{"kind":"work","external_ref":"fake-work","name":"Synthetic work"},"confidence":1,"method":"source","evidence":{},"job":null,"job_version":null}
```

Ends may instead be `{kind:"message"|"entity"|"person"|"page"|"table_row",id}`.
Exact repeated active link assertions return the existing record; a transaction
advisory lock prevents duplicate concurrent retries after an ambiguous response.
Unmatched external entities become placeholders. Returns a stored link object.
`PATCH /v1/links/{id}` updates type, confidence, method, evidence or status
(`active`/`removed`). `POST /v1/entities` accepts
`{kind,name?,external_ref?,is_placeholder?,metadata?}` and returns the entity.
`POST /v1/entities/merge` accepts `{source,target}`, moves every link and returns
`{ok:true}`. New entity kinds require no migration. Recipient/presence query
semantics use active links of type `author`, `recipient`, `participant`, `present`
between a message and a person, in either direction.

### Plugins

| Route (O/P) | Request | Response |
|---|---|---|
| PUT /v1/plugins/{name}/cursor/{key} | JSON object, opaque cursor | `{plugin,key,cursor,updated_at}` |
| GET /v1/plugins/{name}/cursor/{key} | none | Stored cursor row; 404 if absent |
| POST /v1/plugins/{name}/manifest | Opaque manifest object | Plugin row |
| POST /v1/plugins/{name}/heartbeat | `{interval_s?:3600,error?:null,messages_ingested?:0}` | Plugin row; messages_ingested is a delta |

A successful heartbeat updates last success; an error preserves last success.
Status reports stale when no successful sync exists or two intervals have passed.
Cursors are durable independently of a batch: advance them only after ingest
succeeds. Retrying the batch after a crash is safe.

### Query routes (O/A)

| Route | Request | Response |
|---|---|---|
| POST /v1/sql | `{statement,row_cap?:1000}` | `{rows:[...],row_count,truncated}` |
| GET /v1/catalog | none | Catalogue JSON document |
| GET /v1/context | message_id, before=3, after=3 | Query response |
| GET /v1/search | q, max_results=20 | Query response; flat message columns plus score |
| GET /v1/person | q | Query response of people |
| GET /v1/status | none | `{plugins:[...stale],channels:[{name,count,last_ingest}]}` |
| GET /v1/openapi.json | none (all token kinds) | Generated OpenAPI 3.1 document |

Stable `api` views: messages, message_versions, threads, channels, people,
identifiers, person_names, facts, relations, entities, links, places, visits,
location_track, plugins, search_results; plus `ch_<channel>` typed views.
`messages` joins channel name/flags, thread external ID/title and resolved
person handle/display name. `relations` exposes both the stored kind and inverse.

SQL functions: `context(id,before=3,after=3)`, `search(query,max_results=20)`,
`search_all(query,max_results=20)`, `near(lat,lon,meters)`,
`near_place(name,meters)`, `overlapping(from,to)`, `during(message_id)`,
`person(text)`, `person_messages(person_id)`,
`follow(kind,id,link_type=NULL,depth=1)`, `catalog()`.
Context includes the anchor message. Time intervals are closed and use ended_at
when available. Follow is bidirectional, active-only and cycle-safe. Search uses
websearch_to_tsquery and ts_rank_cd; default search excludes reference channels.
`earth_point` is an internal geographic helper, not accepted as a user SQL call.

**Search limitation:** a WHERE around a limited search filters the ranked
candidate set, rather than pushing a predicate before ranking. Use
`api.search(query,NULL)` followed by WHERE/ORDER BY/LIMIT for exhaustive filtered
ranking. Use `api.search_filtered` or the HTTP filter arguments for exact bounded filtered search.

### Identity registry (O/A)

| Route | Request | Response |
|---|---|---|
| POST /v1/identity/people | `{handle,display_name?,is_owner?:false,status?:"active",note?}` | Person row; only O may set is_owner |
| GET /v1/identity/people | none | Person rows |
| GET /v1/identity/people/{id} | none | Person row or 404 |
| GET /v1/identity/unclaimed | none | Unclaimed, non-ignored identifiers |
| POST /v1/identity/attach | `{identifier_id,person_id}` | `{ok:true}` |
| POST /v1/identity/detach | `{identifier_id}` | `{ok:true}` |
| POST /v1/identity/merge | `{source,target}` | `{id: merge_event_id}` |
| POST /v1/identity/split | `{event_id}` | `{ok:true}` |
| POST /v1/identity/rename | `{person_id,handle,display_name?}` | Updated person |
| POST /v1/identity/ignore | `{identifier_id}` | Updated identifier |
| GET /v1/registry/{resource} | none | Rows for resource |
| POST /v1/registry/{resource} | See below | Inserted row |

Import resources/columns:

- `names`: person_id, name.
- `identifiers`: person_id (nullable), system, kind, value, value_norm, status,
  label, evidence. asserted_by is assigned from the authenticating token.
- `schemes`: system, kind, same_system, same_kind, normalizer
  (`phone`, `email`, `trim`, `lower`).
- `fact-keys`: key. `facts`: person_id, key, value (arbitrary JSON).
- `relation-kinds`: kind, inverse. `relations`: from_id, to_id, kind.

Import parents/allow-lists before dependent rows. These are individual atomic
operations, not a bulk import transaction. Client-side source IDs must be mapped
to returned Throughline IDs. Exact merge/split covers registry data, not removal
of audit events. Ignore also accepts `{thread_id}` and marks thread metadata; there is no resolution review job yet.

## CLI and backups

Named CLI commands cover migrate, serve, token create/list/revoke/location-source,
sql, search, context, person, catalog, status, channels, identity
unclaimed/merge/split/attach/detach/ignore, track detect-visits/retire, reparse,
purge, dump and restore. The generic
`api METHOD PATH [JSON|@file]` command covers every remaining HTTP operation.
`token location-source ID [--off]` grants or withdraws the track-writing
capability of an existing, unrevoked plugin token; like token creation it uses
the owner connection, so the token does not have to be reissued. `track retire`
is described under "Visits and withdrawn track points".
All endpoint-backed commands use HTTP. Only local administration uses the owner
connection. Commands accept `--json`; default text uses compact key=value rows. Help is intentionally concise.

Dump uses PostgreSQL custom format, and restore uses --no-owner and
--exit-on-error. Roles must exist on a new cluster. Configure THROUGHLINE_PG_BIN
or PATH. Restore into an empty database; it does not overwrite a populated one.
The database URL is passed to PostgreSQL tools without shell interpolation.

## Scope and remaining work

The tests exercise synthetic data only and isolate their database with a random
name. The SDK/contract kit should build against the HTTP shapes above and the
generated schema, without importing core modules. Token creation stays outside
the SDK. Retry whole failed batches, persist cursors after successful batches,
and do not interpret rejected items as infrastructure failures.

Built since the initial core (v1.1 waves 1-6, see
[v1.1 agent retrieval](#v11-agent-retrieval-waves-1-4) and
[waves 5-6](#v11-waves-5-6-people-writes-transcripts-commitments-disclosure-ops)):
page revisions and passages, an asker gate enforced in the database
(security-barrier views keyed to transaction settings, not row-level
security), claims, a closed link-type registry, annotations, source
coverage and feeds (arrival cursors), the recall/expand/match operators, result
sets, plans, packets, a librarian (optional investigate mode, bounded curate mode), versioned
recipes, a person-centric read model, the write contract (idempotency keys,
receipts, compare-and-set, message aliases), transcript-native behaviour
(media-noise flags, the speaker inbox), a request and commitment ledger,
sensitivity labels with expiring grants, and verified backups. Real source
plugins exist for several sources (plugins/).

Not implemented: jobs and the job runner, tables, rules, numeric policy scores
(M4); people and hidden-field policies, groups, SSO and default-deny for
owner-named unknown askers (M6; channel, thread and message labels ship); an
MCP/WebMCP server and memory providers (M7; the web interface is implemented:
a static shell under `/ui/` plus the `/v1/web` routes, docs/web/backend.md). LIB-4
operations other than match and expand (and the two registered provider
operations classify_media_noise and extract_requests), fetch of images and
raw payload fields, a native GBrain service (BRN-1/2/5/8) and the pinned
upstream GBrain schema are deferred. docs/v11/index.md maps every v1.1
requirement, each LIB section, the design priorities P1..P11 and the twelve
behaviour scenarios to their tests or to the deferral and its reason.

Deferred after wave 6, with the reason:

- Recall hard-scope filters on identity_quality and time_precision: the
  shared scope-function signature (api.scope_filter) is fixed by every leg.
  Both are filterable in SQL (api.appearances, api.attribution) and in the
  result-set filter op.
- Identity state as of a time: person_id and merges are rewritten in place,
  not replayed, so person functions and attribution answer for the current
  identity state even under as_of or known_by.
- A real model's quality: only the deterministic local providers and recorded
  responses run in tests. That covers the reranker, the semantic model and
  the controller, and also the media-noise judgement and request extraction
  (both run behind the provider operation registry with their local rules).
- Further hardening before the API is exposed to untrusted users: timing side
  channels, the red-team suite, the per-row cost of the barrier for
  restricted askers at scale. Identity undo after intervening edits remains
  follow-up work.

## SDK, contract kit and public twin

`sdk/` is an independent distribution depending only on httpx and pydantic.
It uses HTTP exclusively; the core never imports it. The runner feeds both
historical and live modes from one generator, flushes acknowledged batches
before cursor writes, and redacts declared secrets from its log handler.
Optional contract tooling starts the core externally in disposable databases.
`plugins/template` and `plugins/template-node` demonstrate the process contract
in two languages. See plugin-http-contract.md and writing-a-plugin.md.

`fixtures/` generates 12 invented people with multiple fake handles, two years
of chats, transcript turns, consumption links, reference pages and a stopped
track. All loading goes through HTTP. Docker and CI target PostgreSQL 16 with
pgvector; Immich and GBrain are not bundled.

## M3 conversations and optional embeddings

Migration 0008 adds `tl.conversations`, `tl.embeddings`, `tl.embedding_models`
and the small append-only `tl.embedding_ledger`. Existing messages are grouped
when the migration runs. `api.conversations` exposes spans, timestamps, estimated
tokens, text hash and closed state. Search rows append `conversation_id`,
`first_thread_no`, and `last_thread_no`; existing columns and keyword defaults
remain unchanged.

Channel registration accepts two additional optional settings:
`conversation_whole_thread` (default false) and `conversation_max_tokens`
(default 1500, minimum 32). Transcript and email plugins should set
`conversation_whole_thread=true`. This disables gap boundaries, while preserving
the size limit. The existing `conversation_gap_s` defaults to 1800 and determines
idle closure even for whole-thread channels. Changes to channel settings take
effect on the next affected-thread ingest or explicit rebuild.

Grouping runs as window/aggregate SQL over current versions of affected threads,
under the existing channel/thread locks. Time boundaries use the previous
message's end (or start when absent). Text is joined with newlines and split at
6000 characters by default, with 64 characters of overlap; estimates use four
characters per token. A single oversized message can span multiple chunks.
Chunk IDs persist by thread and deterministic ordinal; spans and hashes are
refreshed on edits/backfills. IDs must not be treated as immutable content IDs.
`throughline rebuild-conversations` locks channels and computes the same grouping
for all threads. Hash changes immediately invalidate old vectors. A late message
reopens a span while its idle gap is still running; historical spans whose gap
has already passed are immediately eligible for re-embedding. The worker also
closes idle conversations without requiring further ingestion.

Configure the following only when embeddings are wanted:

| Variable | Meaning |
|---|---|
| THROUGHLINE_EMBEDDING_BASE_URL | OpenAI-compatible base ending in `/v1` |
| THROUGHLINE_EMBEDDING_KEY | Optional bearer key (local servers may omit it) |
| THROUGHLINE_EMBEDDING_MODEL | Model identifier; change it for a new model/version |
| THROUGHLINE_EMBEDDING_DIMENSIONS | Positive vector dimension count |
| THROUGHLINE_EMBEDDING_PRICE_PER_MILLION | Estimated dollars per million tokens, default 0.02 |

`throughline embed run --once` handles one batch of up to 64 closed conversations;
repeat until it reports zero. `throughline embed run` continuously drains batches
and checks for idle closures every ten seconds. It requires the database URL and
app role credentials. Batches commit vectors and provider usage/cost together;
a failed batch can be retried. A process failure after a provider response but
before commit may repeat a billable provider call; the ledger records committed
responses, not unknowable provider-side charges. Worker execution is serialized
with an advisory lock. No jobs subsystem is involved.

Model rows store dimensions and an active flag. A new model remains inactive
until every currently closed conversation has a current vector and no thread is
awaiting deferred maintenance; the old model continues serving during that
backfill. The second condition matters because conversations of a dirty thread
are hidden from the worker: "nothing visible is missing" does not prove coverage
while any thread is dirty, so neither a replacement model nor the first model is
activated until the dirty set is empty. Query embeddings use the active model
and its dimensions. Keep the old model available at the configured endpoint
until handover completes. Never reuse a model identifier for changed weights or
dimensions. Status includes per-model embedded token totals and estimated cost;
provider usage is preferred, with a character-based estimate if absent. Pages,
table rows and images (EMB-5) remain outside this milestone.

`GET /v1/search` adds `mode=keyword|meaning|fused`, `person` (ID), `channel`
(name), `after_at`, `before_at`, and `place` (name, within 100 meters).
Limits are 1..1000. Time bounds are inclusive message start timestamps. Filters
select eligible messages before any cutoff; a conversation needs an eligible
message to participate. Meaning search computes exact cosine distances against
current active-model vectors after filtering, compatible with pgvector 0.6.
There is no approximate vector index or recall loss. This exact implementation
has not been benchmarked at five million messages; the performance targets at
that scale are still unverified.

Meaning results choose the highest keyword-ranked eligible message in each
conversation using the query text, then lowest message ID to break ties; SQL
callers may supply optional `query_text` after the place argument. With no shared
words the representative is the first eligible ID. This is a lexical proxy for
the best message, not a second message-level embedding. Fused search uses RRF
with constant 60 over exhaustive keyword-message and meaning-conversation ranks,
combines equal message IDs, then limits. SQL functions are:

```sql
SELECT * FROM api.search_filtered('orchard',10,NULL,'synthetic.chat');
SELECT * FROM api.search_meaning('[1,0,0]',10,NULL,'synthetic.chat');
SELECT * FROM api.search_fused('orchard','[1,0,0]',10,NULL,'synthetic.chat');
```

All accept optional person, channel, after, before and place parameters in that
order after the limit. An outer WHERE around a bounded SQL function still only
filters its returned candidates. For arbitrary predicates use a NULL limit,
then WHERE / ORDER BY score DESC,id / LIMIT. Explicit filter arguments are the
recommended bounded form. Keyword scores are independent per row, so exhaustive
ranking then filtering is mathematically identical to filtering then ranking.

For agents starting with text, `POST /v1/embed {"text":"orchard"}` returns
`{"embedding":[...],"model":"..."}` for the active model. Serialize this array as a quoted
vector literal in a subsequent `POST /v1/sql` call to `api.search_meaning` (or
`api.search_fused`). This two-step helper performs no SQL substitution and adds
no database write privilege. To pin the model across a handover, pass the returned model as the final
optional `serving_model` argument (after `query_text` for meaning search, after
place for fused search). HTTP search pins this automatically.
Both requests require owner/agent authorization;
the SQL query still runs under the AST validator and reader role in a read-only
transaction. `api.catalog()` describes this helper and the filter semantics.
Unconfigured model operations return a clear unavailable error; ingest, SQL and
keyword search continue normally. The CLI adds `search --mode` and its generic
`api` command covers the helper and all HTTP filters.

### Identity import history

Migration 0009 adds original identity metadata and source references without changing existing required HTTP fields. People have `kind`, `source_system`, `source_id` and writable original `created_at`. Name rows retain kind, primary flag, note, source and creation/retirement timestamps; retired names are stored but excluded from `api.person` resolution. Facts and relations retain their source metadata and lifecycle timestamps. `api.facts` and `api.relations` expose current assertions, while `api.facts_history` and `api.relations_history` retain all original rows. `api.person_names` includes name history. `api.catalog()` includes the actual identity column names and SQL types.

`tl.not_same` / `api.not_same` stores canonical distinct person pairs with reasons. Merge checks follow existing merged handles before comparing pairs, so merging a third person cannot discard an earlier distinction. Only an owner supplying `override_not_same=true` may bypass it. Original pairs remain stored, preserving their meaning after a split. Identifier attachment retains the existing rule that reassignment requires a merge.

Migration runners hold a fixed advisory transaction lock in the maintenance `postgres` database throughout the run when that database is reachable, protecting cluster-wide role creation and alteration across otherwise independent destination databases. A server without a connectable `postgres` database migrates under the per-database migration lock alone, which still protects migration application.

## Review hardening (migrations 0010-0018)

Runtime passwords are mandatory unless `THROUGHLINE_DEV=1` is explicitly set.
Development mode permits a passwordless connection to a locally configured trust
authentication server; it does not install a published default password.
Migrations never change existing cluster role passwords. After creating roles,
run `throughline configure-passwords` explicitly with both runtime password
variables set. This is an administrative, cluster-wide operation, separate from
schema migration. The root `.env.example` already requires explicit unique
values. In compose, the one-shot `migrate` service applies migrations and sets
both role passwords, and is the only service given the superuser URL; the API
service starts after it completes and receives coordinates and role passwords only.

The reader has no privileges on `tl`. API functions that need internal data use
SECURITY DEFINER with fixed paths containing only trusted schemas; CREATE in
public is revoked. The role defaults to read-only, cannot create temporary
objects, and cannot execute query-executing, large-object, file-reading,
advisory-lock, notification, or configuration built-ins revoked by the new
migrations. PUBLIC grants must be revoked too because PostgreSQL privileges are
additive. These function ACL changes are database-local; role settings are
cluster-wide. Installing new extensions requires reviewing their PUBLIC grants.
The AST validator remains a second boundary and rejects attribute function
notation. HTTP SQL responses stop at 4 MiB of serialized row data as well as the
row limit. An oversized first row returns no rows with truncated=true. The
limit bounds responses, not PostgreSQL aggregate working memory; statement
and work-memory settings still apply.

Message links are re-pointed in the same transaction as superseding a version.
Old IDs resolve through the version chain in context and follow. New migrations
repair preexisting version-bound links and remove exact duplicate assertions.
Merged entities resolve to their survivor for new references. Attach/import
refuse merged people. Non-owner actors cannot rename/merge owner-asserted people
or move the owner flag; client assertion authors are archival metadata in
original_asserted_by, while asserted_by always comes from the token. Split
requires reverse dependency order, restores the source references, and does not
replay a target's old name over a later rename. Conflicting asserted identifier
normalization requires explicit owner reconciliation rather than silently
combining assertions.

Identifier normalization belongs to the core. NULL same_system/same_kind means
the original scheme, and registry imports can claim unclaimed ingest-created
identifiers. Adding a scheme also normalizes earlier automatic identifiers.
Legacy identifiers already stored with NULL system/kind cannot be attributed to
a source platform from their stored value alone: migration 0013 preserves each
under legacy_unresolved/legacy_unresolved_<id>. The owner must reconcile these
rows using source records; inventing a platform would risk a false identity.
Who computes destination norms for reconciliation and rejects collisions,
retains source authors separately, and its verify command retains source counts.

Thread metadata is stored in tl.threads.metadata and returned by api.threads.
New versions retain their complete original normalized envelope, so a no-op
reparse preserves display names, thread values and original timestamp offsets.
For records predating envelope retention, the original author display name and
end-time offset are not recoverable from the old schema alone; reparse can
preserve the recorded start offset but needs a source mapper to recover those
missing values. Empty thread metadata does not change the legacy input hash.

Large affected threads defer physical numbering and conversation regrouping:
only a dirty-thread row is written on subsequent batches. Public message views
compute exact dense numbers for dirty threads, so direct context SQL is correct.
Stored conversations of a dirty thread describe an older message order, so every
read surface (api.conversations, search, context, meaning search, including
direct reader-role SQL) goes through `tl.live_conversations` and reports no
conversation for such a thread (NULL conversation fields, no meaning-search
hit) rather than a stale one. Reads are therefore correct without
materialization; materializing only makes those conversations available again.

Materialization (`throughline.derived`) handles one dirty thread per
transaction under that thread's single channel lock, so no lock spans threads,
channels or network calls. HTTP SQL/search/context reads and query embedding
give pending maintenance a bounded chance first: they start (or join) one
background pass per API process and wait at most
`THROUGHLINE_READ_MATERIALIZE_WAIT_S` seconds (default 2) for it. The pass only
try-locks: a thread whose channel is held by an ingest or reparse is skipped
and stays dirty for the next pass, so a read never waits for an ingest lock.
The pass continues after the reader stops waiting, and a failed or slow pass
never fails the read. The read then runs against whatever is materialized;
threads still dirty simply show no conversations yet. The embedding worker
runs the same try-lock pass before each batch and holds no transaction, row
lock or channel lock during the embedding request (a session advisory lock on
an idle connection serializes workers). `throughline rebuild-conversations`
materializes everything, waiting for channel locks.
No conversation rebuild runs for unchanged batches. Location attachment during
message ingest is limited to incoming rows; late track attachment still examines
track-derived messages globally and is not yet an incremental geographic job.

Track-derived locations retain a point reference in api.message_locations,
including source. Closer late samples replace previous track attachments; source
locations always win. Plugin track writes require an owner-created token with
--location-source, or an existing plugin token granted the capability with
`throughline token location-source ID`; ordinary plugin tokens cannot stamp
other sources' messages.

### Visits and withdrawn track points
Stop detection uses a running centroid and skips one isolated excursion that
immediately returns within the radius. Consecutive in-radius samples of one
source stay one stay across a silence of up to the visit maximum gap:
`THROUGHLINE_VISIT_MAX_GAP_S` (default 43200 seconds, 12 hours), overridden per
run by `max_gap_s` on `POST /v1/track/detect-visits` or `--max-gap-s` on
`throughline track detect-visits`. A stationary device may report rarely
(overnight, or only on movement), so the gap is deliberately much longer than
the minimum stay, which is only the shortest duration reported as a visit. A
longer silence splits the stay; movement during a silence cannot be inferred.

Every visit keeps one external id for life and is versioned only under that id;
a visit is never superseded by a different visit. Each run reconciles the
recomputed stays with the stored visits: a stay whose natural id (source and
first point) exists keeps it, reviving a retired visit if needed; a remaining
stay adopts the unclaimed active visit sharing the most track points (a backfill
that extends a stay makes a new version of it); any other stay is new; and an
active visit that no stay claimed is retired. Retirement writes a new version
of that visit under its own id with payload `retired:true`, removes its
rule-generated place links and excludes it from api.visits. Repeating detection
creates no further versions. The response's `retired` counts visits retired by
the run. Runs are serialized on the visits channel.

A bad point or a whole bad source is withdrawn with the owner-only
`throughline track retire (--source NAME | --point-id ID ...) [--reason TEXT]
[--restore]`. It needs the owner database role: runtime roles cannot write
`tl.retired_track`, so no HTTP token can do it. Nothing is deleted; the points
stay in tl.location_track as ground truth and are ignored by location
attachment and stop detection. Every message version located by a withdrawn
point loses that location; current versions move to the next nearest usable
point within tolerance, or are left with loc_reason
`no_track_point_within_tolerance`. Rerunning the command repairs versions an
earlier withdrawal could not see, and `--restore` reverses it. The command does
not touch visits: run `throughline track detect-visits` (or the endpoint)
afterwards so visits built from the withdrawn points are retired or recomputed.

All authenticated HTTP requests, reads included, produce token-attributed audit events, without
copying request bodies or secrets into the log. Links and identity events carry
token IDs too. The app role cannot update/delete/truncate either audit table.
Owner-only purge removes message versions, raw payloads, normalized envelopes,
point associations, links, conversations, embeddings and vanished threads in one
transaction. Shared/explicit registry assertions remain; unused automatic,
unclaimed identifiers are removed. Audit history remains intentionally retained.
Plugin links must use source method and may join only the plugin's own messages
and entities (see Boundary follow-up below). Plugins can patch only their
own plugin's links; owner assertions remain owner-only. Entity merges require an
owner or agent. Channel names are bounded to 60 ASCII bytes so typed-view names
cannot collide through PostgreSQL's 63-byte identifier truncation, and native
visits registration cannot clobber another plugin's channel.

Restore reapplies the reader boundary because pg_dump does not retain catalog-function and database TEMP privileges. Subsequent migrations reapply the same owner-only hardening function without rotating passwords.
Owner registry assertions also protect their participating people from non-owner rename/merge. Migration 0021 derives this protection from earlier owner audit history; it cannot infer or undo an earlier stolen name automatically.
Ingest auxiliary copies join a transaction-local table of inserted message IDs,
not the complete message history. Already-dirty threads skip counting and all
empty numbering/regrouping calls; location association is planned only when the
incoming batch actually contains track-derived rows. Identity and thread lookups
are restricted to the incoming keys. Link endpoints have dedicated indexes, and
historical message-ID resolution reads the version chain without materializing
thread numbers. These changes bound repeated historical-import work by the
incoming batch rather than the accumulated thread size.
Recomputation that disproves an earlier stop retires it: a new version of the same visit (same external id) is written as an explicit retired native stub, its rule-generated place links are removed, and the stub is excluded from api.visits. A visit is never superseded by a different visit. Repeating detection does not create further retirement versions; a later matching detection revives the same logical visit.
Thread metadata keys ignored and asserted_by are reserved for core identity decisions. Source objects using either key are retained in full under metadata.source_metadata, with other source keys also merged normally. A source sync cannot overwrite an owner ignore decision, and agents cannot demote its assertion author.

## Boundary follow-up (migration 0024)

### Plugin link authority

A plugin token asserts source links about its own data. For every link, on
`POST /v1/links` and in the `links` array of `POST /v1/ingest/messages` alike:

- `method` must be `source` (the default). `owner` needs an owner token.
- A message end, given as `{channel, external_id}` or `{kind:"message", id}`,
  must be in a channel registered to the token's plugin.
- Any other end must be an entity: `{kind:"entity", id}` or a placeholder
  `{kind, external_ref, name?}`. Entity-to-entity links are allowed; this is how
  connectors describe structure (an episode is part of a show, a track was
  created by an artist). People, pages and table rows are refused: identity and
  derived data are not a plugin's to assert, so a plugin cannot write the
  `author`/`recipient`/`participant`/`present` links that feed
  `api.person_messages`.
- Batch links are authorized after the batch's messages are stored, in the same
  transaction, so a batch may link to messages it delivers. A refused link
  returns 403 and rolls back the whole batch, messages included.

The manifest's `link_types` list is written by the plugin itself, so it is
documentation for the catalog and not an authorization list; a connector such
as GBrain forwards link types defined by its source.

`PATCH /v1/links/{id}` by a plugin token is allowed only for a source link
written by a token of the same plugin, so a reissued token keeps working. A link
stored before writers were recorded (`token_id` NULL) counts as the plugin's
when all its message ends are in the plugin's channels, it has at least one, and
its other end is an entity. A plugin can never set or change an owner link, or
move a link away from `source`. A missing link returns 404.

`tl.links.token_id` and `tl.entities.token_id` record the writing token.

### Audit log

Each authenticated request writes one `tl.audit_log` row: token, asker,
`statement` (the SQL text for `/v1/sql`, `/v1/search`, `/v1/person`,
`/v1/context` and `/v1/catalog`; otherwise method and path), `row_count` for
reads, `http_status`, and `detail` with route parameters such as merged entity
ids or the created link id. Failed and refused requests are recorded with their
status. Request bodies are never copied. A request header `X-Throughline-Client`
with the value `web`, `cli` or `sdk` is recorded as `detail.client`; any other
value is ignored.

### Identity authority in registry imports

`asserted_by` on identifiers, facts and relations is the authenticating token's
kind. A client can lower it, never raise it: an owner token that sends
`asserted_by:"agent"` stores the record with agent authority, which is how a
registry of agent or model proposals is imported without becoming owner
assertions and without protecting the people it mentions. Any other supplied
label is kept as `original_asserted_by`. The Who importer uses this: a source
row whose label is listed in `WHO_OWNER_LABELS` (default `owner`) is sent as
`owner`, every other row, unlabelled ones included, as `agent`, always with the
source label as `original_asserted_by`. People themselves are still stamped with
the kind of the token that created them. Agent tokens cannot add an identifier
or a name to the owner person or to an owner-asserted person, the same rule
`/v1/identity/attach` and `rename` apply.

### Read-only SQL

A WITH name is accepted as a relation only where PostgreSQL makes it visible:
inside its own statement, and for a non-recursive WITH only in later entries and
the body. SQL-standard syntax that the parser turns into `pg_catalog` calls
(EXTRACT, AT TIME ZONE, SUBSTRING, TRIM, POSITION, OVERLAY, OVERLAPS,
LIKE ... ESCAPE, SIMILAR TO) is accepted.

PostgreSQL resolves `alias.name` as `name(alias)` when the alias has no such
column, and the row of a FROM function returning a scalar is that scalar. The
validator therefore accepts `alias.name` on a FROM function only when the
function is a row-returning `api` function, or `name` is the item's own column
(its alias, or a declared column alias). Only function calls may appear in FROM.

Built-in functions are accepted unqualified and only from a fixed list
(`query.BUILTINS`). Besides the aggregates, text and time functions of the
first versions it holds, for the web interface: binning and arithmetic (`floor`,
`ceil`, `ceiling`, `width_bucket`, `date_bin`, `ln`, `log`, `sqrt`, `power`,
`exp`, `sign`, `mod`), aggregates including the ordered-set ones
(`percentile_cont`, `percentile_disc`, `mode`, `stddev_samp`, `stddev_pop`,
`var_samp`, `corr`, `string_agg`, `bool_or`, `bool_and`), window functions
(`lag`, `lead`, `first_value`, `last_value`, `nth_value`, `ntile`,
`percent_rank`, `cume_dist`), inspection (`split_part`, `concat`, `char_length`,
`cardinality`, `array_length`, `jsonb_typeof`, `jsonb_array_length`), time
(`to_timestamp`, `age`, `justify_interval`, `make_interval`, `isfinite`) and
four set-returning functions (`jsonb_array_elements`,
`jsonb_array_elements_text`, `jsonb_object_keys`, `unnest`). Every one is
immutable or stable, without side effects and without access to files, the
network, settings or catalogues; arguments, `WITHIN GROUP`, `FILTER` and window
definitions are walked like any other expression, so no listed function can
carry an unlisted call. Pattern functions (`regexp_*`), `repeat`, and anything
reading settings stay out, and the types `xid`/`xid8` are refused so `age`
cannot read the transaction counter. docs/web/backend.md gives the reasoning
per class.

`POST /v1/sql` also answers `columns` (`[{name,type}]` in output order, with
PostgreSQL type names, present without rows, duplicate names kept) and accepts
`validate_only`, which checks and plans the statement without running it and
answers `{valid,error,code,columns}`.

The statement runs as a subquery of a wrapper that replaces any row above 8 MiB
of row text with an empty marker and stops one row past the cap, and the result
is streamed row by row. The API process therefore never receives or decodes an
oversized row; the response still stops at 4 MiB of serialized rows. PostgreSQL
itself still builds the row, bounded by the statement timeout.

### Roles

Migration 0010 granted `throughline_app` EXECUTE on the file, large-object,
dblink and `*_to_xml` functions it revoked from PUBLIC. Migration 0024 revokes
them; the app role keeps `pg_advisory_*` and `set_config` only.
`tl.harden_reader()` now verifies the result and raises when a privilege
survives, for example when migrating as a role that does not own the catalog
functions. Migration therefore requires a superuser: it creates and alters
cluster roles and changes ACLs on `pg_catalog` functions.

Pooled connections are checked before reuse. A connection the server closed
(restart, idle timeout) is discarded and replaced instead of failing a request.

## v1.1 agent retrieval (waves 1-4)

v1.1 keeps the single PostgreSQL database, immutable ground truth, plugins and
agent-controlled retrieval. Everything new is forward-only migrations
0032-0046, Python in the API process and new `api` objects; no existing
migration, no column of `api.messages`, `api.message_versions` or
`api.search_results` and no legacy search signature changed. In SQL the plan's
`tl.recall`/`tl.expand`/`tl.match`/`tl.librarian` are `api.*`: `tl` is the
private schema the reader cannot use. This chapter folds the per-component
notes of `docs/v11/`; each section names the file with the details, and
`docs/v11/index.md` maps every v1.1 requirement to its tests.

### Registration points

Components register instead of editing shared files: HTTP routes are modules
of `api/routes/` exporting `router`; CLI commands are modules of
`cli_commands/` exporting `register(group)` and appear under
`throughline lib` (the root help budget, INT-1, has room for one row only);
allow-list additions are `allowlist/NNNN_name.json`; catalogue rows are
`tl.api_functions` and `tl.catalog_sections`, dynamic subjects
`api.describe_<subject>()`; record kinds `tl.record_sources`; gates
`tl.gates`; purge statements `tl.purge_hooks`; registry seeds `tl.seed_hooks`
(re-applied by `tl.reseed()` after tests truncate); retrieval legs, stages,
rankers and semantic operators are found by module scan. Tests truncate every
`tl` table except a keep-list. (docs/v11/plumbing.md)

### Record kinds and time

A channel has one record kind: `life_event`, `reference`, `page_revision` or
`derived`. Page revisions and derived records are generated text: never their
own evidence, supported only by their provenance sources, `unsupported` when
they have none. Every record view returns one contract (record_id, kind,
ref_id, message_id, version, ..., generated, unsupported, provenance, support,
expansion); `tl.record_sources` maps the prefixes `m`, `p` (passage), `l`
(link), `e` (entity) and `c` (claim) to gated views, and one hydrator plus
`api.result_set` read through it, so every operator returns the same keys.
Over HTTP records also carry `evidence_class` (primary, normalized, derived,
inference), computed from contract fields. Time: `record_time` is when it
happened (for a page, the revision time); `subject_from`/`subject_to` with
precisions is what it is about; `ingested_at` is the only time of an edit.
`as_of` is a record-time cutoff, `known_by` an ingest-time cutoff;
`time_basis` record | subject | either | effective chooses the filter.
Location applies to life events (source, else track) and derived records
(source only); otherwise `loc_reason` is `not_applicable`.
(docs/v11/records.md)

### Access gate and scope modes

A `Scope(token_id, token_kind, asker, mode)` is built once per request; the
only place that opens a reader connection, `query.reader_transaction(scope)`,
sets `throughline.scope_mode` (owner | asker | deny), `throughline.asker` and
`throughline.token_id`. An owner token is unrestricted unless it names an
asker; a non-owner token with an askers list must act as one of them (a single
entry binds automatically, a header naming another is 403); a non-owner token
with an empty list is a full-read token. Registered askers are `owner` or
`restricted` with channel and thread grants; only the owner database
connection edits them (`throughline lib asker`).

`tl.install_gate(view, predicate, definition)` builds `tl.<view>_all`, a
security-barrier `tl.<view>_gated` and `api.<view>` as the union of both,
switched by `api.asker_unrestricted()`. Unrestricted askers keep index
push-down; for restricted askers caller predicates cannot run below the gate,
which closes the error oracle (division and cast probes through `/v1/sql`).
Every api view exposing record columns is installed this way; views built only
over gated views inherit it; `api.recipes` (developer-written text, no
personal data) is the one documented exception. Visibility is
`api.row_visible`: a channel or thread grant, and for page revisions and
derived records every provenance source visible (recursive, fail closed).
Routes that use the app role call `deps.require_unrestricted` before any
lookup, unless they serve only the caller's own working state (sessions,
result sets, proposals, approvals, traces, feeds). `api.policy_version()`
moves on every policy change; saved sets, traces and investigations made
under an older version answer 409. (docs/v11/access.md)

### Providers and egress

Embedder, Reranker, SemanticModel and Controller each have a deterministic
local implementation (hash embedder, lexical reranker, rule matcher,
fixed-plan controller). Rerank, semantic and controller default to local;
embeddings stay unavailable until configured. `providers.call()` estimates,
applies the cost gate (caller budget and per-token limits; approvals bound to
token, asker, inputs, budgets and policy version), checks egress, calls,
writes `tl.model_ledger` and a trace event. Every HTTP provider host is
checked against `THROUGHLINE_EGRESS_ALLOW` (loopback implicit) before a socket
opens. Caches (query embeddings, rerank scores, verdicts, the semantic cache)
are keyed by operator, version, model and content hashes and read with
`cache.get(key, scope)`, which re-checks the visibility of every source.
`api.describe('egress')` says whether excerpts can leave the host.
(docs/v11/providers.md)

### Recall pipeline and planner protocol

`recall(scope, RecallRequest)` materializes once, asks the planner for legs,
runs each leg as a validated statement on its own reader connection with its
own timeout, fuses by reciprocal rank (keeping each leg's rank), lets the
planner add a round (bounded by `THROUGHLINE_RECALL_MAX_ROUNDS`), then runs
stages in order (page sources, spans, rerank, dedup, select, group), hydrates
through the record registry and reports execution metadata: status,
truncation, completeness, stages, timings, incomplete legs, budget, warnings,
`as_of_honoured`, coverage and the trace id. Hard scope (persons with role,
channels, kinds, time with basis, place, threads, ids, as_of, known_by,
must_terms, include_ignored) is one SQL predicate applied before ranking
inside every leg and by every stage that creates a hit. Unresolvable scope
returns an empty result with a reason. A failed leg, ranker or stage degrades
the call, never fails it. Rankers are pluggable (`fused` default, `lexical`,
`vector`, `cross_encoder`, a `learned` placeholder); spans select several
minimal message spans per conversation; selection deduplicates by provenance
and aliases, adds scoped context dependencies and fits a token budget;
grouping builds manifests, including the episode grain. Recall never calls the
semantic or controller provider; escalation to the librarian happens only in
the route, with token permission, a hint and a budget.
(docs/v11/recall.md, ranking.md, selection.md)

### Evidence classes and episodes

`evidence_class` is primary for source records, normalized for mechanical
renderings, derived for pages, passages, claims and links, and inference for
model output; a record prefix without a rule gets a default and a warning.
`api.episodes_of(message_ids)` assigns stable `ep:` ids to gap-based sessions
in a thread (a whole-thread channel is one episode); a count at record grain
over hits carries the warning `hit_count_not_incident_count`.
(docs/v11/semantic_sql.md, selection.md)

### Result sets

A result set (`rs_` + 32 hex) is temporary working state of one token and
asker: ordinals, record ids, pinned versions, spans, scores, roles,
dependencies and groups, never text. `api.result_set(handle)` checks token,
asker, expiry and policy version and joins members live to the gated record
views, so content and visibility are always current. Sessions keep history;
transforms (filter, order, dedup, group, union, intersect, difference) and
inspect views produce new sets. Handles are inputs to SQL, recall, expand,
match and plans. (docs/v11/result_sets.md)

### Semantic SQL subset and taint rules

`api.recall`, `api.expand`, `api.match` and `api.librarian` are not Postgres
functions. `query_ex` finds such calls (only as the single function of a FROM
item, constant arguments, at most four, the librarian top-level and alone),
validates the statement with placeholders before any work, estimates and
gates cost, runs the operators under one budget, splices the real handles and
validates again before the unchanged reader path runs it. A taint analysis
marks ranked-limited functions, non-exhaustive handles and LIMIT levels and
flags (or with `strict_exact` rejects) aggregates, DISTINCT, GROUP BY and
window aggregates over them. (docs/v11/semantic_sql.md, expand_match.md)

### Plans and packets

`POST /v1/plans` runs a validated DAG of sql, recall, expand, match, transform
and inspect nodes under one budget (branches concurrently). `POST /v1/packets`
copies records of one live read of a set (text, ids, versions, spans, three
times, support, evidence_class) with orientation notes flagged generated, each
verified against delivered references, plus coverage, unresolved needs,
truncation, cost and a continuation reference. (docs/v11/semantic_sql.md)

### Lineage, link types and annotations

Provenance rows tie derived records to source versions; links, relations,
and facts share validity intervals; link changes are kept in history
and superseded, never overwritten; entity merges are reversible
(docs/v11/lineage.md). Link types form a closed registry with total
normalisation and synonym folding; undeclared plugin types become candidates
without stopping an import; agents and rule or model writers cannot invent
types; the owner registers, merges and retires (docs/v11/link_types.md).
Annotations record effective time, place, location, ignore, media noise and
speaker without touching ground truth; ignored records are excluded from
recall by default, `include_ignored` brings them back, owner values win over
sources and jobs; agents and restricted askers propose (docs/v11/annotations.md).
Standalone claims, commitments, and librarian curate were removed in migration
`0074_drop_claims_commitments_curate.sql`; their functionality is covered by
lenses and pages (docs/v11/pages.md).

### Coverage and feeds

Coverage is queryable: `api.coverage_for(channels, after, before, kinds)`,
`GET /v1/coverage`, `api.describe('coverage')`, recall `execution.coverage`
and packet `known_source_gaps`. An unknown status or shape degrades to partial
with the raw detail kept; a gap opens only for partial reports or explicit
gaps and closes only on a report that names or verifiably covers it. Absence
is reported as 'not found in covered sources' or 'not found in coverage',
never as 'did not happen'. Feeds are named cursors of a token and asker on
arrival time (ingested_at, message id): `api.news`, `api.pending`, a
compare-and-set mark after work, and a settle horizon enforced in the
database (an ingest transaction longer than the settle window is refused;
restores are exempt). The former `/v1/consumers` routes remain as deprecated
aliases. (docs/v11/coverage.md, feeds.md)

### Librarian

The librarian (`investigate`, level 3, optional) is a bounded controller loop
over four tools that wrap library calls any agent can make (describe, plan
execution, set inspection, packet building), with a baseline recall as
fallback, no recursion, no budget increase and no writes beyond traces,
working sets and pending proposals; investigations store references only and
are rebuilt on read (docs/v11/librarian.md). Ongoing synthesis and linking
are handled by pages lenses (docs/v11/pages.md).

### Agent surface and recipes

The surface an agent needs is small: the catalogue (`api.describe`), SQL,
context expansion, recall for discovery and the optional librarian
(`api.describe('surface')`, `skill/throughline/SKILL.md`). Internal techniques
(legs, rankers, stages) are not presented as tools. Recipes are versioned
Markdown files in `src/throughline/recipes/` with JSON front matter (name,
version, schema_version, status, summary, needs, statements, expect) and a
body of adaptation notes and troubleshooting. Migration stores each version
once in `tl.recipes` and refuses a file that changed without a version bump;
`api.recipes` lists the current tested version per name (a proposal never
appears) and `api.describe('recipe:<name>')` shows one. `recipes.run` executes
a recipe through a Scope or over HTTP, passing the handle of a semantic call
to the next statement as `{{handle}}`, and reports a recipe whose needs are
absent as skipped, never failed; the test suite executes every tested recipe
on a fixture corpus. Routes: `GET /v1/recipes`, `GET /v1/recipes/{name}`;
CLI: `throughline lib recipe list|show|run`.

### Purge hooks, seed hooks and benchmark

`throughline purge CHANNEL --confirm` runs every `tl.purge_hooks` statement
before its own deletes, so passages, passage vectors, claims and their
relations, proposals, provenance, link history, caches, verdicts, annotations
and the channel's coverage state, gaps and reports go with them; result sets,
investigations and traces hold ids only and resolve to nothing. Seed hooks
re-apply registry seeds (link types, synonyms, coverage reasons, feed
settings) after a test truncation. The public-twin benchmark
(`python -m bench`) scores recall stages, evidence groups, clusters, roles,
temporal and leakage cases, abstention, latency and the librarian
configurations, plus the twelve behaviour scenarios; a behaviour whose features
are absent is reported skipped, never zero. (docs/v11/benchmark.md)

### Consolidated tests

`tests/test_v11_surface.py` (catalogue equals allow-list and semantic
registry, documentation, describe subjects, skill limits, one envelope over
HTTP and CLI, no push, generated text flagged, levels, help budget, every
route reachable), `tests/test_v11_security_matrix.py` (every route and api
view under a restricted asker, error oracle, canaries through every operator,
derived inheritance, revalidation, caches, counts, purge, ground truth, token
matrix, prompt injection, egress) and `tests/test_v11_recipes.py` enumerate
routes, views, prefixes and providers from the running system, so later waves
extend them by registering things. Features of a package still being built are
wave-integration checks that skip naming the missing feature.

## v1.1 waves 5-6: people, writes, transcripts, commitments, disclosure, ops

Waves 5 and 6 turn lessons from a long-running assistant's real workload
into database guarantees instead of agent prose: migrations 0046 (coverage, feeds),
0047 (people), 0048 (write contract), 0049 (transcripts), 0050
(commitments), 0051 (disclosure), 0052 (surface) and 0053 (open loops in
one-to-one threads). This chapter folds
docs/v11/people.md, write_contract.md, transcripts.md, commitments.md,
access.md (disclosure) and ops.md; those files keep the full tables.

### Person read model

One identity registry (tl.people, tl.person_names, tl.identifiers), no second
crosswalk. Only a confirmed author identifier sets tl.messages.person_id
(triggers on messages and identifiers, for every writer); suspected and
ignored accounts count for nobody in any tool until the owner confirms them
(`POST /v1/identity/identifiers/{id}/status`, idempotent with a receipt).
api.appearances gives one gated row per current record with `is_me` (owner
attribution or direction outgoing), `party_id`, `direction`,
`attribution_status` (confirmed, self_direction, annotated, suspected,
unknown), `identity_quality` and `time_precision`; the shared hydrator puts
the same five keys on every message record as `record.attribution`. The
owner's side of every person function is `is_me`, so "me and X" works where
the owner's account is not attached.

An exchange is a reply_to link, or the next turn in the same thread within
THROUGHLINE_EXCHANGE_WINDOW_S (1800 s) by a different author; replies are
resolved once into tl.exchanges, refreshed by the routes within
THROUGHLINE_PEOPLE_REFRESH_MS and disclosed as `exchange_index_stale` when
still stale. The functions (`person_brief` first, then `person_timeline`,
`person_arc`, `dms_with`, `between`, `between_threads`, `last_contact`,
`open_loops`, `interactions`, `around`, `day`, `together`, `mentions`,
`circle`, `ties`, `rhythm`) read gated views: a restricted asker gets counts,
ranks and lists over granted rows and no identity data. Strength is
exchanges plus shared conversations; an open loop is an ask (a '?' or
THROUGHLINE_ASK_PATTERNS) with no later message by the other party, closed by
the asker's own closing line (THROUGHLINE_CLOSING_PATTERNS) or by
`horizon_days` without activity. Open loops read the threads where the owner
wrote plus a person's one-to-one threads and channels where the owner never
did (0053), so an ask alone in its thread is still a loop. Recall takes
person_role `between`. CLI:
`throughline lib person brief|timeline|arc|dms|last|loops|circle|ties|rhythm|mentions|around|day|together|between|index|status`.

### Write contract

Every record write takes an `Idempotency-Key` (8-200 characters, per token,
30 days in tl.write_receipts) through the pinned interface `with
writes.idempotent(conn, token, request, body) as slot:` inside the route's
own transaction: the same key and request replay the stored skeleton with
`receipt.replayed: true`, a different request is 409, concurrent duplicates
serialize on an advisory lock. Receipts are read back in the same
transaction (`kind, id, record_id, version, status, stored_hash, counts,
written_at, replayed`; tl.stored_hash per kind), so no agent reads back what
it wrote. Compare-and-set: `expected_version` on ingest items,
`expected_hash` on link, claim, annotation and entity changes (409 with the
current hash). Link and entity batches answer per item; an authorization
failure stays a whole-batch 403. THROUGHLINE_REQUIRE_IDEMPOTENCY=true makes
keys mandatory for agent tokens.

Message aliases (tl.message_aliases) and per-key deliveries
(tl.message_deliveries) give provisional identity without duplicates: the
first external id stays canonical, an alias resolves to it, a new key
upgrades a record only when its identity quality ranks higher
(THROUGHLINE_IDENTITY_QUALITY_ORDER), and alternating archive and live runs
create no versions; two existing records of one message get a same_as link
that recall's dedup collapses.

Wave 6 retrofitted the remaining record writes: coverage reports and
attempts, feed create, drop and settings honour the key and answer a
keyed request with a receipt (`coverage_report`, `coverage_attempt`,
`consumer`, `consumer_settings`; an un-keyed call keeps its old response
shape); the transcript writes answer with `noise_run` and annotation
receipts. `POST /v1/feeds/{name}/mark` needs no key: it is
compare-and-set, and its answer carries a receipt-shaped object `{kind
consumer_cursor, id, stored_hash of the cursor, replayed}` that a repeated
mark returns unchanged. tests/test_v11_security_matrix.py keeps the explicit
registry: WRITE_ROUTES (expected contract per route) and EXEMPT (a reason per
route: reads that answer a query, the caller's own working state such as
sessions, sets and approvals, plugin plumbing, maintenance jobs, the
compare-and-set mark). Every app route with POST, PATCH, PUT or DELETE must
be in exactly one list, so a new unlisted write route fails the suite.

### Transcripts

A recording is one thread of a conversation_whole_thread channel; its turns
are authored by a speaker_label (unenrolled) or a voice_profile (enrolled).
A transcript is never rewritten: flags are annotations. `POST
/v1/transcripts/noise/run` judges each recording since its last ingest
through the provider operation `classify_media_noise`; the local rule flags a
turn as media when its word 4-grams overlap the text of media playing at that
time by at least THROUGHLINE_MEDIA_NOISE_OVERLAP (0.5), or by markers and
lyric-like repetition, and keeps conversation about media. Flags are per
segment, excluded by default like ignored turns (include_ignored brings them
back), never override an owner annotation, and fail open: a provider error,
timeout or pending approval writes no flag and marks the run partial. Only
enrolled voices become people (a CHECK on the identifier kind, for every
role); `GET /v1/transcripts/speakers` is the unrestricted-only speaker inbox,
resolved by `/assign` (owner speaker annotations) and `/ignore`. A recording
is one episode and one conversation; it is never split by time gaps.

### Disclosure

Sensitivity labels (intimate, medical, financial, work,
third_party_confidence and any the owner adds) on channels, threads and
messages (a messages.access_label naming a label counts, so labels apply to
history) narrow every grant: labelled content is denied by default even
inside a granted channel, and a grant reveals it only when it lists the
label. Grants carry a purpose (audit only) and an expiry. All of it lives in
api.asker_sees and api.row_visible (same signatures), so every gated view,
recall leg, set, packet, person function, coverage view and feed
inherits it. api.policy_version stays monotonic: an expiry passing raises it
like a revocation, and the grant trigger adds the expiries a change undoes,
so saved sets and investigations return 409. CLI: `throughline lib access
label|unlabel|labels|grant|extend` (owner).

### Ops

`throughline lib backup run` takes row counts and content hashes inside the
dump's own exported snapshot, encrypts with THROUGHLINE_BACKUP_ENCRYPT_CMD
and copies off site with THROUGHLINE_BACKUP_OFFSITE_CMD (a plaintext off-site
copy is refused); `lib backup verify-restore` restores into a scratch
database (THROUGHLINE_SCRATCH_DATABASE_URL) and compares with those stored
values plus probe queries; `lib backup check` exits non-zero when the last
good backup is older than THROUGHLINE_BACKUP_STALE_HOURS (36) for an external
alarm. tl.backup_runs holds counts and hashes only; GET /v1/status shows the
backup age beside ingest age, stale plugins and coverage gaps. The catalogue
section `ops` (0052) describes the commands.

### Surface after wave 6

The surface section (0052) keeps its order (catalogue, SQL, context, recall,
optional librarian) and adds the entry points: the person brief is the first
call for any question about a person; attribution and evidence_class are
read on every row; coverage comes before any statement of absence; incidents
are counted by episode; new arrivals come through a feed; writes carry
an Idempotency-Key and trust the receipt. Internal techniques are still not
tools. The skill (skill/throughline/SKILL.md) says the same in under 1,200
tokens, and the recipes person_brief_first, last_contact_pitfall,
relationship_arc, open_loops_both_ways, between_versus_author,
around_a_time, circle_and_ties, write_with_receipt, verify_backup_alarm,
spoken_instruction_to_work and disclose_by_label execute on the fixture
corpus (the last two once commitments and labels are installed).

### Consolidated tests after wave 6

The later surface suite, tests/test_v11_surface_*.py (registry equals allow-list and semantic
registry including the wave 5-6 functions, the skill, attribution in every
record envelope over HTTP and CLI, the index mapping of P1..P11 and the
twelve scenarios, the help budget with every lib command) and the wave 5-6
part of tests/test_v11_security_matrix.py (the write-route registry,
idempotency and receipts on every write route, the feed mark receipt,
new routes reader-path or refusing a restricted asker, new views gated with
the error oracle closed, no canary in person functions, transcripts or the
speaker inbox, purge of aliases, deliveries, receipts, exchanges, noise
state and commitments, ground truth unchanged after noise runs, alias
upgrades, commitment audits and label changes, and the token matrix of the
new routes).

## Agent retrieval, embedding vendors and source health (migrations 0054-0061)

Post-wave-6 additions harden live operations, vendor embeddings and the
context-window ergonomics of AI agents reading Throughline directly:

- **Per-channel embeddings and vendors (0054, `docs/v11/embeddings.md`):**
  `tl.channels.embed` (default `true`) lets owners opt channels out of vector
  indexing (`throughline embed channels --disable NAME`). Native vendor
  clients support Voyage (`voyage-4-large`, contextual `voyage-context-4`,
  async batch backfill), Gemini (`gemini-embedding-2`, async batch backfill)
  and OpenRouter (`data_collection: "deny"` by default), with monthly spend
  caps (`THROUGHLINE_EMBEDDING_MONTHLY_USD`) and half-precision HNSW indexes
  over full-precision cosine rescoring beyond `throughline.ann_exact_limit`.
- **Label-aware egress (0055, `docs/v11/encryption.md`):** `tl.labels.egress`
  (default `false`) prevents labelled content and anything derived from it
  from being sent to off-host model providers unless explicitly allowed with
  `throughline lib access egress LABEL --allow`.
- **Source health and arrival cadence (0057, `docs/v11/sources.md`):** tracks
  plugin runs, outages (`tl.source_outages`), channel pauses
  (`tl.channel_pauses`), and a 168-hour-of-week arrival cadence model to detect
  failing, silent, and stalled sources, verify catch-up on recovery, and fire
  one-shot state-transition alerts (`throughline lib sources check`).
- **Agent-shaped retrieval (0058-0061, `docs/agent-retrieval.md`):**
  - `POST /v1/find`: runs the recall pipeline and formats merged thread-context
    episodes with `later_in_thread`, `matched_people`, `other_matching_channels`,
    `newer_in_pool`, `sort=recent`, owner-local timestamps (`THROUGHLINE_OWNER_TZ`),
    and query-matched excerpts within `max_chars`.
  - `POST /v1/timeline`: exhaustive multi-query websearch plus optional
    high-similarity semantic episodes, grouped by `api.episodes_of` with
    per-year/channel summaries, `sample="spread"`, and channel coverage.
  - `POST /v1/orient`: single-call working-memory snapshot of visible channels,
    recent telemetry, strictly-future calendar events, recent voice summaries,
    recently active chat threads, and optional per-person catch-ups.
  - `throughline.agent_view`: shapes `/v1/sql` responses for context windows
    (dropping `tsv`/`payload_hash`, cutting long cells with `substr` hints,
    capping total output; pass `raw: true` for verbatim rows).
  - `tools/rerank_server.py`: standalone local FastAPI cross-encoder reranker
    (`BAAI/bge-reranker-v2-m3`) speaking `POST /rerank` on loopback.
  - Migration `0061` fixes aliased phone and email identifier scheme normalizers.
