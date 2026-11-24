# Passages of page revisions (migration 0037)

Requirements: BRN-6, BRN-5 (historical and authorized revisions), EMB-2
(amended), ACL-9, QRY-18/QRY-20 (scope on passages), CST-5.

A page is one message per revision: versions of one `external_id` in a channel
of record kind `page_revision`, with `happened_at` = revision time. Pages
enter retrieval only as passages; message legs never return a whole page. Any
channel of kind `page_revision` gets passages; no plugin code is involved.

## Full text

`passages.full_text(payload, paths, content)` builds the text a revision is
split from:

- `paths` is the channel's `passage_text_paths`: a list of entries, each one or
  more JSON pointers (RFC 6901) into the stored raw payload separated by `|`.
  The first pointer of an entry with a non-blank value wins; entries are
  joined by a blank line. A string is used as is, a list is rendered element by
  element (one per line), any other value as JSON.
- Without paths, without a payload, or when no pointer has a value, the text is
  the message content.

The raw payload is never cut, so a page whose content was cut at ingest (for
example at 250,000 bytes) is still split and searchable in full.
`char_start`/`char_end` of every passage index this text (Python string
offsets, i.e. code points): `content == text[char_start:char_end]`.

## Splitting rules

`passages.split(text, max_tokens=400, min_tokens=40)` is deterministic: the
same text always gives the same passages, ordinals, offsets and hashes. The
token estimate is `(characters + 3) // 4`, the providers' estimate.

1. Markdown ATX headings (`#` to `######`, up to three leading spaces) start
   sections. Text before the first heading is a section with an empty heading
   path. Headings inside fenced code blocks (```` ``` ```` or `~~~`) are text.
   `heading_path` is the list of enclosing heading titles, outermost first.
2. A section within `max_tokens` is one passage candidate. A larger section is
   split at paragraphs (blank lines); a paragraph that is still too large at
   sentence ends and line breaks; a sentence that is still too large at the hard
   limit (`max_tokens * 4` characters, cut at the last whitespace in the second
   half of the window when there is one). The pieces are packed greedily back
   together up to `max_tokens`.
3. Adjacent candidates of which one is small (below `min_tokens`) are merged
   while the result stays within `max_tokens`: pieces of one split section
   with each other; whole sections with a sibling, parent or child section. The
   merged passage keeps the common heading path.
4. Leading and trailing whitespace is never part of a passage; a passage is
   never empty. Every non-whitespace character of the text is in exactly one
   passage.

`THROUGHLINE_PASSAGE_MAX_TOKENS` and `THROUGHLINE_PASSAGE_MIN_TOKENS` change
the limits for `build()`; passages already stored are not rebuilt.

## Store

`tl.passages(id, message_id, channel_id, external_id, version, ordinal,
heading_path, char_start, char_end, content, text_hash, token_estimate, tsv)`
with `UNIQUE(message_id, ordinal)`, a GIN index on `tsv` and an index on
`(channel_id, external_id, version)`. `text_hash` is the SHA-256 of the
content. Rows go with their revision (`ON DELETE CASCADE`).

`passages.build(conn, limit=200)` stores passages for up to `limit`
page-revision versions that have none, current revisions first, and returns the
number of revisions it built. It is idempotent and safe to run concurrently:
each revision is written by one run only. The run takes a transaction lock on
the revision without waiting (`tl.try_lock_passage_build`) and inserts only
when the revision still has no passages, so a revision another run is
building or has built is skipped and counted only there, and the passages of
one revision always come from a single split, even when two runs use
different token limits. The caller owns the transaction; the lock is released
when it ends.

## History (BRN-5)

Every revision keeps its passages. Which revision answers is decided in SQL,
among the revisions the asker may read:

| call | revision per page |
| --- | --- |
| default | the latest visible revision |
| `as_of` | the latest visible revision with revision time <= `as_of` (and ingested by `known_by` when given) |
| `current_only => false` (no `as_of`) | every visible revision |

A revision whose provenance names a hidden source is not visible
(`api.row_visible`, migration 0036), so a restricted asker falls back to the
latest earlier revision whose sources are all visible.

## Views

Both are built by `tl.install_gate` and read `tl.passages` joined to the
ungated `tl.message_versions_all`. Predicate: `api.row_visible(message_id,
channel_id, thread_id)` of the revision and, when the passage has passage-level
sources of its own, every one of them visible. For a restricted asker no caller
predicate runs below the gate, so expressions that fail (division by zero,
casts) report nothing about hidden passages.

- `api.passages`: `passage_id, record_id ('p:'||id), message_id, channel_id,
  channel, external_id, version, is_current, ordinal, heading_path,
  char_start, char_end, content, token_estimate, revision_time, thread_id`,
  then `tsv`.
- `api.passage_records`: the record contract of `api.record_versions`
  (same columns, order and types), registered as prefix `p` in
  `tl.record_sources`. `kind` = `passage`, `ref_id` = passage id,
  `message_id` = revision, `record_time` = revision time, subject and author
  columns of the revision, `generated` = true. `support` = the passage's own
  source-grade provenance (`tl.provenance` rows `('passage', id)`, origins
  source, owner, job) when it has any, else the revision's
  (`api.support_of('page_revision', message_id, version)`); `unsupported` =
  `support = '[]'`. `expansion` = `{page: [channel, external_id], revision:
  message_id, sources: true}`.
- The lineage of a passage (`api.provenance` rows and
  `api.sources('passage', id)`) is shown to a restricted asker only when the
  passage itself passes the `api.passages` gate: this migration extends
  `api.derived_visible` with kind `passage`.

## Functions

All read `api.passages` (and `api.message_versions` for revision
eligibility), so the gate applies; every filter is applied before ranking and
the limit. `max_results => NULL` returns every eligible passage.

```sql
api.search_passages(query text, max_results int DEFAULT 20, channels text[],
  as_of timestamptz, current_only boolean DEFAULT true, after_at timestamptz,
  before_at timestamptz, time_basis text DEFAULT 'record', thread_ids bigint[],
  message_ids bigint[], must_terms text[], source_ids bigint[],
  exclude_message_ids bigint[], known_by timestamptz)
  RETURNS TABLE(passage_id bigint, message_id bigint, score real)
api.vector_passages(query_embedding vector, max_results int DEFAULT 20,
  <the same filters>, serving_model text DEFAULT NULL)
  RETURNS TABLE(passage_id bigint, message_id bigint, score real)
api.passages_of(message_id bigint) RETURNS SETOF api.passages
```

- `search_passages` ranks with `ts_rank_cd` over `websearch_to_tsquery`.
- `vector_passages` is an exact cosine ranking (`1 - distance`) over
  `tl.passage_embeddings` of the active model, or of `serving_model`; vectors
  of another dimension are skipped.
- `time_basis`: `record` = overlap of `[record time, end]` with
  `[after_at, before_at]`; `subject` = overlap with the subject interval, a
  missing bound or one of precision `unknown` is open, and a revision without a
  subject interval falls back to its record time; `either` = one of the two.
  Anything else is an error.
- `must_terms`: every term must match the passage (`plainto_tsquery`); a term
  without lexemes (a stop word) is ignored.
- `source_ids`: keep passages with at least one source-grade provenance source
  among the given messages, matched by record key (any version counts); the
  passage's own sources replace the revision's when it has any. Person and
  place scopes reach passages this way: the caller passes the in-scope source
  messages.

All three are registered in `tl.api_functions` (search and vector with
`ranked_limit_arg` 2 and `default_limit` 20, exactness exact), in the
allow-list `allowlist/0037_passages.json`, and described by
`api.describe('passages')`.

## Vectors (EMB-2)

`tl.passage_embeddings(text_hash, model, embedding)`: one vector per distinct
passage text and model, so identical text on several pages or revisions is
embedded once.

`passages.embed_once(batch=64)` embeds up to `batch` distinct texts of passages
of current revisions (of every revision when
`THROUGHLINE_PASSAGE_EMBED_HISTORY=true`) that lack a vector of the configured
model. The call goes through the module attribute
`throughline.embeddings.embed`, looked up at call time, with no transaction
open; each batch writes one `tl.embedding_ledger` row, like the conversation
worker. A configuration error (no embedding provider, changed dimensions)
returns 0 without a call. `passages.pending(conn, model=None)` reports
`revisions_without_passages`, `texts_without_vector` (for the configured model;
`None` when there is none), `model` and `history`.

`embeddings.run_once()` runs a conversation batch, then `passages.build` and
`passages.embed_once`, and returns conversations embedded plus revisions split
plus passage texts embedded. Conversations of threads of page-revision
channels are neither embedded nor counted as missing. A model becomes the
serving model only when both are covered: no visible closed conversation
outside page-revision channels lacks its vector, no thread awaits maintenance,
no page revision lacks passages and no eligible passage text lacks a vector.

The EMB-2 count is therefore: closed conversations outside page-revision
channels + distinct passage texts (of current revisions, or of all revisions
with history enabled).

## Purge

Hook `passages` (ord 20): delete the passages of the purged channel with their
passage-level provenance, then the vectors whose text no passage of another
channel still has.

## Command line

`throughline lib passages build [--limit N] [--embed] [--json]` splits every
revision without passages (and with `--embed` embeds until nothing is left);
`throughline lib passages status [--json]` prints the pending counts, the
number of passages and of distinct texts.

## Limits

- Passages are built by the embedding worker or the command; until a
  revision is built it has no passages, so a page whose latest visible
  revision is not built yet is not found.
- Vectors exist for current revisions unless history is enabled, so the
  vector function under `as_of` finds only revisions that have vectors.
- Changing `passage_text_paths` or the token limits does not rebuild stored
  passages.
