# Lineage

Package `throughline.lineage`, `throughline.api.routes.lineage`, link and
entity routes in `throughline.api`, `ingest.add_link`, migration
`0036_lineage.sql`. Requirements BRN-6 (source references), BRN-10, BRN-13
(links, entities), BRN-14, BRN-15 (evidence rule), BRN-5 (authorized
revision), QRY-26 and QRY-29 (provenance source), ACL-9; foundations for
BEN-15 and BEN-21.

## Validity

`tl.links`, `tl.relations` and `tl.facts` carry the same four columns as the
subject interval of a message:

| Column | Meaning |
|---|---|
| `valid_from`, `valid_to` | half-open interval `[valid_from, valid_to)` in which the assertion holds |
| `valid_from_precision`, `valid_to_precision` | `exact`, `day`, `month`, `year`, `approximate` or `unknown` |

- A missing bound with no precision is **open** (unbounded on that side).
- A missing bound with precision `unknown` is a bound that exists but is not
  known. `api.applies_at` treats it as unbounded too; the precision is what
  tells the two apart, so a caller can choose to exclude unknown bounds.
- A missing bound with any other precision is refused (HTTP 400), as is
  `valid_to` before `valid_from`.
- A day-precision date is sent as the bare day (`"2020-03-01"`, read as
  00:00 UTC) or a timestamp, with precision `day`. Any other timestamp must
  carry a UTC offset.

Writers: `POST /v1/links` and batch links (validity is part of the
replay-equality check, so a changed interval is a new link), `PATCH
/v1/links/{id}`, `POST /v1/registry/relations` and `POST /v1/registry/facts`.
`api.links`, `api.relations`, `api.relations_history`, `api.facts` and
`api.facts_history` show the columns; relations and facts stay
unrestricted-only.

## Links: pinned versions, history and supersession

- `from_version_id` / `to_version_id` are the message ids a link named when it
  was created. A database trigger fills them for every writer and refuses
  any update that changes them. Ingest repoints `from_id` / `to_id` to each
  new version; the pinned ids never move.
- Every update and delete of a link stores the image before the change in
  `tl.link_history(history_id, link_id, op, old, changed_at, token_id)`. The
  trigger is the only writer; runtime roles can read and never write it. A
  purge records nothing. `api.link_history` is gated with the rule of
  `api.links` applied to the old image; `GET /v1/links/{id}/history` (owner,
  agent; unrestricted askers only) returns `{link, history}`.
- `POST /v1/links/{id}/supersede` creates the replacement (fields not given are
  copied from the old link) and keeps both rows: the old one gets status
  `superseded` and `superseded_by`. Writer rules are those of PATCH; an
  unchanged replacement is 400, a link that is not active is 409.
- Evidence rule: a `model` link needs a non-empty `evidence.message_ids`
  array, enforced on insert and on change of method or evidence (400; the
  database refuses it for every writer).

## Reversible entity merges

`POST /v1/entities/merge {source, target}` moves every link end and records
`tl.entity_events(op 'merge', source, target, moved, token_id, at)`, where
`moved` lists each moved link, its sides and a hash of its state after the
merge (no link content). The response is `{ok, event_id, moved}`.
`POST /v1/entities/split {event_id}` restores exactly those links to the
source and clears `merged_into`, refusing with 409 when any moved link has
changed since (or the merge was already split, or the source entity changed);
links added to the target later stay. An agent cannot split an owner's merge.

## Provenance

`tl.provenance(derived_kind, derived_id, source_message_id, source_version,
relation, origin, job, job_version, token_id, asker, created_at)`, primary key
`(derived_kind, derived_id, source_message_id)`. The source version is pinned
when the row is written. Runtime roles may insert, never change or delete.

**Source-grade** origins are `source` (a plugin cites the sources of its own
record), `owner` and `job` (a versioned maintenance job). Only they count as
support (`api.support_of`, the record contract's `support` and `unsupported`)
and decide visibility. Origin `agent` is kept apart: never support, never
visibility, not listed in `api.provenance`.

Writers:

1. **Declared link types.** A channel's `provenance_link_types` names link
   types (for GBrain pages `cites`). An active link of such a type from a
   page_revision or derived message to a message records the target as a
   source (origin by link method: `source`, `owner`, else `job`). Because
   ingest repoints links to each new revision, every revision inherits the
   sources of its predecessor. Other link types, and links from other kinds of
   record, record nothing.
2. **`POST /v1/provenance`** (see the HTTP contract).
3. **`lineage.add_provenance(conn, token_or_job, derived_kind, derived_id,
   sources, relation, origin, job, job_version)`** for jobs (passages, the
   librarian's curate mode, claims).

Removing or superseding a link does not remove provenance already written: the
record keeps being gated by that source (the safe direction).

## Visibility (ACL-9)

`api.row_visible(message_id, channel_id, thread_id)` is replaced with the same
signature, so every gated view and search function inherits it: a restricted
asker sees a page_revision or derived record only when its channel or thread
is granted **and** every source-grade source is visible, recursively. The walk
stops at depth 4: a deeper lineage, a cycle or a missing source hides the
record (fail closed). As a consequence:

- `api.messages`, `api.message_versions`, `api.records`, `api.record_versions`,
  legacy `api.search*`, `/v1/search`, `api.sources` and `api.provenance` give
  no row of a page with a hidden source; granting the source's channel makes
  it visible everywhere.
- `api.records_asof` falls back to the latest revision the asker may read:
  hidden revisions are not rows for it. `api.records` (current versions only)
  shows nothing for such a page.
- `api.provenance` shows a row to a restricted asker only when no source of the
  same derived object is hidden (a partly hidden lineage is invisible as a
  whole) and the derived object itself is visible: a derived message when it
  is, a derived link when `api.links` shows it, a derived entity never
  (identity is unrestricted-only), a passage when `api.passages` shows it
  (migration 0037 extends `api.derived_visible`). `api.sources` applies the
  same rule. Claims are gated by their own views.

## Functions

| Function | Returns |
|---|---|
| `api.links_asof(at)` | every link created by `at`, in the state recorded then (from history); a link deleted earlier is absent |
| `api.links_valid_at(at)` | current links whose validity contains `at` |
| `api.follow_asof(kind, id, link_type, depth, at)` | `api.follow` over `links_asof(at)` restricted to active links valid at `at`; a message start is its version stored by `at` |
| `api.relations_asof(at)` | relations in both directions stored by `at` with `applies_at(valid_from, coalesce(valid_to, ended_at), at)`, ended or not |
| `api.facts_asof(at)` | facts stored by `at` with `applies_at(valid_from, coalesce(valid_to, retired_at), at)` |
| `api.sources(kind, id)` | current versions (`api.messages` rows) of the source-grade sources; nothing when any source, or a derived message itself, is hidden |

All are on the query allow-list (`allowlist/0036_lineage.json`), registered
in `tl.api_functions` and described by catalogue section `lineage`
(`api.describe('lineage')`). `GET /v1/sources?kind=&id=` runs `api.sources`
through the audited reader path.

## Purge

Hooks `link_history` and `provenance` delete history images that name a
purged message (as an end, a pinned version or evidence) and provenance whose
derived message, or derived link, is in the purged channel. Provenance rows
whose source is purged cascade with the message. A derived record whose last
source is purged becomes unsourced and is then gated by its own channel only.

## What remains unsourced

Imported pages have source-grade provenance only where the upstream data names
Throughline records (a `cites` link, `throughline_sources` front matter or a
`throughline://` URI delivered through `POST /v1/provenance`) or a job assigns
sources. Every other page or derived record is `unsupported`, never counts as
evidence, and is visible to whoever is granted its own channel or thread.

## Limits

- Link history starts with this migration: `api.links_asof` returns the
  current state for links last changed before it.
- The recursive visibility walk runs per page or derived row for restricted
  askers only; unrestricted askers keep the ungated plan.
