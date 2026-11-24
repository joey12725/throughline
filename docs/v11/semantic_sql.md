# Semantic SQL, plans and evidence packets

Modules `throughline.semantic`, `throughline.semantic_ops` (operator
interface and `recall`), `throughline.plans`, `throughline.packets`; changes
in `throughline.query.query_ex` and `throughline.api.deps.audited`; routes
`POST /v1/sql` (new fields), `POST /v1/plans`, `POST /v1/packets`; commands
`throughline lib plan run FILE [--dry-run]` and `throughline lib packet
HANDLE`; migration `0040_semantic.sql`; allow-list
`allowlist/0040_semantic.json` (`episodes_of`).
Requirements: QRY-31, QRY-36, QRY-37, QRY-38, QRY-41, QRY-42, QRY-43, QRY-13
(SQL form), CST-4, CST-10, LIB-5, LIB-9, INT-8, INT-10; design priority P7 (episode
grain, hit-count warning, evidence fields in packets).

## Semantic functions in SQL

A semantic function is an operator run by the API process, not by Postgres:

```sql
SELECT r.record_id, r.score, m.channel
FROM api.recall(query => 'orchard', k => 10) r
JOIN api.messages m ON m.id = r.message_id
ORDER BY r.ordinal
```

`api.recall` ships here; `api.expand`, `api.match` (w3-semantic-ops) and
`api.librarian` (wave 4) use the same runtime. Each is a row of kind
`semantic` in `tl.api_functions`; none is a Postgres function and none is on
the query allow-list (`query.FUNCTIONS`), so a statement that bypassed the
runtime fails validation.

`query.query_ex` asks `semantic.find_calls(statement)`. With no semantic call
nothing changes. Otherwise `semantic.run`:

1. checks placement and arguments, replaces every call by
   `api.result_set('rs_000…')` (keeping the alias) and runs `validate_sql`
   **before any work**. With `strict_exact` the exactness check also runs
   here, assuming a non-exact operator yields a ranked sample;
2. clamps the budgets to the token (a caller can only lower them) and opens
   one `BudgetTracker`, one `Execution` and one `Trace` for the statement;
3. resolves `input_sql` sets (below);
4. collects every operator's estimates; `dry_run` stops here and returns no
   rows with `execution.plan` and `execution.estimate`;
5. passes the cost gate once: `cost.authorize` with
   `cost.request_hash(statement, scope, inputs with member counts, budgets)`.
   Over the limit the response is HTTP 200 with no rows,
   `execution.status = 'approval_required'`, `execution.approval` and zero
   model calls. The approval runs exactly this statement once, for the same
   token, asker, input member counts, budgets and access policy version
   (`approval_id` on the next request);
6. runs the operators concurrently under the tracker; a failing operator
   fails the statement with 400 unless it is `degradable` (then its set is
   empty and `truncated`, and the execution is degraded); an operator still
   running at the deadline is a failure;
7. splices the real handles, validates again and runs the statement through
   the unchanged reader path (`query_ex` with the same Execution,
   `materialize=False`: deferred maintenance is waited for once per
   statement).

The response keeps the shape `{rows, truncated, row_count, execution}`.
`execution.semantic_calls` lists each call's name, alias, handle,
completeness and member count; `execution.stages` has one
`semantic:<name>` stage per call; `execution.trace_id` names the trace (its
events hold statements, ids, counts and hashes, never excerpts).
`query.query()` still returns exactly `{rows, truncated, row_count}`.

### Placement rules (QRY-37)

Allowed: the **single function of a FROM item** of a non-recursive query
level (the top level, a CTE or a FROM subquery). Every violation is HTTP 400
`semantic function api.<name> is not supported here: <construct>`, raised
before anything runs:

| Construct | Example |
|---|---|
| target list, WHERE, JOIN ON, GROUP BY, HAVING, ORDER BY, window, VALUES, LIMIT | `SELECT api.recall('x')` |
| LATERAL | `FROM api.messages m, LATERAL api.recall(m.content) r` |
| WITH ORDINALITY, column definition list, ROWS FROM with several functions | `FROM api.recall('x') WITH ORDINALITY r` |
| recursive CTE | `WITH RECURSIVE c AS (SELECT * FROM api.recall('x')) ...` |
| column-reference, subquery, function-call or parameter argument | `api.recall(query => (SELECT 'x'))` |
| unknown, repeated or missing parameter; positional after named | `api.recall(nope => 1)` |
| more than `THROUGHLINE_MAX_SEMANTIC_CALLS` (4) calls | five calls |
| `top_level_only` operator (librarian) not alone in the top-level FROM | |

Arguments are constants: positional or named, casts of constants and
`ARRAY[...]` of constants. Call spans are byte offsets into the UTF-8
statement taken from the parser's token stream, so parentheses inside string
literals and multi-byte text are handled.

### Input sets

An operator's input parameter (for example `input` of expand and match) is
given as a handle, `input => 'rs_<32 hex>'`, or as SQL,
`input_sql => '<SELECT>'` with a mandatory `max_rows => 1..10000`. The nested
statement must pass `validate_sql`, may not contain a semantic call, and must
return a column `record_id`, `passage_id`, `message_id` or `id`. Its rows are
saved as one of the caller's sets. A handle that is not the caller's (other
token or asker, expired, earlier policy) is a 400 before anything runs.

## Exactness: the taint analysis (QRY-38)

`semantic.analyze()` runs on every statement (semantic or not). A relation is
**not a population** (tainted) when it is:

- a ranked function (`tl.api_functions.ranked_limit_arg`: `search`,
  `search_all`, `search_filtered`, `search_meaning`, `search_fused`,
  `search_records`, `vector_records`, `search_passages`, `vector_passages`,
  `entity_search`) whose limit argument is omitted (the default applies) or
  anything but an explicit `NULL`;
- `api.result_set` of a set whose completeness is `ranked_sample` or
  `truncated`;
- a query level with `LIMIT`/`FETCH`;
- a table read with `TABLESAMPLE`;
- any subquery or CTE that reads a tainted relation, also through a
  `WHERE ... IN (SELECT ...)` or `JOIN ... ON ... IN (SELECT ...)` filter.
  Taint propagates outward through references and joins.

`count`, `sum`, `avg`, `min`, `max`, `array_agg`, `jsonb_agg`, `json_agg`,
`DISTINCT`, `GROUP BY` and window aggregates over a tainted relation add the
warning `aggregate over a ranked top-k or sampled set, not a population`;
with `strict_exact` they are rejected with 400 before the statement runs.
`execution.taint` names the tainted relations (alias or CTE name) with the
reason. Every statement that reads a handle lists it in `execution.handles`
with its completeness.

The same analysis decides the completeness of an `input_sql` set:
`exhaustive` only when the statement is untainted and `max_rows` did not cut
it, `ranked_sample` when it is tainted, `truncated` when it was cut.

### Grain: a hit count is not an incident count (P7)

`count(*)` or `count(x)` over the rows of a search or ranked function, or of
a recall, expand or match set, at record grain adds the warning code
`hit_count_not_incident_count` (in `execution.warning_codes`, with the advice
in `execution.warnings`). A level that counts `episode_id` from
`api.episodes_of` (or groups by it) is at episode grain and does not warn.
This is a warning only, even under `strict_exact`.

`api.episodes_of(message_ids bigint[])` returns `message_id, episode_id,
thread_id, first_message_id, started_at, ended_at, message_count`. For the
threads of the given messages it builds gap-based sessions over the current
messages in `thread_no` order: a new session starts when the `happened_at`
gap exceeds the channel's `conversation_gap_s`; a channel with
`conversation_whole_thread` is one session per thread. `episode_id` is
`ep:<thread_id>:<first message id of the session>` (`ep:m:<id>` without a
thread), the same on every call while the thread does not change. A late
message that bridges two sessions merges them. It reads only the gated
`api.message_versions`, so a restricted asker gets rows only for visible
messages and sessions (and `message_count`) are built from visible messages
only. More than 10,000 ids is an error (SQLSTATE 54000).

```sql
SELECT count(DISTINCT e.episode_id) AS incidents
FROM api.episodes_of(ARRAY(SELECT id FROM api.search_filtered('orchard', NULL))) e
```

The catalogue section `grain` (`api.describe('grain')`) states the units
record, episode, day and conversation and the stability caveat.

## Operator interface (`throughline.semantic_ops`)

A module in `src/throughline/semantic_ops/` exporting `OP = SemanticOp(...)`
adds an operator (tests and plugins may `register()` one):

| Field | Meaning |
|---|---|
| `name` | function name; SQL form `api.<name>(...)`, plan op `<name>` |
| `params` | `{name: type}` in positional order; types `text, int, float, bool, json, timestamptz, text[], int[], handle` |
| `required` | parameters that must be given |
| `input_params` | parameters that take a result set (handle, or `input_sql` + `max_rows` in SQL) |
| `exact`, `cost_class`, `version` | as in the operator contract |
| `top_level_only` | only alone in the top-level FROM (librarian) |
| `degradable` | a failure leaves an empty truncated set instead of failing |
| `estimate(ctx, args)` | `[providers.base.Estimate]`, before anything runs |
| `run(ctx, args)` | returns the handle of a new set owned by `ctx.scope` |

`OpContext` carries `scope`, `token_row`, `tracker` (the request's one
`BudgetTracker`), `execution`, `trace` and `in_investigation`. Operators
spend only through `providers.call(..., tracker=ctx.tracker)`, read only
under `ctx.scope`, receive input parameters already resolved to handles and
never return rows.

`recall` maps flat arguments (`query, k, channels, kinds, person, persons,
after, before, time_basis, as_of, known_by, raw_only, expand, strategy,
ranker, tokens, deadline_ms, group_by, exhaustive, include_ignored, options`)
onto `RecallRequest`; `options` (JSON) takes any other request field and is
merged under the flat arguments. It always saves (`save` on) and strips
`hints.escalate`: escalation is a route decision, never part of a statement
or a plan. Its estimate is empty (recall never calls a semantic model or a
controller).

## Plans (QRY-41, QRY-42)

`POST /v1/plans {plan: {nodes: [{id, op, args, inputs}]}, budgets, dry_run,
approval_id}` (owner or agent; restricted askers too: everything runs on the
reader path and writes only their own sets).

| op | args | inputs |
|---|---|---|
| `sql` | `statement` with `{{id}}` placeholders (replaced by the quoted handle), `max_rows` (default 1000) | from the placeholders |
| `recall`, `expand`, `match` | the operator's parameters | `{param: node}` |
| `transform` | `op` (union, intersect, subtract, filter, order, head, sample, dedup, group, ungroup), `params` | `[node, ...]` |
| `inspect` | `view`, `selector`, `token_budget` | `[node]` |

`librarian` is never accepted. `plans.validate` checks the whole plan first:
unique ids, known ops, arguments against each operator's parameters, inputs
that name nodes, no cycle, at most `THROUGHLINE_MAX_PLAN_NODES` (24) nodes,
and every sql statement validated with its placeholders replaced (semantic
calls are refused there). `plans.execute`:

- estimates, then passes the cost gate once (approval_required: nothing
  runs); `dry_run` returns the validated plan and the estimates;
- waits for deferred maintenance once per plan;
- dispatches every ready node concurrently; before dispatch each ready node
  gets `remaining model cost / ready nodes` (and its share of model calls),
  reserved from the one tracker, and runs under its own sub-budget with the
  remaining deadline; afterwards the reservation is replaced by what it spent;
- isolates failures: a failed node's dependants are skipped with the reason,
  the rest stands and the status is `degraded`;
- dispatches nothing after the deadline (skipped, reason `deadline`);
- assembles `{handles, previews, execution}` in plan order. `previews` has
  `{handle, completeness, member_count, sample}` per set and the inspect
  result per inspect node; `execution.nodes` has every node's status.

## Evidence packets (LIB-9, QRY-43)

`packets.build(scope, handle, notes=, unresolved=, token_budget=,
investigation=)` and `POST /v1/packets {handle, notes, unresolved,
token_budget}` return:

`investigation_id, result_set_ref, execution_status, records[],
orientation[], coverage, evidence_adequacy, unresolved_needs,
truncation_and_omissions, cost_and_latency, continuation_ref, manifest`.

Records come from **one** `resultsets.read(scope, handle)`, so every field is
copied from the gated record views under the caller's scope: `record_id`,
`revision_id {message_id, version}`, `kind`, `original_excerpt` (the stored
text), `span`, `event_time` (subject start, else record time),
`recorded_time` (record time), `ingested_time`, `applicable_time {from, to,
precisions}`, `source_refs` (support), `claim_metadata`, `generated`,
`unsupported`, `evidence_class` and `attribution` (copied from the record
when present, else null), `discovery_reasons`.

Notes (`{generated_text, supporting_refs, uncertainty}`) become
`orientation` entries flagged `generated: true` only when they have at least
one supporting ref, every ref matches a delivered record on `record_id` and
`version`, and every record id written in the text (a prefix registered in
`tl.record_sources` at build time, followed by `:digits`) is delivered. A
dropped note is listed in `truncation_and_omissions` with the reason. Note
text is never placed in a record field.

`execution_status` (how the read went) and `evidence_adequacy`
(completeness, records delivered and omitted, notes kept and dropped,
unresolved needs) are separate; there is no single complete flag. Over the
token budget the packet carries the records that fit, a `manifest` of the
rest and `continuation_ref {result_set, offset, investigation_id}`.
`coverage.known_source_gaps` stays empty until source coverage (wave 4)
fills it.

## Catalogue

Migration 0040 adds the `recall` row (kind `semantic`), the `episodes_of`
row (kind `sql`, exact, free, no ranked limit) and the sections
`semantic_sql` (subset, every rejection, taint rules, examples), `plans`,
`envelope` (the one result envelope `{records | rows, execution,
result_set?, manifest?}`) and `grain`.
