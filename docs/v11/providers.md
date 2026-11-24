# Model providers, egress, ledger, cache and the cost gate

Package `throughline.providers`, `throughline.cost`, migration `0035_models.sql`.

Throughline uses models for four things. Each is a provider kind with one
interface, a deterministic local implementation, and an OpenAI-compatible HTTP
implementation. With nothing configured the service makes no outbound call.

| Kind | Interface | Local implementation | HTTP endpoint | Default |
|---|---|---|---|---|
| `embedding` | `embed(texts, model=None, dimensions=None) -> (vectors, Usage)` | `LocalHashEmbedder` (`local-hash-v1`) | `POST {base}/embeddings` | unavailable until configured |
| `rerank` | `score(query, passages) -> (scores, Usage)` | `LocalLexicalReranker` | `POST {base}/rerank` | local |
| `semantic` | `apply(operation, instruction, items, output_schema) -> (results, Usage)` | `LocalRuleModel` | `POST {base}/chat/completions` | local |
| `controller` | `turn(brief, state) -> (action, Usage)` | `LocalPlanController` | `POST {base}/chat/completions` | local |

Every provider has `name`, `version`, `local`, `model` and `estimate(...)`,
which returns an `Estimate(purpose, provider, model, calls, tokens, cost,
local)` without calling anything. `version` changes whenever the output for
the same input may change and belongs in every cache key.

## Selection

`providers.get(kind)` returns the active provider or raises
`ProviderUnavailable` (a `ValueError`, so routes answer 400).

- `THROUGHLINE_<KIND>_PROVIDER` is `local`, `http`, `none`, or a named
  vendor that serves the kind: `voyage` (embedding, rerank), `gemini`
  (embedding, semantic, controller) or `openrouter` (all four). A vendor
  has a default endpoint and reads its key from `THROUGHLINE_<KIND>_KEY` or
  its conventional variable (`VOYAGE_API_KEY`, `GEMINI_API_KEY` /
  `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`); see `embeddings.md`.
- Embedding, when the variable is unset: HTTP once
  `THROUGHLINE_EMBEDDING_BASE_URL`, `_MODEL` and `_DIMENSIONS` are all set,
  otherwise `embeddings unavailable: configure ...` exactly as before. The
  local embedder is opt-in (`THROUGHLINE_EMBEDDING_PROVIDER=local`).
- Rerank, semantic and controller default to `local`. With `http` they read
  `THROUGHLINE_<KIND>_BASE_URL`, `_MODEL`, `_KEY` and `_PRICE_PER_MILLION`;
  semantic and controller fall back, setting by setting, to
  `THROUGHLINE_MODEL_*`. The one exception is the key: `THROUGHLINE_MODEL_KEY`
  is used only with `THROUGHLINE_MODEL_BASE_URL`, so a kind that has its own,
  different base URL gets no key unless its own `_KEY` is set.
- `providers.override(kind, provider)` is a context manager for tests and
  scripted runs: `provider` is an instance, or `None` for "unavailable". It is
  process-wide, so worker threads see it.

`.env.example` lists every variable.

## Local implementations

They are pure Python and deterministic: the same input gives the same output
on every machine. They are lexical and exist so that everything runs and is
testable offline; they do not stand in for a real model's quality.

- **Embedder.** Lower-cased `\w+` tokens, unigrams and bigrams, each hashed
  with SHA-256 into a signed bucket, L2-normalised. Dimensions come from
  `THROUGHLINE_EMBEDDING_DIMENSIONS` (default 256).
- **Reranker.** Share of the distinct query tokens present in the passage plus
  0.1 for each query bigram present. `rank()` keeps input order for ties.
- **Rule model, operation `match`.** Content words of the condition (tokens
  minus a fixed stop list): all present gives `match` with the character
  offsets of the first occurrence of each; none present gives `nonmatch`;
  otherwise `uncertain`.
- **Plan controller.** Plays `providers.local.PLAN`, a tuple with one action
  per turn. Turn `n` is `state["turn"]` (from 0); the last entry repeats. A
  string value `"$name"` is replaced by `state[name]`. The plan is data: the
  librarian replaces the constant, not the class.
- **Synonyms.** `THROUGHLINE_LOCAL_SYNONYMS` (a JSON file path or inline JSON)
  maps tokens to a canonical token for all three lexical implementations.
  A map is part of their output, so it is part of their `version` and `model`:
  with a map set they read `local-hash-v1+syn-<digest>` (and likewise for the
  reranker and rule model). Changing the map therefore misses every cache
  entry made under the old one, and the embedding worker treats it as a new
  model (a normal model handover) instead of mixing vectors of both maps.

## Semantic operations

`SemanticModel.apply` takes items `{id, text}` and returns, in the same order,
`{id, output, evidence, note}` where `evidence` is a list of `[start, end]`
character offsets into that item's text. Only `match` is registered (output
`match`, `nonmatch` or `uncertain`). `extract`, `compare`, `relate` and
`summarize` are reserved names: calling them raises `ValueError` today, and
adding one later changes no signature, cache key or ledger column.

The HTTP implementation sends the items in a JSON `data` field that the system
message declares to be data. It accepts only answers for ids it sent; a
missing or invalid answer becomes `uncertain`, evidence outside the text is
dropped, and a reply that is not the expected JSON object raises
`ProviderUnavailable`.

## Egress

`providers.egress.check(url)` runs before every HTTP provider request.
Loopback (`127.0.0.0/8`, `::1`, `localhost`) is always allowed. Any other host
must be in `THROUGHLINE_EGRESS_ALLOW` as `host` or `host:port`; otherwise
`EgressDenied` (a `ProviderUnavailable`) is raised before a socket is opened
or a name is resolved. Names are compared as written. Proxies from the
environment are ignored and redirects are not followed, so the host that was
checked is the host that receives the request. The URL is parsed both by the
standard library and by httpx (which sends the request); when the two disagree
on scheme, host or port, or either sees user information, it is refused.

This applies to an embedding endpoint too: a remote endpoint configured before
v1.1 stops working until its host is added to `THROUGHLINE_EGRESS_ALLOW`.

`tl.provider_state` holds, per kind, the implementation name, model, `local`
and `excerpts_leave_host`. The API writes it at start
(`providers.sync_state`). `api.describe_egress()` returns it as JSON for
`api.describe('egress')`: no host names and no keys.

## Calling a provider

```python
provider = providers.get("rerank")
scores, usage = providers.call(
    "rerank",
    "rerank",
    lambda: provider.score(query, passages),
    estimate=provider.estimate(query, passages),
    scope=scope,
    tracker=tracker,
    trace=trace,
)
```

`providers.call(kind, purpose, fn, *, estimate, scope, tracker=None,
trace=None, origin='query', origin_ref=None)`:

1. reserves the estimate on the `BudgetTracker` (cost only for non-local
   estimates, calls always) and raises `ProviderUnavailable('budget
   exhausted')` when it does not fit, before `fn` runs;
2. runs `fn() -> (result, Usage)`;
3. writes one `tl.model_ledger` row (purpose, provider, model, tokens, cost,
   calls, token, trace, origin, origin reference);
4. adds one trace event `model.<kind>` holding names, counts and a reason,
   never text;
5. turns transport and JSON decoding errors into `ProviderUnavailable` without
   the original message, which could contain request or response text.

A reservation is returned only when egress was denied, because then nothing
was sent. `origin` is one of `query`, `worker`, `job`, `rule`, `curator`,
`librarian`.

## Ledger and spend

`tl.model_ledger` has one row per provider call made through `providers.call`.
The embedding worker keeps `tl.embedding_ledger` (one row per batch). The view
`tl.spend` is both together. `GET /v1/spend` (owner) and
`throughline lib cost spend` total it by purpose, provider, model and origin.

## Embeddings

`throughline.embeddings` keeps `settings`, `embed`, `vector`, `run_once`,
`_run_batch`, `run` and `embed_search`.

- `embed(texts, model=None, dimensions=None) -> (vectors, token_count)`
  delegates to the embedding provider. The worker and `embed_search` look the
  module attribute up at call time, so a replacement with the same shape is
  used by both.
- `embed_search(text, with_model=False, *, scope=None, tracker=None,
  trace=None)` embeds with the model that currently serves search. The vector
  is cached under operator `query_embedding` with a key over provider, provider
  version, serving model, dimensions and the text's hash, so a repeated query
  makes no call and a vector of the previous serving model is never reused
  during a handover. A call that is made goes through `providers.call` with
  origin `query` and timeout `THROUGHLINE_EMBEDDING_QUERY_TIMEOUT_S` (default
  5 s). Worker batches keep 60 s. Without a scope the in-process owner scope is
  used and the ledger row has no token.

## Cache

`tl.semantic_cache` stores derived values (query embeddings, rerank scores,
verdicts).

```python
key = cache.key(operator, operator_version, model, *parts)
value = cache.get(key, scope)
cache.put(
    key,
    operator,
    operator_version,
    model,
    input_hash,
    value,
    source_message_ids=(),
    ttl_s=None,
)
```

- `parts` must pin the input exactly: content hashes, or a message id together
  with its version. An id alone is not enough.
- `cache.get` requires a scope. An entry with `source_message_ids` is returned
  only when `SELECT api.sources_visible(ids)` is true for that scope, run
  through `query.reader_transaction(scope)`, so a cached value derived from a
  hidden message is never returned to an asker who cannot see it. If the gate
  function does not exist, only an owner-mode scope reads such entries.
- An entry without source messages (a query embedding) is derived from the
  caller's own input and is readable under any scope.
- `cache.put` never fails the operation that produced the value; it returns
  `False` and logs when the row could not be written.
- The purge hook `semantic_cache` deletes every entry whose
  `source_message_ids` overlap the messages of the purged channel.

## Cost gate

Each token has `max_model_cost` (USD per request, default 0.10) and
`approve_limit` (default 0); set them with
`throughline lib token-limits ID --set max_model_cost=0.5`.

```python
digest = cost.request_hash(
    statement_or_request, scope, [(handle, member_count), ...], budgets
)
cost.authorize(
    token_row,
    estimates,
    scope=scope,
    tracker=tracker,
    caller_limit=None,
    approval_id=None,
    request_hash=digest,
)
```

Every operation that may spend on a model collects its estimates and calls
`authorize` before the first call.

- The total is the sum over non-local estimates. Local work never needs
  approval.
- The limit is the lowest of the caller's limit, the token's `max_model_cost`
  and the tracker's model-cost budget. A caller can lower the limit, never
  raise it.
- At or under the limit, `authorize` returns.
- Over the limit it raises `cost.ApprovalRequired(estimate, limit,
  approval_id, explanation)` and stores a pending row in `tl.approvals`.
  Nothing has been called or spent. The explanation is fixed text built from
  the numbers. Asking again for the same request renews the same pending row.
- With `approval_id`, the request runs when that approval is `approved`, was
  asked by the same token and the same asker, has the same `request_hash` and
  the same policy version, and the new total is at most 1.2 times the approved
  cost. The approval then becomes `used`: it authorizes one run. The tracker's
  model-cost budget is raised to cover the approved estimate.

`request_hash` covers the statement or request body, token, asker, every input
handle with its member count, and the budgets. A result set that gained a
member, a changed budget or a changed statement is a different request and
needs a new approval. Every caller must build the hash with this helper.

### Approving

| Call | Who |
|---|---|
| `GET /v1/approvals[?status=]` | owner: all; anyone else: those asked by its own token and asker |
| `POST /v1/approvals/{id}/approve` | owner: any pending approval; agent: its own, cost at most its `approve_limit` |
| `POST /v1/approvals/{id}/reject` | owner: any; agent: its own |
| `GET /v1/spend[?since=&until=]` | owner |
| `throughline lib cost approvals \| approve ID [--reject] \| spend` | owner database connection |

Approving, rejecting and spend call `deps.require_unrestricted`: a restricted
asker gets 403 before any lookup. Listing does not, because an approval is the
caller's own working state and holds an estimate (purposes, models, counts,
cost), never text; a restricted asker sees only its own. A decision on an
approval that is no longer pending returns 409; an id the caller may not see
returns 404.

## Tables

| Object | Purpose | App role |
|---|---|---|
| `tl.model_ledger` | one row per provider call | select, insert |
| `tl.spend` (view) | model ledger and embedding ledger together | select |
| `tl.semantic_cache` | derived values, scope-checked on read | select, insert, update, delete |
| `tl.approvals` | cost approvals | select, insert, update of decision and estimate columns |
| `tl.provider_state` | active provider per kind | select, insert, update |
| `tl.tokens.max_model_cost`, `approve_limit` | per-token limits | select (owner writes) |
| `api.describe_egress()` | provider state as JSON | execute (reader too) |

The reader role has no access to any of the tables.

Channels that are not embedded, the vendors' input types, contextual
embedding, batch jobs and the nearest-neighbour index are in `embeddings.md`.

## Not covered here

Quality of any real embedding model, cross-encoder, semantic model or
controller needs credentials and is not tested; the HTTP implementations are
tested against recorded responses on loopback. The estimate of a remote call
uses characters / 4 as the token count, so its accuracy against a provider's
real tokenizer is not established.
