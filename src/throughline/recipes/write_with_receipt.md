---
{
  "name": "write_with_receipt",
  "version": 2,
  "schema_version": "0052",
  "status": "tested",
  "summary": "Write with an Idempotency-Key and trust the receipt: replays are no-ops, a reused key with another body is 409, no read-back needed.",
  "needs": [
    "subject:write_contract"
  ],
  "statements": [
    "SELECT api.describe('write_contract') AS contract"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "contract"
    ]
  }
}
---

# Write with a key, trust the receipt

Every record write takes an `Idempotency-Key` header (8 to 200 characters, scoped to your token, kept 30 days). The response carries a receipt read back in the same transaction: kind, id, record_id, version, status, stored_hash, counts and replayed.

## The rules

- Retry with the same key and the same body: the stored response comes back with `receipt.replayed: true` and nothing else happens.
- The same key with a different body is HTTP 409. Make a new key for a new write.
- Do not read back what you wrote: the receipt is the read-back (stored_hash, version, counts).
- Compare-and-set: pass `expected_hash` (a receipt's stored_hash) to PATCH /v1/links/{id}, supersede, retract and merge; a changed row is 409 with the current hash. Ingest items take expected_version.
- Coverage reports, coverage attempts, feed create, drop and settings, identifier status and transcript writes follow the same contract. A feed mark is compare-and-set without a key and returns a receipt-shaped object (kind consumer_cursor).
- THROUGHLINE_REQUIRE_IDEMPOTENCY=true makes the key mandatory for agent tokens.

## Troubleshooting

- A replayed receipt holds ids, hashes and statuses, never record text.
- Batches (links, entities) answer per item; an authorization failure is a whole-batch 403.
