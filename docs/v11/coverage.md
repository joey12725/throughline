# Source coverage

Migration `0046_coverage.sql`, module `throughline.coverage`, routes in
`api/routes/coverage.py`, command `throughline lib coverage`.

Coverage says what each source reports it delivered, per channel and scope
key (for example a chat or an account): a status, reason codes, the covered
window, the high water mark and the open gaps. It lets an answer say "not
found in the sources we have, and these sources are complete for that
window" instead of guessing.

## Statuses and reasons

| status | meaning |
| --- | --- |
| `complete` | the source delivered everything in the window |
| `partial` | something in the window is missing or unknown; see reasons and gaps |
| `best_effort` | the source cannot say what is missing (no history listing); the default when nothing was reported |

Reason codes live in `tl.coverage_reasons` (a seed hook): `not_reported`,
`run_failed`, `stale`, `source_cap`, `source_error`, `rate_limited`,
`auth_failed`, `quarantined_rows`, `filtered_by_policy`, `listing_lag`,
`partial_history`, `unknown_shape`, `unrecognised_status`,
`unrecognised_reason`, `retry_pending`, `empty_verified`.

## Unknown degrades to partial

A strict gate must never turn partial data into zero progress, so
`coverage.normalize` never raises on shape:

- `complete`, `partial` and `best_effort` are kept. Harmless synonyms map to
  complete (`THROUGHLINE_COVERAGE_COMPLETE_SYNONYMS`, default `ok, success,
  done, empty, empty_thread`). An empty-like status (`empty`, `empty_thread`)
  is complete with reason `empty_verified` only when the report says
  `verified: true`; otherwise it is partial with `unrecognised_status`.
- Any other status is partial with `unrecognised_status`; the raw value is kept
  in `raw_status`.
- A `detail` that is not an object is stored as `{"value": ...}` with
  `unknown_shape`; an unparsable timestamp becomes null with `unknown_shape`.
  A report whose shape was not understood is not proof of completeness, so a
  `complete` with `unknown_shape` is stored as partial.
- An unknown reason code is kept in `raw_detail.unrecognised_reasons` and adds
  `unrecognised_reason`.
- The raw report is logged in `tl.coverage_reports` (at most 64 KB; larger
  ones are truncated and flagged) beside its normalised form.

Only authentication and plugin ownership give 4xx. A report naming another
plugin's channel (or an unknown one) is refused and counted.

## Gaps

A gap is not absence: it marks a window the source did not deliver.

- A gap opens only from an explicit `gaps: [{from, to, reason}]` entry, or over
  the report window when the normalised status is `partial`.
- A `best_effort` report records its status and reasons but opens no window
  gap, so sources without history listings do not pile up permanent gaps.
- A gap closes only when a report names it in `closes` (by id, or by
  `{channel, scope_key, from, to}` matching its window), or covers
  `[gap_from, gap_to]` within its window with status complete and
  `rows_delivered > 0` or `verified: true`. A healthy empty report without
  `verified` never closes an earlier failed interval.
- An attempt (`POST .../coverage/attempt`) on a channel without coverage
  stores a placeholder row (`best_effort`, `not_reported`); `api.coverage`
  leaves it out as soon as the channel has a reported row, for example one per
  scope key. A `run_failed` row at scope key `''` (what the SDK posts when a
  run fails) is turned back into such a placeholder by the next successful
  report (complete or best_effort) for a scope key of the same channel.
- `GET /v1/plugins/{name}/coverage/gaps` lists the open gaps of the plugin's
  own channels with ids, so a stateless importer can close them.

Purging a channel (`throughline purge`) removes its coverage rows, gaps and
report log through purge hooks, so no scope key, raw detail or 'complete'
status outlives the purged messages.

## Staleness

A plugin whose last success (heartbeat or complete report) is older than twice
its interval is stale: reason `stale` is added and `complete` degrades to
`partial` in `api.coverage`. `GET /v1/status` reports `staleness`:
newest ingest and its age, each plugin's last success, age and staleness,
partial and best-effort channels, the number of open gaps, and the last
backup and verification from `tl.backup_runs` (stale above
`THROUGHLINE_BACKUP_STALE_HOURS`, default 36, or when there is no run).

## HTTP

- `POST /v1/plugins/{name}/coverage` `{reports: [{channel?, scope_key?, status,
  reasons?, window {from, to}?, high_water?, rows_delivered?, pending_retries?,
  gaps?, closes?, verified?, detail?}]}` (at most 1,000) answers `{stored,
  degraded: [{index, reasons}], refused, gaps_opened: [{index, id, from, to}],
  gaps_closed: [ids]}`. Plugin token for its own plugin, or owner.
- `POST /v1/plugins/{name}/coverage/attempt` `{channels?}` notes an attempt.
- `GET /v1/plugins/{name}/coverage/gaps?channel=&scope_key=`.
- `GET /v1/coverage?channels=&after=&before=` (owner or agent; audited reader
  path): `api.coverage_for` rows plus plugin-level rows for unrestricted
  callers only.

## SQL

- `api.coverage` and `api.coverage_gaps`: gated views. A restricted asker sees
  rows of granted channels only; plugin-level rows (no channel) are
  unrestricted-only. Every channel without a report has a default row
  (`best_effort`, `not_reported`).
- `api.coverage_for(channels, after_at, before_at, kinds)`: per visible
  channel the worst status (best_effort < partial < complete), reasons,
  covered range, last success, staleness, the open gaps overlapping the window
  and `window_status` (`covered`, `partly_covered`, `not_covered`, `unknown`).
- `api.describe('coverage')`: the catalogue section, reason codes and the
  coverage of your channels.

## Recall and packets

Every recall response carries `execution.coverage` computed on the final
records on every path (results, empty candidates, records cut by hydration or
the token budget, the unresolvable-scope early return): `{status, covered,
channels, partial_channels, gaps, absence}`. When no record is a hit, or a
`hints.must_terms` term is in no hit (listed in `missing_terms`),
`absence` is `not_found_in_covered_sources` when every channel in scope is
complete and covers the window, else `not_found_in_coverage`. It never says
that something did not happen. The warning `absence_is_not_evidence` is added
when coverage is not complete, and `coverage_current_only` when `as_of` or
`known_by` is set (coverage describes today). A failure of the coverage
statement degrades the recall and never fails it.

Packets fill `coverage.known_source_gaps` (partial channels and open gaps of
the packet records' channels under the caller's scope) and
`coverage.source_status` per channel.

## SDK

`throughline_sdk.coverage(channel, scope_key, status, reasons, window,
high_water, rows_delivered, gaps, closes, verified, detail)` is a record the
runner posts after the messages yielded before it. `Plugin.execute` posts an
attempt for the manifest channels before `records()`, a `best_effort` report
with `rows_delivered` and `high_water` for channels that delivered messages
without a coverage record, and on an exception a `partial` report with
`run_failed` and a redacted detail (a failure there never masks the original
error). A dry run sends nothing. `Client.coverage`, `Client.coverage_attempt`
and `Client.coverage_gaps` are available to importers.
