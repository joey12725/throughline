---
{
  "name": "scored_above_threshold",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Messages with a field score above a threshold for chosen people (QRY-12).",
  "needs": [],
  "statements": [
    "SELECT m.id, m.happened_at, (m.fields->>'rating')::int AS rating, m.content FROM api.messages m WHERE m.channel = 'synthetic.chat' AND (m.fields->>'rating')::int >= 8 AND m.person_id IN (SELECT p.id FROM api.person('synthetic-friend') p) ORDER BY rating DESC"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "id",
      "rating"
    ]
  }
}
---

# Scored above a threshold for chosen people

Channel fields are typed per channel (`api.describe('channel:<name>')` lists them). Cast the JSON field and filter.

## Adapting

- Use the field names of your channel; several people: `api.person` for each, or `person_id = ANY(ARRAY[...])`.

## Troubleshooting

- A field absent on a message yields NULL and drops the row; that is not a zero score.
- Author versus involved: person_messages and person_role 'author' select what a person wrote; a conversation the person took part in also holds other people's messages. Use person_role 'involved' (recall) or read the thread when you mean participation.
