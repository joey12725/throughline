# Annotations

Module `throughline.annotations`, routes `throughline.api.routes.annotations`,
CLI `throughline lib annotate|annotation`, migration `0042_claims.sql`. Design
priority P4a (default exclusion of ignorable records, effective time basis) and
P4b (annotations).

**Annotations never rewrite ground truth.** An annotation states something about
a message beside it; content, payloads and times stay as delivered.

## Attributes

| Attribute | Value |
|---|---|
| `effective_time` | `{at, ended_at?, precision}` (timestamps with offset) |
| `effective_place` | `{name, entity_id?}` |
| `location` | `{lat, lon, accuracy_m?}` |
| `ignore` | `{ignored: boolean, reason?}` |
| `media_noise` | `{is_noise: boolean, score?, evidence?, matched_record_id?}` |
| `speaker` | `{person_id}` (identity: unrestricted askers only) |

Values are checked by the database. `ignore` and `media_noise` are booleans,
so a "not ignored" or "not noise" can override a flag.

Rows (`tl.annotations`) are keyed by channel and `external_id`;
`target_version` NULL applies to every version, so re-import and new versions
keep the curation. Rows are never edited: supersede writes a new row, retract
changes the status (reason in the status history).

## Writers

- Owner token: written directly (method owner).
- Jobs (Python `annotations.create(conn, {job, job_version, method}, ...)`, for
  example a media-noise run): method rule or model. A job never writes over an
  active owner annotation of the same attribute and target (the write is
  skipped and reported), and never supersedes or retracts one. The database
  refuses it too: an owner annotation is never superseded by a job row, and
  its status never changes under a non-owner token.
- Agent tokens and restricted scopes: `POST /v1/annotations` returns 202 with
  a proposal; the owner accepts it into a job annotation.
- Plugin tokens: 403.

## Precedence

For each attribute: **owner > source field > rule > model**, the latest
within a method. The source field is the record's own non-empty
`fields.ignore_reason` (Wave and any channel declaring that field), counted
as an ignore without being copied. A record is ignored when an owner
`ignore` says so (the owner's ignore decides alone), else when the effective
`ignore` or `media_noise` asserts true. So an owner `{ignored: false}`
restores a record a source or a job flagged, and later jobs do not override
it; an owner `{is_noise: false}` restores a job's noise flag.

## Recall

`tl.recall_scope` (behind `api.scope_filter`, `search_records`,
`vector_records`, `list_records` and every recall leg, expansion and stage
that creates a hit):

- **Ignored by default.** A record whose effective annotation state is ignored is
  eligible only when kinds contains the pseudo-kind `ignored` (recall:
  `include_ignored`). `ignored` is removed before kind matching; kinds of only
  `ignored` means the default kinds plus ignored records.
- **`time_basis = 'effective'`** tests `[coalesce(effective_time,
  record_time), coalesce(effective_end, ended_at, effective_time,
  record_time)]` against the window with the record rule. `tl.recall_check`
  accepts `effective`; any other unknown basis still raises.
- **Cutoffs.** Under `known_by` only annotations created by then apply, in the
  state they had then (no future curation leaks into a historical knowledge
  view). Under `as_of` alone the current annotations apply, limited to those
  for every version or for the version valid at `as_of` (as_of is world time,
  not knowledge time).
- The legacy `api.search*` functions are unchanged.

## Reading

- `api.annotations` – annotations joined to the current version of their
  target; a restricted asker sees those of messages it may see, never
  speaker rows. `GET /v1/annotations?message_id=`.
- `api.effective(message_ids)` – effective time, end and precision, place,
  location, ignored with reasons, media noise, speaker (NULL for restricted
  askers) and the annotation ids that apply; reads gated views only.
  `GET /v1/effective?ids=1,2,3`.

## Performance

The effective annotation state is not computed for a record without annotations and without an
`ignore_reason` field (one index probe). For the rest `tl.recall_scope`
evaluates the `tl.overlay_state` rule in line (a per-row call of the
SECURITY DEFINER helper would cost about ten times the work). With 2,000
annotations over 20,000 messages `search_records` stays within 25% of its
time without annotations (tested).

## Purge

The purge hook deletes the channel's annotations with their provenance and
status history.
