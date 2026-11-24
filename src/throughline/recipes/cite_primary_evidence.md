---
{
  "name": "cite_primary_evidence",
  "version": 2,
  "schema_version": "0074",
  "status": "tested",
  "summary": "Cite primary evidence: prefer source records over generated text.",
  "needs": [
    "function:recall"
  ],
  "statements": [
    "SELECT r.record_id, r.kind, r.generated, r.unsupported, r.support FROM api.recall(query => 'orchard', k => 10) r WHERE NOT r.generated ORDER BY r.ordinal"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "record_id",
      "generated",
      "support"
    ]
  }
}
---

# Cite primary evidence

Every record returned by recall, expand, match, result sets, plans and packets over HTTP carries evidence_class: primary (the source record itself), normalized (a mechanical rendering of a source), derived (computed from sources: pages, passages, links) and inference (model output). In SQL the same distinction is generated, unsupported and support.

## Adapting

- From a page passage, follow support (or api.sources) to the primary records and cite those.
- Packets (POST /v1/packets) copy evidence_class into every record.

## Troubleshooting

- Generated text is never its own evidence; unsupported = true means it has no sources at all.
- Throughline returns evidence; the conclusion is yours. Cite record ids.
