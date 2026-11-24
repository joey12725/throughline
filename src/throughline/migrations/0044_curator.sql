-- The selective curator (BRN-6, BRN-12, BRN-13, BRN-15, BRN-16, BRN-17,
-- MSG-10, CST-7, ARC-6).
--
-- The curator is a bounded maintenance command (`throughline lib curator
-- run`). It is never in a query path, it writes derived data only (links with
-- evidence, concept and place links, passage provenance, claims, proposals)
-- and it never changes ground truth. These two tables hold its cursor and one
-- row per run; they hold ids and counts only, never message text.
--
-- Forward-only. Nothing here depends on a migration of the same wave.

CREATE TABLE tl.curator_state(
 key text PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 100),
 cursor jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(cursor)='object'),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp());

CREATE TABLE tl.curator_runs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 finished_at timestamptz,
 policy jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(policy)='object'),
 considered int NOT NULL DEFAULT 0 CHECK (considered>=0),
 judged int NOT NULL DEFAULT 0 CHECK (judged>=0),
 model_calls int NOT NULL DEFAULT 0 CHECK (model_calls>=0),
 cost numeric NOT NULL DEFAULT 0 CHECK (cost>=0),
 links_written int NOT NULL DEFAULT 0 CHECK (links_written>=0),
 claims_written int NOT NULL DEFAULT 0 CHECK (claims_written>=0),
 concepts_written int NOT NULL DEFAULT 0 CHECK (concepts_written>=0),
 provenance_written int NOT NULL DEFAULT 0 CHECK (provenance_written>=0),
 proposals_written int NOT NULL DEFAULT 0 CHECK (proposals_written>=0),
 corroborated int NOT NULL DEFAULT 0 CHECK (corroborated>=0),
 status text NOT NULL DEFAULT 'running' CHECK (status IN
  ('running','done','max_items','budget_exhausted','approval_required','provider_unavailable','failed')),
 error text CHECK (error IS NULL OR length(error)<=500));
CREATE INDEX curator_runs_started ON tl.curator_runs(started_at DESC,id DESC);

-- The curator runs on the owner connection; the runtime roles never touch
-- its state.
REVOKE ALL ON tl.curator_state,tl.curator_runs FROM PUBLIC,throughline_app,throughline_reader;

-- Purge (ARC-5): a concept entity the curator made rests only on its
-- mentions; when every one of them is in the purged channel, its name is text
-- derived from purged messages, so the entity, its provenance and its links
-- go too. Runs before the provenance hook (41) while the sources can still be
-- joined.
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('curator_concepts',35,
  'WITH doomed AS MATERIALIZED (SELECT e.id FROM tl.entities e WHERE e.external_ref LIKE ''curator:%''
    AND EXISTS(SELECT 1 FROM tl.provenance p JOIN tl.messages m ON m.id=p.source_message_id WHERE p.derived_kind=''entity'' AND p.derived_id=e.id AND m.channel_id=$1)
    AND NOT EXISTS(SELECT 1 FROM tl.provenance p JOIN tl.messages m ON m.id=p.source_message_id WHERE p.derived_kind=''entity'' AND p.derived_id=e.id AND m.channel_id<>$1)),
   prov AS (DELETE FROM tl.provenance p WHERE p.derived_kind=''entity'' AND p.derived_id IN (SELECT d.id FROM doomed d)),
   ends AS (DELETE FROM tl.links l WHERE (l.to_kind=''entity'' AND l.to_id IN (SELECT d.id FROM doomed d)) OR (l.from_kind=''entity'' AND l.from_id IN (SELECT d.id FROM doomed d)))
   DELETE FROM tl.entities e WHERE e.id IN (SELECT d.id FROM doomed d)')
ON CONFLICT (name) DO NOTHING;

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('curator',jsonb_build_object(
  'description','The selective curator: an explicitly started, bounded maintenance run (throughline lib curator run) that writes evidence-backed links, concept and place links, passage-level provenance and claims, or proposals of them. It is never in a query path: recall, expand, match and SQL make no model call for it and read what it wrote like any other derived data.',
  'policy','A TOML file named by THROUGHLINE_CURATOR_POLICY. Absent or empty: nothing is done and no model is called. Fields: channels, since, tasks (links, concepts, places, claims, passage_sources), triggers (min_thread_messages, min_participants, terms, entity_kinds), unattended (false: every write becomes a proposal), max_items_per_run, max_model_calls_per_run, max_cost_per_run, job_version, link_types.',
  'rules',jsonb_build_array(
   'Only triggered conversations are judged; ignored records (overlay) are never candidates.',
   'Antecedents are narrowed without a model (shared rare terms, shared linked entities or people, alias matches, the same place within a window, vector neighbours as one more signal) and only the top ones are judged.',
   'Every policy link type must be an active registered type (GET /v1/link-types); an unregistered one stops the policy from loading. New types are proposed (kind link_type).',
   'Links carry method model (non-local model) or rule, job curator, job_version, confidence and evidence {message_ids, signals, rationale}. Owner links are never changed; an earlier curator link for the same pair is superseded, not overwritten.',
   'Concepts and places are entities with about links carrying evidence; coordinates are never written to a record; ambiguous names become proposals.',
   'Passage-level sources are a subset of the page''s own sources, never a source the page does not have.',
   'Claims are typed and source-backed (claims.create); an opinion is an attributed judgment; a statement without a source is stored unsupported or skipped; the same claim corroborates instead of duplicating.',
   'One transaction per item, cursor advanced last; the run stops at its item, call and cost budgets; non-local spend passes the cost gate; spend is recorded with ledger origin curator.'),
  'tables',jsonb_build_object(
   'tl.curator_state','The cursor (owner only).',
   'tl.curator_runs','One row per run: policy, considered, judged, model_calls, cost, links_written, claims_written, concepts_written, provenance_written, proposals_written, corroborated, status (owner only).'),
  'recall',jsonb_build_object(
   'claims','The claims leg: keyword match on claim statements (api.claim_records; as_of through api.claims_asof); a claim is eligible only when one of its source messages passes the full hard scope (time_basis subject uses the claim''s validity interval).',
   'page_sources','Stage order 5: the source messages of passage and claim hits become hits after api.scope_filter with the full hard scope (discovery leg page_sources). Off with hints.page_sources=false or raw_only; raw legs never depend on it.'),
  'commands',jsonb_build_array('throughline lib curator run [--once] [--dry-run]','throughline lib curator status')))
ON CONFLICT (subject) DO NOTHING;
