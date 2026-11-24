---
{
  "name": "match_over_bounded_set",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Judge every member of a bounded set against a condition (three-valued; NULL on failure).",
  "needs": [
    "function:recall",
    "function:match"
  ],
  "statements": [
    "SELECT r.record_id FROM api.recall(query => 'orchard', k => 5) r",
    "SELECT m.record_id, m.meta->>'verdict' AS verdict FROM api.match(condition => 'mentions an orchard', input => '{{handle}}', max_rows => 10) m"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "record_id",
      "verdict"
    ]
  }
}
---

# Match over a bounded set

api.match judges each member: match, nonmatch or uncertain, and NULL when the evaluation failed (never counted as zero). max_rows bounds the cost; the estimate is checked against your budget before any model call.

## Adapting

- Input from SQL: `input_sql => 'SELECT ...', max_rows => n`.
- Filter verdicts: `WHERE m.meta->>'verdict' = 'match'`.

## Troubleshooting

- approval_required in execution means the estimate exceeded your limit; nothing was spent.
- An aggregate over ranked results is not a count. count(*) over api.search(..., 20) or over a recall handle counts a ranked sample; execution.warnings says so. Count with a NULL limit (exhaustive) or over plain views.
