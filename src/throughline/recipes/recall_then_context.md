---
{
  "name": "recall_then_context",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Discover with recall, then read the thread around every hit through the saved handle.",
  "needs": [
    "function:recall"
  ],
  "statements": [
    "SELECT r.record_id, r.message_id, r.channel FROM api.recall(query => 'orchard', k => 5) r",
    "SELECT c.anchor_id, m.thread_no, m.content FROM api.context_many(ARRAY(SELECT x.message_id FROM api.result_set('{{handle}}') x WHERE x.message_id IS NOT NULL), 2, 2) c JOIN api.messages m ON m.id = c.message_id ORDER BY c.anchor_id, m.thread_no"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "anchor_id",
      "content"
    ]
  }
}
---

# Recall, then context

`api.recall(query => ...)` is discovery when the words are unknown. The API runs it first, saves the result as one of your result sets and the statement reads it; execution.semantic_calls names the handle. A later statement reads the same set with `api.result_set('<handle>')` (written `{{handle}}` in this recipe).

## Adapting

- Add hard scope to the recall call (see recall_hard_scope) rather than filtering afterwards.
- POST /v1/expand with mode 'context' does the same expansion as a new result set.

## Troubleshooting

- Handles belong to your token and asker and expire; a policy change makes them stale (409). Recall again.
- An aggregate over ranked results is not a count. count(*) over api.search(..., 20) or over a recall handle counts a ranked sample; execution.warnings says so. Count with a NULL limit (exhaustive) or over plain views.
