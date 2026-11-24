---
{
  "name": "historically_valid_relationship",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Join records to the relationships that were valid at a past time.",
  "needs": [],
  "statements": [
    "SELECT l.id, l.type, l.valid_from, l.valid_to, m.content FROM api.links_valid_at('2020-06-01Z') l JOIN api.messages m ON l.from_kind = 'message' AND m.id = l.from_id ORDER BY l.id"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "type",
      "valid_from",
      "valid_to",
      "content"
    ]
  }
}
---

# Historically valid relationship join

`api.links_valid_at(at)` returns links whose validity interval contains `at`; `api.links_asof(at)` returns links as they were recorded at `at` (history of edits). Relations and facts have `_asof` functions with the same shape.

## Adapting

- Validity of any row: `api.applies_at(valid_from, valid_to, at)`.
- Follow a graph at a time: `api.follow_asof(...)`.

## Troubleshooting

- Historical relationships hold for a validity interval [valid_from, valid_to). Use api.links_valid_at(at), api.relations_asof(at) or api.applies_at(valid_from, valid_to, at); a current link is no proof the relationship held at an earlier time.
- as_of is a record-time cutoff (which version was valid then); known_by is an ingest-time cutoff (what Throughline had stored by then). Without known_by, edits other than page revisions have no time of their own.
