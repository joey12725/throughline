---
{
  "name": "verify_backup_alarm",
  "version": 1,
  "schema_version": "0052",
  "status": "tested",
  "summary": "Owner: back up, verify the restore against snapshot values, and wire the stale-backup alarm.",
  "needs": [
    "subject:ops"
  ],
  "statements": [
    "SELECT api.describe('ops') AS ops"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "ops"
    ]
  }
}
---

# Verify a backup and wire the stale alarm (owner)

1. `throughline lib backup run` dumps the database, takes counts and content hashes inside the dump's own snapshot, encrypts with THROUGHLINE_BACKUP_ENCRYPT_CMD and copies off site with THROUGHLINE_BACKUP_OFFSITE_CMD (a plaintext off-site copy is refused).
2. `throughline lib backup verify-restore FILE` restores into a scratch database and compares counts and hashes with the stored snapshot values, plus probe queries.
3. `throughline lib backup check` exits non-zero when the last good backup is older than THROUGHLINE_BACKUP_STALE_HOURS (36); run it from cron or a monitor.

## Troubleshooting

- GET /v1/status shows backup age next to ingest age, stale plugins and coverage gaps.
- A verify that differs is a failed backup, not a warning.
