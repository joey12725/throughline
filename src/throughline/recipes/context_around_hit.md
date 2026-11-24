---
{
  "name": "context_around_hit",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "The previous ten and next five messages around a hit (QRY-12).",
  "needs": [],
  "statements": [
    "SELECT c.id, c.thread_no, c.happened_at, c.content FROM api.context((SELECT m.id FROM api.messages m WHERE m.external_id = 'fake-6' LIMIT 1), 10, 5) c ORDER BY c.thread_no"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "id",
      "thread_no",
      "content"
    ]
  }
}
---

# Context around a hit

`api.context(message_id, before, after)` reads the numbered thread around a message. Always read context before concluding from a single hit.

## Adapting

- Many anchors at once: `api.context_many(ARRAY[...], before, after)` returns (anchor_id, message_id, ...).
- From a result set: see the recipe recall_then_context.

## Troubleshooting

- Context is the thread's order, not time across channels; use api.during for parallel channels.
