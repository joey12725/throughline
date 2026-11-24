-- 0045 recipes: versioned, execution-tested procedural knowledge (INT-9,
-- LIB-3) and the agent surface section of the catalogue (INT-7, INT-8).
--
-- A recipe is a file src/throughline/recipes/<name>.md. throughline.recipes.sync
-- (called by db._migrate after the migrations and before tl.harden_reader())
-- stores every (name, version) once; a file that changed without a version
-- bump stops the migration. Stored versions are never rewritten, so the
-- history of a recipe stays readable.
--
-- api.recipes holds no personal data: recipe text is written by developers,
-- never derived from messages. It is the documented exception to the rule that
-- every api view is installed through tl.install_gate (same for every asker).
CREATE TABLE tl.recipes(
 name text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]*$'),
 version int NOT NULL CHECK (version >= 1),
 schema_version text NOT NULL CHECK (schema_version ~ '^[0-9]{4}$'),
 status text NOT NULL CHECK (status IN ('tested','proposed','retired')),
 summary text NOT NULL,
 body text NOT NULL,
 statements jsonb NOT NULL CHECK (jsonb_typeof(statements)='array' AND jsonb_array_length(statements) BETWEEN 1 AND 8),
 expect jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(expect)='object'),
 needs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(needs)='array'),
 content_hash text NOT NULL,
 synced_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(name, version));
-- Runtime roles read recipes only through api.recipes; only the owner (the
-- migration connection) writes them.
REVOKE ALL ON tl.recipes FROM PUBLIC,throughline_app,throughline_reader;

-- The current version of every recipe: the highest version that is not a
-- proposal, listed only while it is tested. A proposed version never appears
-- here until a tested file replaces it (and CI executed that file); a retired
-- latest version removes the name.
CREATE VIEW api.recipes AS
 SELECT r.name,r.version,r.schema_version,r.status,r.summary,r.needs,r.statements,r.expect,r.body
 FROM tl.recipes r
 WHERE r.status='tested'
  AND r.version=(SELECT max(x.version) FROM tl.recipes x WHERE x.name=r.name AND x.status<>'proposed');
GRANT SELECT ON api.recipes TO throughline_reader,throughline_app;

CREATE FUNCTION api.describe_recipes() RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT jsonb_build_object(
  'description','Versioned recipes: worked, execution-tested statement sequences with adaptation notes. Read one with api.describe(''recipe:<name>'') or SELECT * FROM api.recipes.',
  'how','Each recipe lists needs (functions, subjects or routes it requires), one or a few statements (a later one may name {{handle}}, the result set of the previous statement''s semantic call) and an expectation it met on the test corpus. Adapt the literals, keep the shape.',
  'recipes',(SELECT coalesce(jsonb_agg(jsonb_build_object('name',r.name,'version',r.version,'summary',r.summary,'needs',r.needs,'schema_version',r.schema_version) ORDER BY r.name),'[]'::jsonb) FROM api.recipes r))
$$;
COMMENT ON FUNCTION api.describe_recipes() IS 'Tested recipes: name, version, summary and needs; one recipe with recipe:<name>';
GRANT EXECUTE ON FUNCTION api.describe_recipes() TO throughline_reader,throughline_app;

-- Catalogue completeness (INT-8): every registry row documents every
-- parameter its operator accepts. api.expand gained role and within after its
-- row was written (0041).
UPDATE tl.api_functions SET signature=replace(signature,'keep_input)','keep_input, role, within)')
 WHERE name='expand' AND kind='semantic' AND signature LIKE '%keep_input)%' AND signature NOT LIKE '%within%';

-- The agent surface in one section (INT-7). The order is the order of use.
INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('surface',jsonb_build_object(
  'description','How an agent uses the archive: catalogue first, SQL as the primitive, context around hits, recall for discovery, the librarian only for large investigations.',
  'order',jsonb_build_array(
   jsonb_build_object('step','catalogue','call','api.describe() / GET /v1/describe','why','Learn channels, record kinds, functions with exactness and cost, sections and recipes before querying.'),
   jsonb_build_object('step','sql','call','POST /v1/sql (api views and allow-listed api functions)','why','The primitive. Exact SQL is complete and reproducible; read execution.completeness on every answer.'),
   jsonb_build_object('step','context','call','api.context(message_id, before, after) / api.context_many / POST /v1/expand','why','Read the surrounding thread or linked records of a hit before concluding.'),
   jsonb_build_object('step','recall','call','api.recall(query => ...) in SQL / POST /v1/recall','why','Discovery when the words are unknown: ranked, limited, saved as a result set handle that SQL can join.'),
   jsonb_build_object('step','librarian','optional',true,'call','api.librarian(...) / POST /v1/librarian','why','Only for large investigations; it plans calls of the steps above and never does anything an agent could not do directly.')),
  'execution','Every answer carries execution metadata: status, completeness (exhaustive, ranked sample, truncated), truncation, warnings and, where reported, coverage. A gap in coverage is not absence.',
  'evidence','Records carry evidence_class (primary, normalized, derived, inference), generated and unsupported. Cite primary evidence; generated text is never its own evidence.',
  'conclusions','Throughline returns evidence. The agent draws the conclusion and cites record ids.',
  'recipes','api.describe(''recipes'') lists tested recipes; api.describe(''recipe:<name>'') shows one.'))
ON CONFLICT (subject) DO NOTHING;
