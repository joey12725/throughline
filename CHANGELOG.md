# Changelog

All notable changes to Throughline are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/); see the
[compatibility policy](docs/design-scope.md#compatibility-policy).

## [Unreleased]

## [0.1.0]

First public release.

### Core and data model

- PostgreSQL 16 + pgvector store of immutable ground truth: messages, raw
  payloads, recordings and transcripts, location tracks and visits, pages,
  with version chains and SHA-256 payload hashes.
- Split database roles: the API writes as `throughline_app` and queries as
  `throughline_reader`; only owner administration uses the superuser.
- One identity registry across accounts, handles, e-mail addresses, phone
  numbers and enrolled voices, with reversible merges and splits and
  suspected identifiers that count for nobody until confirmed.
- Idempotent write contract (`Idempotency-Key`, same-transaction receipts,
  compare-and-set, message aliases), claims, links with a closed type
  registry, lineage and provenance, commitments, annotations.
- Source coverage, source health (failing, silent and stalled sources from
  an arrival-cadence model), outages and catch-up, and named feeds with
  exactly-once delivery.
- Access control in PostgreSQL: restricted askers with channel and thread
  grants, sensitivity labels, expiring grants, label-aware model egress.

### Retrieval

- `POST /v1/find` (hybrid keyword and semantic search returning episodes
  with thread context), `POST /v1/timeline` (exhaustive chronological
  counts), `POST /v1/orient` (working-memory snapshot), `POST /v1/sql`
  (AST-validated read-only SQL over stable `api.*` views and per-channel
  typed views), people functions (`api.person_brief`, `api.last_contact`,
  `api.open_loops`, `api.between`, …).
- Semantic operators `api.recall`, `api.expand`, `api.match`, result sets,
  episode grouping and an optional delegated librarian.
- Optional embeddings (local, Voyage, Gemini, OpenRouter, any OpenAI-style
  HTTP server) and reranking; offline by default.
- A host-neutral agent skill (`skill/throughline/SKILL.md`), tested query
  recipes, and an MCP server at `/v1/mcp`.

### Derived knowledge

- **Lenses**: scheduled model passes over new records that write attributed,
  re-runnable annotations, sharing one model call per input and model.
- **Pages**: maintained daily, weekly, person and topic pages built by
  lenses, with revisions, provenance and export sinks (Markdown files,
  commands).
- **Classifiers**: automatic annotations per message from a model prompt or
  sandboxed Python.
- **Images**: images as a native modality through Immich (sources,
  classification, faces, visual search) behind `api.assets` and related
  views.

### Plugins and connectors

- Language-independent plugin HTTP contract and process contract, the
  `throughline-sdk` Python package (Apache-2.0) with the
  `throughline-contract` verification suite, and Python and Node.js
  templates.
- Connectors in-tree for AI chats, messages, e-mail and calendars, notes,
  location, health, media, social networks, meetings and finance, described
  for onboarding in `plugins/throughline-plugins.toml`.

### Web interface

- Web UI served under `/ui/`: search, day and people views, places, graph,
  SQL with a helper agent, live arrivals, sources, ledger.
- Sign-in with a password or any OpenID Connect provider (Authentik
  documented), accounts managed from the CLI.
- Onboarding wizard (and an agent-driven alternative): account, model keys,
  Immich, source selection, plugin repositories from a GitHub link, history
  import with progress, the people resolver and agent connection over MCP.

### Desktop Companion

- Throughline Companion for Windows, macOS and Linux: runs desktop
  connectors (local chat apps, browser history, notes, iMessage, …),
  optional screen and audio capture, and a bridge that lets server
  connectors ask the desktop for a secret instead of keeping a copy.

### Operations

- Docker install (`scripts/install.sh`): one image, PostgreSQL, the API, a
  maintenance scheduler (embeddings, lenses, curation, alerts, backups) and
  optional server connectors; `scripts/update.sh` updates with a
  pre-migration snapshot and automatic rollback.
- Snapshot-consistent encrypted backups, off-site copy, scratch-database
  restore drills and staleness alarms; whole-install bundles (database,
  roles, opted-in databases and files).
- Public-twin benchmark (`python -m bench`) with the twelve behaviour
  scenarios, and a synthetic twin (`python -m fixtures`) for demos and tests.

[Unreleased]: https://github.com/joey12725/throughline/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/joey12725/throughline/releases/tag/v0.1.0
