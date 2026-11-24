-- Review fixes: stable references, safe reader boundary and retained envelopes.
ALTER TABLE tl.people ADD COLUMN asserted_by text;
ALTER TABLE tl.links ADD COLUMN token_id bigint REFERENCES tl.tokens;
ALTER TABLE tl.identity_events ADD COLUMN token_id bigint REFERENCES tl.tokens;
CREATE TABLE tl.message_envelopes(message_id bigint PRIMARY KEY REFERENCES tl.messages,envelope jsonb NOT NULL);
GRANT SELECT,INSERT ON tl.message_envelopes TO throughline_app;
CREATE FUNCTION api.current_message(message_id bigint) RETURNS bigint LANGUAGE sql STABLE AS $$
 SELECT current.id FROM api.message_versions old JOIN api.messages current USING(channel_id,external_id) WHERE old.id=$1
$$;
CREATE OR REPLACE FUNCTION api.context(message_id bigint,before int DEFAULT 3,after int DEFAULT 3) RETURNS SETOF api.messages LANGUAGE sql STABLE AS $$ SELECT m.* FROM api.messages m JOIN api.messages a ON a.id=api.current_message(message_id) AND m.thread_id=a.thread_id WHERE m.thread_no BETWEEN a.thread_no-greatest(before,0) AND a.thread_no+greatest(after,0) ORDER BY m.thread_no $$;
CREATE OR REPLACE FUNCTION api.follow(kind text,id bigint,link_type text DEFAULT NULL,depth int DEFAULT 1) RETURNS TABLE(kind text,id bigint,depth int) LANGUAGE sql STABLE AS $$ WITH RECURSIVE walk(kind,id,depth,path) AS (SELECT follow.kind,CASE WHEN follow.kind='message' THEN api.current_message(follow.id) ELSE follow.id END,0,ARRAY[follow.kind||':'||CASE WHEN follow.kind='message' THEN api.current_message(follow.id) ELSE follow.id END] UNION ALL SELECT CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_kind ELSE l.from_kind END,CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_id ELSE l.from_id END,w.depth+1,w.path||CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_kind||':'||l.to_id ELSE l.from_kind||':'||l.from_id END FROM walk w JOIN api.links l ON ((l.from_kind=w.kind AND l.from_id=w.id) OR (l.to_kind=w.kind AND l.to_id=w.id)) WHERE w.depth<follow.depth AND l.status='active' AND (link_type IS NULL OR l.type=link_type) AND NOT (CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_kind||':'||l.to_id ELSE l.from_kind||':'||l.from_id END=ANY(w.path))) SELECT DISTINCT kind,id,depth FROM walk WHERE depth>0 $$;
CREATE OR REPLACE FUNCTION api.search_filtered(query text,max_results int DEFAULT 20,person bigint DEFAULT NULL,channel text DEFAULT NULL,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,place text DEFAULT NULL) RETURNS SETOF api.search_results LANGUAGE sql STABLE AS $$
 SELECT s.* FROM api.search(query,NULL) s WHERE (person IS NULL OR s.person_id=person) AND (search_filtered.channel IS NULL OR s.channel=search_filtered.channel) AND (after_at IS NULL OR s.happened_at>=after_at) AND (before_at IS NULL OR s.happened_at<=before_at) AND (place IS NULL OR s.id IN(SELECT id FROM api.near_place(place,100))) ORDER BY score DESC,id LIMIT max_results $$;
CREATE OR REPLACE FUNCTION api.search_meaning(query_embedding vector,max_results int DEFAULT 20,person bigint DEFAULT NULL,channel text DEFAULT NULL,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,place text DEFAULT NULL,query_text text DEFAULT '',serving_model text DEFAULT NULL) RETURNS SETOF api.search_results LANGUAGE sql STABLE AS $$
 WITH eligible AS MATERIALIZED (
 SELECT m.*,c.id cid,c.first_thread_no first_no,c.last_thread_no last_no FROM api.messages m JOIN tl.conversations c ON c.thread_id=m.thread_id AND m.thread_no BETWEEN c.first_thread_no AND c.last_thread_no
 WHERE NOT m.is_reference AND (person IS NULL OR m.person_id=person) AND (search_meaning.channel IS NULL OR m.channel=search_meaning.channel) AND (after_at IS NULL OR m.happened_at>=after_at) AND (before_at IS NULL OR m.happened_at<=before_at) AND (place IS NULL OR m.id IN(SELECT id FROM api.near_place(place,100)))
 ), ranked AS MATERIALIZED (
 SELECT e.conversation_id,(1-(e.embedding <=> query_embedding))::real score FROM tl.embeddings e JOIN tl.embedding_models model ON model.model=e.model AND ((serving_model IS NULL AND model.active) OR model.model=serving_model) JOIN tl.conversations c ON c.id=e.conversation_id AND c.text_hash=e.text_hash WHERE EXISTS(SELECT 1 FROM eligible m WHERE m.cid=c.id)
 ) SELECT original.*,r.score,m.cid,m.first_no,m.last_no FROM ranked r JOIN LATERAL(SELECT * FROM eligible e WHERE e.cid=r.conversation_id ORDER BY ts_rank_cd(e.tsv,websearch_to_tsquery('english',query_text)) DESC,e.id LIMIT 1)m ON true JOIN api.messages original ON original.id=m.id ORDER BY r.score DESC,m.id LIMIT max_results $$;
REVOKE ALL ON ALL TABLES IN SCHEMA tl FROM throughline_reader;
REVOKE ALL ON SCHEMA tl FROM throughline_reader;
ALTER ROLE throughline_reader SET default_transaction_read_only=on;
ALTER ROLE throughline_reader SET search_path=pg_catalog,api;
ALTER ROLE throughline_reader SET work_mem='4MB';
REVOKE UPDATE,DELETE,TRUNCATE ON tl.audit_log,tl.identity_events FROM throughline_app;
-- Search functions access internal vectors under their migration owner, never
-- under a client-controlled path. No arbitrary SQL is executed by these functions.
DO $$ DECLARE f regprocedure; BEGIN
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' LOOP
  EXECUTE format('ALTER FUNCTION %s SECURITY DEFINER SET search_path=pg_catalog,api,public',f);
 END LOOP;
END $$;
-- PUBLIC grants are inherited: revoking from the reader alone is ineffective.
DO $$ DECLARE f regprocedure; BEGIN
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname IN ('pg_catalog','public') AND (p.proname ~ '^(lo_|pg_read_|pg_ls_|dblink|query_to_xml|table_to_xml|database_to_xml|schema_to_xml|pg_advisory_|pg_try_advisory_)' OR p.proname IN ('ts_stat','set_config')) LOOP
  EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC,throughline_reader',f);
  EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO throughline_app',f);
 END LOOP;
 EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC,throughline_reader',current_database());
 EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO throughline_app',current_database());
END $$;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA api TO throughline_reader,throughline_app;
