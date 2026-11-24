-- Passage store for page revisions (BRN-6, BRN-5, EMB-2, ACL-9, QRY-18/20).
--
-- A page is one message per revision (versions of one external_id; the record
-- time of a revision is its revision time). Its full text comes from the raw
-- payload through tl.channels.passage_text_paths (message content when NULL),
-- so text beyond any cut of the content is still split and searchable.
-- throughline.passages splits each revision into heading-aware passages;
-- identical passage texts share one vector per model.
--
-- Forward-only. Reads tl.provenance and the replaced api.row_visible /
-- api.support_of of migration 0036. Every view exposing passage text is built
-- by tl.install_gate, so a revision hidden from the asker (including one whose
-- provenance names a hidden source) has no passage row, and caller predicates
-- never run below the gate for a restricted asker.

-- 1. Tables ------------------------------------------------------------------
CREATE TABLE tl.passages(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 message_id bigint NOT NULL REFERENCES tl.messages ON DELETE CASCADE,
 channel_id bigint NOT NULL,
 external_id text NOT NULL,
 version int NOT NULL,
 ordinal int NOT NULL CHECK(ordinal>=0),
 heading_path text[] NOT NULL DEFAULT '{}',
 char_start int NOT NULL CHECK(char_start>=0),
 char_end int NOT NULL,
 content text NOT NULL CHECK(length(btrim(content))>0),
 text_hash text NOT NULL,
 token_estimate int NOT NULL CHECK(token_estimate>0),
 tsv tsvector GENERATED ALWAYS AS (to_tsvector('english',content)) STORED,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(char_end>char_start),
 UNIQUE(message_id,ordinal));
CREATE INDEX passages_search ON tl.passages USING gin(tsv);
CREATE INDEX passages_page ON tl.passages(channel_id,external_id,version);
CREATE INDEX passages_text_hash ON tl.passages(text_hash);
-- One vector per distinct passage text and model (EMB-2).
CREATE TABLE tl.passage_embeddings(
 text_hash text NOT NULL,
 model text NOT NULL,
 embedding vector NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(text_hash,model));
GRANT SELECT,INSERT,DELETE ON tl.passages,tl.passage_embeddings TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.passages_id_seq TO throughline_app;
-- One builder per revision (throughline.passages.build): a transaction lock
-- in its own two-key namespace, taken without waiting. The app role has no
-- pg_try_advisory_* of its own.
CREATE FUNCTION tl.try_lock_passage_build(message_id bigint) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT pg_try_advisory_xact_lock(71824038,hashtext(try_lock_passage_build.message_id::text))
$$;
REVOKE EXECUTE ON FUNCTION tl.try_lock_passage_build(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.try_lock_passage_build(bigint) TO throughline_app;

-- 2. Gated views ---------------------------------------------------------------
-- A passage is visible when its revision is (api.row_visible: channel or
-- thread grant, and every provenance source of the revision visible) and, when
-- the passage has passage-level sources of its own, every one of them is.
SELECT tl.install_gate('passages',
 'api.row_visible(v.message_id, v.channel_id, v.thread_id) AND NOT EXISTS(SELECT 1 FROM tl.provenance s WHERE s.derived_kind=''passage'' AND s.derived_id=v.passage_id AND s.origin IN (''source'',''owner'',''job'') AND NOT api.message_visible(s.source_message_id))',
 $def$
SELECT p.id AS passage_id,
 'p:'||p.id AS record_id,
 p.message_id,
 p.channel_id,
 v.channel,
 p.external_id,
 p.version,
 v.superseded_by IS NULL AS is_current,
 p.ordinal,
 p.heading_path,
 p.char_start,
 p.char_end,
 p.content,
 p.token_estimate,
 v.happened_at AS revision_time,
 v.thread_id,
 p.tsv
FROM tl.passages p
JOIN tl.message_versions_all v ON v.id=p.message_id
$def$);

-- Record contract view of passages (prefix p). Columns, order and types are
-- those of api.record_versions. Support is the passage's own source-grade
-- provenance when it has any, else the revision's.
SELECT tl.install_gate('passage_records',
 'api.row_visible(v.message_id, (SELECT x.channel_id FROM tl.messages x WHERE x.id=v.message_id), v.thread_id) AND NOT EXISTS(SELECT 1 FROM tl.provenance s WHERE s.derived_kind=''passage'' AND s.derived_id=v.ref_id AND s.origin IN (''source'',''owner'',''job'') AND NOT api.message_visible(s.source_message_id))',
 $def$
SELECT 'p:'||p.id AS record_id,
 'passage'::text AS kind,
 p.id AS ref_id,
 p.message_id,
 p.version,
 v.superseded_by IS NULL AS is_current,
 v.channel,
 p.external_id,
 v.thread_id,
 v.thread_no,
 p.content,
 v.happened_at AS record_time,
 v.ended_at,
 v.ingested_at,
 x.subject_from,
 x.subject_to,
 x.subject_from_precision,
 x.subject_to_precision,
 v.person_id,
 v.author_handle,
 v.lat,
 v.lon,
 v.loc_reason,
 v.is_outside,
 p.heading_path,
 p.char_start,
 p.char_end,
 NULL::jsonb AS claim,
 true AS generated,
 s.support='[]'::jsonb AS unsupported,
 jsonb_build_object('plugin',c.plugin,'channel',v.channel,'external_id',p.external_id,'version',p.version,'method','passage','ordinal',p.ordinal) AS provenance,
 s.support,
 jsonb_build_object('page',jsonb_build_array(v.channel,p.external_id),'revision',p.message_id,'sources',true) AS expansion
FROM tl.passages p
JOIN tl.message_versions_all v ON v.id=p.message_id
JOIN tl.messages x ON x.id=p.message_id
JOIN tl.channels c ON c.id=p.channel_id
CROSS JOIN LATERAL (SELECT coalesce(
  (SELECT jsonb_agg(jsonb_build_object('message_id',pv.source_message_id,'version',pv.source_version,'relation',pv.relation) ORDER BY pv.source_message_id)
   FROM tl.provenance pv WHERE pv.derived_kind='passage' AND pv.derived_id=p.id AND pv.origin IN ('source','owner','job')),
  api.support_of('page_revision',p.message_id,p.version)) AS support) s
$def$);
INSERT INTO tl.record_sources VALUES ('p','api.passage_records');

-- 3. Scope ---------------------------------------------------------------------
-- Time test of one revision. record: overlap of [record time, end] with
-- [after_at, before_at]; subject: overlap with the subject interval, where a
-- missing bound or one of precision 'unknown' is open and a revision without
-- a subject interval falls back to its record time; either: one of the two.
CREATE FUNCTION tl.passage_time_ok(happened_at timestamptz,ended_at timestamptz,subject_from timestamptz,subject_to timestamptz,from_precision text,to_precision text,after_at timestamptz,before_at timestamptz,time_basis text) RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public AS $$
 WITH t AS (SELECT
  (after_at IS NULL OR coalesce(ended_at,happened_at)>=after_at) AND (before_at IS NULL OR happened_at<=before_at) AS by_record,
  CASE WHEN subject_from IS NULL AND subject_to IS NULL THEN NULL ELSE
   (after_at IS NULL OR subject_to IS NULL OR to_precision='unknown' OR subject_to>=after_at)
   AND (before_at IS NULL OR subject_from IS NULL OR from_precision='unknown' OR subject_from<=before_at) END AS by_subject)
 SELECT CASE time_basis
  WHEN 'record' THEN by_record
  WHEN 'subject' THEN coalesce(by_subject,by_record)
  ELSE by_record OR coalesce(by_subject,by_record) END FROM t
$$;
REVOKE EXECUTE ON FUNCTION tl.passage_time_ok(timestamptz,timestamptz,timestamptz,timestamptz,text,text,timestamptz,timestamptz,text) FROM PUBLIC;

-- Eligible passages. Reads api.passages and api.message_versions, so the gate
-- of the asker applies before anything else. Revision eligibility is decided
-- among the revisions VISIBLE to the asker:
--   default            the latest visible revision of each page (a current
--                      revision with a hidden source falls back to the latest
--                      earlier one the asker may read);
--   as_of              the latest visible revision with revision time <= as_of
--                      (and ingested by known_by when given);
--   current_only false all visible revisions (without as_of).
-- Every filter is applied here, before any ranking or limit.
CREATE FUNCTION tl.passage_scope(q tsquery,channels text[],as_of timestamptz,current_only boolean,after_at timestamptz,before_at timestamptz,time_basis text,thread_ids bigint[],message_ids bigint[],must_terms text[],source_ids bigint[],exclude_message_ids bigint[],known_by timestamptz)
RETURNS TABLE(passage_id bigint,message_id bigint,text_hash text,tsv tsvector) LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
 WITH revs AS MATERIALIZED (
  SELECT m.id,m.channel_id,m.external_id,m.version,m.happened_at
  FROM api.message_versions m JOIN tl.channels c ON c.id=m.channel_id
  WHERE c.record_kind='page_revision'
   AND (passage_scope.channels IS NULL OR c.name=ANY(passage_scope.channels))
   AND (passage_scope.as_of IS NULL OR m.happened_at<=passage_scope.as_of)
   AND (passage_scope.known_by IS NULL OR m.ingested_at<=passage_scope.known_by)
 ), chosen AS MATERIALIZED (
  SELECT r.id FROM revs r WHERE passage_scope.as_of IS NULL AND NOT coalesce(passage_scope.current_only,true)
  UNION ALL
  (SELECT DISTINCT ON (r.channel_id,r.external_id) r.id FROM revs r
   WHERE passage_scope.as_of IS NOT NULL OR coalesce(passage_scope.current_only,true)
   ORDER BY r.channel_id,r.external_id,CASE WHEN passage_scope.as_of IS NOT NULL THEN r.happened_at END DESC NULLS LAST,r.version DESC)
 )
 SELECT p.passage_id,p.message_id,x.text_hash,p.tsv
 FROM api.passages p
 JOIN chosen ch ON ch.id=p.message_id
 JOIN tl.passages x ON x.id=p.passage_id
 JOIN tl.messages r ON r.id=p.message_id
 WHERE (passage_scope.q IS NULL OR p.tsv @@ passage_scope.q)
  AND (passage_scope.thread_ids IS NULL OR p.thread_id=ANY(passage_scope.thread_ids))
  AND (passage_scope.message_ids IS NULL OR p.message_id=ANY(passage_scope.message_ids))
  AND (passage_scope.exclude_message_ids IS NULL OR NOT (p.message_id=ANY(passage_scope.exclude_message_ids)))
  AND (passage_scope.must_terms IS NULL OR NOT EXISTS(
   SELECT 1 FROM unnest(passage_scope.must_terms) t(term)
   WHERE numnode(plainto_tsquery('english',t.term))>0 AND NOT p.tsv @@ plainto_tsquery('english',t.term)))
  AND tl.passage_time_ok(r.happened_at,r.ended_at,r.subject_from,r.subject_to,r.subject_from_precision,r.subject_to_precision,passage_scope.after_at,passage_scope.before_at,coalesce(passage_scope.time_basis,'record'))
  -- Person and place scopes reach passages through their sources: the caller
  -- passes the in-scope source messages. A source is matched by its record
  -- key, so any version of an in-scope message counts. The passage's own
  -- sources replace the revision's when it has any.
  AND (passage_scope.source_ids IS NULL OR EXISTS(
   SELECT 1 FROM tl.provenance pv
   JOIN tl.messages s ON s.id=pv.source_message_id
   JOIN tl.messages k ON k.channel_id=s.channel_id AND k.external_id=s.external_id
   WHERE k.id=ANY(passage_scope.source_ids) AND pv.origin IN ('source','owner','job')
    AND ((pv.derived_kind='passage' AND pv.derived_id=p.passage_id)
     OR (pv.derived_kind='message' AND pv.derived_id=p.message_id AND NOT EXISTS(
      SELECT 1 FROM tl.provenance pp WHERE pp.derived_kind='passage' AND pp.derived_id=p.passage_id AND pp.origin IN ('source','owner','job'))))))
$$;
REVOKE EXECUTE ON FUNCTION tl.passage_scope(tsquery,text[],timestamptz,boolean,timestamptz,timestamptz,text,bigint[],bigint[],text[],bigint[],bigint[],timestamptz) FROM PUBLIC;

-- 4. Functions -----------------------------------------------------------------
CREATE FUNCTION api.search_passages(query text,max_results int DEFAULT 20,channels text[] DEFAULT NULL,as_of timestamptz DEFAULT NULL,current_only boolean DEFAULT true,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,time_basis text DEFAULT 'record',thread_ids bigint[] DEFAULT NULL,message_ids bigint[] DEFAULT NULL,must_terms text[] DEFAULT NULL,source_ids bigint[] DEFAULT NULL,exclude_message_ids bigint[] DEFAULT NULL,known_by timestamptz DEFAULT NULL)
RETURNS TABLE(passage_id bigint,message_id bigint,score real) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE q tsquery;
BEGIN
 IF coalesce(search_passages.time_basis,'record') NOT IN ('record','subject','either') THEN
  RAISE EXCEPTION 'time_basis must be record, subject or either' USING ERRCODE='invalid_parameter_value';
 END IF;
 q := websearch_to_tsquery('english',coalesce(search_passages.query,''));
 IF numnode(q)=0 THEN RETURN; END IF;
 RETURN QUERY
  SELECT e.passage_id,e.message_id,ts_rank_cd(e.tsv,q)::real AS score
  FROM tl.passage_scope(q,search_passages.channels,search_passages.as_of,search_passages.current_only,search_passages.after_at,search_passages.before_at,search_passages.time_basis,search_passages.thread_ids,search_passages.message_ids,search_passages.must_terms,search_passages.source_ids,search_passages.exclude_message_ids,search_passages.known_by) e
  ORDER BY 3 DESC,e.passage_id
  LIMIT search_passages.max_results;
END $$;

-- Exact cosine ranking over the vectors of the active model (or a named one).
-- Vectors exist for the passages of current revisions unless
-- THROUGHLINE_PASSAGE_EMBED_HISTORY=true, so historical revisions are found
-- through api.search_passages.
CREATE FUNCTION api.vector_passages(query_embedding vector,max_results int DEFAULT 20,channels text[] DEFAULT NULL,as_of timestamptz DEFAULT NULL,current_only boolean DEFAULT true,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,time_basis text DEFAULT 'record',thread_ids bigint[] DEFAULT NULL,message_ids bigint[] DEFAULT NULL,must_terms text[] DEFAULT NULL,source_ids bigint[] DEFAULT NULL,exclude_message_ids bigint[] DEFAULT NULL,known_by timestamptz DEFAULT NULL,serving_model text DEFAULT NULL)
RETURNS TABLE(passage_id bigint,message_id bigint,score real) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF coalesce(vector_passages.time_basis,'record') NOT IN ('record','subject','either') THEN
  RAISE EXCEPTION 'time_basis must be record, subject or either' USING ERRCODE='invalid_parameter_value';
 END IF;
 RETURN QUERY
  WITH eligible AS MATERIALIZED (
   SELECT e.passage_id,e.message_id,e.text_hash
   FROM tl.passage_scope(NULL,vector_passages.channels,vector_passages.as_of,vector_passages.current_only,vector_passages.after_at,vector_passages.before_at,vector_passages.time_basis,vector_passages.thread_ids,vector_passages.message_ids,vector_passages.must_terms,vector_passages.source_ids,vector_passages.exclude_message_ids,vector_passages.known_by) e)
  SELECT e.passage_id,e.message_id,(1-(pe.embedding <=> vector_passages.query_embedding))::real AS score
  FROM eligible e
  JOIN tl.passage_embeddings pe ON pe.text_hash=e.text_hash
  JOIN tl.embedding_models em ON em.model=pe.model AND ((vector_passages.serving_model IS NULL AND em.active) OR em.model=vector_passages.serving_model)
  WHERE vector_dims(pe.embedding)=vector_dims(vector_passages.query_embedding)
  ORDER BY 3 DESC,e.passage_id
  LIMIT vector_passages.max_results;
END $$;

-- Every visible passage of one revision, in order.
CREATE FUNCTION api.passages_of(message_id bigint) RETURNS SETOF api.passages LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT p.* FROM api.passages p WHERE p.message_id=passages_of.message_id ORDER BY p.ordinal
$$;
GRANT EXECUTE ON FUNCTION api.search_passages(text,int,text[],timestamptz,boolean,timestamptz,timestamptz,text,bigint[],bigint[],text[],bigint[],bigint[],timestamptz),
 api.vector_passages(vector,int,text[],timestamptz,boolean,timestamptz,timestamptz,text,bigint[],bigint[],text[],bigint[],bigint[],timestamptz,text),
 api.passages_of(bigint) TO throughline_app,throughline_reader;

-- 5. Catalogue -----------------------------------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('search_passages','sql','search_passages','Rank passages of page revisions by keywords. One revision per page by default (the latest the asker may read), the revision valid at as_of, or all revisions with current_only false; channel, time (record, subject or either), thread, revision, required-term, source and exclusion filters apply before ranking','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.search_passages(''orchard'',20)','1',2,20),
 ('vector_passages','sql','vector_passages','Exact cosine ranking of passages for a query embedding supplied by the caller, with the filters of search_passages; vectors of the active model or of serving_model','exact','embedding',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.vector_passages(''[1,0,0]'',20)','1',2,20),
 ('passages_of','sql','passages_of','Every passage of one page revision in order, with heading path and character offsets into the revision''s full text','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.passages_of(1)','1',NULL,NULL);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('search_passages','vector_passages','passages_of');

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('passages',jsonb_build_object(
  'description','Page revisions are split into passages: heading-aware pieces of the revision''s full text, each with its heading path and character offsets. Pages enter retrieval as passages, never as whole messages.',
  'views',jsonb_build_object(
   'api.passages','One row per passage: passage_id, record_id (p:<id>), message_id (the revision), channel, external_id, version, is_current, ordinal, heading_path, char_start, char_end, content, token_estimate, revision_time, thread_id',
   'api.passage_records','Passages in the record contract (kind passage, generated true); support is the passage''s own sources, else the revision''s; unsupported when there are none'),
  'functions',jsonb_build_array('search_passages','vector_passages','passages_of'),
  'full_text','The text of a revision is taken from its raw payload through the channel''s passage_text_paths (the message content when the channel declares none), so text beyond a cut of the content is searchable. char_start and char_end index that text: content = text[char_start:char_end].',
  'revisions','Default: the latest revision of each page the asker may read; a revision citing a hidden source is not readable, so an earlier authorized revision is used. as_of: the revision valid at that record time (known_by: stored by then). current_only false: every revision.',
  'scope','Every filter applies before ranking. source_ids keeps passages with at least one provenance source among the given messages: person and place scopes reach passages through their sources.',
  'vectors','One vector per distinct passage text and model. Passages of current revisions are embedded; history only with THROUGHLINE_PASSAGE_EMBED_HISTORY=true.'));

-- 6. Purge ---------------------------------------------------------------------
-- Passages of the channel go first (with their passage-level provenance), then
-- vectors whose text no other passage still has.
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES(
 'passages',20,
 'WITH gone AS (DELETE FROM tl.passages p WHERE p.channel_id=$1 RETURNING p.id,p.text_hash), lineage AS (DELETE FROM tl.provenance v WHERE v.derived_kind=''passage'' AND v.derived_id IN (SELECT g.id FROM gone g)) DELETE FROM tl.passage_embeddings e WHERE e.text_hash IN (SELECT g.text_hash FROM gone g) AND NOT EXISTS(SELECT 1 FROM tl.passages p WHERE p.text_hash=e.text_hash AND p.channel_id<>$1)');

-- 7. Lineage of passages -----------------------------------------------------
-- The lineage of a passage (api.provenance, api.sources('passage', id)) is
-- readable only when the passage itself passes its gate in api.passages.
CREATE OR REPLACE FUNCTION api.derived_visible(kind text,id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 RETURN CASE derived_visible.kind
  WHEN 'message' THEN api.message_visible(derived_visible.id)
  WHEN 'link' THEN EXISTS(SELECT 1 FROM tl.links l WHERE l.id=derived_visible.id AND api.link_image_visible(to_jsonb(l)))
  WHEN 'entity' THEN false
  WHEN 'passage' THEN EXISTS(SELECT 1 FROM api.passages p WHERE p.passage_id=derived_visible.id)
  ELSE true END;
END $$;

-- 8. Access description --------------------------------------------------------
-- The rule text now covers sourced records (0036) and passages.
CREATE OR REPLACE FUNCTION api.describe_access() RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 WITH me AS (
  SELECT nullif(current_setting('throughline.scope_mode',true),'') scope_mode,
   CASE WHEN current_setting('throughline.scope_mode',true)='asker' THEN current_setting('throughline.asker',true) END asker
 ), registered AS (
  SELECT a.name,a.mode FROM tl.askers a JOIN me ON me.asker=a.name
 )
 SELECT jsonb_build_object(
  'scope_mode',me.scope_mode,
  'asker',me.asker,
  'asker_mode',(SELECT mode FROM registered),
  'unrestricted',api.asker_unrestricted(),
  'granted_channels',CASE WHEN me.asker IS NULL THEN '[]'::jsonb ELSE (SELECT coalesce(jsonb_agg(c.name ORDER BY c.name),'[]') FROM tl.asker_grants g JOIN tl.channels c ON c.id=g.channel_id WHERE g.asker=me.asker) END,
  'granted_threads',CASE WHEN me.asker IS NULL THEN 0 ELSE (SELECT count(*) FROM tl.asker_grants g WHERE g.asker=me.asker AND g.thread_id IS NOT NULL) END,
  'rule','An unrestricted asker reads everything. A restricted asker reads messages, threads, conversations and message locations of granted channels and threads, and links whose message ends and cited evidence messages are all visible. A page revision or derived record is readable only when, in addition, every source it cites is visible (checked through sources of sources to depth 4); a passage only when its revision and its own sources are; the lineage of a record only when the record and all its sources are. People, identifiers, facts, relations, entities, the location track and plugins are not readable.',
  'policy_version',api.policy_version())
 FROM me
$$;
