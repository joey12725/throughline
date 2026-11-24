-- 0062 lenses (standing questions that keep a typed, cited table), jobs and arrival rules (M4: TBL-1..5, JOB-1..7, RUL-1..3,
-- ACL-4, ACL-6, QRY-1, QRY-9, QRY-11, ARC-5, ARC-6).
--
-- Forward-only. All tl objects are schema-qualified.

-- 1. Core tables ------------------------------------------------------------------
CREATE TABLE tl.table_defs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 name text UNIQUE NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,43}$'),
 description text NOT NULL DEFAULT '',
 columns jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(columns)='array'),
 prompt text NOT NULL DEFAULT '',
 context_query text,
 channels text[] NOT NULL DEFAULT '{}',
 grain text NOT NULL DEFAULT 'message' CHECK (grain IN ('message','conversation','thread','day')),
 worker text NOT NULL DEFAULT 'llm' CHECK (worker IN ('code','jev','llm','agent')),
 worker_config jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(worker_config)='object'),
 schedule text,
 depends_on text[] NOT NULL DEFAULT '{}',
 natural_key text[] NOT NULL DEFAULT '{}',
 version int NOT NULL DEFAULT 1 CHECK (version >= 1),
 enabled boolean NOT NULL DEFAULT true,
 lazy_backfill boolean NOT NULL DEFAULT false,
 max_items_per_run int NOT NULL DEFAULT 200 CHECK (max_items_per_run >= 1),
 max_cost_per_run double precision NOT NULL DEFAULT 0.50 CHECK (max_cost_per_run >= 0),
 created_by_token_id bigint REFERENCES tl.tokens ON DELETE SET NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp());

CREATE TABLE tl.table_rows(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 table_id bigint NOT NULL REFERENCES tl.table_defs(id) ON DELETE CASCADE,
 natural_key text NOT NULL CHECK (length(natural_key) > 0),
 window_key text,
 data jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(data)='object'),
 version int NOT NULL DEFAULT 1 CHECK (version >= 1),
 superseded_by bigint REFERENCES tl.table_rows(id) ON DELETE SET NULL,
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','retracted')),
 confidence real CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
 method text NOT NULL CHECK (method IN ('code','rule','jev','model','owner')),
 evidence jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence)='object'),
 no_source_reason text,
 job text NOT NULL,
 job_version text NOT NULL,
 owner_locked boolean NOT NULL DEFAULT false,
 owner_correction_note text,
 source_message_ids bigint[] NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK (coalesce(array_length(source_message_ids,1),0) > 0 OR (no_source_reason IS NOT NULL AND length(btrim(no_source_reason)) > 0)));
CREATE UNIQUE INDEX table_rows_active_key ON tl.table_rows(table_id,natural_key) WHERE superseded_by IS NULL AND status='active';
CREATE INDEX table_rows_table_window ON tl.table_rows(table_id,window_key) WHERE superseded_by IS NULL;
CREATE INDEX table_rows_table_status ON tl.table_rows(table_id,status,id DESC);
CREATE INDEX table_rows_sources_gin ON tl.table_rows USING gin(source_message_ids);

CREATE TABLE tl.table_cursors(
 table_id bigint PRIMARY KEY REFERENCES tl.table_defs(id) ON DELETE CASCADE,
 last_message_id bigint NOT NULL DEFAULT 0,
 last_ingested_at timestamptz,
 done_ids bigint[] NOT NULL DEFAULT '{}',
 processed_windows jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(processed_windows)='object'),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp());

CREATE TABLE tl.table_runs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 table_id bigint NOT NULL REFERENCES tl.table_defs(id) ON DELETE CASCADE,
 trigger_kind text NOT NULL CHECK (trigger_kind IN ('manual','schedule','rule','lazy','backfill','batch')),
 status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','max_items','budget_exhausted','approval_required','provider_unavailable','failed')),
 considered int NOT NULL DEFAULT 0,
 processed int NOT NULL DEFAULT 0,
 written int NOT NULL DEFAULT 0,
 updated int NOT NULL DEFAULT 0,
 skipped_locked int NOT NULL DEFAULT 0,
 invalid int NOT NULL DEFAULT 0,
 model_calls int NOT NULL DEFAULT 0,
 tokens int NOT NULL DEFAULT 0,
 cost double precision NOT NULL DEFAULT 0,
 detail jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail)='object'),
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 finished_at timestamptz);
CREATE INDEX table_runs_table ON tl.table_runs(table_id,id DESC);

CREATE TABLE tl.rules(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 name text UNIQUE NOT NULL CHECK (name ~ '^[a-z][a-z0-9_-]{0,62}$'),
 description text NOT NULL DEFAULT '',
 channels text[] NOT NULL DEFAULT '{}',
 filter jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(filter)='object'),
 worker text NOT NULL DEFAULT 'code' CHECK (worker IN ('code','jev','llm')),
 worker_config jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(worker_config)='object'),
 window_seconds int CHECK (window_seconds IS NULL OR window_seconds > 0),
 min_count int NOT NULL DEFAULT 1 CHECK (min_count >= 1),
 action_kind text NOT NULL CHECK (action_kind IN ('notify','run_table')),
 action_config jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(action_config)='object'),
 enabled boolean NOT NULL DEFAULT true,
 last_message_id bigint NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp());

CREATE TABLE tl.rule_deliveries(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 rule_id bigint NOT NULL REFERENCES tl.rules(id) ON DELETE CASCADE,
 message_id bigint NOT NULL REFERENCES tl.messages(id) ON DELETE CASCADE,
 dedup_key text UNIQUE NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','failed')),
 attempts int NOT NULL DEFAULT 0,
 last_error text,
 payload jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload)='object'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 delivered_at timestamptz);
CREATE INDEX rule_deliveries_status ON tl.rule_deliveries(status,id);

REVOKE ALL ON tl.table_defs,tl.table_rows,tl.table_cursors,tl.table_runs,tl.rules,tl.rule_deliveries FROM PUBLIC,throughline_reader;
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.table_defs,tl.table_rows,tl.table_cursors,tl.table_runs,tl.rules,tl.rule_deliveries TO throughline_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA tl TO throughline_app;

-- 2. Provenance & visibility (TBL-3, ACL-4, ACL-9) -------------------------------
DO $$ DECLARE con text; BEGIN
 FOR con IN
  SELECT c.conname FROM pg_constraint c
  WHERE c.conrelid='tl.provenance'::regclass AND c.contype='c'
    AND pg_get_constraintdef(c.oid) LIKE '%derived_kind%'
 LOOP
  EXECUTE format('ALTER TABLE tl.provenance DROP CONSTRAINT %I',con);
 END LOOP;
END $$;
ALTER TABLE tl.provenance ADD CONSTRAINT provenance_derived_kind
 CHECK (derived_kind IN ('message','passage','claim','link','entity','annotation','table_row'));

-- Gated views over table metadata, unified rows and rules.
SELECT tl.install_gate('table_defs','false',
 'SELECT id,name,description,columns,prompt,context_query,channels,grain,worker,worker_config,schedule,depends_on,natural_key,version,enabled,lazy_backfill,max_items_per_run,max_cost_per_run,created_at,updated_at FROM tl.table_defs');
SELECT tl.install_gate('table_rows',
 'api.sources_visible(v.source_message_ids)',
 $def$
SELECT r.id,d.name AS table_name,r.table_id,r.natural_key,r.window_key,r.data,
 r.version,r.superseded_by,r.status,r.confidence,r.method,r.evidence,
 r.no_source_reason,r.job,r.job_version,r.owner_locked,r.owner_correction_note,
 r.source_message_ids,true AS generated,
 (coalesce(array_length(r.source_message_ids,1),0)=0) AS unsupported,
 r.created_at,r.updated_at
FROM tl.table_rows r
JOIN tl.table_defs d ON d.id=r.table_id
WHERE r.superseded_by IS NULL AND r.status='active'
$def$);
SELECT tl.install_gate('rules','false',
 'SELECT id,name,description,channels,filter,worker,worker_config,window_seconds,min_count,action_kind,action_config,enabled,last_message_id,created_at,updated_at FROM tl.rules');

CREATE OR REPLACE FUNCTION api.derived_visible(kind text,id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 RETURN CASE derived_visible.kind
  WHEN 'message' THEN api.message_visible(derived_visible.id)
  WHEN 'link' THEN EXISTS(SELECT 1 FROM tl.links l WHERE l.id=derived_visible.id AND api.link_image_visible(to_jsonb(l)))
  WHEN 'entity' THEN false
  WHEN 'passage' THEN EXISTS(SELECT 1 FROM api.passages p WHERE p.passage_id=derived_visible.id)
  WHEN 'claim' THEN EXISTS(SELECT 1 FROM api.claims_history c WHERE c.id=derived_visible.id)
  WHEN 'annotation' THEN EXISTS(SELECT 1 FROM api.annotations a WHERE a.id=derived_visible.id)
  WHEN 'table_row' THEN EXISTS(SELECT 1 FROM tl.table_rows r WHERE r.id=derived_visible.id AND api.sources_visible(r.source_message_ids))
  ELSE true END;
END $$;

-- 3. Auto-generated typed, gated SQL views per lens -------------------------------
CREATE FUNCTION tl.refresh_lens(lens_name text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl,api,public AS $$
DECLARE
 d tl.table_defs;
 col jsonb;
 cols text := '';
 typ text;
 vname text;
 vhist text;
 base_sel text;
 cur_def text;
 hist_def text;
BEGIN
 SELECT * INTO STRICT d FROM tl.table_defs WHERE name=lens_name;
 vname := 'lens_'||d.name;
 vhist := 'lens_'||d.name||'_versions';
 IF octet_length(vhist) > 57 THEN
  RAISE EXCEPTION 'lens view name exceeds 57 bytes: %',vhist;
 END IF;
 FOR col IN SELECT * FROM jsonb_array_elements(d.columns) LOOP
  typ := CASE col->>'type'
   WHEN 'text' THEN 'text'
   WHEN 'integer' THEN 'bigint'
   WHEN 'number' THEN 'double precision'
   WHEN 'boolean' THEN 'boolean'
   WHEN 'timestamp' THEN 'timestamptz'
   WHEN 'json' THEN 'jsonb'
   ELSE NULL END;
  IF typ IS NULL THEN RAISE EXCEPTION 'invalid column type: %',col->>'type'; END IF;
  cols := cols||format(', (r.data%s%L)::%s AS %I',
   CASE WHEN typ='jsonb' THEN '->' ELSE '->>' END,
   col->>'name',typ,col->>'name');
 END LOOP;
 -- Drop prior views so schema changes (reordering/changing column types) succeed cleanly.
 EXECUTE format('DROP VIEW IF EXISTS api.%I',vname);
 EXECUTE format('DROP VIEW IF EXISTS tl.%I',vname||'_gated');
 EXECUTE format('DROP VIEW IF EXISTS tl.%I',vname||'_all');
 EXECUTE format('DROP VIEW IF EXISTS api.%I',vhist);
 EXECUTE format('DROP VIEW IF EXISTS tl.%I',vhist||'_gated');
 EXECUTE format('DROP VIEW IF EXISTS tl.%I',vhist||'_all');
 DELETE FROM tl.gates WHERE view_name IN (vname,vhist);

 base_sel := format(
  'SELECT r.id, %L::text AS table_name, r.natural_key, r.window_key%s, '
  ||'r.version, r.superseded_by, r.status AS row_status, r.confidence, r.method, r.evidence, '
  ||'r.no_source_reason, r.job, r.job_version, r.owner_locked, r.owner_correction_note, '
  ||'r.source_message_ids, true AS generated, '
  ||'(coalesce(array_length(r.source_message_ids,1),0)=0) AS unsupported, '
  ||'r.data, r.created_at, r.updated_at '
  ||'FROM tl.table_rows r WHERE r.table_id=%s',
  d.name, cols, d.id);
 cur_def := base_sel||' AND r.superseded_by IS NULL AND r.status=''active''';
 hist_def := base_sel;

 PERFORM tl.install_gate(vname,'api.sources_visible(v.source_message_ids)',cur_def);
 PERFORM tl.install_gate(vhist,'api.sources_visible(v.source_message_ids)',hist_def);
END $$;
REVOKE EXECUTE ON FUNCTION tl.refresh_lens(text) FROM PUBLIC,throughline_reader;
GRANT EXECUTE ON FUNCTION tl.refresh_lens(text) TO throughline_app;

CREATE FUNCTION tl.drop_lens_views(lens_name text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl,api,public AS $$
DECLARE vname text := 'lens_'||lens_name; vhist text := 'lens_'||lens_name||'_versions';
BEGIN
 IF lens_name IS NULL OR lens_name !~ '^[a-z][a-z0-9_]{0,43}$' THEN
  RAISE EXCEPTION 'invalid lens name: %',lens_name;
 END IF;
 EXECUTE format('DROP VIEW IF EXISTS api.%I',vname);
 EXECUTE format('DROP VIEW IF EXISTS tl.%I',vname||'_gated');
 EXECUTE format('DROP VIEW IF EXISTS tl.%I',vname||'_all');
 EXECUTE format('DROP VIEW IF EXISTS api.%I',vhist);
 EXECUTE format('DROP VIEW IF EXISTS tl.%I',vhist||'_gated');
 EXECUTE format('DROP VIEW IF EXISTS tl.%I',vhist||'_all');
 DELETE FROM tl.gates WHERE view_name IN (vname,vhist);
END $$;
REVOKE EXECUTE ON FUNCTION tl.drop_lens_views(text) FROM PUBLIC,throughline_reader;
GRANT EXECUTE ON FUNCTION tl.drop_lens_views(text) TO throughline_app;

-- Deleting a lens definition also removes its rows' provenance. tl.provenance
-- is append-only for throughline_app (0036), so the delete runs as definer.
CREATE FUNCTION tl.delete_lens(lens_name text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl,api,public AS $$
DECLARE tid bigint;
BEGIN
 IF lens_name IS NULL OR lens_name !~ '^[a-z][a-z0-9_]{0,43}$' THEN
  RAISE EXCEPTION 'invalid lens name: %',lens_name;
 END IF;
 SELECT id INTO tid FROM tl.table_defs WHERE name=lens_name FOR UPDATE;
 IF tid IS NULL THEN
  RETURN false;
 END IF;
 DELETE FROM tl.provenance WHERE derived_kind='table_row'
  AND derived_id IN (SELECT id FROM tl.table_rows WHERE table_id=tid);
 PERFORM tl.drop_lens_views(lens_name);
 DELETE FROM tl.table_defs WHERE id=tid;
 RETURN true;
END $$;
REVOKE EXECUTE ON FUNCTION tl.delete_lens(text) FROM PUBLIC,throughline_reader;
GRANT EXECUTE ON FUNCTION tl.delete_lens(text) TO throughline_app;

-- 4. Write contract stored_hash extension ----------------------------------------
CREATE OR REPLACE FUNCTION tl.stored_hash(kind text, id bigint) RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE
 doc jsonb;
 dk text;
BEGIN
 IF stored_hash.kind='message' THEN
  SELECT jsonb_build_array('message',c.name,m.external_id,m.version,m.payload_hash,
          encode(sha256(convert_to(m.content,'UTF8')),'hex'))
    INTO doc FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE m.id=stored_hash.id;
 ELSIF stored_hash.kind='link' THEN
  SELECT jsonb_build_array('link',l.type,l.from_kind,l.from_id,l.to_kind,l.to_id,l.method,l.evidence,
          l.confidence,l.valid_from,l.valid_to,l.valid_from_precision,l.valid_to_precision,l.status,l.superseded_by)
    INTO doc FROM tl.links l WHERE l.id=stored_hash.id;
 ELSIF stored_hash.kind='entity' THEN
  SELECT jsonb_build_array('entity',e.kind,e.name,e.external_ref,e.metadata,e.is_placeholder,e.merged_into)
    INTO doc FROM tl.entities e WHERE e.id=stored_hash.id;
 ELSIF stored_hash.kind='claim' THEN
  SELECT jsonb_build_array('claim',c.statement,c.claim_type,c.attributed_person_id,c.attributed_handle,
          (SELECT coalesce(jsonb_agg(jsonb_build_array(s.message_id,s.version,s.relation) ORDER BY s.message_id,s.version,s.relation),'[]'::jsonb)
             FROM tl.claim_sources s WHERE s.claim_id=c.id),
          c.status,c.superseded_by)
    INTO doc FROM tl.claims c WHERE c.id=stored_hash.id;
 ELSIF stored_hash.kind='annotation' THEN
  SELECT jsonb_build_array('annotation',a.target_channel_id,a.target_external_id,a.target_version,a.attribute,
          a.value,a.method,a.valid_from,a.valid_to,a.status,a.superseded_by)
    INTO doc FROM tl.annotations a WHERE a.id=stored_hash.id;
 ELSIF stored_hash.kind='proposal' THEN
  SELECT jsonb_build_array('proposal',p.kind,encode(sha256(convert_to(p.payload::text,'UTF8')),'hex'),p.status)
    INTO doc FROM tl.proposals p WHERE p.id=stored_hash.id;
 ELSIF stored_hash.kind='person' THEN
  SELECT jsonb_build_array('person',p.handle,p.display_name,p.is_owner,p.status,p.kind,p.merged_into,p.asserted_by)
    INTO doc FROM tl.people p WHERE p.id=stored_hash.id;
 ELSIF stored_hash.kind='identifier' THEN
  SELECT jsonb_build_array('identifier',i.system,i.kind,i.value_norm,i.person_id,i.status,i.asserted_by)
    INTO doc FROM tl.identifiers i WHERE i.id=stored_hash.id;
 ELSIF stored_hash.kind='table_def' THEN
  SELECT jsonb_build_array('table_def',d.name,d.version,d.columns,d.channels,d.grain,d.worker,d.schedule,d.depends_on,d.natural_key,d.enabled,
          encode(sha256(convert_to(coalesce(d.prompt,'')||'|'||coalesce(d.context_query,''),'UTF8')),'hex'))
    INTO doc FROM tl.table_defs d WHERE d.id=stored_hash.id;
 ELSIF stored_hash.kind='table_row' THEN
  SELECT jsonb_build_array('table_row',r.table_id,r.natural_key,r.version,r.status,r.superseded_by,r.owner_locked,r.job,r.job_version,
          encode(sha256(convert_to(r.data::text,'UTF8')),'hex'),r.source_message_ids)
    INTO doc FROM tl.table_rows r WHERE r.id=stored_hash.id;
 ELSIF stored_hash.kind='rule' THEN
  SELECT jsonb_build_array('rule',r.name,r.channels,r.worker,r.action_kind,r.enabled,r.window_seconds,r.min_count)
    INTO doc FROM tl.rules r WHERE r.id=stored_hash.id;
 ELSIF stored_hash.kind='provenance' OR stored_hash.kind LIKE 'provenance:%' THEN
  dk := coalesce(nullif(split_part(stored_hash.kind,':',2),''),'message');
  SELECT jsonb_build_array('provenance',dk,stored_hash.id,
          jsonb_agg(jsonb_build_array(p.source_message_id,p.source_version,p.relation,p.origin)
            ORDER BY p.source_message_id,p.source_version,p.relation,p.origin))
    INTO doc FROM tl.provenance p WHERE p.derived_kind=dk AND p.derived_id=stored_hash.id
    HAVING count(*)>0;
 ELSE
  RAISE EXCEPTION 'stored_hash: unknown kind %',stored_hash.kind USING ERRCODE='invalid_parameter_value';
 END IF;
 IF doc IS NULL THEN
  RETURN NULL;
 END IF;
 RETURN encode(sha256(convert_to(doc::text,'UTF8')),'hex');
END $$;
REVOKE EXECUTE ON FUNCTION tl.stored_hash(text,bigint) FROM PUBLIC,throughline_reader;
GRANT EXECUTE ON FUNCTION tl.stored_hash(text,bigint) TO throughline_app;

-- 5. Purge hooks (ARC-5) ---------------------------------------------------------
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('table_rows',34,
  'WITH doomed AS MATERIALIZED (
     SELECT r.id FROM tl.table_rows r
     WHERE EXISTS(SELECT 1 FROM tl.messages m WHERE m.id=ANY(r.source_message_ids) AND m.channel_id=$1)),
   prov AS (
     DELETE FROM tl.provenance p WHERE p.derived_kind=''table_row'' AND p.derived_id IN (SELECT id FROM doomed))
   DELETE FROM tl.table_rows r WHERE r.id IN (SELECT id FROM doomed)');

-- 6. Catalogue section (INT-8, QRY-9) --------------------------------------------
INSERT INTO tl.catalog_sections(subject,body,version) VALUES
 ('lenses',jsonb_build_object(
  'description','Lenses: standing questions over your records that keep a typed, cited table up to date, plus jobs and arrival rules (M4: TBL-1..5, JOB-1..7, RUL-1..3). Each lens is defined by a typed schema, prompt, optional context query, and schedule or arrival rule, and exposes its table as a gated typed read-only view api.lens_<name>.',
  'views',jsonb_build_object(
   'api.table_defs','Defined lenses (unrestricted only)',
   'api.table_rows','Current active rows across all lenses (gated by source message visibility)',
   'api.lens_<name>','Auto-generated typed view of lens <name>''s table, gated by source message visibility (api.sources(''table_row'', id) jumps to source messages)',
   'api.lens_<name>_versions','Full row version history for lens <name>',
   'api.rules','Defined arrival rules (unrestricted only)'),
  'workers',jsonb_build_array('code','jev','llm','agent'),
  'grains',jsonb_build_array('message','conversation','thread','day'),
  'routes','POST /v1/lenses; GET /v1/lenses; GET /v1/lenses/{name}; DELETE /v1/lenses/{name}; POST /v1/lenses/{name}/quote; POST /v1/lenses/{name}/run; POST /v1/lenses/run-batch; GET /v1/lenses/{name}/rows; PATCH /v1/lenses/{name}/rows/{row_id}; POST /v1/rules; GET /v1/rules; POST /v1/rules/evaluate',
  'cli','throughline lib lens define|list|show|delete|quote|run|run-batch|rows|edit-row; throughline lib rules define|list|evaluate'),
  1)
ON CONFLICT (subject) DO UPDATE SET body=EXCLUDED.body,version=tl.catalog_sections.version+1;

UPDATE tl.catalog_sections
SET body = jsonb_set(
  body,
  '{receipt_kinds}',
  coalesce(body->'receipt_kinds', '[]'::jsonb) || jsonb_build_array('table_def', 'table_row', 'rule')
),
version = coalesce(version, 1) + 1
WHERE subject = 'write_contract';

-- 7. Feature names in the catalogue ---------------------------------------------
-- overlay -> annotations, consumers -> feeds, curator -> the librarian's curate
-- mode. The new subjects carry the content; each old subject stays as a
-- deprecated alias (the same content plus renamed_to) so callers that still
-- ask api.describe('overlay'|'consumers'|'curator') get an answer. Storage
-- and SQL names (tl.consumers, api.consumer_horizon, tl.overlay_state,
-- tl.curator_runs, job 'curator', receipt kinds consumer*) are unchanged.
INSERT INTO tl.catalog_sections(subject,body,version)
SELECT 'annotations',
 replace(replace(body::text,
  'Records whose effective overlay is ignored','Records whose effective annotation state is ignored'),
  'Annotations overlay messages without changing them: effective time, effective place, location, ignore, media noise and speaker. Overlays never rewrite ground truth.',
  'Annotations state something about messages without changing them: effective time, effective place, location, ignore, media noise and speaker. Annotations never rewrite ground truth.')::jsonb,
 1
FROM tl.catalog_sections WHERE subject='overlay'
ON CONFLICT (subject) DO UPDATE SET body=EXCLUDED.body,version=tl.catalog_sections.version+1;

INSERT INTO tl.catalog_sections(subject,body,version)
SELECT 'feeds',
 replace(body::text,'consumer state','feed state')::jsonb || jsonb_build_object(
  'description','Feeds: named cursors over arrivals for your token and asker. api.news(name, max_results) returns a page of arrivals in arrival order; api.pending(name) counts what is left; POST /v1/feeds/{name}/mark moves the cursor. Routes POST|GET /v1/feeds, GET /v1/feeds/{name}/news|pending, POST /v1/feeds/{name}/mark, DELETE /v1/feeds/{name}, GET|PUT /v1/feeds/settings; CLI throughline lib feed create|list|news|pending|mark|drop|settings.',
  'former_name','Feeds were called consumers: the /v1/consumers... routes and throughline lib consumer are deprecated aliases with identical behaviour; responses carrying consumer also carry feed; SQL and storage keep the old word (api.consumer_horizon, the consumer column of api.pending, receipt kinds consumer, consumer_cursor, consumer_settings).'),
 1
FROM tl.catalog_sections WHERE subject='consumers'
ON CONFLICT (subject) DO UPDATE SET body=EXCLUDED.body,version=tl.catalog_sections.version+1;

UPDATE tl.catalog_sections l
SET body = l.body || jsonb_build_object(
  'description','The librarian, in two modes. investigate (on demand, optional): a bounded controller over the same library any agent can call that returns an evidence packet; POST /v1/librarian, throughline lib librarian investigate GOAL, or SELECT * FROM api.librarian(goal => ...); never required, never run by default. curate (maintenance): an explicitly started, bounded run (throughline lib librarian curate run) that writes evidence-backed links, concept and place entities, passage-level provenance and claims, or proposals of them; never in a query path.',
  'modes',jsonb_build_object(
   'investigate','On demand: goal in, evidence packet out (see loop, tools, request, statuses, guarantees).',
   'curate',(replace(replace(c.body::text,
      'ignored records (overlay) are never candidates','ignored records (annotations) are never candidates'),
      'A TOML file named by THROUGHLINE_CURATOR_POLICY.','A TOML file named by THROUGHLINE_LIBRARIAN_POLICY (else the former THROUGHLINE_CURATOR_POLICY).')::jsonb - 'description')
     || jsonb_build_object(
      'description','A bounded maintenance run that writes evidence-backed links, concept and place links, passage-level provenance and claims, or proposals of them. Recall, expand, match and SQL make no model call for it and read what it wrote like any other derived data. Written data keeps job curator and ledger origin curator.',
      'commands',jsonb_build_array('throughline lib librarian curate run [--once] [--dry-run]','throughline lib librarian curate status'),
      'former_name','The curate mode was the curator: throughline lib curator run|status, the module throughline.curator and THROUGHLINE_CURATOR_POLICY remain as deprecated aliases.'))),
 version = l.version + 1
FROM tl.catalog_sections c
WHERE l.subject='librarian' AND c.subject='curator';

UPDATE tl.catalog_sections o
SET body = n.body || jsonb_build_object('renamed_to',n.subject,
  'description','Deprecated subject, renamed to '||n.subject||': '||(n.body->>'description')),
 version = o.version + 1
FROM tl.catalog_sections n
WHERE (o.subject,n.subject) IN (('overlay','annotations'),('consumers','feeds'));

UPDATE tl.catalog_sections o
SET body = (n.body->'modes'->'curate') || jsonb_build_object('renamed_to','librarian',
  'description','Deprecated subject: the curator is now the librarian''s curate mode (api.describe(''librarian''), modes.curate). '||(n.body->'modes'->'curate'->>'description')),
 version = o.version + 1
FROM tl.catalog_sections n
WHERE o.subject='curator' AND n.subject='librarian';

UPDATE tl.catalog_sections
SET body = replace(replace(replace(replace(replace(replace(replace(replace(replace(body::text,
  'Agents, the librarian and the curator propose','Agents and the librarian (investigate and curate) propose'),
  '(including the curator on the owner connection)','(including the librarian''s curate mode on the owner connection)'),
  'effective time (overlay, else record time)','effective time (annotations, else record time)'),
  'gets an overlay annotation media_noise','gets an annotation media_noise'),
  'from the overlay speaker or attribution','from the annotated speaker or attribution'),
  'Process only what is new with a named consumer: news, work, then mark (compare-and-set).','Process only what is new with a named feed: news, work, then mark (compare-and-set).'),
  'consumer create, drop and settings (POST /v1/consumers, DELETE /v1/consumers/{name}, PUT /v1/consumers/settings)','feed create, drop and settings (POST /v1/feeds, DELETE /v1/feeds/{name}, PUT /v1/feeds/settings; the deprecated /v1/consumers... aliases behave the same)'),
  'POST /v1/consumers/{name}/mark is idempotent','POST /v1/feeds/{name}/mark (deprecated alias POST /v1/consumers/{name}/mark) is idempotent'),
  'throughline lib librarian GOAL','throughline lib librarian investigate GOAL')::jsonb,
 version = version + 1
WHERE subject IN ('claims','link_types','people','transcripts','commitments','surface','write_contract');
