---
{
  "name": "recall_hard_scope",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Recall under a hard scope (channels and time) that is applied before ranking.",
  "needs": [
    "function:recall"
  ],
  "statements": [
    "SELECT r.record_id, r.channel, r.record_time FROM api.recall(query => 'orchard', channels => ARRAY['synthetic.chat'], after => '2019-12-31Z', before => '2020-02-01Z', k => 10) r ORDER BY r.record_time"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "record_id",
      "channel",
      "record_time"
    ]
  }
}
---

# Recall with hard scope

Scope arguments (channels, kinds, persons with person_role, after/before with time_basis, place, threads, as_of, known_by) are one predicate applied inside every leg before ranking, so the k results are the best k inside the scope.

## Adapting

- time_basis 'subject' filters on the period a record is about; 'either' matches both.
- Unresolvable scope (unknown place or person) returns an empty result with a reason, never an unscoped one.

## Troubleshooting

- A WHERE clause outside a limited search filters only the candidates that search returned: `SELECT * FROM api.search('x', 20) WHERE channel='c'` can return nothing while channel c has matches. Put filters into the function's own arguments (search_filtered, recall's channels/after/before) or pass a NULL limit.
- record_time (happened_at) is when the record happened; for a page revision it is the revision time. subject_from/subject_to is the period the record is about. Choose time_basis record | subject | either in recall.
