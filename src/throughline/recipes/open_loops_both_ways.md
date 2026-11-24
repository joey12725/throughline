---
{
  "name": "open_loops_both_ways",
  "version": 1,
  "schema_version": "0052",
  "status": "tested",
  "summary": "Open loops in both directions: asks the owner has not answered (owner_owes) and asks the other side has not answered (they_owe).",
  "needs": [
    "function:open_loops"
  ],
  "statements": [
    "SELECT o.direction, o.message_id, o.asked_at, o.counterpart_party_id, o.age_days, o.last_thread_activity_at FROM api.open_loops(NULL, '1 day', 100000, 50) o ORDER BY o.direction, o.asked_at"
  ],
  "expect": {
    "min_rows": 2,
    "columns": [
      "direction",
      "message_id",
      "asked_at",
      "age_days"
    ]
  }
}
---

# Open loops in both directions

An open loop is an ask (a line ending in '?' or matching the request lexicon, THROUGHLINE_ASK_PATTERNS) older than `older_than` with no later message by the other party in that thread. `direction` is `owner_owes` (they asked, the owner has not answered) or `they_owe` (the owner asked).

## Adapting

- One person: `api.open_loops(person_id, '1 day', 30, 50)`. Everybody: pass NULL.
- `horizon_days` drops wound-down chats (no activity for that many days); this recipe widens it to 100000 because its corpus is old. Keep the default 30 for "what do I owe people now".
- Closing lines by the asker ("never mind", "all good"; THROUGHLINE_CLOSING_PATTERNS) close a loop.

## Troubleshooting

- A reply in another channel does not close a loop in this one; read `api.context` around the ask before you nag anyone.
- The owner side is `is_me` (owner attribution or direction outgoing), so it works where the owner's own account is not confirmed.
