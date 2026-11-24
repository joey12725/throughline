---
{
  "name": "as_of_page_lookup",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Read a page as it was at a time: the revision valid at a record-time cutoff.",
  "needs": [],
  "statements": [
    "SELECT r.record_id, r.version, r.record_time, r.content FROM api.records_asof('2020-01-01T00:30:00Z') r WHERE r.channel = 'synthetic.page'"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "record_id",
      "version",
      "record_time",
      "content"
    ]
  }
}
---

# As-of page lookup

`api.records_asof(at, known_by)` returns per (channel, external_id) the version valid at `at`. Page revisions are chosen by revision time (record_time).

## Adapting

- Add known_by to ask what Throughline knew at that time (an ingest-time cutoff).
- Passages of that revision: `api.passages_of(message_id)`.

## Troubleshooting

- as_of is a record-time cutoff (which version was valid then); known_by is an ingest-time cutoff (what Throughline had stored by then). Without known_by, edits other than page revisions have no time of their own.
- Pages are generated text: cite their sources (`api.sources('message', id)`), not the page itself.
