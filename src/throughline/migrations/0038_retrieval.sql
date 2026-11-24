-- Shared retrieval library, SQL side (QRY-13..22, QRY-29, QRY-30, MSG-7,
-- MSG-9, BRN-5, BEN-21).
--
-- One hard-scope predicate (tl.recall_scope, exposed as api.scope_filter) is
-- applied inside every function that creates a hit, before any ORDER BY or
-- LIMIT. Eligible rows come only from gated api views (api.message_versions,
-- api.records_asof), so the asker gate decides visibility first.
--
-- Scope arguments S, in this order, in every function below:
--   kinds text[]            message record kinds; NULL = life_event and derived.
--                           reference only when listed; page_revision never
--                           (pages enter recall through passages)
--   channels text[]
--   persons bigint[]
--   person_role text        author | involved (author, or a message by the
--                           person in the thread, or an active link to the
--                           person; both read through the gated api views,
--                           so a message the asker cannot see never makes
--                           another one match)
--   after_at, before_at     overlap with the time interval chosen by time_basis
--   time_basis text         record | subject | either
--   lat, lon, meters        within meters of a point (meters default 100)
--   thread_ids bigint[]
--   include_outside boolean
--   as_of, known_by         record-time and ingest-time cutoffs: the eligible
--                           set is api.records_asof(coalesce(as_of,'infinity'),
--                           known_by), each version matched on its own text
--   must_terms text[]       every term must match the record's own text
--   exclude_message_ids bigint[]
--
-- Forward-only. Nothing here depends on a later migration or on a function of
-- the same wave: api.links_asof and api.sources are used by Python only.

-- 1. Internal predicates -----------------------------------------------------
-- Not callable by runtime roles; the api functions become SECURITY DEFINER
-- with a fixed search_path through tl.harden_reader() after every migration.
CREATE FUNCTION tl.recall_overlaps(lo timestamptz,hi timestamptz,after_at timestamptz,before_at timestamptz) RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT ($3 IS NULL OR $2 IS NULL OR $2>=$3) AND ($4 IS NULL OR $1 IS NULL OR $1<=$4) $$;
-- record: [record_time, coalesce(ended_at, record_time)].
-- subject: the subject interval; a missing bound or one of precision 'unknown'
--   is open; a record without any subject bound falls back to record time.
-- either: record or subject.
CREATE FUNCTION tl.recall_time_ok(record_time timestamptz,ended_at timestamptz,subject_from timestamptz,subject_to timestamptz,from_precision text,to_precision text,after_at timestamptz,before_at timestamptz,time_basis text) RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,tl AS $$
 SELECT ($7 IS NULL AND $8 IS NULL) OR CASE coalesce($9,'record')
  WHEN 'record' THEN tl.recall_overlaps($1,coalesce($2,$1),$7,$8)
  WHEN 'subject' THEN CASE WHEN $3 IS NULL AND $4 IS NULL THEN tl.recall_overlaps($1,coalesce($2,$1),$7,$8)
   ELSE tl.recall_overlaps(CASE WHEN $5 IS DISTINCT FROM 'unknown' THEN $3 END,CASE WHEN $6 IS DISTINCT FROM 'unknown' THEN $4 END,$7,$8) END
  ELSE tl.recall_overlaps($1,coalesce($2,$1),$7,$8)
   OR ((($3 IS NOT NULL) OR ($4 IS NOT NULL)) AND tl.recall_overlaps(CASE WHEN $5 IS DISTINCT FROM 'unknown' THEN $3 END,CASE WHEN $6 IS DISTINCT FROM 'unknown' THEN $4 END,$7,$8))
 END $$;
CREATE FUNCTION tl.recall_check(person_role text,time_basis text) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
BEGIN
 IF coalesce(person_role,'author') NOT IN ('author','involved') THEN RAISE EXCEPTION 'person_role must be author or involved' USING ERRCODE='invalid_parameter_value'; END IF;
 IF coalesce(time_basis,'record') NOT IN ('record','subject','either') THEN RAISE EXCEPTION 'time_basis must be record, subject or either' USING ERRCODE='invalid_parameter_value'; END IF;
 RETURN true;
END $$;

-- The hard scope. Rows: the eligible message version per record, with what
-- callers order and group by. Every argument is qualified with the function
-- name because a column of the same name would otherwise win.
CREATE FUNCTION tl.recall_scope(message_ids bigint[],kinds text[],channels text[],persons bigint[],person_role text,after_at timestamptz,before_at timestamptz,time_basis text,lat float8,lon float8,meters float8,thread_ids bigint[],include_outside boolean,as_of timestamptz,known_by timestamptz,must_terms text[],exclude_message_ids bigint[])
RETURNS TABLE(message_id bigint,record_time timestamptz,thread_id bigint,thread_no int) LANGUAGE sql STABLE SET search_path=pg_catalog,api,public AS $$
 WITH base AS (
  SELECT m.id,m.channel_id,m.thread_id,m.thread_no,m.happened_at AS record_time,m.ended_at,m.person_id,m.lat,m.lon,m.is_outside
  FROM api.message_versions m
  WHERE recall_scope.as_of IS NULL AND recall_scope.known_by IS NULL AND m.superseded_by IS NULL
   AND (recall_scope.message_ids IS NULL OR m.id=ANY(recall_scope.message_ids))
  UNION ALL
  SELECT r.message_id,x.channel_id,r.thread_id,r.thread_no,r.record_time,r.ended_at,r.person_id,r.lat,r.lon,r.is_outside
  FROM api.records_asof(coalesce(recall_scope.as_of,'infinity'::timestamptz),recall_scope.known_by) r
  JOIN tl.messages x ON x.id=r.message_id
  WHERE (recall_scope.as_of IS NOT NULL OR recall_scope.known_by IS NOT NULL)
   AND (recall_scope.message_ids IS NULL OR r.message_id=ANY(recall_scope.message_ids))
 )
 SELECT b.id,b.record_time,b.thread_id,b.thread_no
 FROM base b JOIN tl.messages x ON x.id=b.id JOIN tl.channels c ON c.id=b.channel_id
 WHERE tl.recall_check(recall_scope.person_role,recall_scope.time_basis)
  AND c.record_kind<>'page_revision'
  AND c.record_kind=ANY(coalesce(recall_scope.kinds,ARRAY['life_event','derived']))
  AND (recall_scope.channels IS NULL OR c.name=ANY(recall_scope.channels))
  AND (recall_scope.thread_ids IS NULL OR b.thread_id=ANY(recall_scope.thread_ids))
  AND (recall_scope.include_outside IS NOT FALSE OR NOT coalesce(b.is_outside,false))
  AND (recall_scope.exclude_message_ids IS NULL OR b.id<>ALL(recall_scope.exclude_message_ids))
  AND (recall_scope.persons IS NULL OR b.person_id=ANY(recall_scope.persons)
   OR (recall_scope.person_role='involved' AND (
    EXISTS(SELECT 1 FROM api.messages o WHERE o.thread_id=b.thread_id AND o.person_id=ANY(recall_scope.persons))
    OR EXISTS(SELECT 1 FROM api.links l WHERE l.status='active' AND (
     (l.from_kind='message' AND l.from_id=b.id AND l.to_kind='person' AND l.to_id=ANY(recall_scope.persons))
     OR (l.to_kind='message' AND l.to_id=b.id AND l.from_kind='person' AND l.from_id=ANY(recall_scope.persons)))))))
  AND tl.recall_time_ok(b.record_time,b.ended_at,x.subject_from,x.subject_to,x.subject_from_precision,x.subject_to_precision,recall_scope.after_at,recall_scope.before_at,recall_scope.time_basis)
  AND (recall_scope.lat IS NULL OR recall_scope.lon IS NULL OR (b.lat IS NOT NULL AND b.lon IS NOT NULL
   AND earth_distance(api.earth_point(recall_scope.lat,recall_scope.lon),api.earth_point(b.lat,b.lon))<=coalesce(recall_scope.meters,100)))
  AND (recall_scope.must_terms IS NULL OR NOT EXISTS(
   SELECT 1 FROM unnest(recall_scope.must_terms) t(term)
   WHERE numnode(plainto_tsquery('english',t.term))>0 AND NOT (x.tsv @@ plainto_tsquery('english',t.term)))) $$;
REVOKE EXECUTE ON FUNCTION tl.recall_overlaps(timestamptz,timestamptz,timestamptz,timestamptz),tl.recall_time_ok(timestamptz,timestamptz,timestamptz,timestamptz,text,text,timestamptz,timestamptz,text),tl.recall_check(text,text),tl.recall_scope(bigint[],text[],text[],bigint[],text,timestamptz,timestamptz,text,float8,float8,float8,bigint[],boolean,timestamptz,timestamptz,text[],bigint[]) FROM PUBLIC;

-- 2. Leg functions -------------------------------------------------------------
-- A one-column RETURNS TABLE would be a set of bigint, not of rows; the
-- query validator treats row-returning functions alike only when their row
-- type is composite, so the row type is declared.
CREATE TYPE api.scope_match AS (message_id bigint);
CREATE FUNCTION api.scope_filter(message_ids bigint[],kinds text[] DEFAULT NULL,channels text[] DEFAULT NULL,persons bigint[] DEFAULT NULL,person_role text DEFAULT 'author',after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,time_basis text DEFAULT 'record',lat float8 DEFAULT NULL,lon float8 DEFAULT NULL,meters float8 DEFAULT NULL,thread_ids bigint[] DEFAULT NULL,include_outside boolean DEFAULT true,as_of timestamptz DEFAULT NULL,known_by timestamptz DEFAULT NULL,must_terms text[] DEFAULT NULL,exclude_message_ids bigint[] DEFAULT NULL)
RETURNS SETOF api.scope_match LANGUAGE sql STABLE AS $$
 SELECT s.message_id FROM tl.recall_scope($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) s ORDER BY s.message_id $$;

-- Keyword ranking of eligible records. Under as_of/known_by every stored
-- version may match on its own text; the scope keeps the version valid then.
CREATE FUNCTION api.search_records(query text,max_results int DEFAULT 20,kinds text[] DEFAULT NULL,channels text[] DEFAULT NULL,persons bigint[] DEFAULT NULL,person_role text DEFAULT 'author',after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,time_basis text DEFAULT 'record',lat float8 DEFAULT NULL,lon float8 DEFAULT NULL,meters float8 DEFAULT NULL,thread_ids bigint[] DEFAULT NULL,include_outside boolean DEFAULT true,as_of timestamptz DEFAULT NULL,known_by timestamptz DEFAULT NULL,must_terms text[] DEFAULT NULL,exclude_message_ids bigint[] DEFAULT NULL,message_ids bigint[] DEFAULT NULL)
RETURNS TABLE(message_id bigint,score real,conversation_id bigint,first_thread_no int,last_thread_no int) LANGUAGE sql STABLE AS $$
 WITH q AS MATERIALIZED (SELECT websearch_to_tsquery('english',coalesce(search_records.query,'')) AS q),
 hits AS MATERIALIZED (
  SELECT m.id,ts_rank_cd(m.tsv,q.q)::real AS score FROM tl.messages m CROSS JOIN q
  WHERE m.tsv @@ q.q
   AND (search_records.message_ids IS NULL OR m.id=ANY(search_records.message_ids))
   AND (search_records.as_of IS NOT NULL OR search_records.known_by IS NOT NULL OR m.superseded_by IS NULL)),
 ok AS MATERIALIZED (
  SELECT s.* FROM tl.recall_scope(ARRAY(SELECT h.id FROM hits h),search_records.kinds,search_records.channels,search_records.persons,search_records.person_role,search_records.after_at,search_records.before_at,search_records.time_basis,search_records.lat,search_records.lon,search_records.meters,search_records.thread_ids,search_records.include_outside,search_records.as_of,search_records.known_by,search_records.must_terms,search_records.exclude_message_ids) s)
 SELECT h.id,h.score,c.id,c.first_thread_no,c.last_thread_no
 FROM hits h JOIN ok ON ok.message_id=h.id
 LEFT JOIN LATERAL (
  SELECT c.id,c.first_thread_no,c.last_thread_no FROM tl.conversations c
  WHERE search_records.as_of IS NULL AND search_records.known_by IS NULL
   AND c.thread_id=ok.thread_id AND ok.thread_no BETWEEN c.first_thread_no AND c.last_thread_no
  ORDER BY c.first_thread_no,c.id LIMIT 1) c ON true
 ORDER BY h.score DESC,h.id LIMIT search_records.max_results $$;

-- Exact cosine over conversations containing an eligible record; one
-- representative eligible message per conversation (query_text keyword rank,
-- then id). Embeddings exist for current text only.
CREATE FUNCTION api.vector_records(query_embedding vector,max_results int DEFAULT 20,kinds text[] DEFAULT NULL,channels text[] DEFAULT NULL,persons bigint[] DEFAULT NULL,person_role text DEFAULT 'author',after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,time_basis text DEFAULT 'record',lat float8 DEFAULT NULL,lon float8 DEFAULT NULL,meters float8 DEFAULT NULL,thread_ids bigint[] DEFAULT NULL,include_outside boolean DEFAULT true,as_of timestamptz DEFAULT NULL,known_by timestamptz DEFAULT NULL,must_terms text[] DEFAULT NULL,exclude_message_ids bigint[] DEFAULT NULL,message_ids bigint[] DEFAULT NULL,query_text text DEFAULT '',serving_model text DEFAULT NULL)
RETURNS TABLE(message_id bigint,score real,conversation_id bigint,first_thread_no int,last_thread_no int) LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
BEGIN
 IF vector_records.as_of IS NOT NULL OR vector_records.known_by IS NOT NULL THEN
  RAISE EXCEPTION 'vector_records: embeddings exist for current text only; as_of and known_by are not supported' USING ERRCODE='feature_not_supported';
 END IF;
 RETURN QUERY
 WITH eligible AS MATERIALIZED (
  SELECT s.message_id AS id,c.id AS cid,c.first_thread_no AS first_no,c.last_thread_no AS last_no,x.tsv
  FROM tl.recall_scope(vector_records.message_ids,vector_records.kinds,vector_records.channels,vector_records.persons,vector_records.person_role,vector_records.after_at,vector_records.before_at,vector_records.time_basis,vector_records.lat,vector_records.lon,vector_records.meters,vector_records.thread_ids,vector_records.include_outside,NULL::timestamptz,NULL::timestamptz,vector_records.must_terms,vector_records.exclude_message_ids) s
  JOIN tl.conversations c ON c.thread_id=s.thread_id AND s.thread_no BETWEEN c.first_thread_no AND c.last_thread_no
  JOIN tl.messages x ON x.id=s.message_id),
 ranked AS MATERIALIZED (
  SELECT e.conversation_id AS cid,(1-(e.embedding <=> vector_records.query_embedding))::real AS sim
  FROM tl.embeddings e
  JOIN tl.embedding_models model ON model.model=e.model AND ((vector_records.serving_model IS NULL AND model.active) OR model.model=vector_records.serving_model)
  JOIN tl.conversations c ON c.id=e.conversation_id AND c.text_hash=e.text_hash
  WHERE EXISTS(SELECT 1 FROM eligible g WHERE g.cid=c.id))
 SELECT p.id,r.sim,p.cid,p.first_no,p.last_no FROM ranked r
 JOIN LATERAL (SELECT g.* FROM eligible g WHERE g.cid=r.cid ORDER BY ts_rank_cd(g.tsv,websearch_to_tsquery('english',coalesce(vector_records.query_text,''))) DESC,g.id LIMIT 1) p ON true
 ORDER BY r.sim DESC,p.id LIMIT vector_records.max_results;
END $$;

-- Exhaustive enumeration of the scope (max_results NULL = all), by time.
CREATE FUNCTION api.list_records(kinds text[] DEFAULT NULL,channels text[] DEFAULT NULL,persons bigint[] DEFAULT NULL,person_role text DEFAULT 'author',after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,time_basis text DEFAULT 'record',lat float8 DEFAULT NULL,lon float8 DEFAULT NULL,meters float8 DEFAULT NULL,thread_ids bigint[] DEFAULT NULL,include_outside boolean DEFAULT true,as_of timestamptz DEFAULT NULL,known_by timestamptz DEFAULT NULL,must_terms text[] DEFAULT NULL,exclude_message_ids bigint[] DEFAULT NULL,message_ids bigint[] DEFAULT NULL,max_results int DEFAULT NULL)
RETURNS TABLE(message_id bigint,record_time timestamptz,thread_id bigint,thread_no int) LANGUAGE sql STABLE AS $$
 SELECT s.message_id,s.record_time,s.thread_id,s.thread_no
 FROM tl.recall_scope($17,$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16) s
 ORDER BY s.record_time,s.message_id LIMIT $18 $$;

-- Entity and person candidates for a name: trigram similarity, substring and
-- exact identifier. Alternatives are listed side by side, never merged.
-- Reads the gated api.entities / api.people views, so a restricted asker
-- gets nothing.
CREATE FUNCTION api.entity_search(query text,max_results int DEFAULT 20)
RETURNS TABLE(kind text,id bigint,name text,score real) LANGUAGE sql STABLE AS $$
 WITH q AS (SELECT btrim(coalesce(entity_search.query,'')) AS q),
 ent AS (
  SELECT e.kind,e.id,e.name,greatest(
   similarity(e.name,q.q),
   CASE WHEN strpos(lower(e.name),lower(q.q))>0 THEN 0.5 ELSE 0 END,
   CASE WHEN lower(e.name)=lower(q.q) OR e.external_ref=q.q THEN 1 ELSE 0 END)::real AS score
  FROM api.entities e CROSS JOIN q
  WHERE q.q<>'' AND e.merged_into IS NULL AND e.name IS NOT NULL),
 names AS (
  SELECT p.id,p.handle AS name FROM api.people p WHERE p.merged_into IS NULL
  UNION ALL SELECT p.id,p.display_name FROM api.people p WHERE p.merged_into IS NULL AND p.display_name IS NOT NULL
  UNION ALL SELECT n.person_id,n.name FROM api.person_names n JOIN api.people p ON p.id=n.person_id WHERE p.merged_into IS NULL),
 ppl AS (
  SELECT 'person'::text AS kind,p.id,coalesce(p.display_name,p.handle) AS name,greatest(
   coalesce((SELECT max(greatest(similarity(n.name,q.q),CASE WHEN strpos(lower(n.name),lower(q.q))>0 THEN 0.5 ELSE 0 END,CASE WHEN lower(n.name)=lower(q.q) THEN 1 ELSE 0 END)) FROM names n WHERE n.id=p.id),0),
   CASE WHEN EXISTS(SELECT 1 FROM api.identifiers i WHERE i.person_id=p.id AND (i.value=q.q OR i.value_norm=lower(q.q))) THEN 1 ELSE 0 END)::real AS score
  FROM api.people p CROSS JOIN q
  WHERE q.q<>'' AND p.merged_into IS NULL)
 SELECT x.kind,x.id,x.name,x.score FROM (SELECT * FROM ent UNION ALL SELECT * FROM ppl) x
 WHERE x.score>=0.3
 ORDER BY x.score DESC,x.kind,x.id LIMIT entity_search.max_results $$;

-- Context windows for many anchors in one statement (current versions).
CREATE FUNCTION api.context_many(message_ids bigint[],before int DEFAULT 3,after int DEFAULT 3)
RETURNS TABLE(anchor_id bigint,message_id bigint,thread_id bigint,thread_no int) LANGUAGE sql STABLE AS $$
 SELECT i.id,m.id,m.thread_id,m.thread_no
 FROM unnest(context_many.message_ids) WITH ORDINALITY AS i(id,ord)
 JOIN api.messages a ON a.id=api.current_message(i.id)
 JOIN api.messages m ON m.thread_id=a.thread_id
  AND m.thread_no BETWEEN a.thread_no-least(greatest(context_many.before,0),50) AND a.thread_no+least(greatest(context_many.after,0),50)
 ORDER BY i.ord,m.thread_no $$;

-- 3. Link and entity records (RECORD CONTRACT) -------------------------------
-- A link record is visible exactly when its link is: the gate is the stored
-- api.links rule, read through tl.links_gated so a later re-install of the
-- links gate applies here unchanged. Names of entity and person ends are
-- shown to unrestricted askers only. Message ends are pinned (the version the
-- link was made with when that is recorded).
DO $$
DECLARE pin_from text := 'l.from_id'; pin_to text := 'l.to_id';
BEGIN
 IF EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid='tl.links'::regclass AND attname='from_version_id' AND NOT attisdropped) THEN
  pin_from := 'coalesce(l.from_version_id,l.from_id)';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid='tl.links'::regclass AND attname='to_version_id' AND NOT attisdropped) THEN
  pin_to := 'coalesce(l.to_version_id,l.to_id)';
 END IF;
 PERFORM tl.install_gate('link_records','EXISTS(SELECT 1 FROM tl.links_gated g WHERE g.id=v.ref_id)',format($def$
SELECT 'l:'||l.id AS record_id,
 'link'::text AS kind,
 l.id AS ref_id,
 NULL::bigint AS message_id,
 NULL::int AS version,
 coalesce(l.status,'active')='active' AS is_current,
 NULL::text AS channel,
 NULL::text AS external_id,
 NULL::bigint AS thread_id,
 NULL::int AS thread_no,
 l.type||': '||f.label||' -> '||t.label AS content,
 l.created_at AS record_time,
 NULL::timestamptz AS ended_at,
 l.created_at AS ingested_at,
 NULL::timestamptz AS subject_from,
 NULL::timestamptz AS subject_to,
 NULL::text AS subject_from_precision,
 NULL::text AS subject_to_precision,
 NULL::bigint AS person_id,
 NULL::text AS author_handle,
 NULL::double precision AS lat,
 NULL::double precision AS lon,
 'not_applicable'::text AS loc_reason,
 false AS is_outside,
 NULL::text[] AS heading_path,
 NULL::int AS char_start,
 NULL::int AS char_end,
 NULL::jsonb AS claim,
 l.method IN ('rule','model') AS generated,
 l.method IN ('rule','model') AND s.support='[]'::jsonb AS unsupported,
 jsonb_build_object('method',l.method,'confidence',l.confidence,'job',l.job,'job_version',l.job_version,'type',l.type) AS provenance,
 s.support,
 jsonb_build_object('link',l.id,'from',jsonb_build_object('kind',l.from_kind,'id',l.from_id),'to',jsonb_build_object('kind',l.to_kind,'id',l.to_id)) AS expansion
FROM tl.links l
CROSS JOIN LATERAL (SELECT CASE
  WHEN l.from_kind='entity' AND (SELECT api.asker_unrestricted()) THEN coalesce((SELECT e.name FROM tl.entities e WHERE e.id=l.from_id),'entity '||l.from_id)
  WHEN l.from_kind='person' AND (SELECT api.asker_unrestricted()) THEN coalesce((SELECT coalesce(p.display_name,p.handle) FROM tl.people p WHERE p.id=l.from_id),'person '||l.from_id)
  ELSE l.from_kind||' '||l.from_id END AS label) f
CROSS JOIN LATERAL (SELECT CASE
  WHEN l.to_kind='entity' AND (SELECT api.asker_unrestricted()) THEN coalesce((SELECT e.name FROM tl.entities e WHERE e.id=l.to_id),'entity '||l.to_id)
  WHEN l.to_kind='person' AND (SELECT api.asker_unrestricted()) THEN coalesce((SELECT coalesce(p.display_name,p.handle) FROM tl.people p WHERE p.id=l.to_id),'person '||l.to_id)
  ELSE l.to_kind||' '||l.to_id END AS label) t
CROSS JOIN LATERAL (
 SELECT coalesce(jsonb_agg(jsonb_build_object('message_id',x.id,'version',x.version,'relation',e.relation) ORDER BY e.ord,x.id),'[]'::jsonb) AS support
 FROM (
  SELECT %s AS mid,'from'::text AS relation,1 AS ord WHERE l.from_kind='message'
  UNION ALL SELECT %s,'to',2 WHERE l.to_kind='message'
  UNION ALL SELECT j.value::text::bigint,'evidence',3
   FROM jsonb_array_elements(CASE WHEN jsonb_typeof(l.evidence->'message_ids')='array' THEN l.evidence->'message_ids' ELSE '[]'::jsonb END) j
   WHERE jsonb_typeof(j.value)='number' AND j.value::text ~ '^[0-9]{1,18}$'
 ) e JOIN tl.messages x ON x.id=e.mid) s
$def$,pin_from,pin_to));
END $$;

-- Entities have no visible-source proof: unrestricted askers only.
SELECT tl.install_gate('entity_records','false',$def$
SELECT 'e:'||e.id AS record_id,
 'entity'::text AS kind,
 e.id AS ref_id,
 NULL::bigint AS message_id,
 NULL::int AS version,
 e.merged_into IS NULL AS is_current,
 NULL::text AS channel,
 e.external_ref AS external_id,
 NULL::bigint AS thread_id,
 NULL::int AS thread_no,
 coalesce(e.name,'')||' ('||e.kind||')' AS content,
 NULL::timestamptz AS record_time,
 NULL::timestamptz AS ended_at,
 NULL::timestamptz AS ingested_at,
 NULL::timestamptz AS subject_from,
 NULL::timestamptz AS subject_to,
 NULL::text AS subject_from_precision,
 NULL::text AS subject_to_precision,
 NULL::bigint AS person_id,
 NULL::text AS author_handle,
 CASE WHEN jsonb_typeof(e.metadata->'lat')='number' THEN (e.metadata->>'lat')::double precision END AS lat,
 CASE WHEN jsonb_typeof(e.metadata->'lon')='number' THEN (e.metadata->>'lon')::double precision END AS lon,
 CASE WHEN jsonb_typeof(e.metadata->'lat')='number' AND jsonb_typeof(e.metadata->'lon')='number' THEN NULL ELSE 'not_applicable' END::text AS loc_reason,
 false AS is_outside,
 NULL::text[] AS heading_path,
 NULL::int AS char_start,
 NULL::int AS char_end,
 NULL::jsonb AS claim,
 false AS generated,
 false AS unsupported,
 jsonb_build_object('entity_kind',e.kind,'external_ref',e.external_ref,'is_placeholder',e.is_placeholder,'method','registry') AS provenance,
 '[]'::jsonb AS support,
 jsonb_build_object('entity',e.id,'kind',e.kind) AS expansion
FROM tl.entities e
$def$);
INSERT INTO tl.record_sources VALUES ('l','api.link_records'),('e','api.entity_records') ON CONFLICT (prefix) DO NOTHING;

-- 4. Registry, allow-list and catalogue ---------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('scope_filter','sql','scope_filter','The message ids of an array that satisfy a hard scope (kinds, channels, persons with role, time with time basis, place, threads, outside, as_of/known_by, must_terms, exclusions); NULL ids = the whole scope','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.scope_filter(ARRAY[1,2]::bigint[])','1',NULL,NULL),
 ('search_records','sql','search_records','Keyword ranking of the records in a hard scope; the scope is applied before ranking and the limit; as_of matches each version on its own text','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.search_records(''orchard'',20)','1',2,20),
 ('vector_records','sql','vector_records','Exact cosine ranking of conversations containing a record in a hard scope, one representative per conversation; current text only (as_of/known_by raise)','approximate','embedding',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.vector_records(''[1,0,0]'',10)','1',2,20),
 ('list_records','sql','list_records','Exhaustive enumeration of the records in a hard scope by record time; NULL max_results returns all','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.list_records(NULL,ARRAY[''synthetic.chat''])','1',NULL,NULL),
 ('entity_search','sql','entity_search','Entity and person candidates for a name by trigram similarity, substring and exact identifier; alternatives are never merged','approximate','free',1,'owner, agent; unrestricted askers only','SELECT * FROM api.entity_search(''Synthetic place'',20)','1',2,20),
 ('context_many','sql','context_many','Thread context windows around many anchor messages in one call','exact','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.context_many(ARRAY[1,2]::bigint[],2,2)','1',NULL,NULL);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('scope_filter','search_records','vector_records','list_records','entity_search','context_many');

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('recall',jsonb_build_object(
  'description','One retrieval interface over messages, entities and links (POST /v1/recall, throughline lib recall). Legs run concurrently, each one validated statement under the asker scope; results are fused by reciprocal rank and hydrated through the record contract.',
  'interface','POST /v1/recall {query, scope:{persons, person_role, channels, kinds, after, before, time_basis, near:{lat,lon,meters}, place:{name,meters}, thread_ids, message_ids, as_of, known_by, include_reference, include_outside, must_terms, min_score}, hints, k, strategy: auto|keyword|meaning|fused, legs, ranker, rrf_k, expand, raw_only, budgets, save, session, starting_sets, exclude_seen, group_by, exhaustive, trace}',
  'arguments',jsonb_build_object(
   'kinds','Message record kinds. Default life_event and derived; reference only when listed; page_revision is never returned by message functions (pages come from passages).',
   'person_role','author (default) or involved: author, a message by the person in the same thread, or an active link to the person.',
   'time_basis','record (default): [record_time, ended_at]; subject: the subject interval, a missing or unknown bound is open, no interval falls back to record time; either.',
   'place','lat, lon, meters (default 100). A place name is resolved through api.places; an unresolvable place or person returns an empty result with a reason, never an unscoped one.',
   'as_of','Record-time cutoff; known_by: ingest-time cutoff. The eligible set is api.records_asof, each version matched on its own text. The vector leg is skipped (embeddings are current only) and the response lists which legs honoured as_of.',
   'must_terms','Every term must match the record''s own text.',
   'exclude_message_ids','Removed before ranking, so k results are still returned.'),
  'functions',jsonb_build_array('scope_filter','search_records','vector_records','list_records','entity_search','context_many'),
  'records',jsonb_build_object('m','api.record_versions','l','api.link_records','e','api.entity_records (unrestricted askers only)'),
  'guarantees',jsonb_build_array(
   'The hard scope is applied before ranking and before every limit, in every leg and in every expansion that creates a hit.',
   'A ranked result is a sample: completeness is ranked_sample. Counts and aggregates need exhaustive=true (completeness exhaustive, or truncated when a cap or budget cut it) or SQL over api.list_records with a NULL limit.',
   'discovery says why a record was retrieved (leg, rank, score); support says which original messages it may be cited for. They are separate.',
   'A failed, unavailable or timed-out leg or stage degrades the response (execution.incomplete); it never fails it.',
   'recall never calls a semantic model or a controller.'),
  'levels','Recall legs are level 1; stages (spans, rerank, dedup, select, group) are level 2; the librarian is level 3.'));
