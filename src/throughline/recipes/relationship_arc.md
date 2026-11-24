---
{
  "name": "relationship_arc",
  "version": 1,
  "schema_version": "0052",
  "status": "tested",
  "summary": "The arc of a relationship: first, peak and last periods with messages from, to and exchanges per period, and the days since the last contact.",
  "needs": [
    "function:person_arc"
  ],
  "statements": [
    "SELECT a.period, a.messages_from, a.messages_to, a.exchanges, a.first_period, a.peak_period, a.last_period, a.gap_days FROM api.person_arc((SELECT p.id FROM api.people p WHERE p.handle = 'synthetic-friend'), 'month') a"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "period",
      "first_period",
      "peak_period",
      "last_period",
      "gap_days"
    ]
  }
}
---

# Relationship arc

`api.person_arc(person_id, grain)` returns one row per period (week, month, quarter or year, UTC) over the whole history with messages from the person, messages to them and exchanges, plus first_period, peak_period, last_period and gap_days.

## Adapting

- Use `api.interactions(person_id, after_at, before_at, 'week')` for a window instead of the whole history.
- `api.rhythm(person_id, NULL, NULL, 'Europe/Berlin')` says when they write (weekday, hour, month) in a time zone.

## Troubleshooting

- Counts are over messages, not incidents: a burst of 40 lines on one evening is one episode. Count incidents with `api.episodes_of` when the question is "how often".
- A restricted asker's arc covers granted rows only.
