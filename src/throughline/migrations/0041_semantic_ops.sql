-- Semantic operators expand and match (QRY-32, QRY-33, QRY-27, CST-5,
-- CST-6, CST-10, ACL-8).
--
-- 1. Set-based expansion functions. Each takes many anchors at once and reads
--    only gated api views, so a restricted asker never reaches a hidden row.
--    They return candidate rows of any stored version; the Python operator
--    passes every message id through api.scope_filter (kinds, as_of,
--    known_by, include_outside and the caller's scope), which keeps exactly
--    the version eligible under that scope.
-- 2. tl.verdicts: the verdict store of api.match, keyed by content + rubric +
--    model. Only the application role reads and writes it; it has no api
--    view. A verdict is served only for a member the caller's read returned
--    and only when every source message is visible to the caller.
--
-- Forward-only. Nothing here depends on a migration of the same wave
-- (api.episodes_of of 0040 is used by Python only).

-- 1. Expansion functions --------------------------------------------------------
-- Messages whose interval [happened_at, coalesce(ended_at, happened_at)]
-- overlaps an anchor's interval widened by window_s seconds on both sides.
-- The anchor's own record (any version) is never returned.
CREATE FUNCTION api.time_overlap_many(message_ids bigint[],window_s int DEFAULT 0)
RETURNS TABLE(anchor_id bigint,message_id bigint) LANGUAGE sql STABLE AS $$
 WITH a AS MATERIALIZED (
  SELECT DISTINCT v.id,v.channel_id,v.external_id,
   v.happened_at-make_interval(secs=>least(greatest(coalesce(time_overlap_many.window_s,0),0),31622400)) AS lo,
   coalesce(v.ended_at,v.happened_at)+make_interval(secs=>least(greatest(coalesce(time_overlap_many.window_s,0),0),31622400)) AS hi
  FROM api.message_versions v WHERE v.id=ANY(time_overlap_many.message_ids))
 SELECT a.id,m.id FROM a JOIN api.message_versions m
  ON m.happened_at<=a.hi AND coalesce(m.ended_at,m.happened_at)>=a.lo
 WHERE NOT (m.channel_id=a.channel_id AND m.external_id=a.external_id)
 ORDER BY a.id,m.happened_at,m.id $$;

-- Messages within meters of an anchor's location (anchors without one yield
-- nothing). The anchor's own record is never returned.
CREATE FUNCTION api.near_many(message_ids bigint[],meters float8)
RETURNS TABLE(anchor_id bigint,message_id bigint) LANGUAGE sql STABLE AS $$
 WITH a AS MATERIALIZED (
  SELECT DISTINCT v.id,v.channel_id,v.external_id,api.earth_point(v.lat,v.lon) AS p
  FROM api.message_versions v WHERE v.id=ANY(near_many.message_ids) AND v.lat IS NOT NULL AND v.lon IS NOT NULL)
 SELECT a.id,m.id FROM a JOIN api.message_versions m
  ON m.lat IS NOT NULL AND m.lon IS NOT NULL
  AND earth_distance(a.p,api.earth_point(m.lat,m.lon))<=least(greatest(coalesce(near_many.meters,100),0),20000000)
 WHERE NOT (m.channel_id=a.channel_id AND m.external_id=a.external_id)
 ORDER BY a.id,m.id $$;

-- api.follow for many starting objects of one kind. Without `at`: current
-- active links (api.links). With `at`: the links as of `at` that were active
-- and valid then (api.links_asof, api.applies_at), and a message start is the
-- version of it stored by `at`. Rows are the objects reached (kind, id) and,
-- with kind 'link', every link traversed; depth is the step at which each was
-- reached (at most 4). Cycles are not followed.
CREATE FUNCTION api.follow_many(kind text,ids bigint[],link_type text DEFAULT NULL,depth int DEFAULT 1,at timestamptz DEFAULT NULL)
RETURNS TABLE(anchor_id bigint,kind text,id bigint,depth int) LANGUAGE sql STABLE AS $$
 WITH RECURSIVE l AS MATERIALIZED (
  SELECT x.id,x.from_kind,x.from_id,x.to_kind,x.to_id FROM api.links x
  WHERE follow_many.at IS NULL AND x.status='active' AND (follow_many.link_type IS NULL OR x.type=follow_many.link_type)
  UNION ALL
  SELECT x.id,x.from_kind,x.from_id,x.to_kind,x.to_id FROM api.links_asof(follow_many.at) x
  WHERE follow_many.at IS NOT NULL AND x.status='active' AND api.applies_at(x.valid_from,x.valid_to,follow_many.at)
   AND (follow_many.link_type IS NULL OR x.type=follow_many.link_type)
 ), start AS (
  SELECT DISTINCT i.id AS anchor,CASE
   WHEN follow_many.kind<>'message' THEN i.id
   WHEN follow_many.at IS NULL THEN api.current_message(i.id)
   ELSE coalesce(
    (SELECT v.id FROM tl.messages o JOIN tl.messages v ON v.channel_id=o.channel_id AND v.external_id=o.external_id
     WHERE o.id=i.id AND v.ingested_at<=follow_many.at AND api.message_visible(v.id) ORDER BY v.version DESC LIMIT 1),
    api.current_message(i.id)) END AS node
  FROM unnest(follow_many.ids) AS i(id)
 ), walk(anchor,kind,id,depth,path,via) AS (
  SELECT s.anchor,follow_many.kind,s.node,0,ARRAY[follow_many.kind||':'||s.node],NULL::bigint FROM start s WHERE s.node IS NOT NULL
  UNION ALL
  SELECT w.anchor,
   CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_kind ELSE l.from_kind END,
   CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_id ELSE l.from_id END,
   w.depth+1,
   w.path||CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_kind||':'||l.to_id ELSE l.from_kind||':'||l.from_id END,
   l.id
  FROM walk w JOIN l ON ((l.from_kind=w.kind AND l.from_id=w.id) OR (l.to_kind=w.kind AND l.to_id=w.id))
  WHERE w.depth<least(greatest(coalesce(follow_many.depth,1),1),4)
   AND NOT (CASE WHEN l.from_kind=w.kind AND l.from_id=w.id THEN l.to_kind||':'||l.to_id ELSE l.from_kind||':'||l.from_id END=ANY(w.path))
 ), found AS (
  SELECT w.anchor,w.kind,w.id,min(w.depth) AS depth FROM walk w WHERE w.depth>0 GROUP BY 1,2,3
  UNION ALL
  SELECT w.anchor,'link'::text,w.via,min(w.depth) FROM walk w WHERE w.depth>0 GROUP BY 1,3
 ) SELECT f.anchor,f.kind,f.id,f.depth FROM found f ORDER BY f.anchor,f.depth,f.kind,f.id $$;

GRANT EXECUTE ON FUNCTION api.time_overlap_many(bigint[],int),api.near_many(bigint[],float8),api.follow_many(text,bigint[],text,int,timestamptz) TO throughline_app,throughline_reader;

-- 2. Verdict store ------------------------------------------------------------
-- One row per (record version or episode, text hash, operation, condition
-- hash, model, operator version): content + rubric + model. record_prefix
-- 'ep' holds episode-grain verdicts (ref_id = first message id of the
-- episode, version 0). source_message_ids names every message the judged
-- text came from; a verdict is served only to a scope that sees all of them,
-- and a purge removes it with them. Failed evaluations are never stored.
CREATE TABLE tl.verdicts(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 record_prefix text NOT NULL CHECK(record_prefix ~ '^[a-z]{1,8}$'),
 ref_id bigint NOT NULL,
 version int NOT NULL DEFAULT 0,
 text_hash text NOT NULL CHECK(text_hash ~ '^[0-9a-f]{64}$'),
 operation text NOT NULL DEFAULT 'match' CHECK(operation ~ '^[a-z_]{1,32}$'),
 condition_hash text NOT NULL CHECK(condition_hash ~ '^[0-9a-f]{64}$'),
 -- The condition as the caller wrote it (bounded): a rubric, not record text.
 condition text NOT NULL CHECK(length(condition) BETWEEN 1 AND 2000),
 provider text NOT NULL,
 model text NOT NULL,
 operator_version text NOT NULL,
 verdict text NOT NULL CHECK(verdict IN ('match','nonmatch','uncertain')),
 evidence jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(evidence)='array' AND octet_length(evidence::text)<=16384),
 source_message_ids bigint[] NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(record_prefix,ref_id,version,text_hash,operation,condition_hash,model,operator_version));
CREATE INDEX verdicts_sources ON tl.verdicts USING gin(source_message_ids);
REVOKE ALL ON tl.verdicts FROM PUBLIC,throughline_reader;
GRANT SELECT,INSERT,DELETE ON tl.verdicts TO throughline_app;

INSERT INTO tl.purge_hooks(name,ord,statement) VALUES(
 'verdicts',51,
 'DELETE FROM tl.verdicts WHERE source_message_ids && ARRAY(SELECT m.id FROM tl.messages m WHERE m.channel_id=$1)');

-- 3. Registry, allow-list and catalogue ---------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('time_overlap_many','sql','time_overlap_many','Messages whose time interval overlaps the interval of each anchor message widened by window_s seconds, for many anchors in one call','exact','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.time_overlap_many(ARRAY[1,2]::bigint[],3600)','1',NULL,NULL),
 ('near_many','sql','near_many','Messages within meters of the location of each anchor message, for many anchors in one call','exact','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.near_many(ARRAY[1,2]::bigint[],100)','1',NULL,NULL),
 ('follow_many','sql','follow_many','api.follow for many starting objects: objects reached and links traversed (kind link) per anchor, depth at most 4; with at, the links as of that time that were active and valid then','exact','free',2,'owner, agent; links pass the asker gate','SELECT * FROM api.follow_many(''message'',ARRAY[1,2]::bigint[],NULL,1)','1',NULL,NULL),
 ('expand','semantic','expand(input => handle, mode => context|thread|sources|links|time|place|focus, before, after, link_type, depth, as_of, known_by, window_s, meters, focus, k, keep_input) RETURNS result set rows','Expand a result set: thread context (span aware), whole threads, sources of generated records, links, time overlap, place, or a focus-ranked union. Added members have role context and depends_on their anchor; every added message passes the reduced scope and the asker gate','mixed','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.expand(input => ''rs_00000000000000000000000000000000'', mode => ''context'', before => 2, after => 2)','1',NULL,NULL),
 ('match','semantic','match(condition => text, input => handle | input_sql => statement, max_rows => int, grain => record|episode) RETURNS result set rows','Three-valued semantic matching of every member of a set against a condition (match, nonmatch, uncertain); a failed evaluation is NULL with meta.error, never uncertain or nonmatch. Verdicts are cached by content, condition and model','approximate','model',2,'owner, agent; only text the asker can read is sent; cached verdicts are served only for visible records with visible sources','SELECT * FROM api.match(condition => ''mentions a prototype'', input => ''rs_00000000000000000000000000000000'', max_rows => 200) WHERE meta->>''verdict''=''match''','1',NULL,NULL);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.kind='sql' AND f.name IN ('time_overlap_many','near_many','follow_many');

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('expand',jsonb_build_object(
  'description','Grow a result set by a mechanical relation. POST /v1/expand, throughline lib expand, or SELECT * FROM api.expand(input => handle, mode => ...). The output is a new result set.',
  'modes',jsonb_build_object(
   'context','Thread neighbours: before and after messages around each member''s span (exact).',
   'thread','Every message of the members'' threads, capped (exact; truncated when capped).',
   'sources','The provenance sources of generated members (pages, derived records, passages, claims, links) and their support; a raw message is its own source (exact). Nothing for a member with a hidden source.',
   'links','api.follow_many from message, entity and link members: message ends as messages, entity ends as e: records, traversed links as l: records (exact). Under as_of/known_by the links as of the cutoff.',
   'time','Messages whose time overlaps each member''s time widened by window_s (exact).',
   'place','Messages within meters of each member''s location (exact).',
   'focus','The union of context, links and sources, ranked against the focus text by recall restricted to those messages; the best k (approximate, ranked_sample).'),
  'guarantees',jsonb_build_array(
   'Added members have role context (or dependency when asked), depends_on their anchor and discovery expand:<mode>; expand never adds a hit.',
   'Every added message passes api.scope_filter with kinds, as_of, known_by and include_outside, plus the caller''s scope when one is given; the input set''s as_of and known_by are inherited.',
   'Expansion is set-based: a fixed number of statements whatever the number of anchors.',
   'Completeness: exact modes are exhaustive unless capped (truncated); focus is ranked_sample; an input that is not exhaustive caps the output at its own completeness.',
   'At most THROUGHLINE_EXPAND_MAX_INPUT (2,000) input members; more is a 400.'),
  'functions',jsonb_build_array('time_overlap_many','near_many','follow_many','context_many','sources','follow_asof'))),
 ('match',jsonb_build_object(
  'description','Judge every member of a result set against a condition with the semantic provider. POST /v1/match, throughline lib match, or SELECT * FROM api.match(condition => ..., input => handle | input_sql => statement, max_rows => n) WHERE meta->>''verdict''=''match''.',
  'verdicts',jsonb_build_object(
   'match','The model judged that the member satisfies the condition; meta.evidence gives character offsets into the member''s text.',
   'nonmatch','The model judged that it does not.',
   'uncertain','The model said uncertain, or the member has no text (meta.reason not_evaluable, counted separately).',
   'null','The evaluation failed (provider error, timeout, invalid output): meta.verdict is null and meta.error says why. Never uncertain, never nonmatch, never stored; counts.failed and an incomplete execution disclose it.'),
  'grain','record (default): one verdict per member. episode: message members are grouped with api.episodes_of and each episode is judged once on its members'' texts in time order (capped at THROUGHLINE_MATCH_GROUP_MAX_TOKENS, 4,000 tokens, truncation flagged); the anchor member carries the verdict, the other members are context and carry none.',
  'cache','A verdict is keyed by record prefix, id and version, sha256 of the judged text, operation, sha256 of the normalised condition, model and operator version. It is reused only for members the caller''s read returned, only when every source message is visible to the caller, and it survives the expiry of the set that produced it.',
  'cost','Members without a cached verdict are estimated before any call and pass the cost gate (per-token limit, approvals bound to the request, inputs with member counts and budgets). approval_required returns before any call.',
  'qry_8','match is the three-valued ad hoc operator. Numeric policy scores (QRY-8) stay with the job runner (M4).',
  'invariants',jsonb_build_array(
   'Counts before text: count the population with SQL (free) before judging it.',
   'Frozen population: the pinned versions of the set are judged, never the current text.',
   'Content + rubric + model key: a change of text, condition or model is a new verdict; nothing else is.',
   'NULL, never zero: a failed evaluation is null, not nonmatch or uncertain.',
   'Estimate, then approve: nothing is spent before the estimate passes the cost gate.',
   'Verdicts are kept after the set that produced them is forgotten.',
   'Group-grain verdicts are never copied onto members.'),
  'bounds','members <= max_rows <= THROUGHLINE_MATCH_MAX_ROWS (500), checked before any call.',
  'reserved','extract, compare, relate and summarize are reserved operation names; they are not built.'));
