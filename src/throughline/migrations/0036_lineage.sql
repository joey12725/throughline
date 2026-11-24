-- Lineage: temporal validity, link history and supersession, provenance with
-- real writers, source-inherited visibility, as-of functions and reversible
-- entity merges (BRN-6, BRN-10, BRN-13, BRN-14, BRN-15, ACL-9, QRY-26, QRY-29).
--
-- Forward-only. Every api function is SECURITY DEFINER with a fixed
-- search_path, so tl objects are schema-qualified throughout.

-- 1. Validity: the period an assertion holds, beside when it was stored.
--    A bound with a precision qualifies it (exact, day, month, year,
--    approximate). A missing bound is open; a missing bound with precision
--    'unknown' is a bound that exists but is not known. Both are unbounded for
--    api.applies_at; the precision tells them apart.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['links','relations','facts'] LOOP
  EXECUTE format('ALTER TABLE tl.%I
   ADD COLUMN valid_from timestamptz,
   ADD COLUMN valid_to timestamptz,
   ADD COLUMN valid_from_precision text CHECK (valid_from_precision IN (''exact'',''day'',''month'',''year'',''approximate'',''unknown'')),
   ADD COLUMN valid_to_precision text CHECK (valid_to_precision IN (''exact'',''day'',''month'',''year'',''approximate'',''unknown'')),
   ADD CONSTRAINT %I CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to>=valid_from),
   ADD CONSTRAINT %I CHECK ((valid_from IS NOT NULL OR valid_from_precision IS NULL OR valid_from_precision=''unknown'') AND (valid_to IS NOT NULL OR valid_to_precision IS NULL OR valid_to_precision=''unknown''))',
   t,t||'_validity_order',t||'_validity_precision');
 END LOOP;
END $$;

-- 2. Pinned versions and supersession. from_version_id / to_version_id are the
--    message ids the link named when it was created; ingest repoints from_id
--    and to_id to new versions, never these. A superseded link stays stored.
ALTER TABLE tl.links
 ADD COLUMN from_version_id bigint,
 ADD COLUMN to_version_id bigint,
 ADD COLUMN superseded_by bigint REFERENCES tl.links ON DELETE SET NULL;
UPDATE tl.links SET from_version_id=from_id WHERE from_kind='message';
UPDATE tl.links SET to_version_id=to_id WHERE to_kind='message';
CREATE INDEX links_superseded_by ON tl.links(superseded_by) WHERE superseded_by IS NOT NULL;
-- Every writer gets the pinned ids (not only ingest.add_link), and no update
-- moves them.
CREATE FUNCTION tl.pin_link_versions() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.from_kind='message' THEN NEW.from_version_id := coalesce(NEW.from_version_id,NEW.from_id); END IF;
  IF NEW.to_kind='message' THEN NEW.to_version_id := coalesce(NEW.to_version_id,NEW.to_id); END IF;
 ELSIF NEW.from_version_id IS DISTINCT FROM OLD.from_version_id OR NEW.to_version_id IS DISTINCT FROM OLD.to_version_id THEN
  RAISE EXCEPTION 'pinned link versions never change' USING ERRCODE='check_violation';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.pin_link_versions() FROM PUBLIC;
CREATE TRIGGER link_pin_versions BEFORE INSERT OR UPDATE OF from_version_id,to_version_id ON tl.links FOR EACH ROW EXECUTE FUNCTION tl.pin_link_versions();

-- 3. Link history: the image before every change and every delete. Written
--    only by this trigger; runtime roles read it. A purge leaves no trace.
CREATE TABLE tl.link_history(
 history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 link_id bigint NOT NULL,
 op text NOT NULL CHECK (op IN ('update','delete')),
 old jsonb NOT NULL,
 changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 token_id bigint);
CREATE INDEX link_history_link ON tl.link_history(link_id,changed_at,history_id);
CREATE FUNCTION tl.record_link_history() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF current_setting('throughline.purge',true)='on' THEN RETURN NULL; END IF;
 IF TG_OP='UPDATE' AND to_jsonb(NEW)=to_jsonb(OLD) THEN RETURN NULL; END IF;
 INSERT INTO tl.link_history(link_id,op,old,token_id)
 VALUES(OLD.id,lower(TG_OP),to_jsonb(OLD),nullif(current_setting('throughline.token_id',true),'')::bigint);
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.record_link_history() FROM PUBLIC;
CREATE TRIGGER link_history AFTER UPDATE OR DELETE ON tl.links FOR EACH ROW EXECUTE FUNCTION tl.record_link_history();
REVOKE ALL ON tl.link_history FROM PUBLIC,throughline_app,throughline_reader;
GRANT SELECT ON tl.link_history TO throughline_app;

-- 4. Evidence rule (BRN-15): a model-made link names the messages it rests on.
CREATE FUNCTION tl.check_link_evidence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$
BEGIN
 IF NEW.method='model' AND (jsonb_typeof(NEW.evidence->'message_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.evidence->'message_ids')=0) THEN
  RAISE EXCEPTION 'model links require a non-empty evidence.message_ids' USING ERRCODE='check_violation';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.check_link_evidence() FROM PUBLIC;
CREATE TRIGGER link_evidence BEFORE INSERT OR UPDATE OF method,evidence ON tl.links FOR EACH ROW EXECUTE FUNCTION tl.check_link_evidence();

-- 5. Provenance: which source messages (pinned versions) a derived object
--    rests on. Origins source, owner and job are source-grade: they count as
--    support and decide visibility. Agent rows are kept apart and never do.
CREATE TABLE tl.provenance(
 derived_kind text NOT NULL CHECK (derived_kind IN ('message','passage','claim','link','entity')),
 derived_id bigint NOT NULL,
 source_message_id bigint NOT NULL REFERENCES tl.messages ON DELETE CASCADE,
 source_version int NOT NULL,
 relation text NOT NULL DEFAULT 'derived_from' CHECK (length(relation) BETWEEN 1 AND 100),
 origin text NOT NULL CHECK (origin IN ('source','owner','job','agent')),
 job text,
 job_version text,
 token_id bigint REFERENCES tl.tokens,
 asker text,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(derived_kind,derived_id,source_message_id));
CREATE INDEX provenance_source ON tl.provenance(source_message_id);
REVOKE ALL ON tl.provenance FROM PUBLIC,throughline_app,throughline_reader;
GRANT SELECT,INSERT ON tl.provenance TO throughline_app;

-- 6. Automatic writer: a link of a type the from-channel declares in
--    provenance_link_types, from a page revision or derived record to a
--    message, makes the target a source. Ingest repoints links to a new
--    revision (UPDATE OF from_id), so every revision inherits the sources of
--    its predecessor.
CREATE FUNCTION tl.link_provenance() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF NEW.status IS DISTINCT FROM 'active' OR NEW.from_kind<>'message' OR NEW.to_kind<>'message' OR NEW.from_id=NEW.to_id THEN RETURN NULL; END IF;
 INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,relation,origin,job,job_version,token_id)
 SELECT 'message',NEW.from_id,s.id,s.version,'derived_from',
  CASE NEW.method WHEN 'source' THEN 'source' WHEN 'owner' THEN 'owner' ELSE 'job' END,
  NEW.job,NEW.job_version,NEW.token_id
 FROM tl.messages f JOIN tl.channels c ON c.id=f.channel_id JOIN tl.messages s ON s.id=NEW.to_id
 WHERE f.id=NEW.from_id AND c.record_kind IN ('page_revision','derived')
  AND jsonb_typeof(c.provenance_link_types)='array' AND c.provenance_link_types ? NEW.type
 ON CONFLICT DO NOTHING;
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.link_provenance() FROM PUBLIC;
CREATE TRIGGER link_provenance AFTER INSERT OR UPDATE OF from_id,status ON tl.links FOR EACH ROW EXECUTE FUNCTION tl.link_provenance();
-- Links stored before this migration.
INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,relation,origin,job,job_version,token_id)
SELECT 'message',l.from_id,s.id,s.version,'derived_from',
 CASE l.method WHEN 'source' THEN 'source' WHEN 'owner' THEN 'owner' ELSE 'job' END,l.job,l.job_version,l.token_id
FROM tl.links l JOIN tl.messages f ON f.id=l.from_id JOIN tl.channels c ON c.id=f.channel_id JOIN tl.messages s ON s.id=l.to_id
WHERE l.status='active' AND l.from_kind='message' AND l.to_kind='message' AND l.from_id<>l.to_id
 AND c.record_kind IN ('page_revision','derived') AND jsonb_typeof(c.provenance_link_types)='array' AND c.provenance_link_types ? l.type
ON CONFLICT DO NOTHING;

-- 7. Visibility (ACL-9): a page revision or derived record is visible only
--    when its own channel or thread is granted AND every source-grade source
--    is visible, recursively. The walk stops at depth 4: a lineage deeper than
--    that, a cycle, or a source that no longer exists hides the record.
--    Same signature, so every gate that calls it inherits the rule.
CREATE OR REPLACE FUNCTION api.row_visible(message_id bigint,channel_id bigint,thread_id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE rk text; hidden boolean;
BEGIN
 IF current_setting('throughline.scope_mode',true) IS DISTINCT FROM 'asker' THEN RETURN false; END IF;
 IF NOT EXISTS(SELECT 1 FROM tl.asker_grants g WHERE g.asker=current_setting('throughline.asker',true) AND (g.channel_id=row_visible.channel_id OR g.thread_id=row_visible.thread_id)) THEN RETURN false; END IF;
 SELECT c.record_kind INTO rk FROM tl.channels c WHERE c.id=row_visible.channel_id;
 IF rk IS NULL THEN RETURN false; END IF;
 IF rk NOT IN ('page_revision','derived') THEN RETURN true; END IF;
 WITH RECURSIVE walk(node,depth,path,cycle) AS (
  SELECT p.source_message_id,1,ARRAY[row_visible.message_id,p.source_message_id],p.source_message_id=row_visible.message_id
  FROM tl.provenance p WHERE p.derived_kind='message' AND p.derived_id=row_visible.message_id AND p.origin IN ('source','owner','job')
  UNION ALL
  SELECT p.source_message_id,w.depth+1,w.path||p.source_message_id,p.source_message_id=ANY(w.path)
  FROM walk w JOIN tl.messages m ON m.id=w.node JOIN tl.channels c ON c.id=m.channel_id
  JOIN tl.provenance p ON p.derived_kind='message' AND p.derived_id=w.node AND p.origin IN ('source','owner','job')
  WHERE NOT w.cycle AND w.depth<=4 AND c.record_kind IN ('page_revision','derived')
 )
 SELECT EXISTS(SELECT 1 FROM walk w LEFT JOIN tl.messages m ON m.id=w.node
  WHERE w.cycle OR w.depth>4 OR m.id IS NULL OR NOT api.asker_sees(m.channel_id,m.thread_id)) INTO hidden;
 RETURN NOT hidden;
END $$;

-- Support of a generated record is its source-grade provenance.
CREATE OR REPLACE FUNCTION api.support_of(kind text,message_id bigint,version int) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT CASE
  WHEN support_of.kind IN ('life_event','reference') THEN jsonb_build_array(jsonb_build_object('message_id',support_of.message_id,'version',support_of.version,'relation','self'))
  WHEN support_of.kind IN ('page_revision','derived') THEN coalesce((
   SELECT jsonb_agg(jsonb_build_object('message_id',p.source_message_id,'version',p.source_version,'relation',p.relation) ORDER BY p.source_message_id)
   FROM tl.provenance p WHERE p.derived_kind='message' AND p.derived_id=support_of.message_id AND p.origin IN ('source','owner','job')),'[]'::jsonb)
  ELSE '[]'::jsonb END $$;

-- A link image (a tl.links row as jsonb, current or from history) passes the
-- same rule as api.links: a message end, every message end visible, every
-- cited evidence message visible.
CREATE FUNCTION api.link_image_visible(image jsonb) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE fk text := image->>'from_kind'; tk text := image->>'to_kind';
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 IF image IS NULL OR (fk IS DISTINCT FROM 'message' AND tk IS DISTINCT FROM 'message') THEN RETURN false; END IF;
 IF fk='message' AND NOT api.message_visible((image->>'from_id')::bigint) THEN RETURN false; END IF;
 IF tk='message' AND NOT api.message_visible((image->>'to_id')::bigint) THEN RETURN false; END IF;
 RETURN api.evidence_visible(coalesce(image->'evidence','{}'::jsonb));
END $$;

-- 9. Views. New link columns appear in api.links; history and provenance are
--    gated; identity views gain the validity columns and stay unrestricted-only.
SELECT tl.install_gate('links',NULL,'SELECT * FROM tl.links');
SELECT tl.install_gate('link_history','api.link_image_visible(v.old)','SELECT * FROM tl.link_history');
-- The derived object itself, for a restricted asker: a message when it is
-- visible, a link when api.links shows it, never an entity (identity is
-- unrestricted-only). Passages and claims are gated by their own views.
CREATE FUNCTION api.derived_visible(kind text,id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 RETURN CASE derived_visible.kind
  WHEN 'message' THEN api.message_visible(derived_visible.id)
  WHEN 'link' THEN EXISTS(SELECT 1 FROM tl.links l WHERE l.id=derived_visible.id AND api.link_image_visible(to_jsonb(l)))
  WHEN 'entity' THEN false
  ELSE true END;
END $$;
-- A restricted asker sees a provenance row only when no source of the same
-- derived object is hidden (a partly hidden lineage is invisible as a whole)
-- and the derived object is visible (api.derived_visible).
SELECT tl.install_gate('provenance',
 'NOT EXISTS(SELECT 1 FROM tl.provenance q WHERE q.derived_kind=v.derived_kind AND q.derived_id=v.derived_id AND q.origin<>''agent'' AND NOT api.message_visible(q.source_message_id)) AND api.derived_visible(v.derived_kind,v.derived_id)',
 'SELECT * FROM tl.provenance WHERE origin<>''agent''');
SELECT tl.install_gate('relations',NULL,
 'SELECT id,from_id,to_id,kind,asserted_by,note,created_at,ended_at,valid_from,valid_to,valid_from_precision,valid_to_precision FROM tl.relations WHERE ended_at IS NULL
  UNION ALL SELECT r.id,r.to_id AS from_id,r.from_id AS to_id,k.inverse AS kind,r.asserted_by,r.note,r.created_at,r.ended_at,r.valid_from,r.valid_to,r.valid_from_precision,r.valid_to_precision FROM tl.relations r JOIN tl.relation_kinds k USING(kind) WHERE r.ended_at IS NULL');
SELECT tl.install_gate('relations_history',NULL,'SELECT * FROM tl.relations');
SELECT tl.install_gate('facts',NULL,'SELECT * FROM tl.facts WHERE retired_at IS NULL');
SELECT tl.install_gate('facts_history',NULL,'SELECT * FROM tl.facts');

-- 10. As-of and lineage functions.
-- Every link created at or before `at`, in the state it had then: the image
-- before its first change after `at`, else its current state. A link deleted
-- before `at` is absent.
CREATE FUNCTION api.links_asof(at timestamptz) RETURNS SETOF api.links LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT r.* FROM (
  SELECT coalesce(
   (SELECT h.old FROM tl.link_history h WHERE h.link_id=i.id AND h.changed_at>links_asof.at ORDER BY h.changed_at,h.history_id LIMIT 1),
   (SELECT to_jsonb(l) FROM tl.links l WHERE l.id=i.id)) AS image
  FROM (SELECT l.id FROM tl.links l UNION SELECT h.link_id FROM tl.link_history h) i
 ) s CROSS JOIN LATERAL jsonb_populate_record(NULL::api.links,s.image) r
 WHERE s.image IS NOT NULL AND r.created_at<=links_asof.at AND api.link_image_visible(s.image)
 ORDER BY r.id $$;
-- Current links whose validity interval contains `at`.
CREATE FUNCTION api.links_valid_at(at timestamptz) RETURNS SETOF api.links LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT l.* FROM api.links l WHERE api.applies_at(l.valid_from,l.valid_to,links_valid_at.at) ORDER BY l.id $$;
-- api.follow over the links as of `at` that were active and valid then. A
-- message start is the version of it stored by `at` (else its current one).
CREATE FUNCTION api.follow_asof(kind text,id bigint,link_type text DEFAULT NULL,depth int DEFAULT 1,at timestamptz DEFAULT now()) RETURNS TABLE(kind text,id bigint,depth int) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 WITH RECURSIVE start AS (
  SELECT CASE WHEN follow_asof.kind='message' THEN coalesce(
   (SELECT v.id FROM tl.messages o JOIN tl.messages v ON v.channel_id=o.channel_id AND v.external_id=o.external_id WHERE o.id=follow_asof.id AND v.ingested_at<=follow_asof.at AND api.message_visible(v.id) ORDER BY v.version DESC LIMIT 1),
   api.current_message(follow_asof.id)) ELSE follow_asof.id END AS node
 ), l AS MATERIALIZED (
  SELECT * FROM api.links_asof(follow_asof.at) x WHERE x.status='active' AND api.applies_at(x.valid_from,x.valid_to,follow_asof.at) AND (follow_asof.link_type IS NULL OR x.type=follow_asof.link_type)
 ), walk(kind,id,depth,path) AS (
  SELECT follow_asof.kind,s.node,0,ARRAY[follow_asof.kind||':'||s.node] FROM start s WHERE s.node IS NOT NULL
  UNION ALL
  SELECT CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_kind ELSE l.from_kind END,
   CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_id ELSE l.from_id END,
   w.depth+1,
   w.path||CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_kind||':'||l.to_id ELSE l.from_kind||':'||l.from_id END
  FROM walk w JOIN l ON ((l.from_kind=w.kind AND l.from_id=w.id) OR (l.to_kind=w.kind AND l.to_id=w.id))
  WHERE w.depth<follow_asof.depth
   AND NOT (CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_kind||':'||l.to_id ELSE l.from_kind||':'||l.from_id END=ANY(w.path))
 ) SELECT DISTINCT walk.kind,walk.id,walk.depth FROM walk WHERE walk.depth>0 $$;
-- Identity assertions as of `at`, current or not, both directions. Identity
-- is unrestricted-only, like api.relations and api.facts.
CREATE FUNCTION api.relations_asof(at timestamptz) RETURNS SETOF api.relations LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT r.id,r.from_id,r.to_id,r.kind,r.asserted_by,r.note,r.created_at,r.ended_at,r.valid_from,r.valid_to,r.valid_from_precision,r.valid_to_precision
 FROM tl.relations r WHERE (SELECT api.asker_unrestricted()) AND r.created_at<=relations_asof.at AND api.applies_at(r.valid_from,coalesce(r.valid_to,r.ended_at),relations_asof.at)
 UNION ALL
 SELECT r.id,r.to_id,r.from_id,k.inverse,r.asserted_by,r.note,r.created_at,r.ended_at,r.valid_from,r.valid_to,r.valid_from_precision,r.valid_to_precision
 FROM tl.relations r JOIN tl.relation_kinds k USING(kind) WHERE (SELECT api.asker_unrestricted()) AND r.created_at<=relations_asof.at AND api.applies_at(r.valid_from,coalesce(r.valid_to,r.ended_at),relations_asof.at) $$;
CREATE FUNCTION api.facts_asof(at timestamptz) RETURNS SETOF api.facts_history LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT f.* FROM tl.facts f WHERE (SELECT api.asker_unrestricted()) AND f.created_at<=facts_asof.at AND api.applies_at(f.valid_from,coalesce(f.valid_to,f.retired_at),facts_asof.at) ORDER BY f.id $$;
-- Current versions of the source-grade sources of a derived object, in one
-- call. Nothing at all when any source or the derived object is hidden.
CREATE FUNCTION api.sources(kind text,id bigint) RETURNS SETOF api.messages LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT m.* FROM api.messages m
 WHERE m.id IN (
  SELECT c.id FROM tl.provenance p JOIN tl.messages s ON s.id=p.source_message_id
  JOIN tl.messages c ON c.channel_id=s.channel_id AND c.external_id=s.external_id AND c.superseded_by IS NULL
  WHERE p.derived_kind=sources.kind AND p.derived_id=sources.id AND p.origin IN ('source','owner','job'))
 AND api.derived_visible(sources.kind,sources.id)
 AND NOT EXISTS(SELECT 1 FROM tl.provenance p WHERE p.derived_kind=sources.kind AND p.derived_id=sources.id AND p.origin IN ('source','owner','job') AND NOT api.message_visible(p.source_message_id))
 ORDER BY m.id $$;

INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('links_asof','sql','links_asof','Every link created at or before a time, in the state it had then (from link history); links deleted earlier are absent','exact','free',0,'owner, agent; links pass the asker gate','SELECT * FROM api.links_asof(''2020-01-01Z'')','1',NULL,NULL),
 ('links_valid_at','sql','links_valid_at','Current links whose validity interval [valid_from, valid_to) contains a time','exact','free',0,'owner, agent; links pass the asker gate','SELECT * FROM api.links_valid_at(''2020-01-01Z'')','1',NULL,NULL),
 ('follow_asof','sql','follow_asof','api.follow over the links as of a time that were active and valid then','exact','free',2,'owner, agent; links pass the asker gate','SELECT * FROM api.follow_asof(''message'',1,NULL,2,''2020-01-01Z'')','1',NULL,NULL),
 ('relations_asof','sql','relations_asof','Relations in both directions that were stored and valid at a time, ended or not','exact','free',0,'owner, agent; unrestricted askers only','SELECT * FROM api.relations_asof(''2020-01-01Z'')','1',NULL,NULL),
 ('facts_asof','sql','facts_asof','Facts that were stored and valid at a time, retired or not','exact','free',0,'owner, agent; unrestricted askers only','SELECT * FROM api.facts_asof(''2020-01-01Z'')','1',NULL,NULL),
 ('sources','sql','sources','Current versions of the source-grade provenance sources of a derived object (message, passage, claim, link, entity); nothing when any is hidden','exact','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.sources(''message'',1)','1',NULL,NULL);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('links_asof','links_valid_at','follow_asof','relations_asof','facts_asof','sources');

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('lineage',jsonb_build_object(
  'description','Where derived records come from, how long assertions hold, and how links changed.',
  'validity',jsonb_build_object(
   'columns',jsonb_build_array('valid_from','valid_to','valid_from_precision','valid_to_precision'),
   'tables',jsonb_build_array('api.links','api.relations','api.relations_history','api.facts','api.facts_history'),
   'precision',jsonb_build_array('exact','day','month','year','approximate','unknown'),
   'rule','Half-open [valid_from, valid_to). A missing bound is open; a missing bound with precision unknown exists but is not known. Both are unbounded for api.applies_at. A day-precision date is sent as that day with precision day.'),
  'provenance',jsonb_build_object(
   'view','api.provenance: derived_kind, derived_id, source_message_id, source_version (pinned), relation, origin, job, job_version',
   'source_grade','Origins source (a plugin cites its own record''s sources), owner and job count as support and decide visibility. Agent provenance is never source-grade and is not listed.',
   'writers','A link of a type in the from-channel''s provenance_link_types from a page revision or derived record to a message records its target as a source; each new revision inherits the sources of its predecessor. POST /v1/provenance records citations by key.',
   'visibility','A page revision or derived record is visible to a restricted asker only when every source-grade source is visible, recursively to depth 4; deeper lineage and cycles hide it. A restricted asker falls back to the latest revision it may read (api.records_asof).',
   'unsourced','A generated record without source-grade provenance is unsupported and is gated by its own channel grant only.'),
  'history',jsonb_build_object(
   'view','api.link_history: the image of a link before each update and delete, with changed_at and token_id',
   'supersession','A superseded link keeps its row with status superseded and superseded_by naming its replacement.',
   'pinned','from_version_id and to_version_id are the message versions a link named when it was created; from_id and to_id follow new versions.'),
  'functions',jsonb_build_array('links_asof','links_valid_at','follow_asof','relations_asof','facts_asof','sources')));

-- 11. Reversible entity merges.
CREATE TABLE tl.entity_events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 op text NOT NULL CHECK (op IN ('merge','split')),
 source bigint NOT NULL REFERENCES tl.entities,
 target bigint NOT NULL REFERENCES tl.entities,
 moved jsonb NOT NULL DEFAULT '[]'::jsonb,
 merge_event bigint REFERENCES tl.entity_events,
 token_id bigint REFERENCES tl.tokens,
 at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK ((op='split')=(merge_event IS NOT NULL)));
CREATE UNIQUE INDEX entity_events_one_split ON tl.entity_events(merge_event) WHERE op='split';
CREATE TRIGGER attribute_token BEFORE INSERT ON tl.entity_events FOR EACH ROW EXECUTE FUNCTION tl.attribute_token();
REVOKE ALL ON tl.entity_events FROM PUBLIC,throughline_app,throughline_reader;
GRANT SELECT,INSERT ON tl.entity_events TO throughline_app;

-- 12. Purge: history images and provenance of purged messages go with them
--    (source rows cascade from tl.messages).
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('link_history',40,
  'DELETE FROM tl.link_history h WHERE ((h.old->>''from_kind'')=''message'' AND (h.old->>''from_id'')::bigint IN (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1))
   OR ((h.old->>''to_kind'')=''message'' AND (h.old->>''to_id'')::bigint IN (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1))
   OR (h.old->>''from_version_id'')::bigint IN (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1)
   OR (h.old->>''to_version_id'')::bigint IN (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1)
   OR (jsonb_typeof(h.old->''evidence''->''message_ids'')=''array'' AND EXISTS(SELECT 1 FROM jsonb_array_elements(h.old->''evidence''->''message_ids'') x
    WHERE jsonb_typeof(x)=''number'' AND x::text ~ ''^[0-9]{1,18}$'' AND x::text::bigint IN (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1)))'),
 ('provenance',41,
  'DELETE FROM tl.provenance p WHERE (p.derived_kind=''message'' AND p.derived_id IN (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1))
   OR (p.derived_kind=''link'' AND p.derived_id IN (SELECT l.id FROM tl.links l WHERE (l.from_kind=''message'' AND l.from_id IN (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1)) OR (l.to_kind=''message'' AND l.to_id IN (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1))))');

GRANT EXECUTE ON FUNCTION api.link_image_visible(jsonb),api.derived_visible(text,bigint),api.links_asof(timestamptz),api.links_valid_at(timestamptz),api.follow_asof(text,bigint,text,int,timestamptz),api.relations_asof(timestamptz),api.facts_asof(timestamptz),api.sources(text,bigint) TO throughline_app,throughline_reader;
