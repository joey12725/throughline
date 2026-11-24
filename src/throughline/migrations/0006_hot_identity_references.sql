-- Identity references are assigned only from registry lookups by core ingest.
-- Registry rows are never deleted through HTTP. Avoid per-message FK trigger
-- lookups on these cached references in the hot append-only table; the registry
-- itself retains foreign keys, and late resolution updates are set-oriented.
ALTER TABLE tl.messages DROP CONSTRAINT messages_author_identifier_id_fkey;
ALTER TABLE tl.messages DROP CONSTRAINT messages_person_id_fkey;
