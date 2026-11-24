---
{
  "name": "disclose_by_label",
  "version": 1,
  "schema_version": "0052",
  "status": "tested",
  "summary": "Owner: share by label and with an expiring grant; labelled content stays hidden even inside a granted channel.",
  "needs": [
    "subject:disclosure"
  ],
  "statements": [
    "SELECT api.describe('disclosure') AS disclosure",
    "SELECT api.describe('access') AS access"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "access"
    ]
  }
}
---

# Disclose by label and grant expiry (owner)

Labels (intimate, medical, financial, work, third_party_confidence, and any the owner adds) sit on channels, threads and messages and narrow every grant: labelled content is denied by default even in a granted channel.

## The loop

1. Label: `throughline lib access label medical --channel C` (or `--thread ID`).
2. Grant with purpose and expiry: `throughline lib access grant ASKER --channel C --labels work --purpose "project review" --expires 2026-12-31`.
3. Check what the asker sees: `api.describe('access')` under that asker.
4. Extend or revoke: `lib access extend`, `lib access revoke`. Expiry advances the policy version, so saved sets and investigations of that asker return 409.

## Troubleshooting

- Purpose is recorded for audit, not enforced.
- Owner scope is unaffected by labels.
