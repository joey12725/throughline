---
{
  "name": "last_contact_pitfall",
  "version": 2,
  "schema_version": "0052",
  "status": "tested",
  "summary": "When did we last talk: the newest relevant record (last from, last to, last exchange), never the newest record of a shared thread.",
  "needs": [
    "function:last_contact",
    "function:person_timeline"
  ],
  "statements": [
    "SELECT l.last_from_at, l.last_from_message_id, l.last_to_at, l.last_to_message_id, l.last_exchange_at, l.last_contact_at, l.last_contact_message_id FROM api.last_contact((SELECT p.id FROM api.people p WHERE p.handle = 'synthetic-friend')) l"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "last_from_at",
      "last_to_at",
      "last_exchange_at",
      "last_contact_at"
    ]
  }
}
---

# Last contact and the newest-relevant pitfall

`api.last_contact(person_id)` answers "when did we last talk": the last message from them, the last message to them (owner-side messages in their one-to-one threads, and replies to them), the last exchange, and the latest of those as last_contact.

## The pitfall

The newest record of a thread the person is in is not contact with them: a group chat keeps moving after they left it, and a forwarded or quoted line is not a conversation. Never answer with `max(record_time)` over a thread or over `api.messages WHERE content ILIKE '%name%'`. Ask for the newest relevant record instead: `api.last_contact`, or recall with `hints.order = 'newest'` (newest relevant, not newest overall).

## Adapting

- Then read the context of `last_contact_message_id` with `api.context(id, 5, 5)` before you describe what was said.
- `api.person_timeline(person_id, after_at, before_at, max_results)` lists both sides, newest first.

## Troubleshooting

- last_to is NULL when the owner never wrote to them in a one-to-one thread and never replied to them.
- Times are effective times (annotations, else record time).
