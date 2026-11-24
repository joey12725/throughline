# Recall: the shared retrieval library

Migration `0038_retrieval.sql`, allow-list `allowlist/0038_retrieval.json`,
package `throughline.retrieval`, route `POST /v1/recall`, command
`throughline lib recall`. Requirements QRY-13..22, QRY-29, QRY-30, QRY-38,
QRY-40, MSG-7, MSG-9, BRN-5, BEN-21, CST-9, LIB-1.

```python
from throughline.retrieval import recall, RecallRequest
response = recall(scope, RecallRequest(query="orchard", k=10,
                                       scope={"channels": ["synthetic.chat"]}))
response.records      # Records: RECORD CONTRACT keys + retrieval keys
response.execution    # status, completeness, stages, timings_ms, ...
```

## Request

| Field | Meaning |
|---|---|
| `query` | Text to look for. Empty: enumerate the scope (structured leg). |
| `scope` | The hard scope (binding, below). |
| `hints` | Soft preferences for stages and planners. Never narrow or widen the scope; `hints.escalate` is ignored by recall (escalation is the route's business). |
| `k` | Records to return (1..1000). |
| `strategy` | `auto` (lexical, vector, entity; links with starting records; structured without a query), `keyword` (lexical), `meaning` (vector), `fused` (lexical + vector). |
| `legs` | Run exactly these legs (overrides `strategy`). |
| `rrf_k` | Fusion constant; default `THROUGHLINE_RRF_K` or 60. |
| `raw_only` | Raw message legs only (lexical, vector, structured): no entities, links or pages. |
| `expand`, `ranker`, `group_by` | Read by the stages of wave 3 (spans, rerank, select, group). |
| `budgets` | `deadline_ms`, `tokens`, `model_cost`, `model_calls`, `controller_turns`; only lower the token's limits. |
| `starting_sets` | Result-set handles or `m:<id>` record ids; their message ids become `scope.message_ids`. |
| `exclude_seen` | Handles, record ids or message ids removed before ranking (k results are still returned). Message records are excluded inside every leg function; other records (`l:`, `e:` ...) are removed after fusion, before selection. |
| `exhaustive` | Enumerate instead of sampling: no top-k cut (cap `THROUGHLINE_RECALL_EXHAUSTIVE_CAP`, 10,000). |
| `save`, `session` | Keep the records as a result set (`response.result_set`). |
| `trace` | Write a trace (ids, counts, statements; never excerpts) and return `execution.stage_candidates`. |

## Hard scope (QRY-18, QRY-20)

`persons` + `person_role` (`author`, or `involved`: author, a message by the
person in the same thread, or an active link to the person; both read
through the gated views, so a message the asker cannot see never makes
another one match), `channels`,
`kinds`, `after`/`before` with `time_basis`, `near {lat, lon, meters}` or
`place {name, meters}` (not both: 422), `thread_ids`, `message_ids`, `as_of`, `known_by`,
`include_reference`, `include_outside`, `must_terms`, `min_score`.

* One SQL predicate, `api.scope_filter(message_ids, S...)` (internally
  `tl.recall_scope`), is applied inside every leg function and by every
  statement that creates a hit, before ORDER BY and LIMIT. A post-filter of a
  global top-k is never used.
* `kinds`: message kinds default to `life_event` and `derived`; `reference`
  only when listed (or `include_reference`); `page_revision` is never returned
  by message legs (pages come from the passages leg). `link`, `entity`,
  `passage`, `claim` admit those record kinds; a scope listing only them
  disables the message legs.
* `time_basis` (MSG-9): `record` overlaps `[record_time, ended_at]`;
  `subject` overlaps the subject interval, where a missing bound or one with
  precision `unknown` is open and a record without any subject bound falls
  back to record time; `either` is the union.
* A place name resolves through `api.places` (exactly one coordinate), persons
  must exist in `api.people`, handles must be readable. Anything unresolvable
  returns no records and `execution.warnings` `unresolvable_scope:<field>`,
  never an unscoped search. People and places are unrestricted-only views, so
  a restricted asker cannot use those two scopes.
* `min_score` drops candidates whose best per-leg score is below it
  (candidates of unscored legs such as structured are kept).
* Dependency and context rows (wave 3 stages) pass the reduced scope: kinds,
  as_of, known_by, include_outside (`RecallContext.filter_context`).

## As of (BRN-5, BEN-21)

`as_of` is a record-time cutoff, `known_by` an ingest-time cutoff. The eligible
set of every message leg is `api.records_asof(coalesce(as_of, 'infinity'),
known_by)`: one version per record, matched on that version's own stored text,
so an old text is found and a later edit is not; messages ingested after
`known_by` are absent. Conversation spans are NULL under a cutoff. Links are
read from `api.links_asof(known_by, else as_of)` with validity tested at
`as_of` when the link history exists; otherwise only links created by the
cutoff, in their current state, are used and the leg reports
`as_of_honoured: false`. Entities carry no timestamp, so under a cutoff the
entity leg returns an `e:` record only when a link at the cutoff touches it. The vector leg is skipped
(`embeddings_current_only`): embeddings exist for current text only.
`execution.as_of_honoured` lists every leg considered.

## Legs (QRY-14)

| Leg | Function | Exact | as_of |
|---|---|---|---|
| `lexical` | `api.search_records` | yes | yes |
| `vector` | `api.vector_records` (exact cosine, one representative per conversation) | no | skipped |
| `structured` | `api.list_records` (by record time) | yes | yes |
| `entity` | `api.entity_search`, links to messages, `api.scope_filter` | no | with link history |
| `links` | links touching the starting records, `api.scope_filter` | yes | with link history |

Legs of a round run concurrently on a pool of
`THROUGHLINE_RETRIEVAL_CONCURRENCY` (4) threads, each statement one validated
SELECT on its own reader transaction, bounded by the remaining deadline and
`THROUGHLINE_LEG_TIMEOUT_MS`. Legs and stages are discovered by module scan
(`retrieval/legs/*.py` exporting `LEG`, `retrieval/stages/*.py` exporting
`STAGE`); `legs.register()` and `stages.register()` add others.

The entity leg returns `e:` records (support: the evidence messages of the
links used) and the linked messages as hits whose discovery names the entity.
The links leg returns `l:` records (support: evidence messages and the pinned
message ends) and the message ends that pass the scope. Only starting
records that satisfy the full hard scope (and the cutoff) are expanded: a
starting record outside the scope is never a source of hits. Entities have no
visible-source proof, so for a restricted asker the entity leg is skipped with
reason `not_visible_for_asker` (the response is degraded, not failed).

## Fusion, stages, selection (QRY-17, QRY-21)

Results of all rounds are fused by reciprocal rank (`1 / (k + rank)` per leg),
deduplicated by `record_id`; every leg's rank and score stay in `discovery`.
Ties break by reference id, so lexical + vector fuse exactly like the legacy
`api.search_fused`. Stages run by order (page_sources 5, spans 10, rerank 20,
dedup 30, select 40, group 50); a failing stage is skipped and reported.
Without a select stage the top k (all, when exhaustive) are hydrated and then
cut to the token budget (`truncation.reason` `token_budget`, `dropped`
reason `budget`).

## Planner protocol (QRY-15, QRY-21)

`initial(ctx) -> legs`; `next_round(ctx, candidates, round_no) -> (legs or
None, stop_reason)`; `after_recall(ctx)`. Rounds are bounded by
`THROUGHLINE_RECALL_MAX_ROUNDS` (2). The `planner` stage reports `rounds`,
`legs` and `stop_reason`; there is never a sufficiency flag. The default
planner runs one round; `retrieval/planner.py`, when present, replaces it.
Planners and stages may set `ctx.manifest`, append to `ctx.dropped`, write
`ctx.extras` (all copied into the response) and register
`ctx.after_recall` callbacks `(ctx, records)`.

## Records (MSG-7, QRY-29)

Every record has the RECORD CONTRACT keys (`record_id`, `kind`, ... `support`,
`expansion`) plus `span`, `conversation`, `score`, `discovery`, `role`,
`depends_on`, `groups`, `tokens`. `discovery` says why it was retrieved
(`[{leg, rank, score, detail}]`); `support` says which original messages it
may be cited for. Page revisions and derived records are `generated`;
without provenance sources they are `unsupported`. `retrieval.hydrate` is the
only hydrator: one statement per prefix over the view registered in
`tl.record_sources` (`m` `api.record_versions` or `api.records_asof` under a
cutoff, `l` `api.link_records`, `e` `api.entity_records`, `p`, `c` from later
packages). `hydrate.records_for_members(scope, members)` serves result sets.

## Execution (QRY-22, QRY-38, CST-9)

* `status`: `complete`, or `degraded` when a leg was unavailable
  (`embeddings_unavailable`, `embeddings_current_only`,
  `not_visible_for_asker`, ...), timed out, or failed, or a stage failed.
  Reasons never carry error text or data.
* `completeness`: `ranked_sample` (a ranked result is a sample: never count
  from it), `exhaustive` (`exhaustive=true`, every leg complete, nothing cut)
  or `truncated`.
* `exactness`: `exact`, `mixed` or `approximate` from the `exact` flag of
  every leg and stage entry.
* `timings_ms`: `materialize`, `database`, `retrieval`, `leg:<name>`,
  `embedding`, `fusion`, `planner`, `stage:<name>`, `hydrate`, `total`.
* `counts`: `retrieved`, `returned`, `per_leg`, ...

recall never calls a semantic model or a controller (QRY-40).

## SQL functions

All are SECURITY DEFINER, read only gated api views, and are on the query
allow-list:

* `api.scope_filter(message_ids bigint[], S...)` -> `message_id`
* `api.search_records(query, max_results, S..., message_ids)`
* `api.vector_records(query_embedding, max_results, S..., message_ids,
  query_text, serving_model)` (raises under `as_of`/`known_by`)
* `api.list_records(S..., message_ids, max_results)` (NULL = all)
* `api.entity_search(query, max_results)` -> `kind, id, name, score`
* `api.context_many(message_ids, before, after)`

`S` is, in order: `kinds, channels, persons, person_role, after_at, before_at,
time_basis, lat, lon, meters, thread_ids, include_outside, as_of, known_by,
must_terms, exclude_message_ids`. `api.describe('recall')` holds the same
summary.
