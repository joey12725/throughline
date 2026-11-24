---
{
  "name": "coverage_first",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Check source coverage before saying something is absent.",
  "needs": [
    "function:coverage_for"
  ],
  "statements": [
    "SELECT c.channel, c.status, c.window_status, c.reasons, c.gaps FROM api.coverage_for(ARRAY['synthetic.chat'], '2020-01-01Z', '2020-02-01Z') c"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "channel",
      "status",
      "window_status"
    ]
  }
}
---

# Coverage first

Before answering "it never happened" or "nothing about X", read coverage for the channels and window you searched. `api.coverage_for(channels, after, before, kinds)` returns status (complete, partial, best_effort), reasons, open gaps overlapping the window and window_status.

## Adapting

- Recall reports the same in execution.coverage, with absence 'not_found_in_covered_sources' or 'not_found_in_coverage'.
- `throughline lib coverage --channel C --after A --before B` prints it.

## Troubleshooting

- 'Not found in covered sources' (every channel complete and covered) is a statement about the archive; 'not found in coverage' (a channel partial, best_effort or with a gap) is not evidence of absence. Say which.
- A gap is not absence: an open gap means the source did not deliver that interval.
- An unknown status from a plugin degrades to partial; read reasons and the raw detail.
