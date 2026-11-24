# Language-independent plugin HTTP contract

## HTTP shapes

Every route requires `Authorization: Bearer <token>`, including OpenAPI.
`X-Throughline-Asker` is optional; non-owner values must be allowed by the token.
Errors use `{detail: ...}`; database rejections additionally carry `code`.
Missing/invalid request structure returns 422. Scope failures return 403 and
missing/revoked tokens return 401. Tokens are created/revoked only on the box.

Record writes follow the write contract (see "Write contract" below):
an optional `Idempotency-Key` header, a readback `receipt` in the response,
compare-and-set with `expected_version` or `expected_hash`, and per-item
results for link and entity batches.

Permissions: O = owner, A = agent, P = plugin scoped to its own plugin name.
All token kinds may read the channel registry and create entities/links; plugin
links are limited to the plugin's own messages and entities (see below).
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

Three optional channel properties describe the record model. Registration
stores what it receives, like every other channel property, except that an
omitted `record_kind` is resolved as described here:

- `record_kind`: `life_event`, `reference`, `page_revision` or `derived`. When
  omitted a new channel is `reference` if `is_reference`, else `life_event`;
  an existing channel keeps its kind unless `is_reference` changes.
  `reference` requires `is_reference: true`. Life events are their own
  evidence and may take a location from the source or the owner's track;
  derived records keep a source location but never take a track location;
  reference and page-revision records have no location. Page revisions and
  derived records are generated: they are never their own evidence.
- `embed`: optional boolean. `false` registers a new channel without
  vectors (meaning search does not cover it; every other search does). It is
  the default of a new channel only: the owner's later choice
  (`throughline embed channels`) is never overridden by registering again.
- `passage_text_paths`: list of entries, each one or more JSON pointers into
  the raw payload separated by `|` (the first non-empty value wins); the
  entries are joined by a blank line to form a page's passage text.
- `provenance_link_types`: link types whose targets are provenance sources of
  this channel's records.

`POST /v1/ingest/messages` (O/P):

```json
{"plugin":"synthetic","dry_run":false,"messages":[{"channel":"synthetic.chat","external_id":"fake-1","thread":{"external_id":"fake-thread","title":"Synthetic thread"},"happened_at":"2020-01-01T12:00:00+02:00","ended_at":null,"author":{"system":"fake","kind":"account","value":"fake-account","display_name":"Synthetic author"},"content":"Synthetic orchard","fields":{"rating":1},"images":[],"location":{"lat":1,"lon":2,"accuracy_m":5},"access_label":null,"payload":{"synthetic":true}}],"links":[]}
```

Maximum 5,000 items. Thread, channel, external ID, timestamp, content and payload
are required. Timestamp must carry an offset, content must remain nonempty after
trimming; end cannot precede start. Author, images, fields and location are
optional. `subject` is optional:
`{"start":"2019-06-01T00:00:00+00:00","end":"2019-06-30T00:00:00+00:00","start_precision":"month","end_precision":"month"}`
gives the period a record is about when that differs from `happened_at` (a
note written today about a trip last year; for a page revision `happened_at`
is the revision time). Either end may be omitted but not both; timestamps need
an offset; `end` cannot precede `start`; precisions are `exact` (default),
`day`, `month`, `year`, `approximate` or `unknown` (an omitted end is stored
without a precision). The subject is part of the
immutable record and of its payload hash, so changing it creates one new
version; a message without `subject` hashes exactly as before the field
existed. A missing location has `loc_reason`
`no_track_point_within_tolerance` only for a life event; for every other kind
it is `not_applicable`. Returns
`{received,inserted,new_versions,unchanged,rejected:[{index,external_id,error,reason?,current_version?}],aliased:[{index,status,external_id,canonical,link_id}],link_results:[{index,status,reason,link_id}],receipts?}`.

Two optional item keys are outside the payload hash, so replay equality
(MSG-2) is unchanged by them:

- `expected_version` (int): compare-and-set. `0` means the record must not
  exist yet; otherwise it must be the current version. A mismatch rejects
  only that item, with `reason: "version_conflict"` and `current_version`
  (0 when the record does not exist).
- `aliases` (list of at most 10 strings): earlier or provisional external
  ids of the same record in the same channel (a fingerprint or an
  id assigned by a bridge). The first external id a record was stored under stays canonical;
  archive ids never change. Each item resolves in this order: its
  `external_id` is a known alias -> the canonical record; a current record
  exists under `external_id` -> that record; otherwise an alias that names
  an existing record (directly or through an alias) -> the delivered id
  becomes an alias of it (`aliased` entry, status `aliased`). When the
  `external_id` and an alias are already two separate records, one `same_as`
  link (method `source`) joins them (status `pair_linked`) and recall
  collapses the pair. Aliases of a new record are kept, so a later delivery
  under one of them resolves to it. `{channel, external_id}` link ends and
  provenance keys resolve aliases too. `GET /v1/describe` lists the section
  `message_aliases` (feature `message_aliases`) for plugins to probe.
- Per-key versioning of aliased records: once a record has an alias, each
  delivered key keeps the payload hash it last delivered. A delivery under a
  key already seen creates a new version only when that key's payload
  changed since its last delivery. The first delivery under a new key
  records its hash and creates a version only when the payload differs and
  its `fields.identity_quality` ranks strictly higher than the current
  version's in `THROUGHLINE_IDENTITY_QUALITY_ORDER` (comma list, best first;
  default: native ids first, then bridge-assigned ids, then fingerprints,
  then `unknown`; unlisted values rank lowest). Alternating archive and live runs therefore create no versions,
  while a real edit under either key creates one.

`receipts: true` adds one receipt per message item that was not rejected
(`{index, kind:"message", id, version, stored_hash, counts, ...}`); at most
1,000 messages per request (400 otherwise). Default off.

Links in an ingest batch are judged after its messages. Every link is
authorized before any is written: an authorization failure (a foreign
channel or message, a non-source method from a plugin) is 403 for the whole
batch, messages included. A vocabulary refusal (a rule, model or agent
writer with an unregistered type) is a per-item `refused` in `link_results`
and no longer rolls the messages back; any other link error (a missing
message end: `link message does not exist`) is still a 400 for the batch.
Dry runs execute the same path then roll back data writes (sequence values may
advance). IDs and ingestion times are not dry-run promises.

`POST /v1/ingest/track` (O/P) accepts
`{points:[{at,lat,lon,accuracy_m?,source}]}`, maximum 10,000 points, returns
`{received,attached}`. Plugin track sources must equal token plugin scope.

`POST /v1/track/detect-visits?radius_m=100&min_stay_s=600&max_gap_s=43200` (O/A)
returns ingest counts for the native `visits` channel plus `retired`, the number
of visits the run retired. All parameters are optional and must be positive.
`min_stay_s` is the shortest duration reported as a visit. `max_gap_s` is the
longest silence between consecutive in-radius samples of one source that still
counts as the same stay; omitted, it is `THROUGHLINE_VISIT_MAX_GAP_S`, else
43200 (12 hours). A visit keeps one external id for life: a run versions it,
revives it, or retires it (a new version with payload `retired:true`, excluded
from `api.visits`) under that id and never supersedes it by another visit.

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
A plugin token may use only messages of its own channels and entities (by id or
as `{kind,external_ref}` placeholders, on either or both ends) and only method
`source`; anything else returns 403. Batch links may refer to messages delivered
in the same batch. Unmatched external entities become placeholders. Returns
the stored link object with `receipt` (`receipt.outcome` is `created`,
`existing`, `folded` or `candidate`). Re-posting an equal active link returns
it (`existing`).

`POST /v1/links/batch` (O/A/P) accepts `{links:[...]}` (1 to 1,000) and
returns `{results:[{index,status,reason,link_id,receipt}],counts}` with
status `created`, `existing`, `folded` (written under a synonym), `candidate`
(an undeclared plugin type held for the owner), `refused` (vocabulary) or
`error` (for example `link message does not exist`). Authorization is checked
for every item first: any authorization failure is 403 for the whole batch,
nothing is written and the answer names no item (a foreign and an unknown
message read the same). Then each item runs in its own savepoint.

A link may carry a validity interval: `valid_from`, `valid_to` (ISO timestamps
with offset, or a bare `YYYY-MM-DD` day read as 00:00 UTC) and
`valid_from_precision`, `valid_to_precision` (`exact`, `day`, `month`, `year`,
`approximate`, `unknown`). A day-precision date is sent with precision `day`.
A missing bound is open; a missing bound with precision `unknown` exists but is
not known; any other precision on a missing bound, or `valid_to` before
`valid_from`, is 400. Validity is part of the replay-equality check. A
`model`-method link must cite a non-empty `evidence.message_ids` array (400
otherwise). The stored link also reports `from_version_id` / `to_version_id`
(the message ids named at creation, never repointed) and `superseded_by`.

`PATCH /v1/links/{id}` updates type, confidence, method, evidence, status
(`active`/`removed`) or the four validity fields. `POST /v1/links/{id}/supersede`
accepts the fields of a link (any omitted are copied from the old one) and
returns `{superseded, link, receipt}`: the old row stays with status `superseded` and
`superseded_by`; an unchanged replacement is 400, an inactive link 409. Writer
rules for both are the same. Both accept `expected_hash` (a receipt's
`stored_hash`); when the link changed since, the answer is 409
`{detail:{reason:"hash_conflict",current_hash}}`. Every change and delete is kept:
`GET /v1/links/{id}/history` (O/A) returns `{link, history:[{history_id,op,old,changed_at,token_id}]}`.

`POST /v1/entities` accepts
`{kind,name?,external_ref?,is_placeholder?,metadata?}` and returns the entity
with `receipt`. `POST /v1/entities/batch` accepts `{entities:[...]}` (1 to
1,000) and returns `{results:[{index,status,reason,entity_id,receipt}],counts}`
with status `created`, `existing` (same kind and external_ref) or `error`,
under the same authorization rule as link batches.
`POST /v1/entities/merge` accepts `{source,target,expected_hash?}`, moves every link and returns
`{ok:true,event_id,moved,receipt}`. `POST /v1/entities/split` accepts `{event_id,expected_hash?}` and
restores exactly the moved links (`{ok:true,event_id,restored,receipt}`); it is 409 when
any of them changed since the merge or the merge was already split.
`expected_hash` is the source entity's receipt hash. New entity
kinds require no migration. Recipient/presence query
semantics use active links of type `author`, `recipient`, `participant`, `present`
between a message and a person, in either direction.

### Provenance

`POST /v1/provenance` (O/P) records which source messages a derived record
rests on:

```json
{"derived":{"channel":"synthetic.pages","external_id":"fake-page"},"sources":[{"channel":"synthetic.chat","external_id":"fake-1"}],"relation":"cites"}
```

Response `{"written":1,"unresolved":0,"receipt":{...}}` (the receipt covers
every provenance row of the derived record; null when nothing resolved). A plugin token may name as `derived`
only a current message of its own channels whose `record_kind` is
`page_revision` or `derived` (403 otherwise); sources are `{channel,
external_id}` in any channel. Source keys that do not resolve are only counted
in `unresolved`, never reported one by one. An owner token may also name
`derived` as `{kind,id}` (kind `message`, `passage`, `claim`, `link`,
`entity`) and sources as `{id}`. Agent tokens get 403 (they propose instead).
Plugin rows have origin `source`, owner rows origin `owner`; both are
source-grade. Re-sending is idempotent (`written` counts new rows). Each
source's version is pinned at write time.

A channel may instead declare `provenance_link_types`: an active link of one of
those types from its page revision or derived message to a message records the
target as a source, and later revisions inherit it.

A page revision or derived record with source-grade provenance is visible to a
restricted asker only when every source is (recursively, depth 4, fail
closed); a record without any is unsupported and gated by its own channel
grant only. `GET /v1/sources?kind=&id=` (O/A, reader path) returns the current
versions of the sources, or no rows when any of them is hidden.

### Write contract

Every record write (ingest, links, link and entity batches, entities, merge
and split, identity and registry writes, claims, proposals and decisions,
provenance, annotations, link types) follows these rules.

- `Idempotency-Key: <8 to 200 characters>` (optional). Keys are scoped to the
  token and kept 30 days. The same key with the same request (method, path,
  canonical JSON body) returns the stored response with `receipt.replayed:
  true` and nothing else happens; the same key with a different request is
  409 `idempotency key reused with a different request`. Stored responses keep
  receipts, ids, counts and statuses only, never record text, so a replay
  answers with that skeleton (text fields are null). With
  `THROUGHLINE_REQUIRE_IDEMPOTENCY=true` (default false) agent tokens must
  send a key on record writes (400 without it).
- Receipts: `{kind, id, record_id, version, status, stored_hash, counts:
  {links_from, links_to, provenance_citing, passages, timeline, sources},
  written_at, replayed}`, read back from the stored row in the same
  transaction. `version` is the message version; for other kinds the row id.
  `stored_hash` is `tl.stored_hash(kind, id)`. Claim receipts add
  `corroborated`, `new_sources` and `superseded`.
- Compare-and-set: `expected_version` on ingest items (per-item
  `version_conflict`); `expected_hash` on link PATCH and supersede, claim
  supersede, retract and contradict, annotation supersede and retract, and
  entity merge and split (409 with `current_hash`). Replacing a page stays a
  new version, never a clobber.

The SDK sends an `Idempotency-Key` automatically on link, link batch,
provenance and coverage posts (`tl-` + sha256 of the plugin name, the route,
a delivery id and the canonical JSON of the record; the delivery id is new
for every delivery and kept across its retries, so retries replay and a later
run's re-send is processed again), delivers consecutive link records
through `POST /v1/links/batch`, counts `refused`, `candidate`, `folded` and
`error` results in the run report under `links` instead of failing, and
raises on authentication or authorization failures (401, 403). The message
key `aliases` passes through unchanged.

### Plugins

| Route (O/P) | Request | Response |
|---|---|---|
| PUT /v1/plugins/{name}/cursor/{key} | JSON object, opaque cursor | `{plugin,key,cursor,updated_at}` |
| GET /v1/plugins/{name}/cursor/{key} | none | Stored cursor row; 404 if absent |
| POST /v1/plugins/{name}/manifest | Opaque manifest object | Plugin row |
| POST /v1/plugins/{name}/heartbeat | `{interval_s?,error?:null,messages_ingested?:0}` | Plugin row; messages_ingested is a delta |
| GET /v1/plugins/{name}/health | none | `{name,state,kind,interval_s,last_success_at,age_s,newest_at,consecutive_failures,overdue,outage}` |

A successful heartbeat updates last success; an error preserves last success,
counts a failure in a row and keeps the known interval (an absent interval_s
never resets it). Send one heartbeat per run, success or failure: source
health (docs/v11/sources.md) turns them into ok, failing and down states and
into outage records. A run that cannot reach its source must fail (heartbeat
with `error`), not succeed with nothing: an empty success looks healthy and
is only caught later by the arrival-cadence check.
Status reports stale when no successful sync exists or two intervals have passed.
Cursors are durable independently of a batch: advance them only after ingest
succeeds. Retrying the batch after a crash is safe.

### Query routes (O/A)

| Route | Request | Response |
|---|---|---|
| POST /v1/sql | `{statement,row_cap?:1000,raw?:false}` | `{rows:[...],row_count,truncated}` |
| POST /v1/find | `{query,k?:15,scope?,sort?,window?,max_chars?}` | Context-window-shaped episodes with thread context and `searched` metadata |
| POST /v1/timeline | `{query|queries,scope?,semantic?:true,max_episodes?,order?,sample?}` | Exhaustive episode timeline with per-year/channel counts and coverage |
| POST /v1/orient | `{as_of?,days?:14,people?,max_chars?}` | Compact snapshot of channels, telemetry, future calendar, voice and active threads |
| POST /v1/recall | `{query,k?,scope?,strategy?,ranker?,save?}` | Full record-contract hits plus `execution` metadata |
| GET /v1/describe | `subject?` | Self-describing schema, functions, access, egress, coverage and recipes |
| GET /v1/catalog | none | Catalogue JSON document |
| GET /v1/context | message_id, before=3, after=3 | Query response |
| GET /v1/search | q, max_results=20, mode=keyword\|meaning\|fused, person?, channel?, after_at?, before_at?, place? | Query response; flat message columns plus score |
| GET /v1/person | q | Query response of people |
| GET /v1/status | none | `{plugins:[...stale],channels:[{name,count,last_ingest}],staleness:{...}}` |
| GET /v1/openapi.json | none (all token kinds) | Generated OpenAPI 3.1 document |

Stable `api` views: `messages`, `message_versions`, `threads`, `channels`, `people`,
`identifiers`, `person_names`, `facts`, `relations`, `entities`, `links`, `places`, `visits`,
`location_track`, `plugins`, `search_results`, `records`, `appearances`, `coverage`,
plus `ch_<channel>` typed views.
`messages` joins channel name/flags, thread external ID/title and resolved
person handle/display name. `relations` exposes both the stored kind and inverse.

SQL functions include `context(id,before=3,after=3)`, `search(query,max_results=20)`,
`search_all(query,max_results=20)`, `search_filtered(...)`, `search_meaning(...)`,
`search_fused(...)`, `near(lat,lon,meters)`, `near_place(name,meters)`,
`overlapping(from,to)`, `during(message_id)`, `person(text)`, `person_brief(id)`,
`person_messages(person_id)`, `follow(kind,id,link_type=NULL,depth=1)`, `catalog()`,
and `describe(...)`.
Context includes the anchor message. Time intervals are closed and use `ended_at`
when available. Follow is bidirectional, active-only and cycle-safe. Keyword search uses
`websearch_to_tsquery` and `ts_rank_cd`; default search excludes reference channels.
`earth_point` is an internal geographic helper, not accepted as a user SQL call.

**Search limitation:** a `WHERE` around a limited search filters the ranked
candidate set rather than pushing a predicate before ranking. Use
`api.search(query,NULL)` followed by `WHERE`/`ORDER BY`/`LIMIT` for exhaustive filtered
ranking, or use `api.search_filtered`, `POST /v1/find`, `POST /v1/timeline`, or
`POST /v1/recall` for pre-ranking scope filters. Meaning and fused search require an
embedding provider; see [docs/v11/embeddings.md](v11/embeddings.md) and
[docs/agent-retrieval.md](agent-retrieval.md).

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


## Process contract
The manifest JSON schema is language independent:

```json
{"name":"template","version":"0.1.0","channels":[{"name":"template.chat","description":"Invented events","fields":[{"name":"rating","type":"integer","description":"Invented score"}],"is_reference":false,"is_outside":false,"conversation_gap_s":1800,"location_tolerance_s":600}],"settings":{"input":{"env":"TEMPLATE_INPUT","description":"Recorded JSONL path","required":true,"default":null}},"secrets":{"secret":{"env":"TEMPLATE_SECRET","description":"Source credential","required":false,"default":null}},"run_mode":"poll","default_interval_s":3600,"link_types":["part_of"]}
```

`name` matches `[a-z][a-z0-9_]*`; version is a string. Channels, settings,
secrets and link_types are arrays/objects as illustrated. `run_mode` is `poll`,
`webhook`, or `stream`; interval is a positive number of seconds (default 3600).
Declaration keys are local setting names; `env` names the environment variable,
`required` defaults true, `default` defaults null. Values are strings. Resolve
the environment before using defaults and fail clearly on missing required
values. Never put credentials in a manifest or its defaults. Channel field types
and channel naming rules are specified above. Register each channel with the
manifest name as its `plugin` property. The core stores the manifest verbatim.

Every plugin accepts `manifest`, `backfill [--dry-run]`, and `sync [--once]`.
Manifest prints a JSON manifest (see SDK models for declarations). Import commands
print a final JSON object with `counts` keyed by channel, `start` and `end` offset
ISO timestamps (null for no events), and `estimated_model_cost` (zero without
arrival rules). Read `THROUGHLINE_URL` and `THROUGHLINE_TOKEN` from environment.
Settings/secrets declare their environment variable names. Never log secrets,
including in exception tracebacks. Use the same normalization in both modes.
Read cursor `default` (404 means `{}`). Write it only after ALL preceding records
are acknowledged and no message was rejected. Retry transport, 429 and 5xx with
bounded exponential backoff; replay after ambiguous responses. Never retry a
validation error indefinitely. IDs must be stable across replay. Dry-run starts
at the beginning, validates batches with dry_run true, skips auxiliary writes and
cursor/heartbeat writes. Manifest/channel registration is permitted in dry runs.
Identity routes require an owner or agent token; plugin tokens cannot import
identity. There are no arrival rules or model cost estimator in this milestone.

Exact repeated active link assertions (same endpoints, type, confidence,
method, evidence, job and job_version) return the existing link. Changed
assertions create separate links. Track points are idempotent by source and at.
Identity creation and heartbeat deltas are not generally idempotent; do not
automatically retry ambiguous writes to these endpoints.

## Identity import metadata

The existing POST routes accept additive metadata, and GET returns it. `identity/people` accepts `kind`, `source_system`, `source_id`, and original `created_at`; the source system/ID pair is unique. `registry/names` accepts `kind`, `is_primary`, `note`, `source`, `created_at`, `retired_at`. `registry/identifiers` accepts original `asserted_by`, `created_at`, `updated_at`. `registry/facts` accepts `note`, `source`, `as_of` (date), `asserted_by`, `created_at`, `retired_at`. `registry/relations` accepts `note`, `asserted_by`, `created_at`, `ended_at`. Omitted assertion authors default to the caller kind. All timestamps accept offset-bearing ISO timestamps.

`POST /v1/registry/not-same` accepts `person_a`, `person_b`, `reason`, optional `created_at`; pairs are unordered, distinct, and unique. GET lists original pairs. Merges refuse distinct pairs, including pairs inherited through earlier merges. An owner may explicitly pass `override_not_same=true` to the merge route; agents cannot override. Identifier attachment to another person's identifier still requires a merge.

`api.person_names` includes retired names, which are excluded from name resolution. `api.facts` and `api.relations` show current assertions only; `api.facts_history` and `api.relations_history` include retired/ended assertions (relations history lists original direction). `api.not_same` exposes distinctions. `api.catalog()` describes these resources and their column types. Registry GET routes retain all history for import reconciliation.

## Reserved channel field names

Channel fields cannot collide with standard message/view columns. Registration rejects collisions with HTTP 422 and lists reserved names. Reserved names: `id`, `channel_id`, `channel`, `external_id`, `version`, `thread_id`, `thread_no`, `happened_at`, `content`, `fields`, `lat`, `lon`, `person_id`, `superseded_by`, `happened_offset_min`, `ended_at`, `ingested_at`, `author_handle`, `author_identifier_id`, `images`, `loc_accuracy_m`, `loc_method`, `loc_reason`, `access_label`, `payload_hash`, `tsv`, `score`, `conversation_id`, `first_thread_no`, `last_thread_no`, `is_reference`, `is_outside`, `thread_external_id`, `thread_title`, `person_handle`, `display_name`, and the record contract columns `kind`, `record_kind`, `record_id`, `ref_id`, `record_time`, `is_current`, `subject_from`, `subject_to`, `subject_from_precision`, `subject_to_precision`, `provenance`, `support`, `expansion`, `generated`, `unsupported`, `claim`, `heading_path`, `char_start`, `char_end`. A regression test checks every actual `api.messages` column against validation so additions cannot silently drift.


## Authority, location tracks and SQL bounds

### Thread metadata and identity authority

An ingest thread accepts optional `metadata` (a JSON object). Keys are merged into
`tl.threads.metadata`, including multiple thread objects within one batch, and
are returned by `api.threads`. Omitted metadata preserves existing keys.

Assertion authority is always derived from the authenticating token. Legacy
`asserted_by` inputs are accepted as archival `original_asserted_by`, including
lossless Who imports, and cannot grant owner authority. An owner token may lower
authority by sending `asserted_by:"agent"`; the record then keeps agent
authority and does not protect the people it mentions. The Who importer sends
`owner` only for source rows labelled as owner assertions and `agent` for all
others. Agent tokens cannot add
identifiers or names to the owner or an owner-asserted person. Identifiers are normalized
by the core; a supplied `value_norm` is retained as `original_value_norm` while
the canonical norm controls uniqueness and author resolution. An unclaimed
identifier can be claimed atomically; reassignment to another person still
requires a merge. New references to merged people are rejected. Splits require
reverse dependency order and preserve later target names.

### Plugin link authority

Only owner tokens may write or change owner-method links. Plugin links require
source method; every message end must be in the plugin's own channels and any
other end must be an entity. Manifest `link_types` are descriptive and are not
enforced. A plugin can patch only source links written by its own plugin's
tokens; patching a missing link returns 404.
Entity merges require owner or agent authorization. Old message IDs continue to
resolve in `api.context` and `api.follow` after versioning.

### Location tracks and retirement

Nonempty plugin track writes require a token created by the owner with
`--location-source`, or an existing plugin token granted the capability with
`throughline token location-source ID` (`--off` withdraws it; the token itself
is unchanged). The source must still match plugin scope. The associated
point and source are visible through `api.message_locations`; closer later points
replace track-derived locations while explicit source locations take precedence.

No HTTP route withdraws track points. The owner runs
`throughline track retire (--source NAME | --point-id ID ...) [--reason TEXT]
[--restore]` with the owner database role. Withdrawn points are kept but ignored
by location attachment and stop detection; messages they located move to the
next nearest point within tolerance or lose the track location with
`loc_reason` `no_track_point_within_tolerance`. Run
`POST /v1/track/detect-visits` afterwards: the command does not recompute
visits built from those points.

### SQL bounds and deferred maintenance

SQL results retain their existing response shape and stop at a 4 MiB serialized
row-data budget as well as row_cap. An oversized aggregate row may therefore
produce zero rows and truncated=true; a row above 8 MiB of row text is never
sent to the API process. WITH queries and SQL-standard EXTRACT, AT TIME ZONE,
SUBSTRING, TRIM and POSITION are accepted. Every authenticated request, reads
included, is recorded in the audit log with its row count and HTTP status. Public message views compute dense thread
numbers during deferred backfill maintenance. Conversations of a thread awaiting
that maintenance are hidden on every read surface (NULL conversation fields, no
`api.conversations` row, no meaning-search hit) instead of being served stale.
An HTTP query gives pending maintenance a bounded wait first
(`THROUGHLINE_READ_MATERIALIZE_WAIT_S`, default 2 seconds); it never waits for an
ingest lock and never fails because maintenance failed or is slow, so a response
during a large backfill may still lack conversations for the threads being
written. Maintenance takes one thread per transaction and holds no lock across
network calls. The embedding worker does not activate a model, first or
replacement, while any thread awaits maintenance.

### Companion devices and the desktop bridge

See [companion.md](companion.md). A device token is a plugin token scoped to
`companion_<device>`, created by `throughline lib companion enroll`.

- `POST /v1/companion/hello` (device) `{platform, app_version,
  capabilities:[{name, description, allowed_plugins}], plugins:[{name, state,
  last_run_at, last_error}]}` replaces the device's declaration; returns
  `{device, pending}`.
- `GET /v1/companion/poll?wait=0..30` (device) claims pending requests:
  `{requests:[{id, capability, args, plugin, expires_at}]}`.
- `POST /v1/companion/requests/{id}` (device) `{ok, result}` or `{ok:false,
  error}`; answers larger than 1 MB are stored as failed.
- `POST /v1/companion/call` (O/P, never a device) `{capability, args,
  device?, timeout_s: 1..120 (default 30)}` waits for the answer: `200
  {device, result}`; `404 no_device`, `403 not_offered`, `503 offline`, `504
  timeout`, `502` failed (`detail: {reason, message}`).
- `GET /v1/companion/devices` (O) lists devices with `online` (polled within
  90 s), capabilities and connector states.
