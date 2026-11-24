---
{
  "name": "near_place_six_months",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Every time near a named place within a six-month window (QRY-12).",
  "needs": [],
  "statements": [
    "SELECT m.id, m.happened_at, m.channel, m.content FROM api.near_place('Synthetic place', 100) m WHERE m.happened_at >= '2019-10-01Z' AND m.happened_at < '2020-04-01Z' ORDER BY m.happened_at"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "id",
      "happened_at",
      "content"
    ]
  }
}
---

# Near a place in six months

`api.near_place(name, meters)` resolves a place entity and returns every message whose location lies within the radius. It is exact (not ranked), so a WHERE on time filters the whole population.

## Adapting

- Replace the place name and the radius; use `api.near(lat, lon, meters)` for raw coordinates.
- Group by day (`date_trunc('day', happened_at)`) to list visits instead of messages.

## Troubleshooting

- Records without a location (reference text, page revisions, derived rows without a source location) have loc_reason 'not_applicable' and never match.
- An unknown place returns no rows, not an error; check `SELECT * FROM api.places`.
- record_time (happened_at) is when the record happened; for a page revision it is the revision time. subject_from/subject_to is the period the record is about. Choose time_basis record | subject | either in recall.
