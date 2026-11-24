# Semantic operators: expand and match

Modules `throughline.retrieval.expand` and `throughline.retrieval.match`;
operators `throughline.semantic_ops.expand` and `throughline.semantic_ops.match`
(SQL `api.expand`, `api.match`, plan ops `expand`, `match`); routes
`POST /v1/expand`, `POST /v1/match` (`throughline.api.routes.semantic_ops`);
commands `throughline lib expand HANDLE MODE` and
`throughline lib match HANDLE CONDITION`; migration `0041_semantic_ops.sql`;
allow-list `allowlist/0041_semantic_ops.json`.

Requirements: QRY-32 (expand), QRY-33 (match), QRY-27 (explicit expansion),
CST-5 (cache keys), CST-6 (scope-checked cache), CST-10 (cost gate), ACL-8,
QRY-36 (execution metadata), INT-10 (one record shape), LIB-4 (match only)
and the analytics invariants of design priority P7.

Both operators take a result set and return a new one. The HTTP routes and
the SQL runtime return the one envelope `{records, result_set, execution}`;
records are full Records read back through `resultsets.read`, the hydrator
recall and sets use, so the keys are identical everywhere.

## expand

```
expand(scope, *, handle=None, ids=None, mode, before=3, after=3, link_type=None,
       depth=1, as_of=None, known_by=None, window_s=0, meters=100, focus=None,
       k=20, within=None, token_budget=None, tracker=None, execution=None,
       keep_input=True, role="context") -> handle
```

```sql
SELECT * FROM api.expand(input => 'rs_...', mode => 'context', before => 2, after => 2);
```

| Mode | Adds | Exact |
|---|---|---|
| `context` | thread neighbours, `before`/`after` (at most 50) around each message member's span (`span.first_no..last_no`, else its `thread_no`) | yes |
| `thread` | every message of the members' threads, capped by `THROUGHLINE_EXPAND_THREAD_CAP` (1,000) | yes; truncated when capped |
| `sources` | `api.sources` of generated members (page revisions, derived messages, passages, claims, links, entities) plus their support; a raw message is its own source | yes |
| `links` | `api.follow_many` from message, entity and link members: message ends as messages, entity ends as `e:` records, traversed links as `l:` records; `link_type`, `depth` (at most 4) | yes |
| `time` | messages whose `[happened_at, ended_at]` overlaps a member's, widened by `window_s` (`api.time_overlap_many`) | yes |
| `place` | messages within `meters` of a member's location (`api.near_many`) | yes |
| `focus` | the union of context, links and sources, ranked against `focus` by `retrieval.recall` restricted to those messages (`scope.message_ids`); the best `k` | no (ranked_sample) |

Guarantees:

- **Never a hit.** Added members have role `context` (or `dependency` when the
  caller passes `role`), `depends_on` = the anchor's record id and
  `discovery = [{leg: 'expand:<mode>', detail: {anchor}}]`. With
  `keep_input` (default) the input members come first, unchanged; an input
  member is never added again.
- **Scoped.** Every added message id passes `api.scope_filter` with kinds,
  as_of, known_by and include_outside (the reduced filter of QRY-20; the
  default kinds are life_event, derived and the kinds of the input's own
  messages), plus the caller's whole `within` hard scope when given
  (channels, persons, time, place, threads, message ids, must terms). Added
  `e:`/`l:` records are checked through their gated record views and must be
  admitted by `within.kinds`. The statements read only gated api views under
  the caller's scope, so a hidden row is never reached (ACL-8).
- **as_of inherited.** The input set's `as_of`/`known_by` apply unless the
  caller names its own (an explicit argument wins over `within`, which wins
  over the set). `api.scope_filter` then keeps the version valid at the
  cutoff; links mode follows `api.links_asof` at the cutoff (known_by, else
  as_of) and tests validity there. The output set carries the cutoffs.
- **Set-based.** Each mode runs a fixed number of statements, each over all
  anchors at once (`unnest` of the anchors, `api.*_many`, one
  `api.scope_filter`), whatever the number of anchors.
- **Bounded.** At most `THROUGHLINE_EXPAND_MAX_INPUT` (2,000) input members,
  else a 400. At most `THROUGHLINE_EXPAND_MAX_OUTPUT` (5,000) added members.
- **Completeness.** Exact modes are `exhaustive` unless capped (`truncated`);
  focus is `ranked_sample`; a non-exhaustive input caps the output at its own
  completeness (the worse of the two).

## match

```
match(scope, token_row, *, handle, condition, max_rows, grain='record',
      tracker=None, execution=None, approval_id=None) -> handle
```

```sql
SELECT * FROM api.match(condition => 'mentions a prototype',
    input_sql => $$SELECT id FROM api.search_filtered('prototype', 200)$$,
    max_rows => 200)
WHERE meta->>'verdict' = 'match';
```

Every member is judged by the semantic provider (`providers.get('semantic')`,
operation `match`; the deterministic local rule model by default). The output
keeps the input order, roles, scores and the `generated` flag (a verdict is
operator output in `meta`, never record text). Each member's meta:

| Key | Value |
|---|---|
| `verdict` | `match`, `nonmatch`, `uncertain` or **null** |
| `evidence` | `[[start, end], ...]` character offsets into the member's text (at most 20) |
| `evidence_clamped` | true when an offset was out of range or malformed (clamped or dropped) |
| `provider`, `model`, `operator_version`, `cached` | who judged it, and whether the verdict came from the store |
| `reason` | `not_evaluable` for a member without text |
| `error` | why the evaluation failed (`provider_unavailable`, `budget_exhausted`, `timeout`, `invalid_output`, `provider_error`) |

The member's group is its verdict (`match`, `nonmatch`, `uncertain`).

### Three verdicts and NULL

- `match` / `nonmatch`: what the model judged.
- `uncertain`: the model said uncertain, **or** the member has no text
  (`meta.reason = 'not_evaluable'`, counted in `counts.not_evaluable`). Nothing
  else is uncertain.
- **NULL**: the evaluation failed (provider error, timeout, invalid output,
  budget exhausted). The failed batch's members get `meta.verdict = null` and
  `meta.error`; they are never `uncertain`, never `nonmatch`, never stored,
  counted in `counts.failed`, and the execution is flagged `partial: true`
  with an incomplete `match` entry (status degraded). The output completeness
  stays the input's; the failure is reported beside it, not hidden in it.

### Counts

`execution.counts` and the output set's counts keep apart: `retrieved`,
`mechanically_processed`, `semantically_evaluated` (= `applied + cached`),
`applied`, `cached`, `not_evaluable`, `failed`, `match`, `nonmatch`,
`uncertain`, `units` (and `episodes` for episode grain).

### Cache keys (CST-5, CST-6)

`tl.verdicts` holds one row per `(record_prefix, ref_id, version,
sha256(text), operation, sha256(normalised condition), model,
operator_version)`: **content + rubric + model**. The condition is normalised
by NFC and whitespace collapsing only. `operator_version` is
`match-1/<provider version>`. A stored verdict is reused only for a member
the caller's `resultsets.read` returned (so nothing surfaces for a hidden
record) and only when every message in its `source_message_ids` is visible to
the caller now (checked in the database with `api.message_visible`).
`source_message_ids` is the message itself, the revision and sources behind a
passage or claim, the message ends and evidence of a link, or every member
message of an episode. The table has no api view and the reader role cannot
read it. Purge hook `verdicts` deletes rows whose sources overlap the purged
channel. Failed evaluations are never stored.

### Cost (CST-10)

The bound `members <= max_rows <= THROUGHLINE_MATCH_MAX_ROWS` (500) is checked
before anything is read or sent. The members without a servable verdict are
then estimated (`SemanticModel.estimate`), and the estimate passes
`cost.authorize` with `cost.request_hash({op, condition, grain, max_rows,
operator_version}, scope, [(handle, member count)], budgets)` and the request's
tracker. Over the limit, `cost.ApprovalRequired` is raised before any call;
over HTTP that is `execution.status = 'approval_required'` with the approval,
HTTP 200, no records and zero calls. An approved request runs once. Batches
then run through `providers.call` (ledger row, trace event, budget).
`POST /v1/match {dry_run: true}` returns the estimate and counts only.
In SQL the runtime estimates and authorizes the whole statement with the
context the operator then runs with, and the operator does not authorize
again. Any call of the operator whose estimate was not taken with that
context (for example a plan node whose input set comes from an earlier node,
so the plan could not estimate it) passes its own estimate through
`cost.authorize` before any call; a plan node over the limit fails with
approval required instead of producing NULL verdicts. A caller that has
authorized the estimate itself sets `ctx.extras['authorized'] = True`.

### Episode grain (P7)

`grain => 'episode'`: message members are grouped with `api.episodes_of`
(non-message members stay record grain). Each episode is judged once on its
members' texts in time order joined by a blank line, capped at
`THROUGHLINE_MATCH_GROUP_MAX_TOKENS` (4,000 tokens; `meta.truncated` when cut),
under the key `('ep', first message id of the episode, 0, sha256(text), ...)`.
The output has one anchor member per episode (role `hit`, meta `{grain:
'episode', episode_id, verdict, evidence: [{record_id, offsets}], members}`);
the other members are role `context` with `depends_on` = the anchor and **no
verdict**. A group-grain verdict is never copied onto individual messages,
and `tl.verdicts` holds only the `ep` row.

### match versus QRY-8

`api.match` is the three-valued ad hoc operator; its verdicts are cached by
content, rubric and model. Numeric policy scores (QRY-8) stay with the job
runner (M4).

## Analytics invariants

1. **Counts before text.** Count the population with SQL first
   (`SELECT count(*) FROM api.result_set(handle)`; free, no provider call), and
   `POST /v1/match {dry_run: true}` for the estimate.
2. **Frozen population.** The set's pinned versions are judged, never the
   current text; the key uses the pinned text's hash.
3. **Content + rubric + model key.** A change of text, condition or model is
   a new verdict; another set, order, subset, session or `max_rows` is not.
4. **NULL, never zero.** A failed evaluation is null; it never counts as
   nonmatch or uncertain.
5. **Partial is flagged.** `counts.failed`, `execution.partial`, an incomplete
   `match` entry and the set's `origin.partial`.
6. **Estimate, then approve.** Nothing is spent before the estimate passes the
   cost gate; approval_required leaves no ledger row.
7. **Verdicts outlive sets.** The store survives the expiry of the set that
   produced it; a new set over the same members is served from it.
8. **Group grain stays with the group.** Episode verdicts are never copied to
   members.

## SQL functions (allow-listed)

| Function | Rows |
|---|---|
| `api.time_overlap_many(message_ids bigint[], window_s int DEFAULT 0)` | `(anchor_id, message_id)` |
| `api.near_many(message_ids bigint[], meters float8)` | `(anchor_id, message_id)` |
| `api.follow_many(kind text, ids bigint[], link_type text DEFAULT NULL, depth int DEFAULT 1, at timestamptz DEFAULT NULL)` | `(anchor_id, kind, id, depth)`; kind `link` rows are the links traversed; with `at`, the links as of that time (`api.links_asof`, `api.applies_at`) |

They read gated api views only and return candidate versions; the operators
pass every message through `api.scope_filter`.

## Not built here

`extract`, `compare`, `relate` and `summarize` are reserved operation names of
`SemanticModel.apply` and of the verdict store's `operation` column; they are
not built (LIB-4 deferred).
