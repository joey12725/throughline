-- Image settings, shared-post previews and location from history
-- (docs/images.md, 2026-11-22). 0063 and 0064 are applied live and never
-- edited; every object they made that changes here is replaced in place.
-- 1. tl.image_settings: one row (seed hook image_settings) holding the
--    owner's choices: nsfw_mode (hide | blur | show) for unrestricted askers
--    and restricted_nsfw_mode for restricted ones, nsfw_threshold (replaces
--    the 0.6 of 0064), unscanned (hide | show: images not scored yet),
--    shared_previews (include | exclude), location_fallback (off |
--    throughline | throughline_and_immich), location_window_min and
--    location_max_accuracy_m. The app role reads it and updates it through
--    PUT /v1/images/settings (owner tokens only).
-- 2. tl.image_policy(): the settings as they apply to the current asker
--    (nsfw_mode is restricted_nsfw_mode for a restricted asker). STABLE,
--    SECURITY DEFINER; every nsfw filter reads it once per statement.
-- 3. tl.asset_safe(nsfw,kind,enrich,threshold,unscanned) (pure, inlinable)
--    and tl.asset_clean(...,tags,...) (safe and, for an unscored asset, no
--    tag nsfw). A scored asset is judged by its score against the current
--    threshold, so filtering follows a changed threshold at once; the tag
--    follows at the next tags stage. tl.asset_safe(nsfw,kind,enrich) now
--    reads the threshold and unscanned from the settings (STABLE).
-- 4. message_assets role 'preview': the cover image of a shared post or reel
--    (a message whose only text is a link to it). Preview links write no
--    depicts links (tl.refresh_asset_depicts).
-- 5. tl.assets.loc_method ('exif' | 'track') and loc_accuracy_m: where the
--    asset's lat/lon came from (EXIF GPS, or the nearest location track
--    point, pipeline stage locate).
-- 6. Filters by mode: hide leaves unsafe assets out unless include_nsfw;
--    blur and show keep them (api.assets_of flags them: nsfw, blurred);
--    shared_previews=exclude leaves preview links out of images[] and image
--    search. api.assets gains preview, loc_method and loc_accuracy_m;
--    api.assets_of gains nsfw, blurred and location_source.

-- 1. Settings -------------------------------------------------------------------------
CREATE TABLE tl.image_settings(
 one boolean PRIMARY KEY DEFAULT true CHECK (one),
 nsfw_mode text NOT NULL DEFAULT 'hide' CHECK (nsfw_mode IN ('hide','blur','show')),
 nsfw_threshold real NOT NULL DEFAULT 0.6 CHECK (nsfw_threshold > 0 AND nsfw_threshold <= 1),
 unscanned text NOT NULL DEFAULT 'hide' CHECK (unscanned IN ('hide','show')),
 restricted_nsfw_mode text NOT NULL DEFAULT 'hide' CHECK (restricted_nsfw_mode IN ('hide','blur','show')),
 shared_previews text NOT NULL DEFAULT 'include' CHECK (shared_previews IN ('include','exclude')),
 location_fallback text NOT NULL DEFAULT 'throughline_and_immich' CHECK (location_fallback IN ('off','throughline','throughline_and_immich')),
 location_window_min int NOT NULL DEFAULT 30 CHECK (location_window_min BETWEEN 1 AND 1440),
 location_max_accuracy_m int NOT NULL DEFAULT 500 CHECK (location_max_accuracy_m BETWEEN 1 AND 100000),
 updated_at timestamptz NOT NULL DEFAULT now());
GRANT SELECT,UPDATE ON tl.image_settings TO throughline_app;
INSERT INTO tl.seed_hooks(name,ord,statement) VALUES
 ('image_settings',65,'INSERT INTO tl.image_settings(one) VALUES (true) ON CONFLICT (one) DO NOTHING');
DO $$ BEGIN
 EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='image_settings');
END $$;

-- 2. The policy of the current asker ------------------------------------------------------
-- Defaults when the row is missing (before tl.reseed()).
CREATE FUNCTION tl.image_policy() RETURNS TABLE(nsfw_mode text,nsfw_threshold real,unscanned text,shared_previews text,unrestricted boolean)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT CASE WHEN u.ok THEN coalesce(s.nsfw_mode,'hide') ELSE coalesce(s.restricted_nsfw_mode,'hide') END,
  coalesce(s.nsfw_threshold,0.6::real),coalesce(s.unscanned,'hide'),coalesce(s.shared_previews,'include'),u.ok
 FROM (SELECT coalesce(api.asker_unrestricted(),false) AS ok) u
 LEFT JOIN tl.image_settings s ON s.one $$;
REVOKE EXECUTE ON FUNCTION tl.image_policy() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.image_policy() TO throughline_app,throughline_reader;

-- 3. Safe and clean -------------------------------------------------------------------------
-- SQL-standard bodies (RETURN): parsed once here, so a caller needs no
-- USAGE on schema tl (the reader role has none), and no SET clause, so the
-- planner inlines the pure ones.
CREATE FUNCTION tl.asset_safe(nsfw real,kind text,enrich jsonb,threshold real,unscanned text) RETURNS boolean
 LANGUAGE sql IMMUTABLE PARALLEL SAFE
 RETURN CASE
  WHEN nsfw IS NOT NULL THEN nsfw < coalesce(threshold,0.6::real)
  WHEN kind='video' THEN true
  WHEN unscanned='show' THEN true
  ELSE coalesce(jsonb_typeof(enrich->'nsfw')='object' AND NOT (enrich->'nsfw') ? 'error',false) END;
CREATE FUNCTION tl.asset_clean(nsfw real,kind text,enrich jsonb,tags text[],threshold real,unscanned text) RETURNS boolean
 LANGUAGE sql IMMUTABLE PARALLEL SAFE
 RETURN tl.asset_safe(nsfw,kind,enrich,threshold,unscanned) AND (nsfw IS NOT NULL OR NOT coalesce('nsfw'=ANY(tags),false));
GRANT EXECUTE ON FUNCTION tl.asset_safe(real,text,jsonb,real,text),tl.asset_clean(real,text,jsonb,text[],real,text)
 TO throughline_app,throughline_reader;
CREATE OR REPLACE FUNCTION tl.asset_safe(nsfw real,kind text,enrich jsonb) RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog
 RETURN (SELECT tl.asset_safe(nsfw,kind,enrich,p.nsfw_threshold,p.unscanned) FROM tl.image_policy() p);

-- 4. Preview links ------------------------------------------------------------------------
ALTER TABLE tl.message_assets DROP CONSTRAINT message_assets_role_check;
ALTER TABLE tl.message_assets ADD CONSTRAINT message_assets_role_check
 CHECK (role IN ('attachment','inline','self','preview'));

-- 5. Location provenance -------------------------------------------------------------------
ALTER TABLE tl.assets
 ADD COLUMN loc_method text CHECK (loc_method IN ('exif','track')),
 ADD COLUMN loc_accuracy_m real;
UPDATE tl.assets SET loc_method='exif' WHERE lat IS NOT NULL AND lon IS NOT NULL AND loc_method IS NULL;

-- The 0064 function with preview links left out of the faces that name people.
CREATE OR REPLACE FUNCTION tl.refresh_asset_depicts(asset_ids bigint[]) RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
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
  JOIN tl.message_assets ma ON ma.channel_id=m.channel_id AND ma.external_id=m.external_id AND ma.role<>'preview'
  JOIN tl.asset_faces f ON f.asset_id=ma.asset_id
  JOIN tl.identifiers i ON i.id=f.identifier_id AND i.status='confirmed'
  JOIN tl.people p ON p.id=i.person_id AND p.merged_into IS NULL
  WHERE m.id=ANY(mids) GROUP BY m.id,p.id) w;
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

-- 6. Filters ----------------------------------------------------------------------------------
-- api.assets: the 0064 columns (safe now follows the settings) plus preview
-- (every link of the asset is a preview), loc_method and loc_accuracy_m.
SELECT tl.install_gate('assets',
 'EXISTS(SELECT 1 FROM tl.message_assets ma JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL WHERE ma.asset_id=v.id AND api.message_visible(m.id))',
 'SELECT a.id,a.sha256,a.immich_id,a.mime,a.bytes,a.width,a.height,a.duration_s,a.kind,a.tags,a.ocr_text,a.caption,a.taken_at,a.lat,a.lon,a.place,a.nsfw,a.nsfw_labels,a.origin,a.original_name,a.status,a.created_at,a.updated_at,
   tl.asset_clean(a.nsfw,a.kind,a.enrich,a.tags,p.nsfw_threshold,p.unscanned) AS safe,
   (EXISTS(SELECT 1 FROM tl.message_assets x WHERE x.asset_id=a.id AND x.role=''preview'')
    AND NOT EXISTS(SELECT 1 FROM tl.message_assets x WHERE x.asset_id=a.id AND x.role<>''preview'')) AS preview,
   a.loc_method,a.loc_accuracy_m
  FROM tl.assets a CROSS JOIN tl.image_policy() p');

DROP FUNCTION api.assets_of(bigint[],boolean);
-- nsfw: the asset is not safe (flagged). blurred: its thumb and content are
-- served blurred to this request (mode blur, include_nsfw not set).
CREATE FUNCTION api.assets_of(message_ids bigint[],include_nsfw boolean DEFAULT false)
 RETURNS TABLE(message_id bigint,asset_id bigint,ord integer,role text,kind text,tags text[],people jsonb,place jsonb,taken_at timestamptz,ocr text,caption text,width integer,height integer,mime text,has_media boolean,nsfw boolean,blurred boolean,location_source text)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 WITH ids AS MATERIALIZED (SELECT DISTINCT u.id FROM unnest(assets_of.message_ids) u(id) WHERE u.id IS NOT NULL LIMIT 5000),
 vis AS MATERIALIZED (SELECT m.id,m.channel_id,m.external_id FROM ids JOIN tl.messages m ON m.id=ids.id WHERE api.message_visible(m.id)),
 open AS MATERIALIZED (SELECT api.asker_unrestricted() AS ok),
 pol AS MATERIALIZED (SELECT p.nsfw_mode,p.nsfw_threshold,p.unscanned,p.shared_previews FROM tl.image_policy() p),
 hits AS (
  SELECT v.id AS mid,a.*,ma.ord AS link_ord,ma.role AS link_role,
   NOT tl.asset_clean(a.nsfw,a.kind,a.enrich,a.tags,pol.nsfw_threshold,pol.unscanned) AS flagged,pol.nsfw_mode AS mode
  FROM vis v
  JOIN tl.message_assets ma ON ma.channel_id=v.channel_id AND ma.external_id=v.external_id
  JOIN tl.assets a ON a.id=ma.asset_id
  CROSS JOIN pol
  WHERE (pol.shared_previews='include' OR ma.role<>'preview'))
 SELECT h.mid,h.id,h.link_ord,h.link_role,h.kind,h.tags,
  CASE WHEN o.ok THEN coalesce((
   SELECT jsonb_agg(jsonb_build_object('person_id',x.person_id,'name',x.name) ORDER BY x.name,x.person_id)
   FROM (SELECT DISTINCT p.id AS person_id,p.display_name AS name FROM tl.asset_faces f
    JOIN tl.identifiers i ON i.id=f.identifier_id JOIN tl.people p ON p.id=i.person_id
    WHERE f.asset_id=h.id AND i.status='confirmed' AND p.merged_into IS NULL) x),'[]'::jsonb)
  ELSE '[]'::jsonb END,
  h.place,h.taken_at,left(h.ocr_text,300),h.caption,h.width,h.height,h.mime,h.immich_id IS NOT NULL,
  h.flagged,h.flagged AND h.mode='blur' AND NOT assets_of.include_nsfw,h.loc_method
 FROM hits h CROSS JOIN open o
 WHERE assets_of.include_nsfw OR h.mode<>'hide' OR NOT h.flagged
 ORDER BY h.mid,h.link_ord,h.id $$;
GRANT EXECUTE ON FUNCTION api.assets_of(bigint[],boolean) TO throughline_app,throughline_reader;

CREATE OR REPLACE FUNCTION tl.asset_pick(aids bigint[],sims real[],max_results integer,include_nsfw boolean,message_ids bigint[],
 kinds text[],channels text[],persons bigint[],person_role text,after_at timestamptz,before_at timestamptz,time_basis text,
 lat float8,lon float8,meters float8,thread_ids bigint[],include_outside boolean,must_terms text[],exclude_message_ids bigint[])
 RETURNS TABLE(message_id bigint,asset_id bigint,score real)
 LANGUAGE sql STABLE SET search_path=pg_catalog,api,public AS $$
 WITH pol AS MATERIALIZED (SELECT p.nsfw_mode,p.nsfw_threshold,p.unscanned,p.shared_previews FROM tl.image_policy() p),
 near AS MATERIALIZED (
  SELECT u.aid,u.sim FROM unnest(asset_pick.aids,asset_pick.sims) u(aid,sim)
  JOIN tl.assets a ON a.id=u.aid CROSS JOIN pol
  WHERE asset_pick.include_nsfw OR pol.nsfw_mode<>'hide'
   OR tl.asset_clean(a.nsfw,a.kind,a.enrich,a.tags,pol.nsfw_threshold,pol.unscanned)),
 linked AS MATERIALIZED (
  SELECT m.id AS mid,n.aid,n.sim FROM near n
  JOIN tl.message_assets ma ON ma.asset_id=n.aid
  JOIN tl.messages m ON m.channel_id=ma.channel_id AND m.external_id=ma.external_id AND m.superseded_by IS NULL
  CROSS JOIN pol
  WHERE (asset_pick.message_ids IS NULL OR m.id=ANY(asset_pick.message_ids))
   AND (pol.shared_previews='include' OR ma.role<>'preview')),
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
 pol record; any_nsfw boolean; previews boolean;
BEGIN
 IF coalesce(visual_assets.max_results,0) <= 0 OR visual_assets.model IS NULL THEN RETURN; END IF;
 SELECT * INTO pol FROM tl.image_policy();
 any_nsfw := visual_assets.include_nsfw OR pol.nsfw_mode<>'hide';
 previews := pol.shared_previews='include';
 IF visual_assets.asset_ids IS NULL AND to_regclass(format('tl.%I',tl.embedding_index_name('asset_vectors',visual_assets.model,dims))) IS NOT NULL THEN
  EXECUTE format($f$SELECT array_agg(x.asset_id),array_agg(x.s) FROM (
    SELECT v.asset_id,(1-(v.embedding <=> $1))::real AS s FROM tl.asset_vectors v
    WHERE v.model=%L AND vector_dims(v.embedding)=%s
    ORDER BY v.embedding::halfvec(%s) <=> $1::halfvec(%s) LIMIT $2) x$f$,visual_assets.model,dims,dims,dims)
   INTO aids,sims USING query_embedding,greatest(visual_assets.max_results*4,80);
  SELECT array_agg(u.aid ORDER BY u.sim DESC,u.aid),array_agg(u.sim ORDER BY u.sim DESC,u.aid) INTO f_a,f_s
   FROM unnest(aids,sims) u(aid,sim) JOIN tl.assets a ON a.id=u.aid
   WHERE (any_nsfw OR tl.asset_clean(a.nsfw,a.kind,a.enrich,a.tags,pol.nsfw_threshold,pol.unscanned))
    AND (previews OR EXISTS(SELECT 1 FROM tl.message_assets x WHERE x.asset_id=a.id AND x.role<>'preview'))
    AND api.asset_visible(a.id);
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
  AND (any_nsfw OR tl.asset_clean(a.nsfw,a.kind,a.enrich,a.tags,pol.nsfw_threshold,pol.unscanned))
  AND (previews OR EXISTS(SELECT 1 FROM tl.message_assets x WHERE x.asset_id=a.id AND x.role<>'preview'))
  AND api.asset_visible(v.asset_id)
 ORDER BY 2 DESC,1 LIMIT visual_assets.max_results;
END $function$;

CREATE OR REPLACE FUNCTION api.image_search_assets(query text,max_results integer DEFAULT 50,include_nsfw boolean DEFAULT false,asset_ids bigint[] DEFAULT NULL::bigint[])
 RETURNS TABLE(asset_id bigint, score real)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 WITH pol AS MATERIALIZED (SELECT p.nsfw_mode,p.nsfw_threshold,p.unscanned,p.shared_previews FROM tl.image_policy() p)
 SELECT h.asset_id,h.score FROM tl.asset_text_hits(image_search_assets.query,image_search_assets.asset_ids,5000) h
 JOIN tl.assets a ON a.id=h.asset_id CROSS JOIN pol
 WHERE (image_search_assets.include_nsfw OR pol.nsfw_mode<>'hide'
   OR tl.asset_clean(a.nsfw,a.kind,a.enrich,a.tags,pol.nsfw_threshold,pol.unscanned))
  AND (pol.shared_previews='include' OR EXISTS(SELECT 1 FROM tl.message_assets x WHERE x.asset_id=a.id AND x.role<>'preview'))
 ORDER BY h.score DESC,h.asset_id LIMIT greatest(image_search_assets.max_results,0) $$;

-- 7. Catalogue ---------------------------------------------------------------------------------
UPDATE tl.api_functions SET description='The images of many messages in one call: kind, tags, depicted people (unrestricted askers only), place and location_source (exif or track), time, OCR (300 characters), caption, size and the nsfw and blurred flags; unsafe images follow the image settings (hide: only with include_nsfw; blur and show: flagged), shared-post previews follow shared_previews'
 WHERE name='assets_of';
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('assets_of','visual_assets','image_search_assets');
UPDATE tl.catalog_sections SET body=jsonb_set(
 body,'{receipt_kinds}',
 coalesce(body->'receipt_kinds','[]'::jsonb)||jsonb_build_array('image_settings'))
 WHERE subject='write_contract'
  AND NOT (coalesce(body->'receipt_kinds','[]'::jsonb) ? 'image_settings');
