# Throughline documentation

Start with the core guides, then drill into the subsystem specifications under `docs/v11/` as needed.

## Core guides

| Guide | Purpose |
|---|---|
| [Design scope & compatibility](design-scope.md) | Single owner, English full-text search, one time zone, no model egress by default, and what stays stable between releases |
| [Agent retrieval](agent-retrieval.md) | How agents query Throughline: `/v1/find`, `/v1/timeline`, `/v1/orient`, `api.person_brief`, read-only SQL shaping, local cross-encoder reranking, and recipes |
| [Programmatic clients](programmatic-clients.md) | Snowpark-style Python, Go, and Rust clients: lazy pushdown DataFrames, parameterized SQL (`$1, $2`), NDJSON/CSV streaming, and full MCP parity |
| [Architecture & HTTP contract](architecture.md) | Database schema, role isolation, ingest & versioning, identity, location, v1.1 retrieval internals, and scope/deferrals |
| [Configuration reference](configuration.md) | Every environment variable, default value, and where it applies |
| [Infisical integration](infisical.md) | Linking credentials from Infisical by reference without storing secrets in Throughline |
| [Updating](updating.md) | One-command updates, what is kept where, pre-upgrade snapshots and rollback |
| [Companion app](companion.md) | The desktop app: what runs on a desktop vs in Docker, pairing, the desktop bridge, bundled ActivityWatch |
| [Screen and audio capture](capture.md) | Optional screenshots (to Immich) and speech transcripts from the Companion |
| [Onboarding](onboarding.md) | First-run setup in the browser or with an agent, restored installs, people resolution, MCP |
| [Plugin onboarding contract](plugin-onboarding-contract.md) | `throughline-plugins.toml`: listing, installing, configuring and checking plugins from a repository |
| [Writing a plugin](writing-a-plugin.md) | Building, testing, and operating a source connector with `throughline-sdk` |
| [Plugin HTTP contract](plugin-http-contract.md) | Language-independent HTTP routes, write contract, reserved field names, and process contract |
| [Python SDK](../sdk/README.md) | `throughline-sdk` library reference and `throughline-contract` test harness |
| [Agent skill](../skill/throughline/SKILL.md) | Host-neutral instructions you can hand directly to an AI agent |
| [Releasing](releasing.md) | Maintainers: building the public tree, publishing, tagging and branch protection |

## Subsystem specifications (`docs/v11/`)

### Retrieval, ranking & agents

| Spec | Covers |
|---|---|
| [Recall](v11/recall.md) | Multi-leg retrieval pipeline (`POST /v1/recall`), hard scopes, planner rounds, degradation |
| [Ranking & spans](v11/ranking.md) | Pluggable rankers, reciprocal rank fusion, minimal conversation spans, passage ranking, `evidence_class` |
| [Selection & grouping](v11/selection.md) | Budget-aware selection, provenance/alias deduplication, context dependencies, episode grouping |
| [Result sets](v11/result_sets.md) | Scoped `rs_*` handles, sessions, set transforms (`filter`, `union`, `intersect`, `difference`) |
| [Expand & match](v11/expand_match.md) | Set-based mechanical expansion (`POST /v1/expand`) and bounded semantic judgements (`POST /v1/match`) |
| [Semantic SQL, plans & packets](v11/semantic_sql.md) | `api.recall`/`expand`/`match` inside SQL, taint rules for exactness, plan DAGs, evidence packets |
| [Librarian](v11/librarian.md) | Optional level-3 bounded controller loop (`investigate`) that returns an evidence packet |
| [Pages](v11/pages.md) | A graph of cited pages (people, events, days, deals, anything) written by the Librarian through pages lenses (`output = "pages"`: query, prompt, schedule; actions write/edit/link); graph-aware page map in `/v1/find`; health and sinks (Markdown, GBrain) |
| [Embeddings](v11/embeddings.md) | Per-channel opt-out, vendors (Voyage, Gemini, OpenRouter, HTTP), contextual chunks, batch backfill, HNSW/exact ranking |
| [Providers & cost gate](v11/providers.md) | Provider registry, egress checks, model ledger, scoped caches, budget limits and approvals |

### Data model, identity & knowledge

| Spec | Covers |
|---|---|
| [Records & time](v11/records.md) | Record kinds (`life_event`, `reference`, `page_revision`, `derived`), subject time, `as_of`/`known_by`, `api.describe` |
| [People](v11/people.md) | Unified identity registry, confirmed vs. suspected attribution, exchanges, `person_brief`, open loops, circle & ties |
| [Passages](v11/passages.md) | Heading-aware page chunking, authorized revision fallback, passage vectors |
| [Lineage](v11/lineage.md) | Source provenance, pinned versions, link validity intervals and history, reversible entity merges |
| [Link types](v11/link_types.md) | Closed link-type vocabulary, synonym folding, plugin candidate queue |
| [Annotations](v11/annotations.md) | Non-destructive annotations (effective time, place, ignore, media noise, speaker) and proposals |
| [Classifiers](v11/classifiers.md) | Annotations written automatically: per-message Jev/LLM questions and sandboxed Python over whole conversations |
| [Shared model calls](v11/shared-calls.md) | Lenses and classifiers that ask the same model about the same input share one call per tick (schedules ride along); `ctx.jev.ask_many` for several questions about one text |
| [Transcripts](v11/transcripts.md) | Recording episodes, media-noise flagging, enrolled voices vs. speaker inbox |
| [Write contract](v11/write_contract.md) | `Idempotency-Key`, readback receipts, compare-and-set, message aliases and per-key versioning |

### Security, operations & evaluation

| Spec | Covers |
|---|---|
| [Access & disclosure](v11/access.md) | Scope modes, PostgreSQL security-barrier views, sensitivity labels, expiring grants, policy versions |
| [Encryption & egress](v11/encryption.md) | Volume encryption, encrypted backups, transport security, label-aware model egress |
| [Source health](v11/sources.md) | Failure/silence/stall states, arrival cadence model, outage tracking, catch-up and alerts |
| [Coverage](v11/coverage.md) | Source coverage reports, open gaps, absence honesty (`api.coverage_for`) |
| [Feeds](v11/feeds.md) | Named arrival-time cursors (`news`, `pending`, compare-and-set `mark`, settle horizon) |
| [Backups & ops](v11/ops.md) | Snapshot-consistent dumps, encryption & off-site copy, automated scratch-DB restore drills, staleness alarms |
| [Plumbing](v11/plumbing.md) | Extension registration points, traces, execution budgets, test harness conventions |
| [Benchmark](v11/benchmark.md) | Public-twin retrieval benchmark (`python -m bench`) and behaviour scenarios |
| [v1.1 requirement index](v11/index.md) | Complete mapping of v1.1 requirements, LIB sections, design priorities and behaviour scenarios to tests |
