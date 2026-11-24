---
{
  "name": "day_with_person",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Everything a person wrote on one day (QRY-12).",
  "needs": [],
  "statements": [
    "SELECT m.id, m.happened_at, m.channel, m.content FROM api.person_messages((SELECT p.id FROM api.person('synthetic-friend') p LIMIT 1)) m WHERE m.happened_at >= '2020-01-01Z' AND m.happened_at < '2020-01-02Z' ORDER BY m.happened_at"
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

# A day with a person

Resolve the person once with `api.person(name or handle)`, then read their messages in a day window.

## Adapting

- Several matches from api.person: pick the id deliberately instead of LIMIT 1.
- To include the other side of the conversations, read the threads of these messages with `api.context` or `api.context_many`.

## Troubleshooting

- Author versus involved: person_messages and person_role 'author' select what a person wrote; a conversation the person took part in also holds other people's messages. Use person_role 'involved' (recall) or read the thread when you mean participation.
- api.person is unrestricted-only; a restricted asker gets no rows and filters messages by author_handle instead.
- Timestamps are UTC; shift the window for a local day.
