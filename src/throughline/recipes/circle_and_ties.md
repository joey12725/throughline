---
{
  "name": "circle_and_ties",
  "version": 1,
  "schema_version": "0052",
  "status": "tested",
  "summary": "A person's circle ranked by strength (exchanges plus shared conversations) and the breakdown of one tie.",
  "needs": [
    "function:circle",
    "function:ties"
  ],
  "statements": [
    "SELECT c.other_person_id, c.exchanges, c.shared_conversations, c.strength, c.relative_strength, c.rank FROM api.circle(NULL, 'absolute', NULL, NULL, 20) c",
    "SELECT t.person_a, t.person_b, t.exchanges, t.a_responded, t.b_responded, t.shared_conversations, t.strength FROM api.ties((SELECT p.id FROM api.people p WHERE p.is_owner), (SELECT p.id FROM api.people p WHERE p.handle = 'synthetic-friend')) t"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "exchanges",
      "shared_conversations",
      "strength"
    ]
  }
}
---

# Circle and ties

`api.circle(person_id, mode, after_at, before_at, max_results)` ranks a person's ties (NULL is the owner). strength = exchanges + shared conversations. relative_strength = strength / the person's total.

## The exchange definition

Message R responds to T when an active reply_to link goes from R to T, or R is the next turn after T in the same thread within THROUGHLINE_EXCHANGE_WINDOW_S (default 1800 s) by a different author. A shared conversation is an episode with both people in it.

## Adapting

- `api.ties(a, b)` breaks one tie down: exchanges by direction, shared conversations and threads, first and last exchange.
- Use mode 'relative' to compare people with very different volumes.

## Troubleshooting

- Strength counts exchanges, not messages: a long monologue is not a strong tie.
- Over SQL the exchange index may be stale; `api.exchange_index_status()` says how many threads await a rebuild.
