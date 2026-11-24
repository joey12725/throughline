CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE tl.channels ADD COLUMN conversation_whole_thread boolean NOT NULL DEFAULT false;
ALTER TABLE tl.channels ADD COLUMN conversation_max_tokens int NOT NULL DEFAULT 1500 CHECK(conversation_max_tokens>=32);
CREATE TABLE tl.conversations (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 thread_id bigint NOT NULL REFERENCES tl.threads ON DELETE CASCADE,
 first_thread_no int NOT NULL,last_thread_no int NOT NULL,chunk_no int NOT NULL,
 started_at timestamptz NOT NULL,ended_at timestamptz NOT NULL,
 token_estimate int NOT NULL,text_hash text NOT NULL,content text NOT NULL,closed boolean NOT NULL,
 UNIQUE(thread_id,chunk_no)
);
CREATE TABLE tl.embeddings(conversation_id bigint REFERENCES tl.conversations ON DELETE CASCADE,model text, text_hash text NOT NULL,embedding vector NOT NULL,PRIMARY KEY(conversation_id,model));
CREATE TABLE tl.embedding_models(model text PRIMARY KEY,dimensions int NOT NULL,active boolean NOT NULL DEFAULT false);
CREATE UNIQUE INDEX embedding_one_active_model ON tl.embedding_models((true)) WHERE active;
CREATE TABLE tl.embedding_ledger(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,model text NOT NULL,tokens bigint NOT NULL,cost numeric NOT NULL,at timestamptz NOT NULL DEFAULT now());
CREATE FUNCTION tl.regroup_conversations(thread_ids bigint[] DEFAULT NULL) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 CREATE TEMP TABLE regrouped ON COMMIT DROP AS
 WITH ordered AS (
 SELECT m.*,c.conversation_gap_s,c.conversation_whole_thread,c.conversation_max_tokens,
 lag(coalesce(m.ended_at,m.happened_at)) OVER(PARTITION BY m.thread_id ORDER BY m.thread_no) prev
 FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id
 WHERE m.superseded_by IS NULL AND (thread_ids IS NULL OR m.thread_id=ANY(thread_ids))
 ), grouped AS (
 SELECT *,sum(CASE WHEN prev IS NULL OR (NOT conversation_whole_thread AND happened_at-prev>make_interval(secs=>conversation_gap_s)) THEN 1 ELSE 0 END) OVER(PARTITION BY thread_id ORDER BY thread_no) grp FROM ordered
 ), positioned AS (
 SELECT *,sum(length(content)+1) OVER(PARTITION BY thread_id,grp ORDER BY thread_no)-length(content)-1 pos FROM grouped
 -- Expand each message only into chunks it intersects, rather than a range join
 -- comparing every message to every chunk in a large whole-thread recording.
 ), spans AS (
 SELECT p.*,chunk_start*(conversation_max_tokens*4-64) offset_no
 FROM positioned p CROSS JOIN LATERAL generate_series(
 greatest(0,floor((p.pos-conversation_max_tokens*4)::numeric/(conversation_max_tokens*4-64))::bigint+1),
 (p.pos+length(p.content)-1)/(conversation_max_tokens*4-64)) chunk_start
 -- Aggregate numeric span keys before attaching chunk text to avoid sorting
 -- thousands of repeated 6000-character strings for each conversation.
 ), span_bounds AS (
 SELECT thread_id,grp,offset_no,min(thread_no)::int first_thread_no,max(thread_no)::int last_thread_no,
 min(happened_at) started_at,max(coalesce(ended_at,happened_at)) ended_at
 FROM spans GROUP BY thread_id,grp,offset_no
 ), bodies AS (
 SELECT thread_id,grp,min(happened_at) started_at,max(coalesce(ended_at,happened_at)) ended_at,
 max(conversation_gap_s) gap,max(conversation_max_tokens)*4 size,
 string_agg(content,E'\n' ORDER BY thread_no) body FROM positioned GROUP BY thread_id,grp
 ), chunks AS (
 SELECT b.*,offset_no,substring(body FROM offset_no+1 FOR size) content
 FROM bodies b CROSS JOIN LATERAL generate_series(0,greatest(length(body)-1,0),size-64) offset_no
 ) SELECT c.thread_id,p.first_thread_no,p.last_thread_no,
 row_number() OVER(PARTITION BY c.thread_id ORDER BY c.grp,c.offset_no)::int chunk_no,
 p.started_at,p.ended_at,ceil(length(c.content)/4.0)::int token_estimate,
 md5(c.content) text_hash,c.content,p.ended_at+make_interval(secs=>c.gap)<now() closed
 FROM chunks c JOIN span_bounds p ON p.thread_id=c.thread_id AND p.grp=c.grp AND p.offset_no=c.offset_no;
 INSERT INTO tl.conversations(thread_id,first_thread_no,last_thread_no,chunk_no,started_at,ended_at,token_estimate,text_hash,content,closed)
 SELECT thread_id,first_thread_no,last_thread_no,chunk_no,started_at,ended_at,token_estimate,text_hash,content,closed FROM regrouped
 ON CONFLICT(thread_id,chunk_no) DO UPDATE SET first_thread_no=EXCLUDED.first_thread_no,last_thread_no=EXCLUDED.last_thread_no,started_at=EXCLUDED.started_at,ended_at=EXCLUDED.ended_at,token_estimate=EXCLUDED.token_estimate,text_hash=EXCLUDED.text_hash,content=EXCLUDED.content,closed=EXCLUDED.closed;
 DELETE FROM tl.conversations c WHERE (thread_ids IS NULL OR c.thread_id=ANY(thread_ids)) AND NOT EXISTS(SELECT 1 FROM regrouped r WHERE (r.thread_id,r.chunk_no)=(c.thread_id,c.chunk_no));
 DROP TABLE regrouped;
END $$;
CREATE VIEW api.conversations AS SELECT id,thread_id,first_thread_no,last_thread_no,chunk_no,started_at,ended_at,token_estimate,text_hash,closed FROM tl.conversations;
CREATE OR REPLACE VIEW api.search_results AS SELECT m.*,0::real score,c.id conversation_id,c.first_thread_no,c.last_thread_no FROM api.messages m LEFT JOIN tl.conversations c ON c.thread_id=m.thread_id AND m.thread_no BETWEEN c.first_thread_no AND c.last_thread_no WHERE false;
CREATE OR REPLACE FUNCTION api.search(query text,max_results int DEFAULT 20) RETURNS SETOF api.search_results LANGUAGE sql STABLE AS $$ SELECT m.*,ts_rank_cd(m.tsv,websearch_to_tsquery('english',query)),c.id,c.first_thread_no,c.last_thread_no FROM api.messages m LEFT JOIN LATERAL(SELECT * FROM tl.conversations c WHERE c.thread_id=m.thread_id AND m.thread_no BETWEEN c.first_thread_no AND c.last_thread_no ORDER BY c.first_thread_no,c.id LIMIT 1)c ON true WHERE NOT m.is_reference AND m.tsv @@ websearch_to_tsquery('english',query) ORDER BY  (ts_rank_cd(m.tsv,websearch_to_tsquery('english',query))) DESC,m.id LIMIT max_results $$;
CREATE OR REPLACE FUNCTION api.search_all(query text,max_results int DEFAULT 20) RETURNS SETOF api.search_results LANGUAGE sql STABLE AS $$ SELECT m.*,ts_rank_cd(m.tsv,websearch_to_tsquery('english',query)),c.id,c.first_thread_no,c.last_thread_no FROM api.messages m LEFT JOIN LATERAL(SELECT * FROM tl.conversations c WHERE c.thread_id=m.thread_id AND m.thread_no BETWEEN c.first_thread_no AND c.last_thread_no ORDER BY c.first_thread_no,c.id LIMIT 1)c ON true WHERE m.tsv @@ websearch_to_tsquery('english',query) ORDER BY (ts_rank_cd(m.tsv,websearch_to_tsquery('english',query))) DESC,m.id LIMIT max_results $$;
CREATE FUNCTION api.search_filtered(query text,max_results int DEFAULT 20,person bigint DEFAULT NULL,channel text DEFAULT NULL,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,place text DEFAULT NULL) RETURNS SETOF api.search_results LANGUAGE sql STABLE AS $$
 SELECT s.* FROM api.search(query,NULL) s WHERE (person IS NULL OR s.person_id=person) AND (channel IS NULL OR s.channel=channel) AND (after_at IS NULL OR s.happened_at>=after_at) AND (before_at IS NULL OR s.happened_at<=before_at) AND (place IS NULL OR s.id IN(SELECT id FROM api.near_place(place,100))) ORDER BY score DESC,id LIMIT max_results $$;
CREATE FUNCTION api.search_meaning(query_embedding vector,max_results int DEFAULT 20,person bigint DEFAULT NULL,channel text DEFAULT NULL,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,place text DEFAULT NULL,query_text text DEFAULT '',serving_model text DEFAULT NULL) RETURNS SETOF api.search_results LANGUAGE sql STABLE AS $$
 WITH eligible AS MATERIALIZED (
 SELECT m.*,c.id cid,c.first_thread_no first_no,c.last_thread_no last_no FROM api.messages m JOIN tl.conversations c ON c.thread_id=m.thread_id AND m.thread_no BETWEEN c.first_thread_no AND c.last_thread_no
 WHERE NOT m.is_reference AND (person IS NULL OR m.person_id=person) AND (channel IS NULL OR m.channel=channel) AND (after_at IS NULL OR m.happened_at>=after_at) AND (before_at IS NULL OR m.happened_at<=before_at) AND (place IS NULL OR m.id IN(SELECT id FROM api.near_place(place,100)))
 ), ranked AS MATERIALIZED (
 SELECT e.conversation_id,(1-(e.embedding <=> query_embedding))::real score FROM tl.embeddings e JOIN tl.embedding_models model ON model.model=e.model AND ((serving_model IS NULL AND model.active) OR model.model=serving_model) JOIN tl.conversations c ON c.id=e.conversation_id AND c.text_hash=e.text_hash WHERE EXISTS(SELECT 1 FROM eligible m WHERE m.cid=c.id)
 ) SELECT original.*,r.score,m.cid,m.first_no,m.last_no FROM ranked r JOIN LATERAL(SELECT * FROM eligible e WHERE e.cid=r.conversation_id ORDER BY ts_rank_cd(e.tsv,websearch_to_tsquery('english',query_text)) DESC,e.id LIMIT 1)m ON true JOIN api.messages original ON original.id=m.id ORDER BY r.score DESC,m.id LIMIT max_results $$;
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.conversations,tl.embeddings,tl.embedding_models TO throughline_app;
GRANT SELECT,INSERT ON tl.embedding_ledger TO throughline_app;
GRANT SELECT ON tl.conversations,tl.embeddings,tl.embedding_models,tl.embedding_ledger,api.conversations TO throughline_reader;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA tl TO throughline_app;

GRANT USAGE ON SCHEMA tl TO throughline_reader;

ALTER FUNCTION api.catalog() RENAME TO catalog_core;
CREATE FUNCTION api.catalog() RETURNS jsonb LANGUAGE sql STABLE AS $$
 SELECT api.catalog_core() || jsonb_build_object('meaning_search',jsonb_build_object(
 'description','Exact cosine ranking of eligible conversations; filters precede ranking. Representative message uses query-text keyword rank then ID when query_text is supplied, otherwise first eligible ID. Whole-conversation vectors provide no message-level semantic scoring.',
 'sql','SELECT * FROM api.search_meaning(''[1,0,0]'',10,NULL,''synthetic.chat'')',
 'plain_text_helper','POST /v1/embed {"text":"orchard"} returns an embedding array. Pass its serialized vector as a quoted SQL literal to api.search_meaning; the SQL endpoint remains read-only.',
 'keyword','api.search_filtered(query,limit,person,channel,after_at,before_at,place); alternatively api.search(query,NULL) WHERE ... ORDER BY score DESC,id LIMIT 10. WHERE outside bounded api.search filters only its candidates.',
 'place','Place filter uses api.near_place(place,100), in meters.',
 'fused','GET /v1/search?mode=fused uses RRF constant 60 on exhaustive keyword and meaning ranks.',
 'conversations','api.conversations exposes deterministic spans; overlapping chunks may share a span.')) $$;

CREATE FUNCTION api.search_fused(query text,query_embedding vector,max_results int DEFAULT 20,person bigint DEFAULT NULL,channel text DEFAULT NULL,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,place text DEFAULT NULL,serving_model text DEFAULT NULL) RETURNS SETOF api.search_results LANGUAGE sql STABLE AS $$
 WITH k AS MATERIALIZED(SELECT * FROM api.search_filtered(query,NULL,person,channel,after_at,before_at,place)),
 v AS MATERIALIZED(SELECT * FROM api.search_meaning(query_embedding,NULL,person,channel,after_at,before_at,place,query,serving_model)),
 ranks AS(SELECT id,1.0/(60+row_number() OVER(ORDER BY score DESC,id)) weight FROM k UNION ALL SELECT id,1.0/(60+row_number() OVER(ORDER BY score DESC,id)) FROM v),
 scores AS(SELECT id,sum(weight)::real score FROM ranks GROUP BY id),
 hits AS(SELECT DISTINCT ON(id) * FROM(SELECT * FROM v UNION ALL SELECT * FROM k) all_hits ORDER BY id)
 SELECT m.*,s.score,h.conversation_id,h.first_thread_no,h.last_thread_no FROM scores s JOIN hits h USING(id) JOIN api.messages m ON m.id=s.id ORDER BY s.score DESC,m.id LIMIT max_results $$;
REVOKE EXECUTE ON FUNCTION tl.regroup_conversations(bigint[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.regroup_conversations(bigint[]) TO throughline_app;

CREATE OR REPLACE VIEW api.channels AS SELECT * FROM tl.channels;
SELECT tl.regroup_conversations();
