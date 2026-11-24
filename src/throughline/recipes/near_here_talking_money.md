---
{
  "name": "near_here_talking_money",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Times I was near a point and the conversation was about money (QRY-12).",
  "needs": [],
  "statements": [
    "SELECT n.id, n.happened_at, n.content FROM api.near(1, 2, 100) n JOIN api.search_filtered('money', NULL) s ON s.id = n.id ORDER BY n.happened_at"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "id",
      "content"
    ]
  }
}
---

# Near here and talking about money

Join an exact place function with an exhaustive keyword search (limit NULL). Both sides are complete, so the join is complete.

## Adapting

- Swap the keywords; websearch syntax works (`"exact phrase"`, `-exclude`, `or`).
- For meaning instead of words, recall with a place scope (`lat`, `lon`, `meters` in POST /v1/recall) and read the handle.

## Troubleshooting

- A WHERE clause outside a limited search filters only the candidates that search returned: `SELECT * FROM api.search('x', 20) WHERE channel='c'` can return nothing while channel c has matches. Put filters into the function's own arguments (search_filtered, recall's channels/after/before) or pass a NULL limit.
- A conversation about money may use other words: widen with synonyms or check a recall pass.
