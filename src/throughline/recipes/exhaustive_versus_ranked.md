---
{
  "name": "exhaustive_versus_ranked",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Count a population exhaustively instead of counting a ranked sample.",
  "needs": [],
  "statements": [
    "SELECT count(*) AS sample FROM api.search('orchard', 3)",
    "SELECT count(*) AS n FROM api.search_filtered('orchard', NULL, NULL, 'synthetic.chat')"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "n"
    ]
  }
}
---

# Exhaustive enumeration versus ranked sample

The first statement counts a top-3 sample (execution.warnings flags the aggregate); the second passes a NULL limit, so the function enumerates every match and the count is a population count.

## Adapting

- recall with exhaustive => true asks for enumeration when the scope is small enough; check set_completeness.
- strict_exact => true on POST /v1/sql rejects aggregates over ranked input before any work.

## Troubleshooting

- An aggregate over ranked results is not a count. count(*) over api.search(..., 20) or over a recall handle counts a ranked sample; execution.warnings says so. Count with a NULL limit (exhaustive) or over plain views.
- A hit count is not an incident count; see incidents_by_episode.
