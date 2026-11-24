-- Embedding choices per channel, vendor batch jobs, and an approximate
-- nearest-neighbour index behind the meaning legs.
--
-- 1. tl.channels.embed: a channel that is not embedded has no vectors. Its
--    records are still found by every lexical and structured leg.
-- 2. tl.embedding_batches / tl.embedding_batch_items: asynchronous vendor
--    jobs of a historic load and the texts each one holds.
-- 3. One HNSW index per embedding model over half-precision copies of its
--    vectors. api.vector_records and api.vector_passages rank through
--    tl.nearest_conversations / tl.nearest_passage_texts, which stay exact
--    while the eligible set is small and use the index beyond that, always
--    scoring the result with the stored full-precision vector.

-- The index needs pgvector 0.8 (halfvec, iterative index scans). An older
-- installed version is updated when the server has a newer one.
DO $$
BEGIN
 IF (SELECT string_to_array(extversion,'.')::int[] < ARRAY[0,8,0] FROM pg_extension WHERE extname='vector') THEN
  ALTER EXTENSION vector UPDATE;
 END IF;
 IF (SELECT string_to_array(extversion,'.')::int[] < ARRAY[0,8,0] FROM pg_extension WHERE extname='vector') THEN
  RAISE EXCEPTION '0054: pgvector 0.8 or later is required (installed: %)',(SELECT extversion FROM pg_extension WHERE extname='vector');
 END IF;
END $$;

-- 1. Channels ------------------------------------------------------------------
ALTER TABLE tl.channels ADD COLUMN embed boolean NOT NULL DEFAULT true;

-- A vector is never stored for a conversation of a channel that is not
-- embedded: the row is dropped, so a worker that raced the switch stays
-- harmless.
CREATE FUNCTION tl.embeddings_channel_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM tl.conversations c JOIN tl.threads t ON t.id=c.thread_id JOIN tl.channels ch ON ch.id=t.channel_id
   WHERE c.id=NEW.conversation_id AND NOT ch.embed) THEN
  RETURN NULL;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER embeddings_channel_guard BEFORE INSERT ON tl.embeddings FOR EACH ROW EXECUTE FUNCTION tl.embeddings_channel_guard();

-- Switch a channel's embedding on or off. Switching off deletes its
-- conversation vectors and the passage vectors whose text no embedded channel
-- still has; switching on leaves the backlog to the worker.
CREATE FUNCTION tl.set_channel_embedding(channel_name text,enabled boolean) RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$
DECLARE cid bigint; conversations bigint := 0; passages bigint := 0;
BEGIN
 UPDATE tl.channels c SET embed=enabled WHERE c.name=channel_name RETURNING c.id INTO cid;
 IF cid IS NULL THEN
  RAISE EXCEPTION 'unknown channel %',channel_name USING ERRCODE='no_data_found';
 END IF;
 IF NOT enabled THEN
  WITH gone AS (DELETE FROM tl.embeddings e USING tl.conversations c,tl.threads t
   WHERE c.id=e.conversation_id AND t.id=c.thread_id AND t.channel_id=cid RETURNING 1)
  SELECT count(*) INTO conversations FROM gone;
  WITH gone AS (DELETE FROM tl.passage_embeddings e
   WHERE EXISTS(SELECT 1 FROM tl.passages p WHERE p.text_hash=e.text_hash AND p.channel_id=cid)
    AND NOT EXISTS(SELECT 1 FROM tl.passages p JOIN tl.channels ch ON ch.id=p.channel_id AND ch.embed WHERE p.text_hash=e.text_hash)
   RETURNING 1)
  SELECT count(*) INTO passages FROM gone;
  DELETE FROM tl.embedding_batch_items i USING tl.conversations c,tl.threads t
   WHERE c.id=i.conversation_id AND t.id=c.thread_id AND t.channel_id=cid;
 END IF;
 RETURN jsonb_build_object('channel',channel_name,'embed',enabled,'conversation_vectors_deleted',conversations,'passage_vectors_deleted',passages);
END $$;
REVOKE EXECUTE ON FUNCTION tl.set_channel_embedding(text,boolean) FROM PUBLIC;

CREATE OR REPLACE FUNCTION tl.channel_maintenance(cid bigint) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,tl AS $$
 SELECT jsonb_build_object(
  'dirty_threads',(SELECT count(*) FROM tl.dirty_threads d JOIN tl.threads t ON t.id=d.thread_id WHERE t.channel_id=cid),
  'embed',(SELECT ch.embed FROM tl.channels ch WHERE ch.id=cid),
  'closed_conversations',k.closed,
  'embedded_conversations',k.embedded,
  'embedded_share',CASE WHEN k.closed>0 THEN round(k.embedded::numeric/k.closed,4) END)
 FROM (SELECT count(*) FILTER (WHERE c.closed) AS closed,
   count(*) FILTER (WHERE c.closed AND EXISTS(SELECT 1 FROM tl.embeddings e JOIN tl.embedding_models em ON em.model=e.model AND em.active WHERE e.conversation_id=c.id AND e.text_hash=c.text_hash)) AS embedded
  FROM tl.conversations c JOIN tl.threads t ON t.id=c.thread_id WHERE t.channel_id=cid) k $$;

-- 2. Batch jobs ----------------------------------------------------------------
CREATE TABLE tl.embedding_batches(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 provider text NOT NULL,
 model text NOT NULL,
 dimensions int NOT NULL,
 kind text NOT NULL CHECK(kind IN ('conversation','passage')),
 contextual boolean NOT NULL DEFAULT false,
 -- The vendor's handle; null until the job was accepted.
 remote_id text,
 status text NOT NULL DEFAULT 'submitting' CHECK(status IN ('submitting','submitted','completed','failed','cancelled')),
 items int NOT NULL DEFAULT 0,
 stored int NOT NULL DEFAULT 0,
 tokens bigint NOT NULL DEFAULT 0,
 cost numeric NOT NULL DEFAULT 0,
 detail text,
 submitted_at timestamptz NOT NULL DEFAULT now(),
 checked_at timestamptz,
 finished_at timestamptz);
-- One text of a job: a conversation (conversation_id, its text_hash when it
-- was sent) or a passage text (text_hash alone). store=false marks a chunk
-- sent only as context of its neighbours. The texts of an open job are
-- reserved: the ordinary worker does not embed them a second time.
CREATE TABLE tl.embedding_batch_items(
 batch_id bigint NOT NULL REFERENCES tl.embedding_batches ON DELETE CASCADE,
 custom_id text NOT NULL,
 position int NOT NULL,
 conversation_id bigint REFERENCES tl.conversations ON DELETE CASCADE,
 text_hash text NOT NULL,
 store boolean NOT NULL DEFAULT true,
 PRIMARY KEY(batch_id,custom_id,position));
CREATE INDEX embedding_batch_items_conversation ON tl.embedding_batch_items(conversation_id) WHERE conversation_id IS NOT NULL;
CREATE INDEX embedding_batch_items_text ON tl.embedding_batch_items(text_hash) WHERE conversation_id IS NULL;
CREATE INDEX embedding_batches_open ON tl.embedding_batches(status) WHERE status IN ('submitting','submitted');
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.embedding_batches,tl.embedding_batch_items TO throughline_app;

-- 3. Nearest-neighbour index -----------------------------------------------------
-- The vector columns have no fixed size (several models may coexist), so each
-- model gets partial expression indexes of its own size. Half precision
-- halves the index and allows up to 4,000 dimensions; ranking still scores
-- with the stored vector. Returns false when the model is unknown or too
-- wide to index (then ranking stays exact).
CREATE FUNCTION tl.embedding_index_name(table_name text,model_name text,dims int) RETURNS text LANGUAGE sql IMMUTABLE AS $$
 SELECT table_name||'_ann_'||substr(md5(model_name),1,16)||'_'||dims $$;

CREATE FUNCTION tl.ensure_embedding_index(model_name text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE dims int; target text; stale record; t text;
BEGIN
 SELECT m.dimensions INTO dims FROM tl.embedding_models m WHERE m.model=model_name;
 IF NOT FOUND OR dims>4000 THEN RETURN false; END IF;
 FOREACH t IN ARRAY ARRAY['embeddings','passage_embeddings'] LOOP
  target := tl.embedding_index_name(t,model_name,dims);
  -- An index left by the same model name at another size.
  FOR stale IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='tl' AND c.relkind='i' AND c.relname LIKE t||'\_ann\_'||substr(md5(model_name),1,16)||'\_%' AND c.relname<>target LOOP
   EXECUTE format('DROP INDEX tl.%I',stale.relname);
  END LOOP;
  IF to_regclass(format('tl.%I',target)) IS NULL THEN
   EXECUTE format('CREATE INDEX %I ON tl.%I USING hnsw ((embedding::halfvec(%s)) halfvec_cosine_ops) WHERE model=%L AND vector_dims(embedding)=%s',target,t,dims,model_name,dims);
  END IF;
 END LOOP;
 RETURN true;
END $$;
REVOKE EXECUTE ON FUNCTION tl.ensure_embedding_index(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.ensure_embedding_index(text) TO throughline_app;

-- The conversations of `eligible` nearest to q under one model: (id, cosine
-- similarity from the stored vector), only for vectors of the conversation's
-- current text.
--   exact   every eligible vector is compared. Used when k is null (the
--           caller wants all of them), when at most
--           throughline.ann_exact_limit (default 20,000) conversations are
--           eligible, or when the model has no index.
--   index   the HNSW index proposes candidates and the eligibility test is
--           applied while it is scanned (iterative scan), until 2k are
--           found. If the scan gives up first, the exact path answers.
-- Not callable by runtime roles: it reads vectors without the asker gate, so
-- only the gated api functions call it, with an eligible set they computed.
CREATE FUNCTION tl.nearest_conversations(q vector,model_name text,k int,eligible bigint[])
RETURNS TABLE(cid bigint,sim real) LANGUAGE plpgsql STABLE
SET search_path=pg_catalog,public SET hnsw.iterative_scan='relaxed_order' SET hnsw.ef_search='200' AS $$
#variable_conflict use_column
DECLARE dims int := vector_dims(q); exact_limit int; want int; ids bigint[]; sims real[];
BEGIN
 IF model_name IS NULL OR eligible IS NULL OR cardinality(eligible)=0 THEN RETURN; END IF;
 exact_limit := coalesce(nullif(current_setting('throughline.ann_exact_limit',true),'')::int,20000);
 IF k IS NOT NULL AND k>0 AND cardinality(eligible)>exact_limit
   AND to_regclass(format('tl.%I',tl.embedding_index_name('embeddings',model_name,dims))) IS NOT NULL THEN
  want := greatest(k*2,40);
  EXECUTE format($f$SELECT array_agg(x.id),array_agg(x.s) FROM (
    SELECT e.conversation_id AS id,(1-(e.embedding <=> $1))::real AS s FROM tl.embeddings e
    WHERE e.model=%L AND vector_dims(e.embedding)=%s AND e.conversation_id=ANY($2)
     AND EXISTS(SELECT 1 FROM tl.conversations c WHERE c.id=e.conversation_id AND c.text_hash=e.text_hash)
    ORDER BY e.embedding::halfvec(%s) <=> $1::halfvec(%s) LIMIT $3) x$f$,model_name,dims,dims,dims)
   INTO ids,sims USING q,eligible,want;
  IF coalesce(cardinality(ids),0)>=want THEN
   RETURN QUERY SELECT u.id,u.s FROM unnest(ids,sims) u(id,s);
   RETURN;
  END IF;
 END IF;
 RETURN QUERY
  SELECT e.conversation_id,(1-(e.embedding <=> q))::real
  FROM tl.embeddings e JOIN tl.conversations c ON c.id=e.conversation_id AND c.text_hash=e.text_hash
  WHERE e.model=model_name AND vector_dims(e.embedding)=dims AND e.conversation_id=ANY(eligible);
END $$;
REVOKE EXECUTE ON FUNCTION tl.nearest_conversations(vector,text,int,bigint[]) FROM PUBLIC;

-- The same for passage texts: (text_hash, similarity) among `hashes`.
CREATE FUNCTION tl.nearest_passage_texts(q vector,model_name text,k int,hashes text[])
RETURNS TABLE(text_hash text,sim real) LANGUAGE plpgsql STABLE
SET search_path=pg_catalog,public SET hnsw.iterative_scan='relaxed_order' SET hnsw.ef_search='200' AS $$
#variable_conflict use_column
DECLARE dims int := vector_dims(q); exact_limit int; want int; ids text[]; sims real[];
BEGIN
 IF model_name IS NULL OR hashes IS NULL OR cardinality(hashes)=0 THEN RETURN; END IF;
 exact_limit := coalesce(nullif(current_setting('throughline.ann_exact_limit',true),'')::int,20000);
 IF k IS NOT NULL AND k>0 AND cardinality(hashes)>exact_limit
   AND to_regclass(format('tl.%I',tl.embedding_index_name('passage_embeddings',model_name,dims))) IS NOT NULL THEN
  want := greatest(k*2,40);
  EXECUTE format($f$SELECT array_agg(x.id),array_agg(x.s) FROM (
    SELECT e.text_hash AS id,(1-(e.embedding <=> $1))::real AS s FROM tl.passage_embeddings e
    WHERE e.model=%L AND vector_dims(e.embedding)=%s AND e.text_hash=ANY($2)
    ORDER BY e.embedding::halfvec(%s) <=> $1::halfvec(%s) LIMIT $3) x$f$,model_name,dims,dims,dims)
   INTO ids,sims USING q,hashes,want;
  IF coalesce(cardinality(ids),0)>=want THEN
   RETURN QUERY SELECT u.id,u.s FROM unnest(ids,sims) u(id,s);
   RETURN;
  END IF;
 END IF;
 RETURN QUERY
  SELECT e.text_hash,(1-(e.embedding <=> q))::real
  FROM tl.passage_embeddings e
  WHERE e.model=model_name AND vector_dims(e.embedding)=dims AND e.text_hash=ANY(hashes);
END $$;
REVOKE EXECUTE ON FUNCTION tl.nearest_passage_texts(vector,text,int,text[]) FROM PUBLIC;

-- The meaning legs rank through the functions above. Patched in place, as in
-- 0051: everything else about the two functions (signature, security, the
-- eligible set, the choice of the representative message) stays as it is,
-- and a body that no longer has the expected shape fails the migration.
DO $$
DECLARE fn regprocedure; body text; a int; b int; gate text := '';
BEGIN
 -- The mixed-conversation gate of 0051, where it is installed. A database
 -- that ran an earlier 0051 without it keeps its ranking as it was.
 IF to_regprocedure('api.conversation_visible(bigint,integer,integer)') IS NOT NULL THEN
  gate := ' AND api.conversation_visible(c.thread_id,c.first_thread_no,c.last_thread_no)';
 END IF;
 fn := 'api.vector_records(vector,integer,text[],text[],bigint[],text,timestamp with time zone,timestamp with time zone,text,double precision,double precision,double precision,bigint[],boolean,timestamp with time zone,timestamp with time zone,text[],bigint[],bigint[],text,text)'::regprocedure;
 body := pg_get_functiondef(fn);
 a := position(' ranked AS MATERIALIZED (' IN body);
 b := position(' SELECT p.id,r.sim,p.cid' IN body);
 IF a=0 OR b<=a OR (gate<>'' AND position('api.conversation_visible' IN body)=0) OR position('tl.nearest_conversations' IN body)>0 THEN
  RAISE EXCEPTION '0054: % does not have the expected ranking clause',fn;
 END IF;
 EXECUTE substr(body,1,a-1)
  ||' ranked AS MATERIALIZED ('||E'\n'
  ||'  SELECT n.cid,n.sim FROM tl.nearest_conversations(vector_records.query_embedding,'||E'\n'
  ||'   (SELECT model.model FROM tl.embedding_models model WHERE (vector_records.serving_model IS NULL AND model.active) OR model.model=vector_records.serving_model LIMIT 1),'||E'\n'
  ||'   vector_records.max_results,'||E'\n'
  ||'   ARRAY(SELECT c.id FROM tl.conversations c WHERE EXISTS(SELECT 1 FROM eligible g WHERE g.cid=c.id)'||gate||')) n)'||E'\n'
  ||substr(body,b);

 fn := 'api.vector_passages(vector,integer,text[],timestamp with time zone,boolean,timestamp with time zone,timestamp with time zone,text,bigint[],bigint[],text[],bigint[],bigint[],timestamp with time zone,text)'::regprocedure;
 body := pg_get_functiondef(fn);
 a := position('  SELECT e.passage_id,e.message_id,(1-(pe.embedding <=> vector_passages.query_embedding))::real AS score' IN body);
 b := position('  ORDER BY 3 DESC,e.passage_id' IN body);
 IF a=0 OR b<=a OR position('tl.nearest_passage_texts' IN body)>0 THEN
  RAISE EXCEPTION '0054: % does not have the expected ranking clause',fn;
 END IF;
 EXECUTE substr(body,1,a-1)
  ||'  SELECT e.passage_id,e.message_id,n.sim AS score'||E'\n'
  ||'  FROM eligible e'||E'\n'
  ||'  JOIN tl.nearest_passage_texts(vector_passages.query_embedding,'||E'\n'
  ||'   (SELECT em.model FROM tl.embedding_models em WHERE (vector_passages.serving_model IS NULL AND em.active) OR em.model=vector_passages.serving_model LIMIT 1),'||E'\n'
  ||'   vector_passages.max_results,'||E'\n'
  ||'   ARRAY(SELECT DISTINCT g.text_hash FROM eligible g)) n ON n.text_hash=e.text_hash'||E'\n'
  ||substr(body,b);
END $$;

UPDATE tl.api_functions SET description='Cosine ranking of conversations containing a record in a hard scope, one representative per conversation; exact while the scope is small, through the model''s HNSW index beyond that, always scored with the stored vector; current text only (as_of/known_by raise)'
 WHERE name='vector_records';
UPDATE tl.api_functions SET description='Cosine ranking of passages for a query embedding supplied by the caller, with the filters of search_passages; exact while the scope is small, through the model''s HNSW index beyond that; vectors of the active model or of serving_model',exactness='approximate'
 WHERE name='vector_passages';

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('embeddings',jsonb_build_object(
  'description','Meaning search over vectors of conversation chunks and page passages. Vectors are optional per channel: a channel with embed=false has none, and its records are found by the keyword, entity, link and structured legs only.',
  'channels','api.describe(''channel'',name) reports embed for an owner. The owner switches it with throughline embed channels.',
  'ranking','Filters come first: the asker gate and the hard scope decide the eligible set, and only its vectors are ranked. Up to throughline.ann_exact_limit (20,000) eligible conversations or passage texts are compared exactly; beyond that an HNSW index proposes candidates under the same filter. Scores are always cosine similarity from the stored full-precision vector.',
  'input_types','A query and a stored text are embedded differently when the model distinguishes them (Voyage input_type, Gemini task, OpenRouter input_type).',
  'context','With a contextual model (voyage-context-4) a chunk is embedded together with its neighbours of the same thread or page, so its vector also carries what the surrounding text says.'))
ON CONFLICT (subject) DO UPDATE SET body=EXCLUDED.body,version=tl.catalog_sections.version+1;
