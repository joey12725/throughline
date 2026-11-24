-- Agent retrieval (2026-11-21).
-- 1. vector_records (keeping 0051's api.conversation_visible gate): map each eligible message to its conversation with a
--    LATERAL lookup on (thread_id, first_thread_no) instead of a BETWEEN join
--    (92 s -> 8 s on the full archive; the vector leg timed out before).
-- 2. search_records: a natural-language query rarely has every word in one
--    short message, so the keyword leg returned nothing for most questions.
--    When the all-terms (AND) match finds fewer than max_results, a ranked
--    any-terms (OR) match fills the rest, scored by rank times the share of
--    query terms matched and always below every AND hit.
CREATE INDEX IF NOT EXISTS conversations_thread_first ON tl.conversations(thread_id, first_thread_no);
CREATE OR REPLACE FUNCTION api.vector_records(query_embedding vector, max_results integer DEFAULT 20, kinds text[] DEFAULT NULL::text[], channels text[] DEFAULT NULL::text[], persons bigint[] DEFAULT NULL::bigint[], person_role text DEFAULT 'author'::text, after_at timestamp with time zone DEFAULT NULL::timestamp with time zone, before_at timestamp with time zone DEFAULT NULL::timestamp with time zone, time_basis text DEFAULT 'record'::text, lat double precision DEFAULT NULL::double precision, lon double precision DEFAULT NULL::double precision, meters double precision DEFAULT NULL::double precision, thread_ids bigint[] DEFAULT NULL::bigint[], include_outside boolean DEFAULT true, as_of timestamp with time zone DEFAULT NULL::timestamp with time zone, known_by timestamp with time zone DEFAULT NULL::timestamp with time zone, must_terms text[] DEFAULT NULL::text[], exclude_message_ids bigint[] DEFAULT NULL::bigint[], message_ids bigint[] DEFAULT NULL::bigint[], query_text text DEFAULT ''::text, serving_model text DEFAULT NULL::text)
 RETURNS TABLE(message_id bigint, score real, conversation_id bigint, first_thread_no integer, last_thread_no integer)
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'api', 'public'
AS $function$
#variable_conflict use_column
BEGIN
 IF vector_records.as_of IS NOT NULL OR vector_records.known_by IS NOT NULL THEN
  RAISE EXCEPTION 'vector_records: embeddings exist for current text only; as_of and known_by are not supported' USING ERRCODE='feature_not_supported';
 END IF;
 RETURN QUERY
 WITH eligible AS MATERIALIZED (
  SELECT s.message_id AS id,c.id AS cid,c.first_thread_no AS first_no,c.last_thread_no AS last_no,x.tsv
  FROM tl.recall_scope(vector_records.message_ids,vector_records.kinds,vector_records.channels,vector_records.persons,vector_records.person_role,vector_records.after_at,vector_records.before_at,vector_records.time_basis,vector_records.lat,vector_records.lon,vector_records.meters,vector_records.thread_ids,vector_records.include_outside,NULL::timestamptz,NULL::timestamptz,vector_records.must_terms,vector_records.exclude_message_ids) s
  JOIN LATERAL (SELECT k.* FROM (SELECT c0.id,c0.first_thread_no,c0.last_thread_no FROM tl.conversations c0
   WHERE c0.thread_id=s.thread_id AND c0.first_thread_no<=s.thread_no
   ORDER BY c0.first_thread_no DESC LIMIT 4) k WHERE k.last_thread_no>=s.thread_no) c ON true
  JOIN tl.messages x ON x.id=s.message_id),
 ranked AS MATERIALIZED (
  SELECT n.cid,n.sim FROM tl.nearest_conversations(vector_records.query_embedding,
   (SELECT model.model FROM tl.embedding_models model WHERE (vector_records.serving_model IS NULL AND model.active) OR model.model=vector_records.serving_model LIMIT 1),
   vector_records.max_results,
   ARRAY(SELECT c.id FROM tl.conversations c WHERE EXISTS(SELECT 1 FROM eligible g WHERE g.cid=c.id) AND api.conversation_visible(c.thread_id,c.first_thread_no,c.last_thread_no))) n)
 SELECT p.id,r.sim,p.cid,p.first_no,p.last_no FROM ranked r
 JOIN LATERAL (SELECT g.* FROM eligible g WHERE g.cid=r.cid ORDER BY ts_rank_cd(g.tsv,websearch_to_tsquery('english',coalesce(vector_records.query_text,''))) DESC,g.id LIMIT 1) p ON true
 ORDER BY r.sim DESC,p.id LIMIT vector_records.max_results;
END $function$

;

CREATE OR REPLACE FUNCTION api.search_records(query text, max_results integer DEFAULT 20, kinds text[] DEFAULT NULL::text[], channels text[] DEFAULT NULL::text[], persons bigint[] DEFAULT NULL::bigint[], person_role text DEFAULT 'author'::text, after_at timestamp with time zone DEFAULT NULL::timestamp with time zone, before_at timestamp with time zone DEFAULT NULL::timestamp with time zone, time_basis text DEFAULT 'record'::text, lat double precision DEFAULT NULL::double precision, lon double precision DEFAULT NULL::double precision, meters double precision DEFAULT NULL::double precision, thread_ids bigint[] DEFAULT NULL::bigint[], include_outside boolean DEFAULT true, as_of timestamp with time zone DEFAULT NULL::timestamp with time zone, known_by timestamp with time zone DEFAULT NULL::timestamp with time zone, must_terms text[] DEFAULT NULL::text[], exclude_message_ids bigint[] DEFAULT NULL::bigint[], message_ids bigint[] DEFAULT NULL::bigint[])
 RETURNS TABLE(message_id bigint, score real, conversation_id bigint, first_thread_no integer, last_thread_no integer)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'api', 'public'
AS $function$
 WITH q0 AS MATERIALIZED (SELECT websearch_to_tsquery('english',coalesce(search_records.query,'')) AS q),
 q AS MATERIALIZED (
  SELECT q0.q,
   CASE WHEN q0.q::text ~ '[|!()]' OR position(' & ' in q0.q::text)=0 THEN NULL::tsquery[]
        ELSE ARRAY(SELECT x::tsquery FROM regexp_split_to_table(q0.q::text,' & ') x) END AS atoms
  FROM q0),
 hits_and AS MATERIALIZED (
  SELECT m.id,ts_rank_cd(m.tsv,q.q)::real AS score,0 AS tier FROM tl.messages m CROSS JOIN q
  WHERE m.tsv @@ q.q
   AND (search_records.message_ids IS NULL OR m.id=ANY(search_records.message_ids))
   AND (search_records.as_of IS NOT NULL OR search_records.known_by IS NOT NULL OR m.superseded_by IS NULL)),
 or_q AS MATERIALIZED (
  SELECT (SELECT string_agg('('||a::text||')',' | ') FROM unnest(q.atoms) a)::tsquery AS q, q.atoms
  FROM q WHERE q.atoms IS NOT NULL AND (SELECT count(*) FROM hits_and) < search_records.max_results*4),
 hits_or AS MATERIALIZED (
  SELECT t.id,t.score,1 AS tier FROM (
   SELECT m.id,
    (ts_rank_cd(m.tsv,o.q) * (SELECT count(*) FROM unnest(o.atoms) a WHERE m.tsv @@ a)::real/cardinality(o.atoms))::real AS score,
    (SELECT count(*) FROM unnest(o.atoms) a WHERE m.tsv @@ a) AS matched, cardinality(o.atoms) AS n
   FROM tl.messages m CROSS JOIN or_q o
   WHERE m.tsv @@ o.q
    AND NOT EXISTS (SELECT 1 FROM hits_and h WHERE h.id=m.id)
    AND (search_records.message_ids IS NULL OR m.id=ANY(search_records.message_ids))
    AND (search_records.as_of IS NOT NULL OR search_records.known_by IS NOT NULL OR m.superseded_by IS NULL)) t
  WHERE t.matched >= least(2,t.n)
  ORDER BY t.score DESC,t.id LIMIT 5000),
 hits AS MATERIALIZED (SELECT * FROM hits_and UNION ALL SELECT * FROM hits_or),
 ok AS MATERIALIZED (
  SELECT s.* FROM tl.recall_scope(ARRAY(SELECT h.id FROM hits h),search_records.kinds,search_records.channels,search_records.persons,search_records.person_role,search_records.after_at,search_records.before_at,search_records.time_basis,search_records.lat,search_records.lon,search_records.meters,search_records.thread_ids,search_records.include_outside,search_records.as_of,search_records.known_by,search_records.must_terms,search_records.exclude_message_ids) s)
 SELECT h.id,h.score,c.id,c.first_thread_no,c.last_thread_no
 FROM hits h JOIN ok ON ok.message_id=h.id
 LEFT JOIN LATERAL (
  SELECT c.id,c.first_thread_no,c.last_thread_no FROM tl.conversations c
  WHERE search_records.as_of IS NULL AND search_records.known_by IS NULL
   AND c.thread_id=ok.thread_id AND c.first_thread_no<=ok.thread_no AND c.last_thread_no>=ok.thread_no
  ORDER BY c.first_thread_no,c.id LIMIT 1) c ON true
 ORDER BY h.tier,h.score DESC,h.id LIMIT search_records.max_results $function$;

-- 3. vector_records, approximate first: building the eligible set of the
--    whole archive (scope, conversation and text of every message) cost about
--    10 s per call. The HNSW index is asked first for 4x max_results nearest
--    conversations; only their messages go through the hard scope. When that
--    yields max_results conversations they are the answer (same scoring and
--    representative choice as the exact path); otherwise the exact path runs.
CREATE OR REPLACE FUNCTION api.vector_records(query_embedding vector, max_results integer DEFAULT 20, kinds text[] DEFAULT NULL::text[], channels text[] DEFAULT NULL::text[], persons bigint[] DEFAULT NULL::bigint[], person_role text DEFAULT 'author'::text, after_at timestamp with time zone DEFAULT NULL::timestamp with time zone, before_at timestamp with time zone DEFAULT NULL::timestamp with time zone, time_basis text DEFAULT 'record'::text, lat double precision DEFAULT NULL::double precision, lon double precision DEFAULT NULL::double precision, meters double precision DEFAULT NULL::double precision, thread_ids bigint[] DEFAULT NULL::bigint[], include_outside boolean DEFAULT true, as_of timestamp with time zone DEFAULT NULL::timestamp with time zone, known_by timestamp with time zone DEFAULT NULL::timestamp with time zone, must_terms text[] DEFAULT NULL::text[], exclude_message_ids bigint[] DEFAULT NULL::bigint[], message_ids bigint[] DEFAULT NULL::bigint[], query_text text DEFAULT ''::text, serving_model text DEFAULT NULL::text)
 RETURNS TABLE(message_id bigint, score real, conversation_id bigint, first_thread_no integer, last_thread_no integer)
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'api', 'public'
 SET "hnsw.iterative_scan" TO 'relaxed_order'
 SET "hnsw.ef_search" TO '200'
AS $function$
#variable_conflict use_column
DECLARE
 model_name text; dims int := vector_dims(query_embedding); cids bigint[]; sims real[];
 near_ids bigint[];
 f_ids bigint[]; f_sims real[]; f_cids bigint[]; f_first int[]; f_last int[];
BEGIN
 IF vector_records.as_of IS NOT NULL OR vector_records.known_by IS NOT NULL THEN
  RAISE EXCEPTION 'vector_records: embeddings exist for current text only; as_of and known_by are not supported' USING ERRCODE='feature_not_supported';
 END IF;
 SELECT model.model INTO model_name FROM tl.embedding_models model
  WHERE (vector_records.serving_model IS NULL AND model.active) OR model.model=vector_records.serving_model LIMIT 1;
 IF model_name IS NOT NULL AND coalesce(vector_records.max_results,0) > 0
   AND to_regclass(format('tl.%I',tl.embedding_index_name('embeddings',model_name,dims))) IS NOT NULL THEN
  EXECUTE format($f$SELECT array_agg(x.id),array_agg(x.s) FROM (
    SELECT e.conversation_id AS id,(1-(e.embedding <=> $1))::real AS s FROM tl.embeddings e
    WHERE e.model=%L AND vector_dims(e.embedding)=%s
     AND EXISTS(SELECT 1 FROM tl.conversations c WHERE c.id=e.conversation_id AND c.text_hash=e.text_hash)
    ORDER BY e.embedding::halfvec(%s) <=> $1::halfvec(%s) LIMIT $2) x$f$,model_name,dims,dims,dims)
   INTO cids,sims USING query_embedding,greatest(vector_records.max_results*4,40);
  IF coalesce(cardinality(cids),0) > 0 THEN
   SELECT array_agg(m.id) INTO near_ids
    FROM tl.conversations c JOIN tl.messages m ON m.thread_id=c.thread_id
     AND m.thread_no BETWEEN c.first_thread_no AND c.last_thread_no
    WHERE c.id=ANY(cids)
     AND (vector_records.message_ids IS NULL OR m.id=ANY(vector_records.message_ids));
   SELECT array_agg(t.id ORDER BY t.sim DESC,t.id),array_agg(t.sim ORDER BY t.sim DESC,t.id),
          array_agg(t.cid ORDER BY t.sim DESC,t.id),array_agg(t.first_no ORDER BY t.sim DESC,t.id),
          array_agg(t.last_no ORDER BY t.sim DESC,t.id)
    INTO f_ids,f_sims,f_cids,f_first,f_last
   FROM (
   WITH near AS (SELECT u.id AS cid,u.s AS sim FROM unnest(cids,sims) u(id,s)
    JOIN tl.conversations c ON c.id=u.id
    WHERE api.conversation_visible(c.thread_id,c.first_thread_no,c.last_thread_no)),
   eligible AS MATERIALIZED (
    SELECT s.message_id AS id,c.id AS cid,c.first_thread_no AS first_no,c.last_thread_no AS last_no,x.tsv
    FROM tl.recall_scope(near_ids,vector_records.kinds,vector_records.channels,vector_records.persons,vector_records.person_role,vector_records.after_at,vector_records.before_at,vector_records.time_basis,vector_records.lat,vector_records.lon,vector_records.meters,vector_records.thread_ids,vector_records.include_outside,NULL::timestamptz,NULL::timestamptz,vector_records.must_terms,vector_records.exclude_message_ids) s
    JOIN tl.conversations c ON c.id=ANY(cids) AND c.thread_id=s.thread_id AND s.thread_no BETWEEN c.first_thread_no AND c.last_thread_no
    JOIN tl.messages x ON x.id=s.message_id)
   SELECT p.id,r.sim,p.cid,p.first_no,p.last_no FROM near r
   JOIN LATERAL (SELECT g.* FROM eligible g WHERE g.cid=r.cid ORDER BY ts_rank_cd(g.tsv,websearch_to_tsquery('english',coalesce(vector_records.query_text,''))) DESC,g.id LIMIT 1) p ON true
   ORDER BY r.sim DESC,p.id LIMIT vector_records.max_results) t;
   IF coalesce(cardinality(f_ids),0) >= vector_records.max_results THEN
    RETURN QUERY SELECT * FROM unnest(f_ids,f_sims,f_cids,f_first,f_last);
    RETURN;
   END IF;
  END IF;
 END IF;
 RETURN QUERY
 WITH eligible AS MATERIALIZED (
  SELECT s.message_id AS id,c.id AS cid,c.first_thread_no AS first_no,c.last_thread_no AS last_no,x.tsv
  FROM tl.recall_scope(vector_records.message_ids,vector_records.kinds,vector_records.channels,vector_records.persons,vector_records.person_role,vector_records.after_at,vector_records.before_at,vector_records.time_basis,vector_records.lat,vector_records.lon,vector_records.meters,vector_records.thread_ids,vector_records.include_outside,NULL::timestamptz,NULL::timestamptz,vector_records.must_terms,vector_records.exclude_message_ids) s
  JOIN LATERAL (SELECT k.* FROM (SELECT c0.id,c0.first_thread_no,c0.last_thread_no FROM tl.conversations c0
   WHERE c0.thread_id=s.thread_id AND c0.first_thread_no<=s.thread_no
   ORDER BY c0.first_thread_no DESC LIMIT 4) k WHERE k.last_thread_no>=s.thread_no) c ON true
  JOIN tl.messages x ON x.id=s.message_id),
 ranked AS MATERIALIZED (
  SELECT n.cid,n.sim FROM tl.nearest_conversations(vector_records.query_embedding,model_name,
   vector_records.max_results,
   ARRAY(SELECT c.id FROM tl.conversations c WHERE EXISTS(SELECT 1 FROM eligible g WHERE g.cid=c.id) AND api.conversation_visible(c.thread_id,c.first_thread_no,c.last_thread_no))) n)
 SELECT p.id,r.sim,p.cid,p.first_no,p.last_no FROM ranked r
 JOIN LATERAL (SELECT g.* FROM eligible g WHERE g.cid=r.cid ORDER BY ts_rank_cd(g.tsv,websearch_to_tsquery('english',coalesce(vector_records.query_text,''))) DESC,g.id LIMIT 1) p ON true
 ORDER BY r.sim DESC,p.id LIMIT vector_records.max_results;
END $function$;

-- 4. recall_scope: "(message_ids IS NULL OR m.id = ANY(message_ids))" cannot
--    use an index, so every call with candidate ids scanned the whole
--    archive comparing each row with every id (about 1.5 ms per candidate).
--    The two cases are separate branches now; the id branch looks each id up.
CREATE OR REPLACE FUNCTION tl.recall_scope(message_ids bigint[], kinds text[], channels text[], persons bigint[], person_role text, after_at timestamp with time zone, before_at timestamp with time zone, time_basis text, lat double precision, lon double precision, meters double precision, thread_ids bigint[], include_outside boolean, as_of timestamp with time zone, known_by timestamp with time zone, must_terms text[], exclude_message_ids bigint[])
 RETURNS TABLE(message_id bigint, record_time timestamp with time zone, thread_id bigint, thread_no integer)
 LANGUAGE sql
 STABLE
 SET search_path TO 'pg_catalog', 'api', 'public'
AS $function$
 WITH base AS (
  SELECT m.id,m.channel_id,m.thread_id,m.thread_no,m.happened_at AS record_time,m.ended_at,m.person_id,m.lat,m.lon,m.is_outside
  FROM unnest(recall_scope.message_ids) u(id)
  CROSS JOIN LATERAL (SELECT v.* FROM api.message_versions v WHERE v.id=u.id OFFSET 0) m
  WHERE recall_scope.as_of IS NULL AND recall_scope.known_by IS NULL AND m.superseded_by IS NULL
   AND recall_scope.message_ids IS NOT NULL
  UNION ALL
  SELECT m.id,m.channel_id,m.thread_id,m.thread_no,m.happened_at AS record_time,m.ended_at,m.person_id,m.lat,m.lon,m.is_outside
  FROM api.message_versions m
  WHERE recall_scope.as_of IS NULL AND recall_scope.known_by IS NULL AND m.superseded_by IS NULL
   AND recall_scope.message_ids IS NULL
  UNION ALL
  SELECT r.message_id,x.channel_id,r.thread_id,r.thread_no,r.record_time,r.ended_at,r.person_id,r.lat,r.lon,r.is_outside
  FROM api.records_asof(coalesce(recall_scope.as_of,'infinity'::timestamptz),recall_scope.known_by) r
  JOIN tl.messages x ON x.id=r.message_id
  WHERE (recall_scope.as_of IS NOT NULL OR recall_scope.known_by IS NOT NULL)
   AND (recall_scope.message_ids IS NULL OR r.message_id=ANY(recall_scope.message_ids))
 ), opts AS MATERIALIZED (
  SELECT coalesce('ignored'=ANY(recall_scope.kinds),false) AS include_ignored,
   CASE WHEN recall_scope.kinds IS NULL THEN ARRAY['life_event','derived']
    WHEN 'ignored'=ANY(recall_scope.kinds) AND cardinality(array_remove(recall_scope.kinds,'ignored'))=0 THEN ARRAY['life_event','derived']
    ELSE array_remove(recall_scope.kinds,'ignored') END AS kinds,
   coalesce(recall_scope.time_basis,'record')='effective' AS effective
 )
 SELECT b.id,b.record_time,b.thread_id,b.thread_no
 FROM base b JOIN tl.messages x ON x.id=b.id JOIN tl.channels c ON c.id=b.channel_id CROSS JOIN opts
 -- The tl.overlay_state rule, evaluated in line: a per-row call of the
 -- SECURITY DEFINER helper costs ten times the work itself. CASE guarantees
 -- the overlay is not computed on the fast path (a lateral WHERE would
 -- become a join filter evaluated after the work).
 CROSS JOIN LATERAL (SELECT CASE WHEN (NOT opts.include_ignored OR opts.effective)
   AND ((x.fields->>'ignore_reason') IS NOT NULL OR EXISTS(
    SELECT 1 FROM tl.annotations a WHERE a.target_channel_id=x.channel_id AND a.target_external_id=x.external_id
     AND (a.status='active' OR recall_scope.known_by IS NOT NULL)))
  THEN tl.overlay_fold((
   SELECT jsonb_agg(jsonb_build_object('attribute',a.attribute,'value',a.value,'method',a.method,'created_at',a.created_at,'id',a.id,'reason',a.reason))
   FROM tl.annotations a
   WHERE a.target_channel_id=x.channel_id AND a.target_external_id=x.external_id
    AND (a.target_version IS NULL OR a.target_version=x.version)
    AND a.attribute IN ('ignore','media_noise','effective_time')
    AND CASE WHEN recall_scope.known_by IS NULL THEN a.status='active'
     ELSE a.created_at<=recall_scope.known_by AND coalesce((SELECT h.old_status FROM tl.status_history h WHERE h.kind='annotation' AND h.ref_id=a.id AND h.changed_at>recall_scope.known_by ORDER BY h.changed_at,h.id LIMIT 1),a.status)='active' END),
   CASE WHEN jsonb_typeof(x.fields->'ignore_reason')='string' THEN x.fields->>'ignore_reason' END) END AS st) ov
 WHERE tl.recall_check(recall_scope.person_role,recall_scope.time_basis)
  AND c.record_kind<>'page_revision'
  AND c.record_kind=ANY(opts.kinds)
  AND (recall_scope.channels IS NULL OR c.name=ANY(recall_scope.channels))
  AND (recall_scope.thread_ids IS NULL OR b.thread_id=ANY(recall_scope.thread_ids))
  AND (recall_scope.include_outside IS NOT FALSE OR NOT coalesce(b.is_outside,false))
  AND (recall_scope.exclude_message_ids IS NULL OR b.id<>ALL(recall_scope.exclude_message_ids))
  AND (opts.include_ignored OR NOT coalesce((ov.st->>'ignored')::boolean,false))
  AND (recall_scope.persons IS NULL OR b.person_id=ANY(recall_scope.persons)
   OR (recall_scope.person_role='involved' AND (
    EXISTS(SELECT 1 FROM api.messages o WHERE o.thread_id=b.thread_id AND o.person_id=ANY(recall_scope.persons))
    OR EXISTS(SELECT 1 FROM api.links l WHERE l.status='active' AND (
     (l.from_kind='message' AND l.from_id=b.id AND l.to_kind='person' AND l.to_id=ANY(recall_scope.persons))
     OR (l.to_kind='message' AND l.to_id=b.id AND l.from_kind='person' AND l.from_id=ANY(recall_scope.persons)))))))
  AND CASE WHEN opts.effective THEN tl.recall_time_ok(coalesce((ov.st->>'effective_time')::timestamptz,b.record_time),coalesce((ov.st->>'effective_end')::timestamptz,b.ended_at,(ov.st->>'effective_time')::timestamptz,b.record_time),NULL,NULL,NULL,NULL,recall_scope.after_at,recall_scope.before_at,'record')
   ELSE tl.recall_time_ok(b.record_time,b.ended_at,x.subject_from,x.subject_to,x.subject_from_precision,x.subject_to_precision,recall_scope.after_at,recall_scope.before_at,recall_scope.time_basis) END
  AND (recall_scope.lat IS NULL OR recall_scope.lon IS NULL OR (b.lat IS NOT NULL AND b.lon IS NOT NULL
   AND earth_distance(api.earth_point(recall_scope.lat,recall_scope.lon),api.earth_point(b.lat,b.lon))<=coalesce(recall_scope.meters,100)))
  AND (recall_scope.must_terms IS NULL OR NOT EXISTS(
   SELECT 1 FROM unnest(recall_scope.must_terms) t(term)
   WHERE numnode(plainto_tsquery('english',t.term))>0 AND NOT (x.tsv @@ plainto_tsquery('english',t.term)))) $function$;

-- 5. recall_scope runs its statement with the call's values (EXECUTE ...
--    USING), so the planner sees which branches and filters apply. As a
--    plain SQL function it was planned generically for every call: about
--    1.7 s of fixed cost against under 1 ms with a custom plan.
CREATE OR REPLACE FUNCTION tl.recall_scope(message_ids bigint[], kinds text[], channels text[], persons bigint[], person_role text, after_at timestamptz, before_at timestamptz, time_basis text, lat float8, lon float8, meters float8, thread_ids bigint[], include_outside boolean, as_of timestamptz, known_by timestamptz, must_terms text[], exclude_message_ids bigint[])
 RETURNS TABLE(message_id bigint, record_time timestamp with time zone, thread_id bigint, thread_no integer)
 LANGUAGE plpgsql
 STABLE
 SET search_path TO 'pg_catalog', 'api', 'public'
AS $function$
BEGIN
 RETURN QUERY EXECUTE $q$
 WITH base AS (
  SELECT m.id,m.channel_id,m.thread_id,m.thread_no,m.happened_at AS record_time,m.ended_at,m.person_id,m.lat,m.lon,m.is_outside
  FROM unnest($1::bigint[]) u(id)
  CROSS JOIN LATERAL (SELECT v.* FROM api.message_versions v WHERE v.id=u.id OFFSET 0) m
  WHERE $14::timestamptz IS NULL AND $15::timestamptz IS NULL AND m.superseded_by IS NULL
   AND $1::bigint[] IS NOT NULL
  UNION ALL
  SELECT m.id,m.channel_id,m.thread_id,m.thread_no,m.happened_at AS record_time,m.ended_at,m.person_id,m.lat,m.lon,m.is_outside
  FROM api.message_versions m
  WHERE $14::timestamptz IS NULL AND $15::timestamptz IS NULL AND m.superseded_by IS NULL
   AND $1::bigint[] IS NULL
  UNION ALL
  SELECT r.message_id,x.channel_id,r.thread_id,r.thread_no,r.record_time,r.ended_at,r.person_id,r.lat,r.lon,r.is_outside
  FROM api.records_asof(coalesce($14::timestamptz,'infinity'::timestamptz),$15::timestamptz) r
  JOIN tl.messages x ON x.id=r.message_id
  WHERE ($14::timestamptz IS NOT NULL OR $15::timestamptz IS NOT NULL)
   AND ($1::bigint[] IS NULL OR r.message_id=ANY($1::bigint[]))
 ), opts AS MATERIALIZED (
  SELECT coalesce('ignored'=ANY($2::text[]),false) AS include_ignored,
   CASE WHEN $2::text[] IS NULL THEN ARRAY['life_event','derived']
    WHEN 'ignored'=ANY($2::text[]) AND cardinality(array_remove($2::text[],'ignored'))=0 THEN ARRAY['life_event','derived']
    ELSE array_remove($2::text[],'ignored') END AS kinds,
   coalesce($8::text,'record')='effective' AS effective
 )
 SELECT b.id,b.record_time,b.thread_id,b.thread_no
 FROM base b JOIN tl.messages x ON x.id=b.id JOIN tl.channels c ON c.id=b.channel_id CROSS JOIN opts
 -- The tl.overlay_state rule, evaluated in line: a per-row call of the
 -- SECURITY DEFINER helper costs ten times the work itself. CASE guarantees
 -- the overlay is not computed on the fast path (a lateral WHERE would
 -- become a join filter evaluated after the work).
 CROSS JOIN LATERAL (SELECT CASE WHEN (NOT opts.include_ignored OR opts.effective)
   AND ((x.fields->>'ignore_reason') IS NOT NULL OR EXISTS(
    SELECT 1 FROM tl.annotations a WHERE a.target_channel_id=x.channel_id AND a.target_external_id=x.external_id
     AND (a.status='active' OR $15::timestamptz IS NOT NULL)))
  THEN tl.overlay_fold((
   SELECT jsonb_agg(jsonb_build_object('attribute',a.attribute,'value',a.value,'method',a.method,'created_at',a.created_at,'id',a.id,'reason',a.reason))
   FROM tl.annotations a
   WHERE a.target_channel_id=x.channel_id AND a.target_external_id=x.external_id
    AND (a.target_version IS NULL OR a.target_version=x.version)
    AND a.attribute IN ('ignore','media_noise','effective_time')
    AND CASE WHEN $15::timestamptz IS NULL THEN a.status='active'
     ELSE a.created_at<=$15::timestamptz AND coalesce((SELECT h.old_status FROM tl.status_history h WHERE h.kind='annotation' AND h.ref_id=a.id AND h.changed_at>$15::timestamptz ORDER BY h.changed_at,h.id LIMIT 1),a.status)='active' END),
   CASE WHEN jsonb_typeof(x.fields->'ignore_reason')='string' THEN x.fields->>'ignore_reason' END) END AS st) ov
 WHERE tl.recall_check($5::text,$8::text)
  AND c.record_kind<>'page_revision'
  AND c.record_kind=ANY(opts.kinds)
  AND ($3::text[] IS NULL OR c.name=ANY($3::text[]))
  AND ($12::bigint[] IS NULL OR b.thread_id=ANY($12::bigint[]))
  AND ($13::boolean IS NOT FALSE OR NOT coalesce(b.is_outside,false))
  AND ($17::bigint[] IS NULL OR b.id<>ALL($17::bigint[]))
  AND (opts.include_ignored OR NOT coalesce((ov.st->>'ignored')::boolean,false))
  AND ($4::bigint[] IS NULL OR b.person_id=ANY($4::bigint[])
   OR ($5::text='involved' AND (
    EXISTS(SELECT 1 FROM api.messages o WHERE o.thread_id=b.thread_id AND o.person_id=ANY($4::bigint[]))
    OR EXISTS(SELECT 1 FROM api.links l WHERE l.status='active' AND (
     (l.from_kind='message' AND l.from_id=b.id AND l.to_kind='person' AND l.to_id=ANY($4::bigint[]))
     OR (l.to_kind='message' AND l.to_id=b.id AND l.from_kind='person' AND l.from_id=ANY($4::bigint[])))))))
  AND CASE WHEN opts.effective THEN tl.recall_time_ok(coalesce((ov.st->>'effective_time')::timestamptz,b.record_time),coalesce((ov.st->>'effective_end')::timestamptz,b.ended_at,(ov.st->>'effective_time')::timestamptz,b.record_time),NULL,NULL,NULL,NULL,$6::timestamptz,$7::timestamptz,'record')
   ELSE tl.recall_time_ok(b.record_time,b.ended_at,x.subject_from,x.subject_to,x.subject_from_precision,x.subject_to_precision,$6::timestamptz,$7::timestamptz,$8::text) END
  AND ($9::float8 IS NULL OR $10::float8 IS NULL OR (b.lat IS NOT NULL AND b.lon IS NOT NULL
   AND earth_distance(api.earth_point($9::float8,$10::float8),api.earth_point(b.lat,b.lon))<=coalesce($11::float8,100)))
  AND ($16::text[] IS NULL OR NOT EXISTS(
   SELECT 1 FROM unnest($16::text[]) t(term)
   WHERE numnode(plainto_tsquery('english',t.term))>0 AND NOT (x.tsv @@ plainto_tsquery('english',t.term)))) $q$
 USING message_ids, kinds, channels, persons, person_role, after_at, before_at, time_basis, lat, lon, meters, thread_ids, include_outside, as_of, known_by, must_terms, exclude_message_ids;
END $function$;
