# Ranking, spans, passages in recall, evidence class

Package `throughline.retrieval`: `ranking.py`, `stages/spans.py` (order 10),
`stages/rerank.py` (order 20), `legs/passages.py`, plus `hydrate.evidence_class`
and the annotation scope plumbing in `types.py` / `scope_sql.py`. No migration.
Requirements QRY-17, QRY-20, QRY-22, QRY-23, QRY-25, CST-5, BRN-5, BRN-6,
BEN-9, BEN-16, INT-12; design priority P7 (evidence class) and P4a (annotation scope).

```python
from throughline.retrieval import recall, RecallRequest
response = recall(scope, RecallRequest(query="orchard ladder", ranker="cross_encoder"))
response.execution["ranker"]   # {name, version, exact, cost_class, provider, ...}
[r["evidence_class"] for r in response.records]
```

## Rankers (QRY-23)

A ranker orders the top candidates after fusion. Interface (`ranking.Ranker`):

| Member | Meaning |
|---|---|
| `name`, `version` | `version` changes whenever the order for one input may change; it is part of every reported order and cache key. |
| `exact` | Whether the ranker keeps an exact result exact (the rerank stage reports it; `execution.exactness` follows). |
| `cost_class` | `free`, `embedding` or `model`. |
| `needs_text` | The stage hydrates the top candidates through the gated record views first; only text the asker may see is ever handed over. |
| `estimate(ctx, items)` | `[providers.Estimate]`, empty for free rankers. |
| `rank(ctx, query, items)` | `[(item, score)]` best first; items are `RankItem(candidate, text, message_ids)`. |

Built in, each registered as an `OperatorSpec` with `implements='rank'`:

| Name | Order | Exact | Cost |
|---|---|---|---|
| `fused` (default) | reciprocal rank fusion, unchanged (QRY-17) | yes | free |
| `lexical` | the keyword score the lexical leg (or the passages keyword search) gave | yes | free |
| `vector` | the similarity the vector leg (or the passages vector search) gave | yes | free |
| `cross_encoder` | the rerank provider's score of (query, text); its version is `1+<provider>:<provider version>` and execution names the provider, its version and model | no | model |
| `learned` | placeholder: always unavailable (deferred research, BEN-16 first) | no | model |

Choice: `request.ranker`, else `THROUGHLINE_DEFAULT_RANKER`, else `fused`. An
unknown `request.ranker` is the caller's error (400, raised before any work);
an unknown `THROUGHLINE_DEFAULT_RANKER` keeps `fused` and adds a warning.

Adding a ranker: subclass `Ranker`, set `name`, `exact`, `cost_class`,
`needs_text`, implement `rank` (and `estimate` if it may cost), and call
`ranking.register(MyRanker())`. A ranker that asks a model for scores uses
`ranking.score_cached`, never the provider directly.

### The rerank stage (order 20)

Enabled when the ranker is not `fused`. The top `THROUGHLINE_RERANK_TOP_N`
(50) candidates are reranked; the rest keep their place after them. Each
reranked candidate gets a discovery entry
`{leg: 'rerank', rank, score, detail: {ranker, version}}`. A candidate whose
record does not hydrate for the asker sends no text and is placed after the
reranked ones (it would not be returned anyway). A non-local provider passes
`cost.authorize` before anything is sent (the token's `max_model_cost`, the
caller's `budgets.model_cost` and the request budget; over the limit a
pending approval is created). When the ranker cannot answer the fused order
is kept and the stage is reported `failed` with reason `provider_unavailable`,
`timeout` or `approval_required` (with `approval_id`): the recall is
`degraded`, never failed (QRY-22).

### Cached scores (CST-5)

`ranking.score_cached(ctx, query, items)` is the only path from a ranker or
the spans stage to the rerank provider. Key: `(operator, provider version,
model, sha256(query), sha256(text))`; sources: the messages behind the text
(the record itself, a passage's revision and support, a window's messages).
Entries are read with `cache.get(key, ctx.scope)`, so an entry with a source
the asker may not see is a miss and is scored again from the asker's own
text. Only misses reach the provider, in one call; a second identical recall
makes no provider call, and an edited message causes calls only for texts
that contain it. Purging a channel removes its entries (the cache purge hook).

## Spans inside conversations (QRY-25, QRY-20)

The vector leg ranks conversations and names one representative message per
conversation (the legacy rule of `api.vector_records` and QRY-10). The spans
stage turns each such candidate (among the first `max(20, 2k)` candidates)
into up to `THROUGHLINE_SPANS_PER_CONVERSATION` (3) spans:

- The messages of every conversation are loaded in one statement over the
  gated `api.conversations` and `api.messages`.
- **Anchor rule.** A span is anchored only on a message that passes
  `api.scope_filter` with the full hard scope (persons with role, time with
  time basis, place, channels, kinds, terms, exclusions). A neighbour that is
  out of scope may be part of a window only as context and only when it passes
  the reduced filter (kinds, as_of, known_by, include_outside); any other
  message breaks the window. A span therefore never makes an out-of-scope
  message a hit.
- Windows of 1..`THROUGHLINE_SPAN_MAX_MESSAGES` (3) consecutive messages that
  contain an anchor and share a query term are scored as text with
  `score_cached` (operator `spans`). Windows within
  `THROUGHLINE_SPAN_MARGIN` (0.2, relative) of the best are taken best first,
  shorter first on equal scores (minimal spans), never overlapping.
- Each span becomes a candidate anchored on its best in-scope message, with
  `span {first_no, last_no}`, the conversation, the conversation hit's fused
  score and discovery, and `meta.span {score, message_ids}`.
- No window shares a query term with the query: the single representative is
  kept unchanged. `expand: false` or `hints.single_span: true`: the stage does
  not run and the legacy one-row-per-conversation result is returned.
- No message vectors are stored or computed: one vector per conversation (and
  per distinct passage text) stays the rule (EMB-2); spans are scored as text
  at query time and only their scores are cached.

**QRY-10 restated.** "The best message of a conversation" is the behaviour of
the legacy `api.search*` functions and of recall with `single_span`. Recall
may return several spans per conversation and non-message records.

## Page passages in recall (BRN-5, BRN-6)

Leg `passages` (exact, honours as_of, included by `strategy: auto`) calls
`api.search_passages` and, when embeddings are available and neither as_of
nor known_by is set, `api.vector_passages`; the two lists are fused inside the
leg (reciprocal rank, ties by passage id) and the discovery detail keeps both
ranks and scores. Candidates are `p:<passage id>` records of kind `passage`,
hydrated through `api.passage_records` like every kind: heading path,
character offsets into the revision's full text, the revision as
`message_id`, support from the passage's (or the revision's) sources and
`unsupported` when there are none.

- Revision eligibility is decided in SQL: the latest revision the asker may
  read by default (a revision citing a hidden source falls back to the latest
  authorized one), the revision valid at `as_of`, stored by `known_by`.
- Scope arguments passed: channels, as_of, known_by, after/before,
  time_basis, thread_ids, message_ids (revisions), must_terms, exclusions.
- Persons and place reach passages through provenance sources: the in-scope
  source messages are enumerated first with `api.list_records` (persons with
  role, place, include_outside, as_of/known_by; every non-page kind, ignored
  or not) and passed as `source_ids`. More than `THROUGHLINE_SCOPE_SOURCE_CAP`
  (5,000) sources skips the leg with reason `scope_too_broad_for_passages`;
  no in-scope source means no passage, never an unscoped search.
- Unavailable (`skipped` with a reason) with `raw_only`, without a query, or
  when the caller's `kinds` name neither `passage` nor `page_revision`
  (decided on the caller's kinds, never on the rendered SQL list).

## Evidence class (design priority P7)

Every record carries `evidence_class`, the last retrieval key. It is computed
by one pure function, `hydrate.evidence_class(record)`, from contract fields
only, inside the shared hydrator, so recall, result sets
(`records_for_members`), expand, match and packets carry the identical value.
It is never part of the SQL record contract: every record view keeps its
exact column list.

| Class | Records |
|---|---|
| `primary` | life_event and reference messages (not generated) |
| `normalized` | page revisions, derived records and passages with support; entities and links that are not generated (source-stated structure) |
| `derived` | observation claims with sources; generated links made by a rule, and generated links that expose no method; generated entities or messages that are not unsupported |
| `inference` | inference, hypothesis and judgment claims; links made by a model; any generated record with `unsupported` true |

The rules are one dict keyed by kind (`hydrate.EVIDENCE_RULES`); a later kind
adds one entry (w6-commitments: `commitment`). A kind without a rule gets
`derived` when generated and `primary` otherwise, with a warning once per
process. A link's method is read from its `provenance.method` (or
`claim.method`). The `generated` flag (INT-12) is unchanged.

## Annotation scope plumbing (design priority P4a)

`HardScope.include_ignored` (default false) and `time_basis: effective` are
accepted and passed through; their SQL meaning is `tl.recall_scope` from
migration 0042 (records whose effective annotation state is ignored are eligible
only when the SQL kinds list holds the pseudo-kind `ignored`; `effective`
compares the effective interval with the window).

- `scope.kinds` is never rewritten and `ignored` is not a public kind (it is
  refused like any unknown kind). `message_kinds()` and `admits()` stay driven
  by the caller's kinds, so `include_ignored` never switches off the
  passages, entity, link (or later claim and commitment) legs.
- Only the rendered SQL kinds list changes (`scope_sql.values()`/`args()`,
  full and reduced alike): default kinds (`life_event`, `derived`, plus
  `reference` with include_reference) plus `ignored`; an explicit list plus
  `ignored`; `[]` (only non-message kinds named) stays `[]`.
- `time_basis` is passed to the message functions as given. Passage
  functions know record, subject and either; a page revision has no annotations,
  so `effective` is passed to them as `record`.
- Every message leg funnels through `tl.recall_scope`, so no leg changes.
  The legacy `api.search*` functions are unaffected.
