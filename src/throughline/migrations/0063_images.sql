-- Images as a native modality (docs/images.md, 2026-11-21).
-- 1. tl.assets (one content-addressed image or video; bytes live in Immich),
--    tl.message_assets (keyed by the record, so a link survives new message
--    versions), tl.asset_faces (Immich faces; the Immich person is an
--    identifier system='immich', kind='face') and tl.asset_vectors (one
--    vector per asset and model: gemini-embedding-2, siglip2-so400m-384).
-- 2. tsv over kind, tags, caption, place, depicted people, OCR text and the
--    original file name (trigger).
-- 3. Gated views api.assets, api.message_assets, api.asset_faces; batched
--    api.assets_of for response shaping; leg functions api.visual_records
--    (vector) and api.image_search_records (keywords over tsv).
-- 4. Link type depicts (message -> person), identifier scheme immich/face,
--    purge hook. The channel photos.immich is registered by the pipeline.

-- 1. Tables ------------------------------------------------------------------
CREATE TABLE tl.assets(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 sha256 text NOT NULL UNIQUE,
 immich_id uuid UNIQUE,
 mime text,
 bytes bigint,
 width int,
 height int,
 duration_s real,
 kind text CHECK (kind IN ('photo','screenshot','document','artwork','video','other')),
 tags text[] NOT NULL DEFAULT '{}',
 ocr_text text,
 caption text,
 taken_at timestamptz,
 lat double precision,
 lon double precision,
 place jsonb,
 nsfw real,
 nsfw_labels jsonb,
 origin text NOT NULL,
 original_name text,
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','uploaded','enriched','failed','missing')),
 enrich jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 tsv tsvector);
CREATE INDEX assets_tsv ON tl.assets USING gin(tsv);
CREATE INDEX assets_tags ON tl.assets USING gin(tags);
CREATE INDEX assets_taken_at ON tl.assets(taken_at);
CREATE INDEX assets_earth ON tl.assets USING gist(api.earth_point(lat,lon)) WHERE lat IS NOT NULL AND lon IS NOT NULL;
CREATE INDEX assets_kind ON tl.assets(kind);
CREATE INDEX assets_origin ON tl.assets(origin);
CREATE INDEX assets_status ON tl.assets(status);

CREATE TABLE tl.message_assets(
 channel_id bigint NOT NULL REFERENCES tl.channels,
 external_id text NOT NULL,
 asset_id bigint NOT NULL REFERENCES tl.assets ON DELETE CASCADE,
 ord int NOT NULL DEFAULT 0,
 role text NOT NULL DEFAULT 'attachment' CHECK (role IN ('attachment','inline','self')),
 source_ref jsonb NOT NULL DEFAULT '{}',
 PRIMARY KEY (channel_id,external_id,asset_id));
CREATE INDEX message_assets_asset ON tl.message_assets(asset_id);

CREATE TABLE tl.asset_faces(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 asset_id bigint NOT NULL REFERENCES tl.assets ON DELETE CASCADE,
 immich_face_id uuid UNIQUE,
 identifier_id bigint REFERENCES tl.identifiers,
 bbox jsonb NOT NULL,
 score real);
CREATE INDEX asset_faces_asset ON tl.asset_faces(asset_id);
CREATE INDEX asset_faces_identifier ON tl.asset_faces(identifier_id) WHERE identifier_id IS NOT NULL;

CREATE TABLE tl.asset_vectors(
 asset_id bigint NOT NULL REFERENCES tl.assets ON DELETE CASCADE,
 model text NOT NULL,
 embedding vector NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (asset_id,model));

GRANT SELECT,INSERT,UPDATE,DELETE ON tl.assets,tl.message_assets,tl.asset_faces,tl.asset_vectors TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.assets_id_seq,tl.asset_faces_id_seq TO throughline_app;

-- 2. Search text ---------------------------------------------------------------
-- people=false leaves out the depicted people's names: a restricted asker
-- searches that document (computed per call), never the stored tsv.
CREATE FUNCTION tl.asset_document(a tl.assets,people boolean) RETURNS tsvector LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
 SELECT setweight(to_tsvector('english',concat_ws(' ',a.kind,array_to_string(a.tags,' '))),'A')
  || setweight(to_tsvector('english',coalesce(a.caption,'')),'B')
  || setweight(to_tsvector('english',concat_ws(' ',a.place->>'city',a.place->>'state',a.place->>'country')),'B')
  || CASE WHEN people THEN setweight(to_tsvector('english',coalesce((
      SELECT string_agg(DISTINCT p.display_name,' ') FROM tl.asset_faces f
      JOIN tl.identifiers i ON i.id=f.identifier_id
      JOIN tl.people p ON p.id=i.person_id
      WHERE f.asset_id=a.id AND i.status='confirmed' AND p.merged_into IS NULL AND p.display_name IS NOT NULL),'')),'B')
     ELSE ''::tsvector END
  || setweight(to_tsvector('english',coalesce(a.ocr_text,'')),'C')
  || setweight(to_tsvector('english',coalesce(regexp_replace(a.original_name,'[._]+',' ','g'),'')),'D') $$;
REVOKE EXECUTE ON FUNCTION tl.asset_document(tl.assets,boolean) FROM PUBLIC;

-- Faces and names change outside this row: store.replace_faces and
-- store.refresh_depicts touch the row (updated_at) to recompute it.
CREATE FUNCTION tl.assets_tsv() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='UPDATE' THEN NEW.updated_at := now(); END IF;
 NEW.tsv := tl.asset_document(NEW,true);
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.assets_tsv() FROM PUBLIC;
CREATE TRIGGER assets_tsv BEFORE INSERT OR UPDATE ON tl.assets FOR EACH ROW EXECUTE FUNCTION tl.assets_tsv();

-- 3. Nearest-neighbour index per model -------------------------------------------
-- Several models coexist and their sizes come from the stored vectors, as for
-- tl.ensure_embedding_index: one partial half-precision HNSW per (model, dims).
CREATE FUNCTION tl.ensure_asset_vector_index(model_name text,dims int) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE target text;
BEGIN
 IF model_name IS NULL OR dims IS NULL OR dims<1 OR dims>4000 THEN RETURN false; END IF;
 target := tl.embedding_index_name('asset_vectors',model_name,dims);
 IF to_regclass(format('tl.%I',target)) IS NULL THEN
  EXECUTE format('CREATE INDEX %I ON tl.asset_vectors USING hnsw ((embedding::halfvec(%s)) halfvec_cosine_ops) WHERE model=%L AND vector_dims(embedding)=%s',target,dims,model_name,dims);
 END IF;
 RETURN true;
END $$;
CREATE FUNCTION tl.ensure_asset_vector_indexes() RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE r record; n int := 0;
BEGIN
 FOR r IN SELECT DISTINCT v.model,vector_dims(v.embedding) AS dims FROM tl.asset_vectors v LOOP
  IF tl.ensure_asset_vector_index(r.model,r.dims) THEN n := n+1; END IF;
 END LOOP;
 RETURN n;
END $$;
REVOKE EXECUTE ON FUNCTION tl.ensure_asset_vector_index(text,int),tl.ensure_asset_vector_indexes() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.ensure_asset_vector_index(text,int),tl.ensure_asset_vector_indexes() TO throughline_app;

-- 4. Registries (seed hooks, so a truncated registry comes back) ----------------
INSERT INTO tl.seed_hooks(name,ord,statement) VALUES
 ('image_link_types',63,$seed$INSERT INTO tl.link_types(type,description,inverse,status,origin) VALUES
 ('depicts','An image of the message shows this person (a confirmed face)','depicted_in','active','core')
 ON CONFLICT (type) DO NOTHING$seed$),
 ('image_identifier_schemes',63,$seed$INSERT INTO tl.identifier_schemes(system,kind,same_system,same_kind,normalizer) VALUES
 ('immich','face','immich','face','lower')
 ON CONFLICT (system,kind) DO NOTHING$seed$);
DO $$ BEGIN
 EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='image_link_types');
 EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='image_identifier_schemes');
END $$;

-- 5. Gated views -------------------------------------------------------------------
-- A link row is visible with its (current) message. An asset is visible when
-- one of its messages is. Faces name people: unrestricted only.
SELECT tl.install_gate('message_assets','api.message_visible(v.message_id)',
 'SELECT m.id AS message_id,ma.channel_id,c.name AS channel,ma.external_id,ma.asset_id,ma.ord,ma.role,ma.source_ref
  FROM tl.message_assets ma
  JOIN tl.channels c ON c.id=ma.channel_id
  JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL');
SELECT tl.install_gate('assets',
 'EXISTS(SELECT 1 FROM tl.message_assets ma JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL WHERE ma.asset_id=v.id AND api.message_visible(m.id))',
 'SELECT a.id,a.sha256,a.immich_id,a.mime,a.bytes,a.width,a.height,a.duration_s,a.kind,a.tags,a.ocr_text,a.caption,a.taken_at,a.lat,a.lon,a.place,a.nsfw,a.nsfw_labels,a.origin,a.original_name,a.status,a.created_at,a.updated_at
  FROM tl.assets a');
SELECT tl.install_gate('asset_faces','false',
 'SELECT f.id,f.asset_id,f.immich_face_id,f.identifier_id,i.value AS immich_person_id,i.label AS immich_name,i.status AS identifier_status,
   CASE WHEN i.status=''confirmed'' THEN i.person_id END AS person_id,
   CASE WHEN i.status=''confirmed'' THEN p.display_name END AS name,f.bbox,f.score
  FROM tl.asset_faces f
  LEFT JOIN tl.identifiers i ON i.id=f.identifier_id
  LEFT JOIN tl.people p ON p.id=i.person_id');

-- 6. Functions -----------------------------------------------------------------------
-- The images of many messages in one call (response shaping). Messages the
-- asker may not read give no rows; people are named for unrestricted askers
-- only (an empty list otherwise). nsfw assets (nsfw >= 0.6 or tag nsfw) are
-- left out unless include_nsfw. OCR text is cut to 300 characters.
CREATE FUNCTION api.assets_of(message_ids bigint[],include_nsfw boolean DEFAULT false)
 RETURNS TABLE(message_id bigint,asset_id bigint,ord integer,role text,kind text,tags text[],people jsonb,place jsonb,taken_at timestamptz,ocr text,caption text,width integer,height integer,mime text,has_media boolean)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 WITH ids AS MATERIALIZED (SELECT DISTINCT u.id FROM unnest(assets_of.message_ids) u(id) WHERE u.id IS NOT NULL LIMIT 5000),
 vis AS MATERIALIZED (SELECT m.id,m.channel_id,m.external_id FROM ids JOIN tl.messages m ON m.id=ids.id WHERE api.message_visible(m.id)),
 open AS MATERIALIZED (SELECT api.asker_unrestricted() AS ok)
 SELECT v.id,a.id,ma.ord,ma.role,a.kind,a.tags,
  CASE WHEN o.ok THEN coalesce((
   SELECT jsonb_agg(jsonb_build_object('person_id',x.person_id,'name',x.name) ORDER BY x.name,x.person_id)
   FROM (SELECT DISTINCT p.id AS person_id,p.display_name AS name FROM tl.asset_faces f
    JOIN tl.identifiers i ON i.id=f.identifier_id JOIN tl.people p ON p.id=i.person_id
    WHERE f.asset_id=a.id AND i.status='confirmed' AND p.merged_into IS NULL) x),'[]'::jsonb)
  ELSE '[]'::jsonb END,
  a.place,a.taken_at,left(a.ocr_text,300),a.caption,a.width,a.height,a.mime,a.immich_id IS NOT NULL
 FROM vis v
 JOIN tl.message_assets ma ON ma.channel_id=v.channel_id AND ma.external_id=v.external_id
 JOIN tl.assets a ON a.id=ma.asset_id
 CROSS JOIN open o
 WHERE assets_of.include_nsfw OR NOT (coalesce(a.nsfw,0)>=0.6 OR 'nsfw'=ANY(a.tags))
 ORDER BY v.id,ma.ord,a.id $$;

-- Scored assets -> the best-scoring asset of each in-scope current message.
-- Visibility comes from tl.recall_scope (it reads the gated
-- api.message_versions). Internal: called by the leg functions below.
CREATE FUNCTION tl.asset_pick(aids bigint[],sims real[],max_results integer,include_nsfw boolean,message_ids bigint[],
 kinds text[],channels text[],persons bigint[],person_role text,after_at timestamptz,before_at timestamptz,time_basis text,
 lat float8,lon float8,meters float8,thread_ids bigint[],include_outside boolean,must_terms text[],exclude_message_ids bigint[])
 RETURNS TABLE(message_id bigint,asset_id bigint,score real)
 LANGUAGE sql STABLE SET search_path=pg_catalog,api,public AS $$
 WITH near AS MATERIALIZED (
  SELECT u.aid,u.sim FROM unnest(asset_pick.aids,asset_pick.sims) u(aid,sim)
  JOIN tl.assets a ON a.id=u.aid
  WHERE asset_pick.include_nsfw OR NOT (coalesce(a.nsfw,0)>=0.6 OR 'nsfw'=ANY(a.tags))),
 linked AS MATERIALIZED (
  SELECT m.id AS mid,n.aid,n.sim FROM near n
  JOIN tl.message_assets ma ON ma.asset_id=n.aid
  JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL
  WHERE asset_pick.message_ids IS NULL OR m.id=ANY(asset_pick.message_ids)),
 ok AS MATERIALIZED (
  SELECT s.message_id FROM tl.recall_scope(ARRAY(SELECT DISTINCT l.mid FROM linked l),asset_pick.kinds,asset_pick.channels,asset_pick.persons,asset_pick.person_role,asset_pick.after_at,asset_pick.before_at,asset_pick.time_basis,asset_pick.lat,asset_pick.lon,asset_pick.meters,asset_pick.thread_ids,asset_pick.include_outside,NULL::timestamptz,NULL::timestamptz,asset_pick.must_terms,asset_pick.exclude_message_ids) s)
 SELECT b.mid,b.aid,b.sim FROM (
  SELECT DISTINCT ON (l.mid) l.mid,l.aid,l.sim FROM linked l JOIN ok ON ok.message_id=l.mid
  ORDER BY l.mid,l.sim DESC,l.aid) b
 ORDER BY b.sim DESC,b.mid LIMIT asset_pick.max_results $$;
REVOKE EXECUTE ON FUNCTION tl.asset_pick(bigint[],real[],integer,boolean,bigint[],text[],text[],bigint[],text,timestamptz,timestamptz,text,float8,float8,float8,bigint[],boolean,text[],bigint[]) FROM PUBLIC;

-- Messages whose images are nearest to a query embedding of one image model
-- (gemini-embedding-2, siglip2-so400m-384). The model's HNSW index is asked
-- first for 8x max_results assets; when they do not fill max_results after
-- the hard scope, every vector of the model is ranked exactly.
CREATE FUNCTION api.visual_records(
    query_embedding vector,
    model text,
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
    include_nsfw boolean DEFAULT false
)
 RETURNS TABLE(message_id bigint, asset_id bigint, score real)
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'api', 'public'
 SET "hnsw.iterative_scan" TO 'relaxed_order'
 SET "hnsw.ef_search" TO '200'
AS $function$
#variable_conflict use_column
DECLARE
 dims int := vector_dims(query_embedding); aids bigint[]; sims real[];
 f_m bigint[]; f_a bigint[]; f_s real[];
BEGIN
 IF visual_records.as_of IS NOT NULL OR visual_records.known_by IS NOT NULL THEN
  RAISE EXCEPTION 'visual_records: image vectors exist for current assets only; as_of and known_by are not supported' USING ERRCODE='feature_not_supported';
 END IF;
 IF coalesce(visual_records.max_results,0) <= 0 OR visual_records.model IS NULL THEN RETURN; END IF;
 IF to_regclass(format('tl.%I',tl.embedding_index_name('asset_vectors',visual_records.model,dims))) IS NOT NULL THEN
  EXECUTE format($f$SELECT array_agg(x.asset_id),array_agg(x.s) FROM (
    SELECT v.asset_id,(1-(v.embedding <=> $1))::real AS s FROM tl.asset_vectors v
    WHERE v.model=%L AND vector_dims(v.embedding)=%s
    ORDER BY v.embedding::halfvec(%s) <=> $1::halfvec(%s) LIMIT $2) x$f$,visual_records.model,dims,dims,dims)
   INTO aids,sims USING query_embedding,greatest(visual_records.max_results*8,80);
  IF coalesce(cardinality(aids),0) > 0 THEN
   SELECT array_agg(p.message_id ORDER BY p.score DESC,p.message_id),array_agg(p.asset_id ORDER BY p.score DESC,p.message_id),array_agg(p.score ORDER BY p.score DESC,p.message_id)
    INTO f_m,f_a,f_s
    FROM tl.asset_pick(aids,sims,visual_records.max_results,visual_records.include_nsfw,visual_records.message_ids,visual_records.kinds,visual_records.channels,visual_records.persons,visual_records.person_role,visual_records.after_at,visual_records.before_at,visual_records.time_basis,visual_records.lat,visual_records.lon,visual_records.meters,visual_records.thread_ids,visual_records.include_outside,visual_records.must_terms,visual_records.exclude_message_ids) p;
   IF coalesce(cardinality(f_m),0) >= visual_records.max_results THEN
    RETURN QUERY SELECT * FROM unnest(f_m,f_a,f_s);
    RETURN;
   END IF;
  END IF;
 END IF;
 SELECT array_agg(v.asset_id),array_agg((1-(v.embedding <=> visual_records.query_embedding))::real) INTO aids,sims
  FROM tl.asset_vectors v
  WHERE v.model=visual_records.model AND vector_dims(v.embedding)=dims
   AND EXISTS(SELECT 1 FROM tl.message_assets ma WHERE ma.asset_id=v.asset_id);
 IF coalesce(cardinality(aids),0)=0 THEN RETURN; END IF;
 RETURN QUERY SELECT * FROM tl.asset_pick(aids,sims,visual_records.max_results,visual_records.include_nsfw,visual_records.message_ids,visual_records.kinds,visual_records.channels,visual_records.persons,visual_records.person_role,visual_records.after_at,visual_records.before_at,visual_records.time_basis,visual_records.lat,visual_records.lon,visual_records.meters,visual_records.thread_ids,visual_records.include_outside,visual_records.must_terms,visual_records.exclude_message_ids);
END $function$;

-- Assets matching a keyword query on their search text (kind, tags,
-- caption, place, people, OCR, file name), best first: all query words
-- (score above 1) before part of them (at least two, or the only one). For a
-- restricted asker only assets of visible messages are searched, on a text
-- without the depicted people's names. Internal.
CREATE FUNCTION tl.asset_text_hits(query text,asset_ids bigint[],cap integer)
 RETURNS TABLE(asset_id bigint, score real)
 LANGUAGE plpgsql STABLE SET search_path=pg_catalog,api,public AS $function$
#variable_conflict use_column
DECLARE
 q_and tsquery; q_or tsquery; atoms tsquery[]; open boolean := api.asker_unrestricted();
BEGIN
 q_and := websearch_to_tsquery('english',coalesce(asset_text_hits.query,''));
 IF numnode(q_and)=0 OR coalesce(asset_text_hits.cap,0)<=0 THEN RETURN; END IF;
 IF q_and::text !~ '[|!()]' AND position(' & ' in q_and::text)>0 THEN
  atoms := ARRAY(SELECT x::tsquery FROM regexp_split_to_table(q_and::text,' & ') x);
  q_or := (SELECT string_agg('('||a::text||')',' | ') FROM unnest(atoms) a)::tsquery;
 ELSE
  q_or := q_and;
 END IF;
 RETURN QUERY
 WITH docs AS MATERIALIZED (
  SELECT a.id,a.tsv AS doc FROM tl.assets a
  WHERE open AND a.tsv @@ q_or AND (asset_text_hits.asset_ids IS NULL OR a.id=ANY(asset_text_hits.asset_ids))
  UNION ALL
  SELECT d.id,d.doc FROM (
   SELECT a.id,tl.asset_document(a,false) AS doc FROM tl.assets a
   WHERE NOT open AND (asset_text_hits.asset_ids IS NULL OR a.id=ANY(asset_text_hits.asset_ids))
    AND EXISTS(SELECT 1 FROM tl.message_assets ma JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL
     WHERE ma.asset_id=a.id AND api.message_visible(m.id))) d
  WHERE d.doc @@ q_or),
 scored AS (
  SELECT d.id,
   CASE WHEN d.doc @@ q_and THEN 1+ts_rank_cd(d.doc,q_and,32)
    ELSE (SELECT count(*) FROM unnest(atoms) t WHERE d.doc @@ t)::real/cardinality(atoms)*ts_rank_cd(d.doc,q_or,32) END AS s,
   d.doc @@ q_and AS all_terms,
   CASE WHEN atoms IS NULL THEN 1 ELSE (SELECT count(*) FROM unnest(atoms) t WHERE d.doc @@ t) END AS matched,
   coalesce(cardinality(atoms),1) AS n
  FROM docs d)
 SELECT x.id,x.s::real FROM scored x WHERE x.all_terms OR x.matched>=least(2,x.n)
 ORDER BY x.s DESC,x.id LIMIT asset_text_hits.cap;
END $function$;
REVOKE EXECUTE ON FUNCTION tl.asset_text_hits(text,bigint[],integer) FROM PUBLIC;

-- Messages whose images match a keyword query (tl.asset_text_hits), best
-- image per message, under the hard scope of search_records.
CREATE FUNCTION api.image_search_records(
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
    message_ids bigint[] DEFAULT NULL::bigint[],
    include_nsfw boolean DEFAULT false
)
 RETURNS TABLE(message_id bigint, asset_id bigint, score real)
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'api', 'public'
AS $function$
#variable_conflict use_column
DECLARE aids bigint[]; sims real[];
BEGIN
 IF image_search_records.as_of IS NOT NULL OR image_search_records.known_by IS NOT NULL THEN
  RAISE EXCEPTION 'image_search_records: image text exists for current assets only; as_of and known_by are not supported' USING ERRCODE='feature_not_supported';
 END IF;
 IF coalesce(image_search_records.max_results,0) <= 0 THEN RETURN; END IF;
 SELECT array_agg(h.asset_id),array_agg(h.score) INTO aids,sims FROM tl.asset_text_hits(image_search_records.query,NULL,5000) h;
 IF coalesce(cardinality(aids),0)=0 THEN RETURN; END IF;
 RETURN QUERY SELECT * FROM tl.asset_pick(aids,sims,image_search_records.max_results,image_search_records.include_nsfw,image_search_records.message_ids,image_search_records.kinds,image_search_records.channels,image_search_records.persons,image_search_records.person_role,image_search_records.after_at,image_search_records.before_at,image_search_records.time_basis,image_search_records.lat,image_search_records.lon,image_search_records.meters,image_search_records.thread_ids,image_search_records.include_outside,image_search_records.must_terms,image_search_records.exclude_message_ids);
END $function$;

-- Asset-level search (POST /v1/assets/search). An asset is visible when the
-- asker is unrestricted or one of its messages is visible. asset_ids
-- restricts the search to a prefiltered set (exact ranking).
CREATE FUNCTION api.asset_visible(asset_id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF api.asker_unrestricted() THEN RETURN EXISTS(SELECT 1 FROM tl.assets a WHERE a.id=asset_visible.asset_id); END IF;
 RETURN EXISTS(SELECT 1 FROM tl.message_assets ma JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL
  WHERE ma.asset_id=asset_visible.asset_id AND api.message_visible(m.id));
END $$;

CREATE FUNCTION api.visual_assets(query_embedding vector,model text,max_results integer DEFAULT 50,include_nsfw boolean DEFAULT false,asset_ids bigint[] DEFAULT NULL::bigint[])
 RETURNS TABLE(asset_id bigint, score real)
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'api', 'public'
 SET "hnsw.iterative_scan" TO 'relaxed_order'
 SET "hnsw.ef_search" TO '200'
AS $function$
#variable_conflict use_column
DECLARE dims int := vector_dims(query_embedding); aids bigint[]; sims real[]; f_a bigint[]; f_s real[];
BEGIN
 IF coalesce(visual_assets.max_results,0) <= 0 OR visual_assets.model IS NULL THEN RETURN; END IF;
 IF visual_assets.asset_ids IS NULL AND to_regclass(format('tl.%I',tl.embedding_index_name('asset_vectors',visual_assets.model,dims))) IS NOT NULL THEN
  EXECUTE format($f$SELECT array_agg(x.asset_id),array_agg(x.s) FROM (
    SELECT v.asset_id,(1-(v.embedding <=> $1))::real AS s FROM tl.asset_vectors v
    WHERE v.model=%L AND vector_dims(v.embedding)=%s
    ORDER BY v.embedding::halfvec(%s) <=> $1::halfvec(%s) LIMIT $2) x$f$,visual_assets.model,dims,dims,dims)
   INTO aids,sims USING query_embedding,greatest(visual_assets.max_results*4,80);
  SELECT array_agg(u.aid ORDER BY u.sim DESC,u.aid),array_agg(u.sim ORDER BY u.sim DESC,u.aid) INTO f_a,f_s
   FROM unnest(aids,sims) u(aid,sim) JOIN tl.assets a ON a.id=u.aid
   WHERE (visual_assets.include_nsfw OR NOT (coalesce(a.nsfw,0)>=0.6 OR 'nsfw'=ANY(a.tags))) AND api.asset_visible(a.id);
  IF coalesce(cardinality(f_a),0) >= visual_assets.max_results THEN
   RETURN QUERY SELECT * FROM unnest(f_a[1:visual_assets.max_results],f_s[1:visual_assets.max_results]);
   RETURN;
  END IF;
 END IF;
 RETURN QUERY
 SELECT v.asset_id,(1-(v.embedding <=> visual_assets.query_embedding))::real AS s
 FROM tl.asset_vectors v JOIN tl.assets a ON a.id=v.asset_id
 WHERE v.model=visual_assets.model AND vector_dims(v.embedding)=dims
  AND (visual_assets.asset_ids IS NULL OR v.asset_id=ANY(visual_assets.asset_ids))
  AND (visual_assets.include_nsfw OR NOT (coalesce(a.nsfw,0)>=0.6 OR 'nsfw'=ANY(a.tags)))
  AND api.asset_visible(v.asset_id)
 ORDER BY 2 DESC,1 LIMIT visual_assets.max_results;
END $function$;

CREATE FUNCTION api.image_search_assets(query text,max_results integer DEFAULT 50,include_nsfw boolean DEFAULT false,asset_ids bigint[] DEFAULT NULL::bigint[])
 RETURNS TABLE(asset_id bigint, score real)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT h.asset_id,h.score FROM tl.asset_text_hits(image_search_assets.query,image_search_assets.asset_ids,5000) h
 JOIN tl.assets a ON a.id=h.asset_id
 WHERE image_search_assets.include_nsfw OR NOT (coalesce(a.nsfw,0)>=0.6 OR 'nsfw'=ANY(a.tags))
 ORDER BY h.score DESC,h.asset_id LIMIT greatest(image_search_assets.max_results,0) $$;

GRANT EXECUTE ON FUNCTION
 api.asset_visible(bigint),
 api.visual_assets(vector,text,integer,boolean,bigint[]),
 api.image_search_assets(text,integer,boolean,bigint[]),
 api.assets_of(bigint[],boolean),
 api.visual_records(vector,text,integer,text[],text[],bigint[],text,timestamptz,timestamptz,text,float8,float8,float8,bigint[],boolean,timestamptz,timestamptz,text[],bigint[],bigint[],boolean),
 api.image_search_records(text,integer,text[],text[],bigint[],text,timestamptz,timestamptz,text,float8,float8,float8,bigint[],boolean,timestamptz,timestamptz,text[],bigint[],bigint[],boolean)
 TO throughline_app,throughline_reader;

-- 7. Purge: the purged channel's links, then assets no other message uses
--    (their faces and vectors cascade). The Immich bytes stay: they are the
--    owner's library. The data-modifying CTE is not visible to the outer
--    statement, so the remaining-link check excludes the channel itself.
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('message_assets',34,
  'WITH gone AS (DELETE FROM tl.message_assets WHERE channel_id=$1 RETURNING asset_id)
   DELETE FROM tl.assets a WHERE a.id IN (SELECT g.asset_id FROM gone g)
    AND NOT EXISTS(SELECT 1 FROM tl.message_assets ma WHERE ma.asset_id=a.id AND ma.channel_id<>$1)');

-- 8. Catalogue -------------------------------------------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('assets_of','sql','assets_of','The images of many messages in one call: kind, tags, depicted people (unrestricted askers only), place, time, OCR (300 characters), caption and size; nsfw images only with include_nsfw','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.assets_of(ARRAY[1,2]::bigint[])','1',NULL,NULL),
 ('visual_records','sql','visual_records','Messages whose images are nearest to a query embedding of an image model (gemini-embedding-2 or siglip2-so400m-384), best image per message, under the hard scope of vector_records; nsfw images only with include_nsfw','approximate','embedding',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.visual_records(''[1,0,0]'',''siglip2-so400m-384'',20)','1',3,20),
 ('image_search_records','sql','image_search_records','Messages whose images match keywords on their search text (kind, tags, caption, place, people, OCR, file name), under the hard scope of search_records; nsfw images only with include_nsfw','exact','free',1,'owner, agent; rows pass the asker gate; people names match for unrestricted askers only','SELECT * FROM api.image_search_records(''beach'',20)','1',2,20),
 ('visual_assets','sql','visual_assets','Visible assets nearest to a query embedding of an image model, optionally within a prefiltered asset_ids set; nsfw images only with include_nsfw','approximate','embedding',1,'owner, agent; an asset is visible with one of its messages','SELECT * FROM api.visual_assets(''[1,0,0]'',''siglip2-so400m-384'',20)','1',3,50),
 ('image_search_assets','sql','image_search_assets','Visible assets matching keywords on their search text, optionally within a prefiltered asset_ids set; nsfw images only with include_nsfw','exact','free',1,'owner, agent; an asset is visible with one of its messages; people names match for unrestricted askers only','SELECT * FROM api.image_search_assets(''beach'',20)','1',2,50);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('assets_of','visual_records','image_search_records','visual_assets','image_search_assets');
