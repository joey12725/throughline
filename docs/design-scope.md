# Design scope and compatibility

What Throughline is built for, what it deliberately is not, and what stays
stable between releases.

## One owner per install

An install holds one person's archive: their messages, recordings, health,
location, media and pages. Everyone else who reads it is an **asker** with
grants into that archive (an agent, a family member's assistant, a plugin),
not a tenant with an archive of their own.

- The owner token and the database superuser can read and change everything.
  There is no administrator role below them.
- Askers are restricted by channel and thread grants, narrowed further by
  sensitivity labels and expiries, enforced in PostgreSQL security-barrier
  views ([access](v11/access.md)). Askers never write ground truth.
- Running several people on one install is not supported. Run one install
  per person.

## Language: English full-text search

Lexical search (`/v1/find`, `/v1/timeline`, `api.recall`, passages) uses
PostgreSQL's `english` text-search configuration: English stemming and stop
words. Text in other languages is stored, versioned and returned exactly as
received, but they stem and rank poorly. Meaning search through an embedding provider
([embeddings](v11/embeddings.md)) is language-neutral. Configurable
text-search languages are planned, not available.

## Time: one time zone

`THROUGHLINE_TIMEZONE` (an IANA name such as `Europe/Berlin`, default `UTC`)
is the owner's zone. It shapes owner-local timestamps in retrieval results,
day boundaries (`api.day`, `api.rhythm`), lens and page schedules and the
arrival-cadence model of [source health](v11/sources.md). Records are stored
in UTC with their original offsets, so changing the setting changes how
results are shown and grouped, never the data. `scripts/install.sh` uses the
host's zone unless `--timezone` is given. `THROUGHLINE_OWNER_TZ` is the older
name of the same setting.

## Models and egress: off by default

The core needs no model and no account anywhere. With no model settings,
ingest, full-text search, SQL, timelines, people queries, coverage, feeds and
deterministic local reranking make no outbound network calls.

When you configure an embedding, reranking or language-model provider:

- a non-loopback host is refused before a socket opens unless it is listed in
  `THROUGHLINE_EGRESS_ALLOW`;
- records carrying a sensitivity label stay local unless you allow egress
  for that label ([encryption and egress](v11/encryption.md)).

Connectors are different: a connector exists to talk to its service, and
each connector's README says what it contacts.

## Data model guarantees

- Ground truth is append-only: an edit is a new version with a SHA-256
  payload hash; corrections and noise flags are annotations beside it.
- Writes are idempotent (`Idempotency-Key`) and return receipts read back in
  the same transaction ([write contract](v11/write_contract.md)).
- Generated text is always marked as such and never presented as primary
  evidence.

## Compatibility policy

Throughline follows [Semantic Versioning](https://semver.org/). Before 1.0,
a **minor** release (0.x → 0.x+1) may contain breaking changes and a
**patch** release (0.x.y → 0.x.y+1) never does. Every breaking change is
listed in [CHANGELOG.md](../CHANGELOG.md) with what to do. The parts:

| Surface | Promise |
|---|---|
| HTTP API under `/v1` and the `api.*` SQL views | Within a release line, changes are additive: new routes, new optional request fields, new response fields, new views and columns. Clients must ignore fields they do not know. A route or field is removed only in a minor release, after at least one release in which the changelog marked it deprecated. |
| Plugin HTTP contract ([plugin-http-contract.md](plugin-http-contract.md)) and onboarding contract | Same rule. A plugin written for 0.x keeps working on later 0.x releases unless the changelog announces a contract change, with at least one release of overlap. |
| `throughline-sdk` | Versioned on its own, semver. It supports the current and the previous core minor release. |
| Database migrations | Forward-only and never renamed. Any release upgrades any earlier release's database in place; there are no downgrades (restore a backup instead: `scripts/update.sh` takes one before migrating). |
| Internal tables (`tl.*`), Python modules, CLI output without `--json` | No promise. Script against `/v1`, `api.*` or `--json` output instead. |
| Configuration (`THROUGHLINE_*`) | A renamed setting keeps its old name as an alias for at least one minor release. |
