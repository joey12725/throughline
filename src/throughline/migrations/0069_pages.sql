-- Pages: maintained, cited synthesis pages (daily pages, person profiles,
-- weekly rollups) kept up to date from evidence on a schedule.
-- docs/v11/pages.md. The page text itself is a page_revision record on the
-- native channel pages.<family> (versioned like any record, passages,
-- embeddings, recall); these tables hold the family definitions, per-subject
-- state (evidence cursor, freshness, link and citation stats) and run history.

CREATE TABLE tl.page_families(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 name text NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9_]{0,43}$'),
 spec jsonb NOT NULL CHECK (jsonb_typeof(spec)='object'),
 spec_hash text NOT NULL,
 source_path text,
 enabled boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp());

CREATE TABLE tl.page_subjects(
 family_id bigint NOT NULL REFERENCES tl.page_families(id) ON DELETE CASCADE,
 subject_key text NOT NULL CHECK (length(subject_key) BETWEEN 1 AND 200),
 subject_kind text NOT NULL CHECK (subject_kind IN ('day','week','month','person')),
 person_id bigint,
 period_start date,
 period_end date,
 title text,
 status text NOT NULL DEFAULT 'pending'
   CHECK (status IN ('pending','current','catching_up','failed','skipped')),
 page_message_id bigint REFERENCES tl.messages(id) ON DELETE SET NULL,
 revisions int NOT NULL DEFAULT 0,
 evidence_cursor bigint NOT NULL DEFAULT 0,
 evidence_seen bigint NOT NULL DEFAULT 0,
 pending_evidence int NOT NULL DEFAULT 0,
 spec_hash text,
 last_built_at timestamptz,
 last_checked_at timestamptz,
 last_error text,
 failures int NOT NULL DEFAULT 0,
 stats jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(stats)='object'),
 sink_state jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(sink_state)='object'),
 PRIMARY KEY (family_id, subject_key));
CREATE INDEX page_subjects_person ON tl.page_subjects(person_id) WHERE person_id IS NOT NULL;
CREATE INDEX page_subjects_status ON tl.page_subjects(family_id, status);

CREATE TABLE tl.page_runs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 family_id bigint NOT NULL REFERENCES tl.page_families(id) ON DELETE CASCADE,
 trigger_kind text NOT NULL CHECK (trigger_kind IN ('schedule','backlog','manual','rebuild')),
 status text NOT NULL DEFAULT 'running'
   CHECK (status IN ('running','done','backlog','budget_exhausted','provider_unavailable','failed','dry_run')),
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 finished_at timestamptz,
 considered int NOT NULL DEFAULT 0,
 built int NOT NULL DEFAULT 0,
 unchanged int NOT NULL DEFAULT 0,
 failed int NOT NULL DEFAULT 0,
 model_calls int NOT NULL DEFAULT 0,
 tokens bigint NOT NULL DEFAULT 0,
 cost double precision NOT NULL DEFAULT 0,
 detail jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail)='object'));
CREATE INDEX page_runs_family ON tl.page_runs(family_id, id DESC);

REVOKE ALL ON tl.page_families,tl.page_subjects,tl.page_runs FROM PUBLIC,throughline_reader;
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.page_families,tl.page_subjects,tl.page_runs TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.page_families_id_seq,tl.page_runs_id_seq TO throughline_app;
