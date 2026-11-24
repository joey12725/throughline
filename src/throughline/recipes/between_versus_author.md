---
{
  "name": "between_versus_author",
  "version": 1,
  "schema_version": "0052",
  "status": "tested",
  "summary": "Between versus author-only versus involved: a dyad scope returns both sides of the threads where every listed person speaks.",
  "needs": [
    "function:between",
    "function:between_threads"
  ],
  "statements": [
    "SELECT b.message_id, b.party_id, b.is_me, b.direction, b.attribution_status, b.thread_id FROM api.between(ARRAY[(SELECT p.id FROM api.people p WHERE p.is_owner), (SELECT p.id FROM api.people p WHERE p.handle = 'synthetic-friend')]::bigint[], NULL, NULL, 50) b"
  ],
  "expect": {
    "min_rows": 2,
    "columns": [
      "message_id",
      "party_id",
      "is_me",
      "attribution_status"
    ]
  }
}
---

# Between versus author versus involved

Three different questions, three scopes:

- author: messages written by the person (recall person_role author). "What did Sam say".
- between: messages by ANY listed person in threads where EVERY listed person has a message: both sides of a dyad (`api.between(ARRAY[me, sam])`, recall person_role between). "What did Sam and I discuss". Never author-only.
- involved: messages in threads where the person has a message, plus records linked to the person (recall person_role involved). "Everything around Sam".

## Adapting

- The owner side is `is_me`: `party_id` is the owner's person id for owner messages even when the owner's account is not attached.
- `api.between_threads(ARRAY[a, b])` lists the shared threads first; `api.together` lists shared episodes.

## Troubleshooting

- Author versus involved: a count of author rows answers a different question than a dyad count; say which one you ran.
- Suspected accounts do not count for anyone until confirmed.
