# Embeddings: channels, vendors, historic and passive loads, ranking

Modules `throughline.embeddings`, `embedding_context`, `embedding_batches`,
`providers.vendors`, `providers.batch`; migration `0054_embedding_channels.sql`.

Embeddings are optional as a whole and optional per channel. What has vectors
is found by the meaning legs; everything is still found by the keyword,
entity, link and structured legs.

Content under a sensitivity label is embedded by a provider off this host
only when the owner allowed that label to leave (`encryption.md`, migration
`0055_egress.sql`); a local provider embeds everything.

## Which channels are embedded

`tl.channels.embed` (default true) decides. A channel that is not embedded
has no vectors: its closed conversations and its page passages are skipped by
the worker and by batch jobs, they are not counted when a model is checked
for coverage, and a trigger on `tl.embeddings` drops a vector written for one
of its conversations by anyone.

```
throughline embed channels                         # list
throughline embed channels --disable health.sleep --disable calendar
throughline embed channels --enable health.sleep
```

- Switching a channel off deletes its conversation vectors and the passage
  vectors whose text no embedded channel still has
  (`tl.set_channel_embedding`, owner only). Switching it on leaves the
  backlog to the worker or to a batch job.
- A plugin may declare `embed: false` when it registers a channel. The
  declaration is the default of a new channel only: registering an existing
  channel again never changes the owner's choice.
- A recall whose scope names only channels that are not embedded reports the
  vector leg as unavailable with reason `channels_not_embedded`.
- `api.describe('channel', name)` reports `embed` to an owner.

## Vendors

`THROUGHLINE_EMBEDDING_PROVIDER` is `voyage`, `gemini`, `openrouter`, `http`
(any OpenAI-compatible endpoint, as before), `local` or `none`. A vendor needs
a key and its host in `THROUGHLINE_EGRESS_ALLOW`; everything else has a
default.

| | voyage | gemini | openrouter |
|---|---|---|---|
| Host to allow | `api.voyageai.com` | `generativelanguage.googleapis.com` | `openrouter.ai` |
| Key | `THROUGHLINE_EMBEDDING_KEY` or `VOYAGE_API_KEY` | ... or `GEMINI_API_KEY`, `GOOGLE_API_KEY` | ... or `OPENROUTER_API_KEY` |
| Default model (dimensions) | `voyage-4-large` (1024) | `gemini-embedding-2` (1536) | none: set `_MODEL` |
| Query and document types | `input_type` | `taskType` (`gemini-embedding-001`) or the documented task prefix (later models) | `input_type` `search_query` / `search_document` |
| Contextual chunks | `voyage-context-4`, `voyage-context-3` | no | no |
| Batch interface (historic) | yes, two thirds of the price | yes, half the price | not used (no discount, no input type) |
| Also serves | rerank (`rerank-2.5`) | semantic, controller (OpenAI-compatible chat) | rerank, semantic, controller |

- `THROUGHLINE_EMBEDDING_MODEL`, `_DIMENSIONS` and `_PRICE_PER_MILLION`
  override the defaults. A model that is not in the vendor's table in
  `providers/vendors.py` needs `_DIMENSIONS` (and a price, or its ledger rows
  cost 0).
- A stored text is embedded as a document and a search as a query. The worker
  and `embed_search` say which through a context variable, so the contract of
  `embeddings.embed(texts, model, dimensions)` is unchanged.
- Vectors that come back longer than asked are cut to the size and scaled to
  length 1 (these models are Matryoshka-trained); Gemini vectors are always
  scaled, which `gemini-embedding-001` needs below 3072 dimensions.
- OpenRouter requests carry `provider: {"data_collection": "deny"}` unless
  `THROUGHLINE_OPENROUTER_PROVIDER` gives another JSON object (`{}` sends
  none). The cost OpenRouter reports for a request is what the ledger records.
- A rate-limited or failing request (429, 5xx) of a worker batch is tried
  again up to three times after the server's `Retry-After`. A query embedding
  is never retried: the search degrades instead of waiting.
- `THROUGHLINE_RERANK_PROVIDER=voyage|openrouter` and
  `THROUGHLINE_SEMANTIC_PROVIDER` / `THROUGHLINE_CONTROLLER_PROVIDER` =
  `openrouter|gemini` select the same vendors for the other kinds, with the
  same key rule. The shared `THROUGHLINE_MODEL_KEY` is never sent to a vendor.

Changing the model is a model handover as before: the new model's vectors are
built next to the old ones and it starts to serve search once every embedded
conversation and passage text is covered. Changing the dimensions of a model
needs a new model name.

## Contextual embedding

With a contextual model (`voyage-context-4`) a chunk is embedded together with
its neighbours, so "yes, Friday works" carries what the thread was about. A
document is a window over one thread (conversation chunks in order) or one
page revision (passages in order):

- the texts to embed, consecutive and of one thread or page, up to 8,000
  estimated tokens (characters / 4). The estimate runs short: against the
  voyage-context-4 tokenizer it is 1.5x low overall, 2.1x on email and 3.6x
  at worst, so 8,000 keeps a window below the 32,000-token document limit;
- led by the chunks just before the first of them, up to
  `THROUGHLINE_EMBEDDING_CONTEXT_TOKENS` (default 2000, at most 4000, 0 for
  none). Context chunks are sent and paid for again by each window they lead;
  their vectors are not stored again.

So a chunk that arrives later in a long thread is still embedded with the
earlier ones in view. A search is embedded as a document of one chunk with
the query type. A passage text keeps one vector per model (EMB-2): it is
computed in the context of the page where the text was first seen.

## Monthly cap

`THROUGHLINE_EMBEDDING_MONTHLY_USD` caps what embedding spends in a calendar
month: once tl.spend (worker, batch jobs and query embeddings) reaches it,
the worker embeds nothing until the next month. A pass started below the cap
can go over it by at most that pass. The ledger keeps list prices, so free
tokens make the real bill lower. `THROUGHLINE_EMBEDDING_BUDGET_SINCE` (a
timestamp) counts only spend from that moment in the current month, to leave a
one-off load out of the cap.

## Passive and historic loads

**Passive.** `throughline embed run` embeds what is pending with ordinary
requests, 64 conversations and 64 passage texts at a time, as records arrive.

**Historic.** A backlog (a first load, a new model, a channel switched on)
can go to the vendor's asynchronous batch interface instead, which answers
within hours (Voyage: a 12-hour window; Gemini: usually within 24) at the
lower price:

```
throughline embed backfill            # submit the whole backlog, store what is answered
throughline embed backfill --collect  # only store answered jobs
throughline embed backfill --wait     # ... and keep collecting until none is open
throughline embed backfill --cancel 7
throughline embed status              # provider, models, pending, channels, jobs
```

- A job is a row in `tl.embedding_batches`; its texts are reserved in
  `tl.embedding_batch_items`, so the worker does not embed them a second
  time. The reservation is written before the job is sent: a job is never
  paid for without a row that knows its texts.
- The worker stores answered jobs on its own on every pass (asking a job at
  most every `THROUGHLINE_EMBEDDING_BATCH_POLL_S`, 60 seconds). With
  `THROUGHLINE_EMBEDDING_HISTORIC=batch` it also submits by itself whenever at
  least `THROUGHLINE_EMBEDDING_BATCH_MIN` (500) texts of a kind are pending,
  and embeds smaller arrivals directly.
- A job that fails, expires, is cancelled or answers only some requests
  loses nothing: the reservation ends and whatever has no vector is pending
  again for the worker. A reservation whose job was never confirmed is
  released after an hour.
- The ledger gets one row per job at the batch price
  (`tokens * price * discount`).
- OpenRouter and the generic `http` provider have no batch client; their
  historic load is the worker.

## Ranking

`api.vector_records` and `api.vector_passages` keep their signatures and
their rule that filters come first: the asker gate and the hard scope decide
the eligible conversations or passage texts, and only those are ranked.

- **Exact** while at most `throughline.ann_exact_limit` (20,000) are eligible,
  when the caller asks for all results, or when the model has no index: every
  eligible vector is compared.
- **Index** beyond that. Each model has an HNSW index over half-precision
  copies of its vectors (`tl.ensure_embedding_index`, created when the model
  is first used; up to 4,000 dimensions). The eligibility test runs inside
  the index scan (pgvector iterative scan, `relaxed_order`, `ef_search` 200)
  until twice the requested number is found. If the scan gives up first, the
  exact path answers.
- Scores are always cosine similarity computed from the stored
  full-precision vector, so the index only chooses candidates.

`ALTER DATABASE ... SET throughline.ann_exact_limit = N` moves the limit.
`THROUGHLINE_EMBEDDING_INDEX=off` stops the worker from creating indexes.
`tl.nearest_conversations` and `tl.nearest_passage_texts` read vectors without
the asker gate and are not executable by the runtime roles.

Around the vectors, unchanged: reciprocal rank fusion with the keyword and
other legs (`ranking.md`, `recall.md`), the rerank stage (now also Voyage and
OpenRouter rerankers), span extraction and diversity in selection.

## Not covered

- No request here has been sent to a live vendor: the tests need no
  credentials and use recorded shapes on loopback. The Gemini batch client
  follows the published schema of `asyncBatchEmbedContent`, which has no
  worked REST example for inline requests.
- Binary or scalar quantization, multi-vector (late interaction) models,
  query expansion and image embeddings are not implemented.
- Vectors still exist for current text only (`as_of` / `known_by` skip the
  vector leg).
