-- Agent retrieval v2 (2026-11-21).
-- 1. vector_records: pick the representative message inside each winning
--    conversation using weighted AND + OR tsquery over query_text instead of
--    AND-only websearch_to_tsquery. Long conversations (especially whole-thread
--    voice transcripts and multi-topic chats) rarely contain every query word
--    in one turn; AND-only scored 0 on every turn and fell back to turn #1.
-- 2. search_records: length-normalized ts_rank_cd(..., 32) so huge pages do not
--    bury focused chat/transcript matches, and rank OR-fallback matches by
--    fraction of query terms matched first so 4/5 beats 2/5.

CREATE OR REPLACE FUNCTION api.vector_records(
    query_embedding vector,
    max_results integer DEFAULT 20,
    kinds text[] DEFAULT NULL::text[],
    channels text[] DEFAULT NULL::text[],
    persons bigint[] DEFAULT NULL::bigint[],
    person_role text DEFAULT 'author'::text,
    after_at timestamp with time zone DEFAULT NULL::timestamp with time zone,
    before_at timestamp with time zone DEFAULT NULL::timestamp with time zone,
    time_basis text DEFAULT 'record'::text,
    lat double precision DEFAULT NULL::double precision,
    lon double precision DEFAULT NULL::double precision,
    meters double precision DEFAULT NULL::double precision,
    thread_ids bigint[] DEFAULT NULL::bigint[],
    include_outside boolean DEFAULT true,
    as_of timestamp with time zone DEFAULT NULL::timestamp with time zone,
    known_by timestamp with time zone DEFAULT NULL::timestamp with time zone,
    must_terms text[] DEFAULT NULL::text[],
    exclude_message_ids bigint[] DEFAULT NULL::bigint[],
    message_ids bigint[] DEFAULT NULL::bigint[],
    query_text text DEFAULT ''::text,
    serving_model text DEFAULT NULL::text
)
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
 q_and tsquery; q_or tsquery; q_plain text;
BEGIN
 IF vector_records.as_of IS NOT NULL OR vector_records.known_by IS NOT NULL THEN
  RAISE EXCEPTION 'vector_records: embeddings exist for current text only; as_of and known_by are not supported' USING ERRCODE='feature_not_supported';
 END IF;
 q_and := websearch_to_tsquery('english', coalesce(vector_records.query_text, ''));
 q_plain := plainto_tsquery('english', coalesce(vector_records.query_text, ''))::text;
 q_or := CASE WHEN q_plain IS NOT NULL AND q_plain <> ''
              THEN replace(q_plain, ' & ', ' | ')::tsquery
              ELSE q_and END;
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
   JOIN LATERAL (
    SELECT g.* FROM eligible g WHERE g.cid=r.cid
    ORDER BY (ts_rank_cd(g.tsv, q_and, 32) * 4.0 + coalesce(ts_rank_cd(g.tsv, q_or, 32), 0.0)) DESC, g.id
    LIMIT 1) p ON true
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
 JOIN LATERAL (
  SELECT g.* FROM eligible g WHERE g.cid=r.cid
  ORDER BY (ts_rank_cd(g.tsv, q_and, 32) * 4.0 + coalesce(ts_rank_cd(g.tsv, q_or, 32), 0.0)) DESC, g.id
  LIMIT 1) p ON true
 ORDER BY r.sim DESC,p.id LIMIT vector_records.max_results;
END $function$;

CREATE OR REPLACE FUNCTION api.search_records(
    query text,
    max_results integer DEFAULT 20,
    kinds text[] DEFAULT NULL::text[],
    channels text[] DEFAULT NULL::text[],
    persons bigint[] DEFAULT NULL::bigint[],
    person_role text DEFAULT 'author'::text,
    after_at timestamp with time zone DEFAULT NULL::timestamp with time zone,
    before_at timestamp with time zone DEFAULT NULL::timestamp with time zone,
    time_basis text DEFAULT 'record'::text,
    lat double precision DEFAULT NULL::double precision,
    lon double precision DEFAULT NULL::double precision,
    meters double precision DEFAULT NULL::double precision,
    thread_ids bigint[] DEFAULT NULL::bigint[],
    include_outside boolean DEFAULT true,
    as_of timestamp with time zone DEFAULT NULL::timestamp with time zone,
    known_by timestamp with time zone DEFAULT NULL::timestamp with time zone,
    must_terms text[] DEFAULT NULL::text[],
    exclude_message_ids bigint[] DEFAULT NULL::bigint[],
    message_ids bigint[] DEFAULT NULL::bigint[]
)
 RETURNS TABLE(message_id bigint, score real, conversation_id bigint, first_thread_no integer, last_thread_no integer)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'api', 'public'
AS $function$
 WITH q0 AS MATERIALIZED (
  SELECT websearch_to_tsquery('english', coalesce(search_records.query, '')) AS q,
         plainto_tsquery('english', coalesce(search_records.query, '')) AS qp
 ),
 q AS MATERIALIZED (
  SELECT q0.q,
   CASE WHEN q0.q::text ~ '[|!()]' THEN NULL::tsquery[]
        WHEN position(' & ' in q0.qp::text) = 0 THEN NULL::tsquery[]
        ELSE ARRAY(SELECT DISTINCT x::tsquery FROM regexp_split_to_table(q0.qp::text, ' & ') x) END AS atoms
  FROM q0),
 hits_and AS MATERIALIZED (
  SELECT m.id, ts_rank_cd(m.tsv, q.q, 32)::real AS score, 0 AS tier FROM tl.messages m CROSS JOIN q
  WHERE m.tsv @@ q.q
   AND (search_records.message_ids IS NULL OR m.id=ANY(search_records.message_ids))
   AND (search_records.as_of IS NOT NULL OR search_records.known_by IS NOT NULL OR m.superseded_by IS NULL)),
 or_q AS MATERIALIZED (
  SELECT (SELECT string_agg('('||a::text||')',' | ') FROM unnest(q.atoms) a)::tsquery AS q, q.atoms
  FROM q WHERE q.atoms IS NOT NULL AND (SELECT count(*) FROM hits_and) < search_records.max_results*4),
 hits_or AS MATERIALIZED (
  SELECT t.id, t.score, 1 AS tier FROM (
   SELECT m.id,
    ((SELECT count(*) FROM unnest(o.atoms) a WHERE m.tsv @@ a)::real / cardinality(o.atoms)
     * (0.5 + ts_rank_cd(m.tsv, o.q, 32)))::real AS score,
    (SELECT count(*) FROM unnest(o.atoms) a WHERE m.tsv @@ a) AS matched,
    cardinality(o.atoms) AS n
   FROM tl.messages m CROSS JOIN or_q o
   WHERE m.tsv @@ o.q
    AND NOT EXISTS (SELECT 1 FROM hits_and h WHERE h.id=m.id)
    AND (search_records.message_ids IS NULL OR m.id=ANY(search_records.message_ids))
    AND (search_records.as_of IS NOT NULL OR search_records.known_by IS NOT NULL OR m.superseded_by IS NULL)) t
  WHERE t.matched >= least(2, t.n)
  ORDER BY t.matched DESC, t.score DESC, t.id LIMIT 10000),
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
