---
{
  "name": "person_brief_first",
  "version": 1,
  "schema_version": "0052",
  "status": "tested",
  "summary": "Start any question about a person with the brief: who they are, contact, exchanges, open loops, circle rank and coverage in one row.",
  "needs": [
    "function:person_brief"
  ],
  "statements": [
    "SELECT b.person, b.handle, b.first_contact_at, b.last_contact_at, b.message_count, b.messages_to, b.exchange_count, b.open_loop_count, b.circle_rank, b.coverage FROM api.person_brief((SELECT p.id FROM api.people p WHERE p.handle = 'synthetic-friend')) b"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "person",
      "last_contact_at",
      "exchange_count",
      "open_loop_count",
      "coverage"
    ]
  }
}
---

# Person brief first

The default first call for any person question is `api.person_brief(person_id)` (or `GET /v1/people/{id}/brief`). One row: handle, names and identifiers (unrestricted askers only), first and last contact with their message ids, messages from and to the person, exchanges, top channels, open loops, the circle rank among the owner's ties, recent threads, facts, relations and the coverage of the channels involved.

## Adapting

- Find the id with `SELECT * FROM api.person('<handle or name>')`, then brief, then the specific function the brief points at (`api.last_contact`, `api.open_loops`, `api.person_arc`, `api.between`, `api.circle`).
- Read `coverage` before you say "they never wrote about X": a gap is not absence.
- Every row of every person function carries attribution: `is_me`, `direction` and `attribution_status` (confirmed, self_direction, annotated, suspected, unknown). Say how sure the identity is.

## Troubleshooting

- A restricted asker gets counts over granted rows only and no identity fields; that is the gate, not missing data.
- Suspected accounts are excluded until the owner confirms them (`POST /v1/identity/identifiers/{id}/status`), so a brief can be smaller than a name search. Names alone are never links.
- Exchange counts come from an index the person routes refresh before they answer; over SQL read `api.exchange_index_status()` and say so when threads are stale.
