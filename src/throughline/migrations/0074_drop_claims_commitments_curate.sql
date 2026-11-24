-- Drop claims, commitments and librarian curate (functionality now covered by
-- pages and lenses). Shared proposals (tl.proposals) and annotations stay.
--
-- Forward-only. All tl objects are schema-qualified.

-- 1. Remove registry, catalogue, recipes and purge hooks --------------------------
DELETE FROM tl.record_sources WHERE prefix IN ('c', 'r');

DELETE FROM tl.api_functions
WHERE name IN ('claim_sources', 'claims_asof', 'commitment_assessments', 'open_commitments');

DELETE FROM tl.recipes
WHERE name IN ('claim_to_sources', 'spoken_instruction_to_work');

DELETE FROM tl.catalog_sections
WHERE subject IN (
 'claims',
 'commitments',
 'curator',
 'recipe:claim_to_sources',
 'recipe:spoken_instruction_to_work'
);

DELETE FROM tl.purge_hooks
WHERE name IN ('claims', 'commitments', 'curator_concepts');

-- 2. Drop API functions and gated views -------------------------------------------
DROP FUNCTION IF EXISTS api.commitment_assessments(bigint);
DROP FUNCTION IF EXISTS api.open_commitments(bigint, int);
DROP FUNCTION IF EXISTS api.claim_sources(bigint, int, int);
DROP FUNCTION IF EXISTS api.claims_asof(timestamptz);

DROP VIEW IF EXISTS
 api.commitment_records,
 tl.commitment_records_gated,
 tl.commitment_records_all,
 api.commitments,
 tl.commitments_gated,
 tl.commitments_all,
 api.claim_records,
 tl.claim_records_gated,
 tl.claim_records_all,
 api.claims,
 tl.claims_gated,
 tl.claims_all,
 api.claims_history,
 tl.claims_history_gated,
 tl.claims_history_all;

DELETE FROM tl.gates
WHERE view_name IN ('commitments', 'commitment_records', 'claims', 'claims_history', 'claim_records');

-- 3. Drop tables and helper/trigger functions -------------------------------------
DELETE FROM tl.links WHERE from_kind = 'claim' OR to_kind = 'claim';
DELETE FROM tl.provenance WHERE derived_kind = 'claim';
DELETE FROM tl.status_history WHERE kind = 'claim';

DROP TABLE IF EXISTS
 tl.commitment_audit_items,
 tl.commitment_assessments,
 tl.commitment_run_items,
 tl.commitment_sources,
 tl.commitments,
 tl.commitment_runs,
 tl.claim_relations,
 tl.claim_sources,
 tl.claims,
 tl.curator_runs,
 tl.curator_state
CASCADE;

DROP FUNCTION IF EXISTS
 tl.commitment_statement_norm(text),
 tl.commitment_natural_key(text, bigint, text, timestamptz, text),
 tl.commitment_quote_guard(),
 tl.commitment_immutable(),
 tl.commitment_source_guard(),
 tl.commitment_run_guard(),
 tl.commitment_run_item_guard(),
 tl.commitment_assessment_guard(),
 tl.commitment_audit_check(),
 tl.commitment_audit_assessment_check(),
 tl.commitment_audit_items_immutable(),
 tl.claim_statement_norm(text),
 tl.claim_natural_key(text, bigint, text, timestamptz, text, bigint, text, text),
 tl.claim_key();

-- 4. Tighten constraints and shared functions -------------------------------------
DO $$ DECLARE c text; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'tl.status_history'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%kind%' LOOP
  EXECUTE format('ALTER TABLE tl.status_history DROP CONSTRAINT %I', c);
 END LOOP;
END $$;
ALTER TABLE tl.status_history ADD CONSTRAINT status_history_kind_check
 CHECK (kind IN ('annotation'));

CREATE OR REPLACE FUNCTION tl.derived_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE note text;
BEGIN
 IF current_setting('throughline.purge',true)='on' AND session_user NOT IN ('throughline_app','throughline_reader') THEN
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'tl.% rows are never deleted; retract or supersede them',TG_TABLE_NAME USING ERRCODE='insufficient_privilege';
 END IF;
 IF (to_jsonb(NEW)-ARRAY['status','superseded_by']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','superseded_by']) THEN
  RAISE EXCEPTION 'tl.% content is never edited; supersede it',TG_TABLE_NAME USING ERRCODE='check_violation';
 END IF;
 IF NEW.status IS DISTINCT FROM OLD.status OR NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
  note := nullif(current_setting('throughline.status_note',true),'');
  INSERT INTO tl.status_history(kind,ref_id,old_status,old_superseded_by,new_status,note,token_id)
  VALUES ('annotation',OLD.id,OLD.status,OLD.superseded_by,NEW.status,left(note,500),
   CASE WHEN current_setting('throughline.token_id',true) ~ '^[0-9]{1,18}$' THEN current_setting('throughline.token_id',true)::bigint END);
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.derived_guard() FROM PUBLIC;

DO $$ DECLARE c text; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'tl.provenance'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%derived_kind%' LOOP
  EXECUTE format('ALTER TABLE tl.provenance DROP CONSTRAINT %I', c);
 END LOOP;
END $$;
ALTER TABLE tl.provenance ADD CONSTRAINT provenance_derived_kind
 CHECK (derived_kind IN ('message','passage','link','entity','annotation','table_row'));

DELETE FROM tl.proposals WHERE kind IN ('claim', 'commitment_assessment');
UPDATE tl.proposals SET origin = 'librarian' WHERE origin = 'curator';

DO $$ DECLARE c text; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'tl.proposals'::regclass AND contype = 'c' AND (pg_get_constraintdef(oid) LIKE '%kind%' OR pg_get_constraintdef(oid) LIKE '%origin%') LOOP
  EXECUTE format('ALTER TABLE tl.proposals DROP CONSTRAINT %I', c);
 END LOOP;
END $$;
ALTER TABLE tl.proposals ADD CONSTRAINT proposals_kind_check
 CHECK (kind IN ('link','concept','correction','recipe','provenance','link_type','annotation'));
ALTER TABLE tl.proposals ADD CONSTRAINT proposals_origin_check
 CHECK (origin IN ('agent','librarian'));

CREATE OR REPLACE FUNCTION api.derived_visible(kind text,id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 RETURN CASE derived_visible.kind
  WHEN 'message' THEN api.message_visible(derived_visible.id)
  WHEN 'link' THEN EXISTS(SELECT 1 FROM tl.links l WHERE l.id=derived_visible.id AND api.link_image_visible(to_jsonb(l)))
  WHEN 'entity' THEN false
  WHEN 'passage' THEN EXISTS(SELECT 1 FROM api.passages p WHERE p.passage_id=derived_visible.id)
  WHEN 'annotation' THEN EXISTS(SELECT 1 FROM api.annotations a WHERE a.id=derived_visible.id)
  WHEN 'table_row' THEN EXISTS(SELECT 1 FROM tl.table_rows r WHERE r.id=derived_visible.id AND api.sources_visible(r.source_message_ids))
  ELSE true END;
END $$;

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

-- 5. Update write_receipts purge hook (drop tl.claims/tl.claim_sources CTE) -------
UPDATE tl.purge_hooks SET statement =
 'WITH msgs AS MATERIALIZED (SELECT to_jsonb(m.id) AS j, m.id FROM tl.messages m WHERE m.channel_id=$1),
   lnk AS MATERIALIZED (SELECT to_jsonb(l.id) AS j FROM tl.links l
     WHERE (l.from_kind=''message'' AND l.from_id IN (SELECT id FROM msgs))
        OR (l.to_kind=''message'' AND l.to_id IN (SELECT id FROM msgs))),
   refs AS (
     SELECT r.token_id, r.idempotency_key, x.kind, e.v
     FROM tl.write_receipts r
     CROSS JOIN LATERAL jsonb_path_query(r.response, ''strict $.**'') AS o(node)
     CROSS JOIN LATERAL (VALUES
       (CASE o.node->>''kind'' WHEN ''message'' THEN ''m'' WHEN ''link'' THEN ''l'' END, o.node->''id''),
       (''m'', o.node->''message_id''),
       (''l'', o.node->''link_id'')) AS x(kind, val)
     CROSS JOIN LATERAL jsonb_array_elements(
       CASE WHEN jsonb_typeof(x.val)=''array'' THEN x.val ELSE jsonb_build_array(x.val) END) AS e(v)
     WHERE jsonb_typeof(o.node)=''object'' AND x.kind IS NOT NULL AND x.val IS NOT NULL
       AND jsonb_typeof(e.v)=''number''),
   doomed AS (
     SELECT token_id, idempotency_key FROM refs WHERE kind=''m'' AND v IN (SELECT j FROM msgs)
     UNION SELECT token_id, idempotency_key FROM refs WHERE kind=''l'' AND v IN (SELECT j FROM lnk))
   DELETE FROM tl.write_receipts r WHERE (r.token_id, r.idempotency_key) IN (SELECT token_id, idempotency_key FROM doomed)'
WHERE name = 'write_receipts';

-- 6. Refresh catalogue sections ---------------------------------------------------
UPDATE tl.catalog_sections
SET body = (body - 'modes') || jsonb_build_object(
  'description',
  'Optional librarian: a bounded controller over the same library any agent can call. POST /v1/librarian, throughline lib librarian investigate GOAL, or SELECT * FROM api.librarian(goal => ...). Never required; never run by default.'
 ),
 version = coalesce(version, 1) + 1
WHERE subject = 'librarian';

UPDATE tl.catalog_sections
SET body = jsonb_set(
  jsonb_set(
   body,
   '{receipt_kinds}',
   (SELECT coalesce(jsonb_agg(v), '[]'::jsonb)
    FROM jsonb_array_elements(coalesce(body->'receipt_kinds', '[]'::jsonb)) v
    WHERE v#>>'{}' NOT IN ('claim', 'commitment'))
  ),
  '{rules}',
  (SELECT coalesce(jsonb_agg(v), '[]'::jsonb)
   FROM jsonb_array_elements(coalesce(body->'rules', '[]'::jsonb)) v
   WHERE (v#>>'{}') NOT LIKE 'Commitment writes%')
 ),
 version = coalesce(version, 1) + 1
WHERE subject = 'write_contract';

UPDATE tl.catalog_sections
SET body = replace(
  body::text,
  'Rule and model links (including the librarian''s curate mode on the owner connection) may use registered types only.',
  'Rule and model links may use registered types only.'
 )::jsonb,
 version = coalesce(version, 1) + 1
WHERE subject = 'link_types';
