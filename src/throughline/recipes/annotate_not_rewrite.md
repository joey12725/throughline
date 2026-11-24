---
{
  "name": "annotate_not_rewrite",
  "version": 2,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Correct time, place or noise with annotations; never rewrite a record.",
  "needs": [
    "function:effective",
    "route:POST /v1/annotations"
  ],
  "statements": [
    "SELECT e.message_id, e.ignored, e.effective_time, e.effective_place, e.annotation_ids FROM api.effective(ARRAY(SELECT m.id FROM api.messages m WHERE m.channel = 'synthetic.chat')) e ORDER BY e.message_id"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "message_id",
      "ignored",
      "effective_time"
    ]
  }
}
---

# Annotate, never rewrite

Ground truth is append-only. A wrong time, a place, a noisy segment or an ignorable record is corrected with an annotation: POST /v1/annotations {"message_id": id, "attribute": "ignore", "value": {"ignored": true, "reason": "..."}} (other attributes: effective_time, effective_place, location, media_noise, speaker; `api.describe('annotations')` gives each value shape). `api.effective(message_ids)` and GET /v1/effective show the folded result.

## Adapting

- Ignored records are excluded from recall by default; include_ignored => true brings them back.
- The owner un-ignores with {"ignored": false}, which overrides a source ignore_reason and later jobs.
- Agents and restricted askers get 202 with a proposal; the owner decides.

## Troubleshooting

- Annotations are keyed on channel and external_id, so they survive re-import.
- Under known_by, annotations made after the cutoff are not applied.
