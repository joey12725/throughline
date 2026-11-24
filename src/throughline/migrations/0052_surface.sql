-- 0052 surface: the agent surface once people, coverage, consumers and
-- commitments are in (INT-7, INT-8, INT-9,
-- behaviour priorities P1..P11). No retrieval or domain logic: catalogue rows and
-- sections only.
--
-- 1. Catalogue completeness (INT-8): checked, not patched (see below).
-- 2. The 'surface' section gains those packages' entry points (person brief as
--    the default first call for any person question, attribution and
--    evidence_class, coverage before absence, incidents by episode,
--    consumers, write receipts). Its 'order' is unchanged.
-- 3. The 'write_contract' section names the routes retrofitted here
--    (coverage, consumers, transcripts), the commitment writes and every
--    receipt kind the code issues (receipt_kinds), incl. consumer_cursor.
-- 4. A new 'ops' section: backup, verify-restore and the stale alarm.
--
-- tl objects are schema-qualified (no search_path is assumed).

-- 1. Registry completeness --------------------------------------------------------
-- Every people (0047) and coverage (0046) function already has its row, and
-- the commitment and disclosure packages register theirs in their own migrations (0050
-- commitments; 0051 adds no SQL function). Nothing is inserted here: a row
-- written for a function another package has not allow-listed would break
-- the registry-equals-allow-list rule instead of keeping it. The rule is
-- tested both ways (tests/test_v11_surface_behaviours.py).

-- 2. The agent surface -------------------------------------------------------
UPDATE tl.catalog_sections SET body=body||jsonb_build_object(
 'description','How an agent uses the archive: catalogue first, SQL as the primitive, context around hits, recall for discovery, the librarian only for large investigations. Questions about a person start with the person brief; writes carry an Idempotency-Key and trust the receipt.',
 'first_call',jsonb_build_object(
  'person','api.person_brief(person_id) / GET /v1/people/{id}/brief: the default first call for any question about a person (contact, exchanges, open loops, circle rank, coverage); then last_contact, open_loops, person_arc, between, around or circle.',
  'archive','api.describe() / GET /v1/describe'),
 'attribution','Every person row and record carries is_me, direction and attribution_status (confirmed, self_direction, annotated, suspected, unknown). Suspected accounts count for nobody until the owner confirms them.',
 'coverage','Read coverage (api.coverage_for, execution.coverage, the brief''s coverage) before saying something is absent: not found in covered sources is not the same as not found in coverage.',
 'incidents','Count incidents by episode (api.episodes_of, group_by episode), never hits.',
 'new_arrivals','Process only what is new with a named consumer: news, work, then mark (compare-and-set).',
 'writes','Send an Idempotency-Key with every record write; the receipt (stored_hash, version, counts) is the read-back. Same key and body replays, a different body is 409.',
 'techniques','Ranking, fusion, legs and planners are internal techniques of recall, not tools.',
 'recipes','api.describe(''recipes'') lists tested recipes (person_brief_first, last_contact_pitfall, open_loops_both_ways, write_with_receipt and more); api.describe(''recipe:<name>'') shows one.'),
 version=coalesce(version,1)+1
WHERE subject='surface';

-- 3. The write contract on every write route -----------------------------------------
UPDATE tl.catalog_sections SET body=jsonb_set(body,'{rules}',coalesce(body->'rules','[]'::jsonb)||jsonb_build_array(
 'Coverage reports and attempts (POST /v1/plugins/{name}/coverage, /attempt), consumer create, drop and settings (POST /v1/consumers, DELETE /v1/consumers/{name}, PUT /v1/consumers/settings), identifier status and the transcript writes take the same Idempotency-Key and answer with a receipt.',
 'POST /v1/consumers/{name}/mark is idempotent by compare-and-set without a key; its response carries a receipt-shaped object {kind consumer_cursor, id, stored_hash of the cursor, replayed}; replayed is always false (the answer is recomputed under compare-and-set, never replayed from a key store), and a repeated mark returns the identical answer and receipt.',
 'Commitment writes (POST /v1/commitments/extract, /{id}/assess, /audit, PATCH /v1/commitments/{id}/external-ref) take the same Idempotency-Key and answer with commitment receipts (a restricted assess answers 202 with a proposal receipt).',
 'Routes that only read (sql, recall, expand, match, plans, packets, librarian) or write the caller''s own working state (sessions, result sets, approvals) are not record writes.'))
 ||jsonb_build_object('receipt_kinds',jsonb_build_array('message','link','entity','claim','annotation','proposal','provenance','person','identifier','coverage_report','coverage_attempt','consumer','consumer_settings','consumer_cursor','noise_run','commitment','link_type')),
 version=coalesce(version,1)+1
WHERE subject='write_contract';

-- 4. Ops guarantees ---------------------------------------------------
INSERT INTO tl.catalog_sections(subject,body,version) VALUES
 ('ops',jsonb_build_object(
  'description','Backups the owner can trust without reading them: counts and content hashes taken inside the dump''s own snapshot, an encrypted off-site copy, a restore drill compared with those values, and a staleness check for an external alarm.',
  'commands',jsonb_build_object(
   'backup','throughline lib backup run: pg_dump, snapshot counts and hashes, THROUGHLINE_BACKUP_ENCRYPT_CMD, THROUGHLINE_BACKUP_OFFSITE_CMD (a plaintext off-site copy is refused)',
   'verify','throughline lib backup verify-restore FILE: restore into THROUGHLINE_SCRATCH_DATABASE_URL and compare with the stored snapshot values, plus probe queries',
   'alarm','throughline lib backup check: exit status non-zero when the last good backup is older than THROUGHLINE_BACKUP_STALE_HOURS (default 36)',
   'runs','throughline lib backup list: the recorded runs (counts and hashes only, never content)'),
  'status','GET /v1/status reports ingest age, stale plugins, coverage gaps and backup age.',
  'rules',jsonb_build_array(
   'A verify that differs from the snapshot values is a failed backup.',
   'Encryption and off-site copy are external commands the owner configures; no key material is stored by Throughline.')),
  1)
ON CONFLICT (subject) DO NOTHING;
