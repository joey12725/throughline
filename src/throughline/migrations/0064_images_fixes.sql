-- Image fixes after review (docs/images.md, 2026-11-21).
-- 1. Face identifiers start suspected: existing unassigned Immich face
--    clusters that were stored as confirmed become suspected.
-- 2. tl.asset_safe: an asset is safe when NudeNet scored it below 0.6, or it
--    is a video (NudeNet is image-only), or never otherwise. Unscored
--    images, failed scores and given-up scores are unsafe. Every nsfw filter
--    uses it; api.assets gains the column safe.
-- 3. depicts links (face recognition) are for unrestricted askers only:
--    the links gate and api.link_image_visible (history, links_asof,
--    provenance) leave them out for restricted askers.
-- 4. Identity changes of an Immich face identifier (person, status) and
--    person renames and merges refresh the depicts links and search text of
--    the affected assets (triggers; tl.refresh_asset_depicts).
-- 5. image_search_records applies the hard scope before its 5000-hit cap:
--    when the capped hits do not fill max_results, every hit is ranked.

-- 1. Suspected by default -------------------------------------------------------------
-- Before the triggers of section 4, so this update refreshes nothing (these
-- rows have no person and so no depicts links).
UPDATE tl.identifiers SET status='suspected'
 WHERE system='immich' AND kind='face' AND person_id IS NULL AND status='confirmed';

-- 2. Safe assets ----------------------------------------------------------------------
-- The nsfw stage completed successfully when enrich.nsfw is an object without
-- an error (a skipped video is {"skipped":"video"}). A given-up stage keeps
-- its error ({"error":...,"gave_up":true}) and so stays unsafe.
CREATE FUNCTION tl.asset_safe(nsfw real,kind text,enrich jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog AS $$
 SELECT CASE
  WHEN asset_safe.nsfw IS NOT NULL THEN asset_safe.nsfw < 0.6
  WHEN asset_safe.kind='video' THEN true
  ELSE coalesce(jsonb_typeof(asset_safe.enrich->'nsfw')='object' AND NOT (asset_safe.enrich->'nsfw') ? 'error',false) END $$;
GRANT EXECUTE ON FUNCTION tl.asset_safe(real,text,jsonb) TO throughline_app,throughline_reader;

-- api.assets: the same columns plus safe (tl.asset_safe and no tag nsfw).
SELECT tl.install_gate('assets',
 'EXISTS(SELECT 1 FROM tl.message_assets ma JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL WHERE ma.asset_id=v.id AND api.message_visible(m.id))',
 'SELECT a.id,a.sha256,a.immich_id,a.mime,a.bytes,a.width,a.height,a.duration_s,a.kind,a.tags,a.ocr_text,a.caption,a.taken_at,a.lat,a.lon,a.place,a.nsfw,a.nsfw_labels,a.origin,a.original_name,a.status,a.created_at,a.updated_at,
   (tl.asset_safe(a.nsfw,a.kind,a.enrich) AND NOT ''nsfw''=ANY(a.tags)) AS safe
  FROM tl.assets a');

CREATE OR REPLACE FUNCTION api.assets_of(message_ids bigint[],include_nsfw boolean DEFAULT false)
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
 WHERE assets_of.include_nsfw OR (tl.asset_safe(a.nsfw,a.kind,a.enrich) AND NOT 'nsfw'=ANY(a.tags))
 ORDER BY v.id,ma.ord,a.id $$;

CREATE OR REPLACE FUNCTION tl.asset_pick(aids bigint[],sims real[],max_results integer,include_nsfw boolean,message_ids bigint[],
 kinds text[],channels text[],persons bigint[],person_role text,after_at timestamptz,before_at timestamptz,time_basis text,
 lat float8,lon float8,meters float8,thread_ids bigint[],include_outside boolean,must_terms text[],exclude_message_ids bigint[])
 RETURNS TABLE(message_id bigint,asset_id bigint,score real)
 LANGUAGE sql STABLE SET search_path=pg_catalog,api,public AS $$
 WITH near AS MATERIALIZED (
  SELECT u.aid,u.sim FROM unnest(asset_pick.aids,asset_pick.sims) u(aid,sim)
  JOIN tl.assets a ON a.id=u.aid
  WHERE asset_pick.include_nsfw OR (tl.asset_safe(a.nsfw,a.kind,a.enrich) AND NOT 'nsfw'=ANY(a.tags))),
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

CREATE OR REPLACE FUNCTION api.visual_assets(query_embedding vector,model text,max_results integer DEFAULT 50,include_nsfw boolean DEFAULT false,asset_ids bigint[] DEFAULT NULL::bigint[])
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
   WHERE (visual_assets.include_nsfw OR (tl.asset_safe(a.nsfw,a.kind,a.enrich) AND NOT 'nsfw'=ANY(a.tags))) AND api.asset_visible(a.id);
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
  AND (visual_assets.include_nsfw OR (tl.asset_safe(a.nsfw,a.kind,a.enrich) AND NOT 'nsfw'=ANY(a.tags)))
  AND api.asset_visible(v.asset_id)
 ORDER BY 2 DESC,1 LIMIT visual_assets.max_results;
END $function$;

CREATE OR REPLACE FUNCTION api.image_search_assets(query text,max_results integer DEFAULT 50,include_nsfw boolean DEFAULT false,asset_ids bigint[] DEFAULT NULL::bigint[])
 RETURNS TABLE(asset_id bigint, score real)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT h.asset_id,h.score FROM tl.asset_text_hits(image_search_assets.query,image_search_assets.asset_ids,5000) h
 JOIN tl.assets a ON a.id=h.asset_id
 WHERE image_search_assets.include_nsfw OR (tl.asset_safe(a.nsfw,a.kind,a.enrich) AND NOT 'nsfw'=ANY(a.tags))
 ORDER BY h.score DESC,h.asset_id LIMIT greatest(image_search_assets.max_results,0) $$;

-- 3. depicts links: unrestricted askers only --------------------------------------------
-- The predicate of 0033 (unchanged since), plus the depicts rule.
SELECT tl.install_gate('links','(v.from_kind=''message'' OR v.to_kind=''message'') AND (v.from_kind<>''message'' OR api.message_visible(v.from_id)) AND (v.to_kind<>''message'' OR api.message_visible(v.to_id)) AND api.evidence_visible(v.evidence) AND (v.type<>''depicts'' OR api.asker_unrestricted())');

CREATE OR REPLACE FUNCTION api.link_image_visible(image jsonb) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE fk text := image->>'from_kind'; tk text := image->>'to_kind';
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 IF image IS NULL OR (fk IS DISTINCT FROM 'message' AND tk IS DISTINCT FROM 'message') THEN RETURN false; END IF;
 IF image->>'type'='depicts' THEN RETURN false; END IF;
 IF fk='message' AND NOT api.message_visible((image->>'from_id')::bigint) THEN RETURN false; END IF;
 IF tk='message' AND NOT api.message_visible((image->>'to_id')::bigint) THEN RETURN false; END IF;
 RETURN api.evidence_visible(coalesce(image->'evidence','{}'::jsonb));
END $$;

-- 4. Refresh after identity changes ----------------------------------------------------------
-- The SQL twin of store.refresh_depicts(conn, asset_id) for many assets:
-- touches the assets (search text, people names) and brings the depicts
-- links of their current messages (and of messages whose links cite them)
-- in line with the confirmed faces. Returns the number of link rows
-- added, removed or updated.
CREATE FUNCTION tl.refresh_asset_depicts(asset_ids bigint[]) RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE mids bigint[]; wm bigint[]; wp bigint[]; we jsonb[]; n int := 0; k int;
BEGIN
 IF coalesce(cardinality(refresh_asset_depicts.asset_ids),0)=0 THEN RETURN 0; END IF;
 UPDATE tl.assets a SET updated_at=now() WHERE a.id=ANY(refresh_asset_depicts.asset_ids);
 mids := ARRAY(
  SELECT m.id FROM tl.message_assets ma
  JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL
  WHERE ma.asset_id=ANY(refresh_asset_depicts.asset_ids)
  UNION
  SELECT l.from_id FROM tl.links l
  WHERE l.type='depicts' AND l.job='images.depicts' AND l.from_kind='message' AND l.status='active'
   AND jsonb_typeof(l.evidence->'asset_ids')='array'
   AND EXISTS(SELECT 1 FROM jsonb_array_elements_text(l.evidence->'asset_ids') e(v) WHERE e.v::bigint=ANY(refresh_asset_depicts.asset_ids)));
 IF cardinality(mids)=0 THEN RETURN 0; END IF;
 SELECT array_agg(w.mid),array_agg(w.pid),array_agg(w.ev) INTO wm,wp,we FROM (
  SELECT m.id AS mid,p.id AS pid,jsonb_build_object('asset_ids',to_jsonb(array_agg(DISTINCT ma.asset_id ORDER BY ma.asset_id))) AS ev
  FROM tl.messages m
  JOIN tl.message_assets ma ON ma.channel_id=m.channel_id AND ma.external_id=m.external_id
  JOIN tl.asset_faces f ON f.asset_id=ma.asset_id
  JOIN tl.identifiers i ON i.id=f.identifier_id AND i.status='confirmed'
  JOIN tl.people p ON p.id=i.person_id AND p.merged_into IS NULL
  WHERE m.id=ANY(mids) GROUP BY m.id,p.id) w;
 -- Links no longer wanted, and duplicates of a pair (the oldest stays).
 DELETE FROM tl.links l
 WHERE l.type='depicts' AND l.job='images.depicts' AND l.from_kind='message' AND l.to_kind='person' AND l.status='active'
  AND l.from_id=ANY(mids)
  AND (NOT EXISTS(SELECT 1 FROM unnest(wm,wp) w(mid,pid) WHERE w.mid=l.from_id AND w.pid=l.to_id)
   OR EXISTS(SELECT 1 FROM tl.links d WHERE d.type='depicts' AND d.job='images.depicts' AND d.from_kind='message'
    AND d.to_kind='person' AND d.status='active' AND d.from_id=l.from_id AND d.to_id=l.to_id AND d.id<l.id));
 GET DIAGNOSTICS k = ROW_COUNT; n := n+k;
 IF wm IS NULL THEN RETURN n; END IF;
 UPDATE tl.links l SET evidence=w.ev FROM unnest(wm,wp,we) w(mid,pid,ev)
 WHERE l.type='depicts' AND l.job='images.depicts' AND l.from_kind='message' AND l.to_kind='person' AND l.status='active'
  AND l.from_id=w.mid AND l.to_id=w.pid AND l.evidence IS DISTINCT FROM w.ev;
 GET DIAGNOSTICS k = ROW_COUNT; n := n+k;
 INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,confidence,method,evidence,job,job_version)
 SELECT 'depicts','message',w.mid,'person',w.pid,1,'rule',w.ev,'images.depicts','1'
 FROM unnest(wm,wp,we) w(mid,pid,ev)
 WHERE NOT EXISTS(SELECT 1 FROM tl.links l WHERE l.type='depicts' AND l.job='images.depicts' AND l.from_kind='message'
  AND l.to_kind='person' AND l.status='active' AND l.from_id=w.mid AND l.to_id=w.pid)
 ORDER BY w.mid,w.pid;
 GET DIAGNOSTICS k = ROW_COUNT; n := n+k;
 RETURN n;
END $$;
REVOKE EXECUTE ON FUNCTION tl.refresh_asset_depicts(bigint[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.refresh_asset_depicts(bigint[]) TO throughline_app;

-- An Immich face identifier that names someone before or after the change
-- (confirmed with a person) refreshes the assets with its faces. attach,
-- merge, split and the identifier status route all write tl.identifiers, so
-- they are covered here.
CREATE FUNCTION tl.identifier_refresh_depicts() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE ident bigint := CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
BEGIN
 PERFORM tl.refresh_asset_depicts(ARRAY(SELECT DISTINCT f.asset_id FROM tl.asset_faces f WHERE f.identifier_id=ident));
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.identifier_refresh_depicts() FROM PUBLIC;
CREATE TRIGGER identifier_refresh_depicts_update AFTER UPDATE OF person_id,status ON tl.identifiers
 FOR EACH ROW WHEN (NEW.system='immich' AND NEW.kind='face'
  AND (OLD.status IS DISTINCT FROM NEW.status OR OLD.person_id IS DISTINCT FROM NEW.person_id)
  AND ((OLD.status='confirmed' AND OLD.person_id IS NOT NULL) OR (NEW.status='confirmed' AND NEW.person_id IS NOT NULL)))
 EXECUTE FUNCTION tl.identifier_refresh_depicts();
CREATE TRIGGER identifier_refresh_depicts_insert AFTER INSERT ON tl.identifiers
 FOR EACH ROW WHEN (NEW.system='immich' AND NEW.kind='face' AND NEW.status='confirmed' AND NEW.person_id IS NOT NULL)
 EXECUTE FUNCTION tl.identifier_refresh_depicts();
CREATE TRIGGER identifier_refresh_depicts_delete AFTER DELETE ON tl.identifiers
 FOR EACH ROW WHEN (OLD.system='immich' AND OLD.kind='face' AND OLD.status='confirmed' AND OLD.person_id IS NOT NULL)
 EXECUTE FUNCTION tl.identifier_refresh_depicts();

-- A renamed or merged person: the search text of the assets showing them
-- (names) and, for a merge, their depicts links.
CREATE FUNCTION tl.person_refresh_depicts() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 PERFORM tl.refresh_asset_depicts(ARRAY(
  SELECT DISTINCT f.asset_id FROM tl.asset_faces f JOIN tl.identifiers i ON i.id=f.identifier_id
  WHERE i.person_id=NEW.id AND i.system='immich' AND i.kind='face' AND i.status='confirmed'));
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.person_refresh_depicts() FROM PUBLIC;
CREATE TRIGGER person_refresh_depicts AFTER UPDATE OF display_name,merged_into ON tl.people
 FOR EACH ROW WHEN (OLD.display_name IS DISTINCT FROM NEW.display_name OR OLD.merged_into IS DISTINCT FROM NEW.merged_into)
 EXECUTE FUNCTION tl.person_refresh_depicts();

-- 5. image_search_records: scope before the cap -----------------------------------------------
CREATE OR REPLACE FUNCTION api.image_search_records(
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
DECLARE aids bigint[]; sims real[]; cap int := 5000;
 f_m bigint[]; f_a bigint[]; f_s real[];
BEGIN
 IF image_search_records.as_of IS NOT NULL OR image_search_records.known_by IS NOT NULL THEN
  RAISE EXCEPTION 'image_search_records: image text exists for current assets only; as_of and known_by are not supported' USING ERRCODE='feature_not_supported';
 END IF;
 IF coalesce(image_search_records.max_results,0) <= 0 THEN RETURN; END IF;
 -- The best 5000 hits first; when the scope leaves fewer than max_results
 -- of them and more hits exist, every hit is ranked (as visual_records
 -- falls back to an exact ranking).
 SELECT array_agg(h.asset_id),array_agg(h.score) INTO aids,sims FROM tl.asset_text_hits(image_search_records.query,NULL,cap) h;
 IF coalesce(cardinality(aids),0)=0 THEN RETURN; END IF;
 SELECT array_agg(p.message_id ORDER BY p.score DESC,p.message_id),array_agg(p.asset_id ORDER BY p.score DESC,p.message_id),array_agg(p.score ORDER BY p.score DESC,p.message_id)
  INTO f_m,f_a,f_s
  FROM tl.asset_pick(aids,sims,image_search_records.max_results,image_search_records.include_nsfw,image_search_records.message_ids,image_search_records.kinds,image_search_records.channels,image_search_records.persons,image_search_records.person_role,image_search_records.after_at,image_search_records.before_at,image_search_records.time_basis,image_search_records.lat,image_search_records.lon,image_search_records.meters,image_search_records.thread_ids,image_search_records.include_outside,image_search_records.must_terms,image_search_records.exclude_message_ids) p;
 IF cardinality(aids) < cap OR coalesce(cardinality(f_m),0) >= image_search_records.max_results THEN
  IF f_m IS NOT NULL THEN RETURN QUERY SELECT * FROM unnest(f_m,f_a,f_s); END IF;
  RETURN;
 END IF;
 SELECT array_agg(h.asset_id),array_agg(h.score) INTO aids,sims FROM tl.asset_text_hits(image_search_records.query,NULL,2147483647) h;
 RETURN QUERY SELECT * FROM tl.asset_pick(aids,sims,image_search_records.max_results,image_search_records.include_nsfw,image_search_records.message_ids,image_search_records.kinds,image_search_records.channels,image_search_records.persons,image_search_records.person_role,image_search_records.after_at,image_search_records.before_at,image_search_records.time_basis,image_search_records.lat,image_search_records.lon,image_search_records.meters,image_search_records.thread_ids,image_search_records.include_outside,image_search_records.must_terms,image_search_records.exclude_message_ids);
END $function$;
