---
{
  "name": "refine_saved_set",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Refine a saved result set with SQL instead of searching again.",
  "needs": [
    "function:recall",
    "function:result_set"
  ],
  "statements": [
    "SELECT r.record_id FROM api.recall(query => 'orchard', k => 10) r",
    "SELECT x.record_id, x.channel, x.record_time FROM api.result_set('{{handle}}') x WHERE x.channel = 'synthetic.chat' AND x.record_time < '2020-01-01T00:05:00Z' ORDER BY x.record_time"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "record_id",
      "record_time"
    ]
  }
}
---

# Refining a saved set

A handle is an input to SQL, recall, expand, match and plans. Filtering it in SQL refines the same candidates; POST /v1/sets/transform (filter, order, dedup, group, union, intersect, difference) saves the refinement as a new handle.

## Adapting

- Group by episode, conversation or time with the transform 'group'.
- Members are joined live to the gated record views, so content and visibility are current.

## Troubleshooting

- A WHERE clause outside a limited search filters only the candidates that search returned: `SELECT * FROM api.search('x', 20) WHERE channel='c'` can return nothing while channel c has matches. Put filters into the function's own arguments (search_filtered, recall's channels/after/before) or pass a NULL limit.
- A set is a ranked_sample unless it says exhaustive; a WHERE on it never adds records that were not recalled.
