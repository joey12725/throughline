---
{
  "name": "feed_loop",
  "version": 2,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Process only what is new: a named feed cursor keyed on arrival time.",
  "needs": [
    "function:news",
    "function:pending",
    "route:POST /v1/feeds"
  ],
  "statements": [
    "SELECT p.consumer, p.pending, p.horizon FROM api.pending('recipe_demo') p"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "consumer",
      "pending"
    ]
  }
}
---

# Process only what is new

A feed is a named cursor of your token and asker over arrival time (ingested_at, message id), never message time, so a late backfill of old messages is delivered once.

## The loop

1. Create once: POST /v1/feeds {"name": "recipe_demo", "scope": {"channels": [...]}, "start": "now"}.
2. Read: GET /v1/feeds/recipe_demo/news?max=200 (or `SELECT * FROM api.news('recipe_demo', 200)`); each row carries its cursor.
3. Work on the rows.
4. Mark after work: POST /v1/feeds/recipe_demo/mark {"to": <last cursor>, "expected": <previous position>}. Marking is compare-and-set; repeating a mark is a no-op with the same response.

## Troubleshooting

- Never mark before the work is done: a crash between read and mark redelivers (at-least-once delivery, exactly-once marking).
- Rows newer than the settle horizon (api.consumer_horizon()) are not delivered yet; they arrive on a later read, never skipped.
- news is a page of arrivals in arrival order, not a population; use pending for counts.
- The /v1/consumers... routes are deprecated aliases of /v1/feeds...; the SQL names (api.consumer_horizon, the consumer column of api.pending) keep the old word.
