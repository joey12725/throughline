---
{
  "name": "incidents_by_episode",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Count incidents, not hits: group hits into episodes.",
  "needs": [
    "function:episodes_of"
  ],
  "statements": [
    "SELECT count(DISTINCT e.episode_id) AS incidents, count(*) AS hits FROM api.episodes_of(ARRAY(SELECT s.id FROM api.search_filtered('orchard', NULL) s)) e"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "incidents",
      "hits"
    ]
  }
}
---

# Count incidents, not hits

One incident (a conversation, a recording, a visit) usually spans several records. `api.episodes_of(message_ids)` assigns each message a stable episode id ('ep:...') from gap-based sessions in its thread; count DISTINCT episode_id.

## Adapting

- Recall with group_by 'episode' returns a manifest of incidents versus records.
- POST /v1/sets/transform with op 'group' and by 'episode' groups a saved set.

## Troubleshooting

- execution.warnings 'hit_count_not_incident_count' flags a count at record grain.
- A late message that bridges two sessions merges them; the episode says so.
- An aggregate over ranked results is not a count. count(*) over api.search(..., 20) or over a recall handle counts a ranked sample; execution.warnings says so. Count with a NULL limit (exhaustive) or over plain views.
