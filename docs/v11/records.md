# Records: kinds, subject time, the record contract, as-of and describe

Migration `0032_records.sql`, allow-list `allowlist/0032_records.json`,
`throughline.models`, `throughline.ingest`, `throughline.location`,
`throughline.reparse`, route `GET /v1/describe`, command
`throughline lib describe`.

## Record kinds (MSG-8)

Every channel has one `record_kind` in `tl.channels` (and `api.channels`):

| Kind | Evidence | Location | Typical source |
|---|---|---|---|
| `life_event` | itself | source, else the owner's track | messages, plays, purchases |
| `reference` | itself | none | manuals, documentation (requires `is_reference`) |
| `page_revision` | its provenance sources only | none | one revision of a written page |
| `derived` | its provenance sources only | source only, never the track | detected visits, timeline entries |

A channel registered without `record_kind` keeps its stored kind; a new one
(or one whose `is_reference` changes) becomes `reference` when `is_reference`,
otherwise `life_event`. The migration backfilled `reference` for reference
channels and `derived` for the native `visits` channel, which
`location.detect_visits` now registers as `derived`.

Two further channel properties are stored for later components:
`passage_text_paths` (entries of `|`-separated JSON pointers into the raw
payload; the first non-empty pointer of an entry wins and entries are joined
by a blank line) and `provenance_link_types`.

The record contract column names are reserved channel field names.

## Subject time (MSG-9)

`Message.subject = {start, end, start_precision, end_precision}` becomes
`tl.messages.subject_from`, `subject_to`, `subject_from_precision`,
`subject_to_precision`. `happened_at` stays the record time (for a page
revision, the revision time); the subject interval is the period the record is
about. Precisions: `exact` (default), `day`, `month`, `year`, `approximate`,
`unknown`. A CHECK keeps `subject_to >= subject_from`. A precision qualifies
a bound, so an omitted end is stored with a NULL precision (a second CHECK).

The four columns are ground truth: they are in `tl.immutable()`'s column list
and in the payload hash. A message without a subject hashes exactly as before
the field existed, so no stored record is re-versioned; a changed subject is
one new version. The subject is carried through the message envelope and
through `reparse`.

## Location applicability (MSG-10)

An absent location is a value keyed to the kind. `loc_reason` is
`no_track_point_within_tolerance` only for a life event without a source or
track location; every other record without a location is `not_applicable`
(this replaces `reference_channel`). A CHECK admits only these two reasons or
NULL. Track attachment (at ingest, `attach_late`, and after `retire_track`)
considers only life events.

## Record contract (MSG-7)

Every record view in the project has these columns in this order:

`record_id, kind, ref_id, message_id, version, is_current, channel,
external_id, thread_id, thread_no, content, record_time, ended_at,
ingested_at, subject_from, subject_to, subject_from_precision,
subject_to_precision, person_id, author_handle, lat, lon, loc_reason,
is_outside, heading_path, char_start, char_end, claim, generated, unsupported,
provenance, support, expansion`

- `api.record_versions`: one row per message version, `record_id` `m:<id>`.
  Message columns come from `api.message_versions`, so its asker gate applies.
  `record_time` is `happened_at`; `heading_path`, `char_*` and `claim` are
  null for messages.
- `api.records`: the current versions.
- `generated` is true for `page_revision` and `derived`. `support` is
  `api.support_of(kind, message_id, version)`: `[{message_id, version,
  relation:'self'}]` for life events and reference records, `[]` for generated
  records until migration 0036 supplies provenance sources. `unsupported` is
  `generated AND support = '[]'`.
- `provenance` is `{plugin, channel, external_id, version, method:'source'}`;
  `expansion` is `{context, thread, key:[channel, external_id]}`.

`tl.record_sources(prefix, relation)` registers record views by prefix (`m` ->
`api.record_versions`); later migrations add `p`, `l`, `e`, `c`.

## As-of selection (BRN-5)

`api.records_asof(at, known_by DEFAULT NULL)` returns one version per
`(channel, external_id)`:

- page revisions: the revision with the greatest `record_time <= at` (ties:
  highest version);
- other kinds: the highest version with `record_time <= at` and, when given,
  `ingested_at <= known_by`.

Stated limit: without `known_by`, non-page records are returned in their
current version, because an edit has no time of its own besides its ingest
time.

`api.applies_at(valid_from, valid_to, at)` is the half-open validity test
`[valid_from, valid_to)` with open ends unbounded, shared by links, relations,
facts and claims.

## Function registry and describe (INT-8, QRY-16, LIB-3)

`tl.api_functions` lists every function of the SQL surface with kind,
signature (from the system catalogue), description, exactness, cost class,
level, access, an example that runs on an empty database, version, and for
ranked functions the position of the limit argument and its default.
`tl.catalog_sections` holds the `record_kinds`, `time`, `location` and
`levels` sections. Levels are capabilities an agent combines, not modes:
0 exact SQL, 1 discovery, 2 expansion, rerank and selection, 3 the optional
librarian.

`api.catalog()` keeps every key it returned before (now
`api.catalog_identity()`) and adds `functions`, `sections` and
`catalog_version`.

`api.describe(subject)`:

| Subject | Result |
|---|---|
| NULL | index: subjects, visible channels, function names, catalog version |
| `functions` | every registered function |
| `function:<name>` | one function, or null |
| `channel:<name>` | description, kind, fields, current message count, first and last record time, last ingest; `maintenance` (dirty threads, embedded share of closed conversations, plugin last success and error) for an owner scope only, null otherwise |
| `<s>` with a function `api.describe_<s>()` | that function's result (other packages add subjects this way) |
| any other | the `tl.catalog_sections` body, or null |

describe reads channel, message and plugin data only through api views, so the
asker gate scopes it: a hidden channel and an unknown channel both give null.

HTTP: `GET /v1/describe?subject=...` (owner or agent token) runs through the
audited reader path. CLI: `throughline lib describe [SUBJECT] [--json]`.
