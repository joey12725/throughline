# Configuration reference

All settings are environment variables. With only the database and runtime role passwords set, Throughline makes **no outbound network calls**: ingest, keyword search, SQL, timelines, people queries, coverage, feeds, and deterministic local reranking all run locally.

> **Infisical secrets:** Any credential setting (model keys, database passwords, CLI token, or connector secrets) can reference a secret in Infisical (`infisical:SECRET_NAME` or `infisical:/path/SECRET_NAME`) instead of storing the raw secret in Throughline. See [Infisical integration](infisical.md).

> **Docker Compose note:** `docker-compose.yml` passes only the variables it explicitly lists to the `throughline` API container. When you add optional settings in `.env`, forward them to the `throughline` service in `docker-compose.yml` as well.

## Core database, roles and CLI

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_DATABASE_URL` | — | Owner PostgreSQL URL for migrations, tokens, purge, source health CLI and backups; host/port/db coordinates for runtime roles |
| `THROUGHLINE_APP_PASSWORD` | required* | Password for `throughline_app` (API write role). No built-in default |
| `THROUGHLINE_READER_PASSWORD` | required* | Password for `throughline_reader` (read-only query role). No built-in default |
| `THROUGHLINE_DEV` | unset | Set to `1` only for local trust-authenticated development/tests to permit empty runtime role passwords. Never set in production |
| `THROUGHLINE_URL` | `http://127.0.0.1:8000` | Base URL used by the CLI and plugins |
| `THROUGHLINE_TOKEN` | unset | Bearer token used by the CLI and plugins |
| `THROUGHLINE_TIMEZONE` | `UTC` | IANA time zone (e.g. `Europe/Berlin`): owner-local timestamps in `/v1/find`, `/v1/timeline`, `/v1/orient`, `api.day`, `api.rhythm`, lens and page schedules, and arrival-cadence grids (cadence falls back to `TZ`). `THROUGHLINE_OWNER_TZ` is the older name and still read when this is unset |
| `THROUGHLINE_PG_BIN` | unset (`PATH`) | Directory containing `pg_dump`, `pg_restore`, and `psql` when absent from `PATH` |
| `THROUGHLINE_TEST_DATABASE_URL` | `postgresql://postgres@127.0.0.1:54331/postgres` | Administrative `postgres` database used by pytest and `throughline-contract` to create/drop `tl_test_*` databases |

\*Required unless `THROUGHLINE_DEV=1` is explicitly set against a trust-authenticated local Postgres instance.

---

## Egress and embeddings

See [docs/v11/embeddings.md](v11/embeddings.md) and [docs/v11/encryption.md](v11/encryption.md).

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_EGRESS_ALLOW` | empty | Comma- or space-separated hosts (or `host:port`) that non-loopback model endpoints may contact. Everything else is refused before opening a socket |
| `THROUGHLINE_EMBEDDING_PROVIDER` | unset | `voyage`, `gemini`, `openrouter`, `http`, `local` (`local-hash-v1`), or `none`. Unset behaves as `http` if `_BASE_URL`, `_MODEL` and `_DIMENSIONS` are all set, else unavailable |
| `THROUGHLINE_EMBEDDING_BASE_URL` | unset | OpenAI-compatible endpoint base URL ending in `/v1` (for `http` provider) |
| `THROUGHLINE_EMBEDDING_MODEL` | vendor default | Model name (`voyage-4-large` for Voyage, `gemini-embedding-2` for Gemini; required for `openrouter` and `http`) |
| `THROUGHLINE_EMBEDDING_DIMENSIONS` | vendor default (256 for `local`) | Vector dimensions (1–16000). Changing dimensions requires a new model name |
| `THROUGHLINE_EMBEDDING_KEY` | unset | Bearer/API key for the embedding provider (falls back to `VOYAGE_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`, or `OPENROUTER_API_KEY` for those vendors) |
| `THROUGHLINE_EMBEDDING_PRICE_PER_MILLION` | `0.02` (or vendor list price) | Estimated USD per million tokens recorded in the ledger |
| `THROUGHLINE_EMBEDDING_QUERY_TIMEOUT_S` | `5` | Seconds a search waits for its query embedding (max 60; worker batches always wait up to 60) |
| `THROUGHLINE_EMBEDDING_QUERY_CACHE_TTL_S` | `604800` | Query embedding cache lifetime in seconds (7 days) |
| `THROUGHLINE_EMBEDDING_HISTORIC` | `sync` | `batch` lets the worker submit backlogs of `>= _BATCH_MIN` texts to Voyage/Gemini batch APIs; `sync` uses batch only via `throughline embed backfill` |
| `THROUGHLINE_EMBEDDING_BATCH_MIN` | `500` | Minimum pending texts before `HISTORIC=batch` submits a batch job |
| `THROUGHLINE_EMBEDDING_BATCH_POLL_S` | `60` | Minimum seconds between polling open vendor batch jobs |
| `THROUGHLINE_EMBEDDING_CONTEXT_TOKENS` | `2000` | Preceding context tokens sent per window with contextual models like `voyage-context-4` (0–4000) |
| `THROUGHLINE_EMBEDDING_MONTHLY_USD` | unset | Monthly calendar spend cap in USD (list price); worker pauses until next month once reached |
| `THROUGHLINE_EMBEDDING_BUDGET_SINCE` | unset | ISO timestamp before which spend in the current month is excluded from the monthly cap |
| `THROUGHLINE_EMBEDDING_INDEX` | `on` | `off` skips creating HNSW indexes (ranking stays exact) |
| `THROUGHLINE_OPENROUTER_PROVIDER` | `{"data_collection":"deny"}` | JSON object forwarded as OpenRouter's `provider` routing field (`{}` sends none) |
| `THROUGHLINE_PASSAGE_MAX_TOKENS` | `450` | Target maximum tokens per page passage |
| `THROUGHLINE_PASSAGE_MIN_TOKENS` | `80` | Minimum passage token size before merging with a neighbour |
| `THROUGHLINE_PASSAGE_EMBED_HISTORY` | false | Set truthy to embed historical (superseded) page revisions in addition to current ones |

Vendor API key fallbacks (used when `THROUGHLINE_<KIND>_KEY` is unset): `VOYAGE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`.

---

## Reranker, semantic model and librarian controller

See [docs/v11/providers.md](v11/providers.md).

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_RERANK_PROVIDER` | `local` | `local`, `http`, `voyage`, `openrouter`, or `none` |
| `THROUGHLINE_RERANK_BASE_URL`, `_MODEL`, `_KEY`, `_PRICE_PER_MILLION` | unset / `0` | Endpoint (`POST {BASE_URL}/rerank`), model, key and price for the reranker |
| `THROUGHLINE_SEMANTIC_PROVIDER` | `local` | `local`, `http`, `openrouter`, `gemini`, or `none` (used by `api.match` and provider operations) |
| `THROUGHLINE_SEMANTIC_BASE_URL`, `_MODEL`, `_KEY`, `_PRICE_PER_MILLION` | unset / `0` | Chat-completions endpoint and settings for the semantic model |
| `THROUGHLINE_CONTROLLER_PROVIDER` | `local` | `local`, `http`, `openrouter`, `gemini`, or `none` (used by `POST /v1/librarian`) |
| `THROUGHLINE_CONTROLLER_BASE_URL`, `_MODEL`, `_KEY`, `_PRICE_PER_MILLION` | unset / `0` | Chat-completions endpoint and settings for the librarian controller |
| `THROUGHLINE_MODEL_BASE_URL`, `_MODEL`, `_KEY`, `_PRICE_PER_MILLION` | unset / `0` | Shared chat-completions fallback for `SEMANTIC` and `CONTROLLER` (key is only sent to the shared base URL, never to a different host or vendor) |
| `THROUGHLINE_LOCAL_SYNONYMS` | unset | Path to a JSON file or inline JSON synonym table for deterministic local providers |

---

## Retrieval, ranking and SQL bounds

See [docs/v11/recall.md](v11/recall.md), [docs/v11/ranking.md](v11/ranking.md), [docs/v11/selection.md](v11/selection.md), and [docs/v11/semantic_sql.md](v11/semantic_sql.md).

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_DEFAULT_RANKER` | `fused` | Default recall ranker when a request names none (`fused`, `lexical`, `vector`, `cross_encoder`) |
| `THROUGHLINE_READ_MATERIALIZE_WAIT_S` | `2` | Seconds HTTP reads wait for deferred thread maintenance before serving |
| `THROUGHLINE_RECALL_MAX_ROUNDS` | `2` | Maximum adaptive planner rounds per recall call |
| `THROUGHLINE_LEG_TIMEOUT_MS` | `2500` | Per-leg SQL timeout in milliseconds during recall |
| `THROUGHLINE_RETRIEVAL_CONCURRENCY` | `4` | Maximum concurrent retrieval legs |
| `THROUGHLINE_RECALL_EXHAUSTIVE_CAP` | `5000` | Row cap for exhaustive recall strategy |
| `THROUGHLINE_RRF_K` | `60` | Reciprocal rank fusion constant |
| `THROUGHLINE_RERANK_TOP_N` | `30` | Maximum fused candidates forwarded to the reranker |
| `THROUGHLINE_SPANS_PER_CONVERSATION` | `3` | Maximum minimal spans extracted per conversation |
| `THROUGHLINE_SPAN_MARGIN` | `1` | Neighbouring messages included around each span hit |
| `THROUGHLINE_SPAN_MAX_MESSAGES` | `12` | Maximum messages in a single extracted span |
| `THROUGHLINE_DEP_SHORT_TOKENS` | `160` | Token threshold below which a hit pulls surrounding context dependencies |
| `THROUGHLINE_MIN_ROUND_GAIN` | `0.1` | Minimum new-candidate gain required to continue another planner round |
| `THROUGHLINE_SCOPE_SOURCE_CAP` | `2000` | Cap on expanded provenance/scope source IDs per leg |
| `THROUGHLINE_RUNTIME_PROFILES` | unset | Optional JSON runtime profile overrides for the retrieval planner |
| `THROUGHLINE_RESULT_SET_TTL_S` | `86400` | Lifetime of saved `rs_*` result sets and exploration sessions (24 hours) |
| `THROUGHLINE_MAX_SEMANTIC_CALLS` | `4` | Maximum semantic operator calls (`api.recall`/`expand`/`match`) in one SQL statement |
| `THROUGHLINE_MAX_PLAN_NODES` | `20` | Maximum nodes in a `POST /v1/plans` DAG |
| `THROUGHLINE_MATCH_MAX_ROWS` | `200` | Maximum input rows per `POST /v1/match` call |
| `THROUGHLINE_MATCH_GROUP_MAX_TOKENS` | `1200` | Token budget per grouped match batch |
| `THROUGHLINE_EXPAND_MAX_INPUT` | `200` | Maximum input records per `POST /v1/expand` call |
| `THROUGHLINE_EXPAND_MAX_OUTPUT` | `1000` | Maximum output records per `POST /v1/expand` call |
| `THROUGHLINE_EXPAND_THREAD_CAP` | `200` | Maximum messages returned per thread during `expand(mode="thread")` |

---

## Access, people, transcripts, commitments and writes

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_UNKNOWN_ASKER` | `unrestricted` | Set to `deny` so an owner token naming an unregistered `X-Throughline-Asker` sees nothing |
| `THROUGHLINE_REQUIRE_IDEMPOTENCY` | `false` | `true` requires an `Idempotency-Key` header on agent record writes (400 without it) |
| `THROUGHLINE_IDENTITY_QUALITY_ORDER` | `native_id,beeper_id,fingerprint,unknown` | Comma-separated rank (best first) governing message alias upgrades and deduplication |
| `THROUGHLINE_EXCHANGE_WINDOW_S` | `1800` | Seconds within which a different author's next thread turn counts as an exchange |
| `THROUGHLINE_PEOPLE_REFRESH_MS` | `2000` | Time budget to refresh `tl.exchanges` before a person route answers |
| `THROUGHLINE_ASK_PATTERNS` | built-in lexicon | Open-loop question patterns (JSON regex list or `;;`-separated) |
| `THROUGHLINE_CLOSING_PATTERNS` | built-in lexicon | Open-loop closing patterns (JSON regex list or `;;`-separated) |
| `THROUGHLINE_MEDIA_NOISE_OVERLAP` | `0.5` | Word 4-gram overlap with playing media that flags a transcript turn as media noise |
| `THROUGHLINE_VISIT_MAX_GAP_S` | `43200` | Longest silence (12 h) between consecutive in-radius track points still counted as one visit |
| `THROUGHLINE_COVERAGE_COMPLETE_SYNONYMS` | `ok,success,done,empty,empty_thread` | Coverage statuses normalized as complete (empty-like statuses only when verified) |
| `THROUGHLINE_REPARSE_BATCH` | `500` | Commit batch size for `POST /v1/reparse` (bounded to stay within the feed settle window) |

---

## Source health, cadence and plugins

See [docs/v11/sources.md](v11/sources.md).

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_INTERVAL_S` | manifest default | SDK: expected seconds between plugin runs, sent in heartbeats (set when driving `sync --once` from cron/systemd) |
| `THROUGHLINE_SOURCE_DOWN_AFTER` | `3` | Consecutive failed runs that transition a source from `failing` to `down` |
| `THROUGHLINE_CADENCE_QUIET_P` | `0.01` | Silence probability threshold below which a channel is marked `quiet` |
| `THROUGHLINE_CADENCE_OVERDUE_P` | `0.001` | Silence probability threshold below which a channel is marked `overdue` (`stalled`) |
| `THROUGHLINE_ALERT_CMD` | unset | Shell-free command template run by `lib sources check` on state transitions (`{plugin}`, `{state}`, `{title}`, `{message}`) |

---

## Updates

See [updating.md](updating.md).

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_UPGRADE_SNAPSHOT` | `on` | `throughline migrate` dumps the database before applying migrations to one that holds data; `off` skips it |
| `THROUGHLINE_UPGRADE_SNAPSHOT_DIR` | `~/.throughline/upgrade-snapshots` | Where those snapshots go |
| `THROUGHLINE_UPGRADE_SNAPSHOT_KEEP` | `2` | Snapshots kept (newest) |

## Backups and restore drills

See [docs/v11/ops.md](v11/ops.md).

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_BACKUP_DIR` | unset | Default directory for `throughline lib backup run` |
| `THROUGHLINE_BACKUP_KEEP` | `7` | Number of local `throughline-*.dump*` files retained after a successful backup |
| `THROUGHLINE_BACKUP_STALE_HOURS` | `36` | Age past which `/v1/status` flags backups stale and `lib backup check` exits 2 |
| `THROUGHLINE_BACKUP_ENCRYPT_CMD` | unset | Shell-free encryption command (`{in}`, `{out}`). Required unless `--allow-plaintext` is passed |
| `THROUGHLINE_BACKUP_DECRYPT_CMD` | unset | Shell-free decryption command (`{in}`, `{out}`) used by `lib backup verify-restore` |
| `THROUGHLINE_BACKUP_OFFSITE_CMD` | unset | Shell-free off-site copy command (`{file}`). Refuses plaintext dumps by default |
| `THROUGHLINE_BACKUP_TIMEOUT_S` | `3600` | Timeout in seconds for each backup/encrypt/copy step |
| `THROUGHLINE_BACKUP_CONFIG` | `~/.throughline/config/backup.json` | Settings file for whole-install bundles (schedule, destination, off-site, contents); see [ops](v11/ops.md#whole-install-bundles-the-scheduled-backup) |
| `THROUGHLINE_SCRATCH_DATABASE_URL` | unset | Optional scratch database URL for `lib backup verify-restore` (otherwise creates/drops `tl_verify_<random>`) |
