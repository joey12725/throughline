-- Agent retrieval v3 (2026-11-21).
-- Restore 0058's api.search_records scoring (keeping 0059's api.vector_records
-- OR+AND representative-turn fix). Length-normalizing ts_rank_cd(..., 32) and
-- ordering OR-fallback by matched-atom count before cover density caused long
-- incidental messages (developer logs, newsletters, long summaries) that happen
-- to contain multiple query words far apart to outrank focused chat hits.

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
