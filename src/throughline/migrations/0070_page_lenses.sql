-- Pages as lens output (docs/v11/pages.md). A lens with output 'pages' and the
-- 'librarian' worker reads its query/channels/grain like any lens, and the
-- Librarian answers each work item with page actions (write, edit, link,
-- unlink, archive) instead of table rows.
--
-- Pages are one namespace of slugs ('people/alex-rivera', 'events/...'); each
-- revision is a page_revision record on the native channel 'pages'
-- (external_id = slug). tl.pages is the registry (kind, title, aliases, the
-- current revision); tl.page_actions logs every action a lens took.
--
-- Replaces the fixed day/person page families of 0069 (their tables are
-- dropped; their pages are kept as records).

ALTER TABLE tl.table_defs ADD COLUMN output text NOT NULL DEFAULT 'table'
  CHECK (output IN ('table','pages'));
ALTER TABLE tl.table_defs DROP CONSTRAINT IF EXISTS table_defs_worker_check;
ALTER TABLE tl.table_defs ADD CONSTRAINT table_defs_worker_check
  CHECK (worker IN ('code','jev','llm','agent','librarian'));
ALTER TABLE tl.table_defs ADD CONSTRAINT table_defs_librarian_pages
  CHECK ((worker = 'librarian') = (output = 'pages'));

CREATE TABLE tl.pages(
 slug text PRIMARY KEY CHECK (slug ~ '^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*){0,3}$' AND length(slug) <= 160),
 kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_-]{0,39}$'),
 title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 300),
 aliases text[] NOT NULL DEFAULT '{}',
 summary text,
 person_id bigint REFERENCES tl.people(id) ON DELETE SET NULL,
 message_id bigint REFERENCES tl.messages(id) ON DELETE SET NULL,
 version int NOT NULL DEFAULT 0,
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
 created_by text NOT NULL,
 updated_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 stats jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(stats)='object'),
 sink_state jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(sink_state)='object'));
CREATE INDEX pages_kind ON tl.pages(kind, updated_at DESC);
CREATE INDEX pages_title ON tl.pages(lower(title));
CREATE INDEX pages_aliases ON tl.pages USING gin(aliases);
CREATE INDEX pages_person ON tl.pages(person_id) WHERE person_id IS NOT NULL;
CREATE INDEX pages_message ON tl.pages(message_id);

CREATE TABLE tl.page_actions(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 lens text NOT NULL,
 window_key text,
 op text NOT NULL CHECK (op IN ('write','edit','link','unlink','archive')),
 slug text,
 status text NOT NULL CHECK (status IN ('applied','unchanged','refused')),
 detail jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail)='object'),
 at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE INDEX page_actions_lens ON tl.page_actions(lens, id DESC);
CREATE INDEX page_actions_slug ON tl.page_actions(slug, id DESC);

REVOKE ALL ON tl.pages, tl.page_actions FROM PUBLIC, throughline_reader;
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.pages, tl.page_actions TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.page_actions_id_seq TO throughline_app;

DROP TABLE IF EXISTS tl.page_runs, tl.page_subjects, tl.page_families;
