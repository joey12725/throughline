---
{
  "name": "playing_when_said",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "What was playing (or happening) while a message was written (QRY-12).",
  "needs": [],
  "statements": [
    "SELECT d.channel, d.happened_at, d.ended_at, d.content FROM api.messages m CROSS JOIN LATERAL api.during(m.id) d WHERE m.external_id = 'fake-2' AND d.channel = 'synthetic.music'"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "channel",
      "content"
    ]
  }
}
---

# What was playing when someone said something

`api.during(message_id)` returns records whose time interval overlaps the given message's interval. Filter by the channel that records plays.

## Adapting

- Find the anchor message first (search, recall or an id from an earlier answer).
- `api.overlapping(from, to)` does the same for an explicit window.

## Troubleshooting

- Plays need ended_at (a duration); a record without one only matches at its instant.
- record_time (happened_at) is when the record happened; for a page revision it is the revision time. subject_from/subject_to is the period the record is about. Choose time_basis record | subject | either in recall.
