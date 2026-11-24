---
{
  "name": "inspect_trace",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Ask for a trace and read what an operation actually ran.",
  "needs": [
    "function:recall",
    "route:GET /v1/traces/{trace_id}"
  ],
  "statements": [
    "SELECT r.record_id FROM api.recall(query => 'orchard', k => 3) r"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "record_id"
    ]
  }
}
---

# Inspecting a trace

Send the statement with `"trace": true` on POST /v1/sql (or recall, plans, packets); execution.trace_id names the trace. GET /v1/traces/<id> returns its events: statements, row counts, timings, models, tokens and cost. Traces hold ids, counts and hashes, never excerpts.

## Adapting

- `throughline lib trace <id>` prints the same events.
- Compare stages and timings_ms in execution with the trace to see which leg or stage was slow or degraded.

## Troubleshooting

- 404: traces are readable only by the token and asker that wrote them. 409: the access policy changed since.
- execution.incomplete lists legs or stages that failed and degraded the answer.
