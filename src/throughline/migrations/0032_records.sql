-- Record model (MSG-7..MSG-10), as-of selection (BRN-5) and the catalogue
-- registry behind api.catalog() and api.describe() (INT-8, QRY-16).
--
-- Forward-only. Nothing here changes a column of api.messages,
-- api.message_versions or api.search_results, and nothing depends on a later
-- migration. Every api function is SECURITY DEFINER with a fixed search_path,
-- so tl objects are schema-qualified throughout.

-- 1. Record kind is a channel property beside is_reference / is_outside.
ALTER TABLE tl.channels
 ADD COLUMN record_kind text NOT NULL DEFAULT 'life_event' CHECK (record_kind IN ('life_event','reference','page_revision','derived')),
 ADD COLUMN passage_text_paths jsonb,
 ADD COLUMN provenance_link_types jsonb;
UPDATE tl.channels SET record_kind='reference' WHERE is_reference;
UPDATE tl.channels SET record_kind='derived' WHERE name='visits';
ALTER TABLE tl.channels ADD CONSTRAINT channels_reference_kind CHECK (record_kind<>'reference' OR is_reference);
CREATE OR REPLACE VIEW api.channels AS SELECT * FROM tl.channels;

-- 2. Subject time: the interval a record is about, beside the time it
--    happened (happened_at) and the time it was stored (ingested_at). The four
--    columns are ground truth: they join the immutable column list and the app
--    role gets no UPDATE grant on them (its UPDATE grant is per column).
ALTER TABLE tl.messages
 ADD COLUMN subject_from timestamptz,
 ADD COLUMN subject_to timestamptz,
 ADD COLUMN subject_from_precision text CHECK (subject_from_precision IN ('exact','day','month','year','approximate','unknown')),
 ADD COLUMN subject_to_precision text CHECK (subject_to_precision IN ('exact','day','month','year','approximate','unknown')),
 ADD CONSTRAINT messages_subject_order CHECK (subject_from IS NULL OR subject_to IS NULL OR subject_to>=subject_from),
 -- A precision qualifies a bound: an open end carries none.
 ADD CONSTRAINT messages_subject_precision CHECK ((subject_from IS NOT NULL OR subject_from_precision IS NULL) AND (subject_to IS NOT NULL OR subject_to_precision IS NULL));
CREATE OR REPLACE FUNCTION tl.immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF current_setting('throughline.purge',true)='on' AND current_user NOT IN ('throughline_app','throughline_reader') AND session_user NOT IN ('throughline_app','throughline_reader') THEN IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END IF;
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'ground truth cannot be deleted'; END IF;
 IF TG_TABLE_NAME!='messages' THEN RAISE EXCEPTION 'ground truth is immutable'; END IF;
 IF ROW(NEW.id,NEW.channel_id,NEW.external_id,NEW.version,NEW.thread_id,NEW.happened_at,NEW.happened_offset_min,NEW.ended_at,NEW.ingested_at,NEW.author_handle,NEW.content,NEW.images,NEW.fields,NEW.access_label,NEW.payload_hash,NEW.subject_from,NEW.subject_to,NEW.subject_from_precision,NEW.subject_to_precision) IS DISTINCT FROM ROW(OLD.id,OLD.channel_id,OLD.external_id,OLD.version,OLD.thread_id,OLD.happened_at,OLD.happened_offset_min,OLD.ended_at,OLD.ingested_at,OLD.author_handle,OLD.content,OLD.images,OLD.fields,OLD.access_label,OLD.payload_hash,OLD.subject_from,OLD.subject_to,OLD.subject_from_precision,OLD.subject_to_precision) THEN RAISE EXCEPTION 'ground truth is immutable'; END IF;
 RETURN NEW;
END $$;

-- 3. An absent location is a correct value keyed to the record kind. Only
--    life events can be placed by the track, so a missing track point is a
--    reason only for them; every other kind without a source location is
--    'not_applicable'.
UPDATE tl.messages SET loc_reason='not_applicable' WHERE loc_reason='reference_channel';
UPDATE tl.messages m SET loc_reason='not_applicable' FROM tl.channels c WHERE c.id=m.channel_id AND c.record_kind<>'life_event' AND m.lat IS NULL AND m.loc_reason IS DISTINCT FROM 'not_applicable';
ALTER TABLE tl.messages ADD CONSTRAINT messages_loc_reason CHECK (loc_reason IN ('not_applicable','no_track_point_within_tolerance'));

-- 4. What a record may be cited for. Life events and reference records are
--    their own evidence. Page revisions and derived records are generated:
--    their support is their provenance sources, which migration 0036 supplies
--    by replacing this function. Until then they have none.
CREATE FUNCTION api.support_of(kind text,message_id bigint,version int) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT CASE WHEN kind IN ('life_event','reference') THEN jsonb_build_array(jsonb_build_object('message_id',message_id,'version',version,'relation','self')) ELSE '[]'::jsonb END $$;

-- 5. Record contract: one column list, in this order, for every record view in
--    the project. Message columns come from api.message_versions, so whatever
--    gate that view carries applies here as well.
CREATE VIEW api.record_versions AS
SELECT 'm:'||m.id AS record_id,
 c.record_kind AS kind,
 m.id AS ref_id,
 m.id AS message_id,
 m.version,
 m.superseded_by IS NULL AS is_current,
 m.channel,
 m.external_id,
 m.thread_id,
 m.thread_no,
 m.content,
 m.happened_at AS record_time,
 m.ended_at,
 m.ingested_at,
 x.subject_from,
 x.subject_to,
 x.subject_from_precision,
 x.subject_to_precision,
 m.person_id,
 m.author_handle,
 m.lat,
 m.lon,
 m.loc_reason,
 m.is_outside,
 NULL::text[] AS heading_path,
 NULL::int AS char_start,
 NULL::int AS char_end,
 NULL::jsonb AS claim,
 c.record_kind IN ('page_revision','derived') AS generated,
 c.record_kind IN ('page_revision','derived') AND s.support='[]'::jsonb AS unsupported,
 jsonb_build_object('plugin',c.plugin,'channel',m.channel,'external_id',m.external_id,'version',m.version,'method','source') AS provenance,
 s.support,
 jsonb_build_object('context',m.id,'thread',m.thread_id,'key',jsonb_build_array(m.channel,m.external_id)) AS expansion
FROM api.message_versions m
JOIN tl.messages x ON x.id=m.id
JOIN tl.channels c ON c.id=m.channel_id
CROSS JOIN LATERAL (SELECT api.support_of(c.record_kind,m.id,m.version) AS support) s;
CREATE VIEW api.records AS SELECT * FROM api.record_versions WHERE is_current;
GRANT SELECT ON api.record_versions,api.records TO throughline_app,throughline_reader;

-- 6. Registry of record views by record_id prefix. Later migrations add rows
--    (p, l, e, c); the hydrator and api.result_set read through it.
CREATE TABLE tl.record_sources(prefix text PRIMARY KEY,relation text NOT NULL CHECK (relation ~ '^api\.[a-z_]+$'));
INSERT INTO tl.record_sources VALUES ('m','api.record_versions');
GRANT SELECT ON tl.record_sources TO throughline_app;

-- 7. As-of selection. `at` is a record-time cutoff, known_by an ingest-time
--    cutoff. Per (channel, external_id) one version is returned:
--    * page revisions: the revision with the greatest record_time <= at
--      (ties: highest version), because a revision's record time is its
--      revision time;
--    * other kinds: the highest version among those with record_time <= at
--      that were ingested by known_by. Without known_by only the current
--      version is eligible: the time of an edit is known only as ingest time,
--      so there is no earlier state to select by record time alone.
CREATE FUNCTION api.records_asof(at timestamptz,known_by timestamptz DEFAULT NULL) RETURNS SETOF api.record_versions LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT DISTINCT ON (r.channel,r.external_id) r.*
 FROM api.record_versions r
 WHERE r.record_time<=records_asof.at
 AND (records_asof.known_by IS NULL OR r.ingested_at<=records_asof.known_by)
 AND (records_asof.known_by IS NOT NULL OR r.kind='page_revision' OR r.is_current)
 ORDER BY r.channel,r.external_id,CASE WHEN r.kind='page_revision' THEN r.record_time END DESC NULLS LAST,r.version DESC $$;
-- Half-open validity test shared by links, relations, facts and claims.
CREATE FUNCTION api.applies_at(valid_from timestamptz,valid_to timestamptz,at timestamptz) RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT (applies_at.valid_from IS NULL OR applies_at.valid_from<=applies_at.at) AND (applies_at.valid_to IS NULL OR applies_at.at<applies_at.valid_to) $$;

-- 8. Catalogue registry. Packages add rows instead of rewriting api.catalog().
CREATE TABLE tl.api_functions(
 name text PRIMARY KEY,
 kind text NOT NULL CHECK (kind IN ('sql','semantic')),
 signature text NOT NULL,
 description text NOT NULL,
 exactness text NOT NULL CHECK (exactness IN ('exact','approximate','mixed')),
 cost_class text NOT NULL CHECK (cost_class IN ('free','embedding','model')),
 level int NOT NULL CHECK (level BETWEEN 0 AND 3),
 access text NOT NULL,
 example text NOT NULL,
 version text NOT NULL,
 ranked_limit_arg int,
 default_limit int);
CREATE TABLE tl.catalog_sections(subject text PRIMARY KEY,body jsonb NOT NULL,version int NOT NULL DEFAULT 1);
GRANT SELECT ON tl.api_functions,tl.catalog_sections TO throughline_app;
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('context','sql','context','Expand a message within its numbered thread','exact','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.context(1,10,5)','1',NULL,NULL),
 ('search','sql','search','Rank current non-reference messages by keywords','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.search(''orchard'',20)','1',2,20),
 ('search_all','sql','search_all','Rank messages including reference channels by keywords','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.search_all(''orchard'',20)','1',2,20),
 ('search_filtered','sql','search_filtered','Keyword ranking with person, channel, time and place filters applied before ranking','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.search_filtered(''orchard'',20,NULL,''synthetic.chat'')','1',2,20),
 ('search_meaning','sql','search_meaning','Exact cosine ranking of eligible conversations for a query embedding supplied by the caller; one representative message per conversation','approximate','embedding',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.search_meaning(''[1,0,0]'',10,NULL,''synthetic.chat'')','1',2,20),
 ('search_fused','sql','search_fused','Reciprocal rank fusion (constant 60) of exhaustive keyword and meaning ranks','mixed','embedding',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.search_fused(''orchard'',''[1,0,0]'',10)','1',3,20),
 ('near','sql','near','Messages within meters of a point','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.near(1,2,1000)','1',NULL,NULL),
 ('near_place','sql','near_place','Messages near a named place entity','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.near_place(''Synthetic place'',1000)','1',NULL,NULL),
 ('overlapping','sql','overlapping','Messages overlapping a closed time interval','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.overlapping(''2020-01-01Z'',''2020-01-02Z'')','1',NULL,NULL),
 ('during','sql','during','Messages overlapping the time of another message','exact','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.during(1)','1',NULL,NULL),
 ('person','sql','person','Resolve a name, handle or identifier','exact','free',0,'owner, agent; unrestricted askers only','SELECT * FROM api.person(''fake-account'')','1',NULL,NULL),
 ('person_messages','sql','person_messages','Current messages authored by a person','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.person_messages(1)','1',NULL,NULL),
 ('follow','sql','follow','Follow active links in either direction without cycles','exact','free',2,'owner, agent; links pass the asker gate','SELECT * FROM api.follow(''message'',1,NULL,2)','1',NULL,NULL),
 ('catalog','sql','catalog','Describe the SQL surface: channels, views, functions, sections','exact','free',0,'owner, agent','SELECT api.catalog()','1',NULL,NULL),
 ('records_asof','sql','records_asof','One record version per (channel, external_id) as of a record-time cutoff and an optional ingest-time cutoff','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.records_asof(''2020-01-01Z'')','1',NULL,NULL),
 ('applies_at','sql','applies_at','Whether a half-open validity interval [valid_from, valid_to) contains a time; an open end is unbounded','exact','free',0,'owner, agent','SELECT api.applies_at(NULL,''2021-01-01Z'',''2020-06-01Z'')','1',NULL,NULL),
 ('describe','sql','describe','Describe the archive: index, functions, one function, one channel with coverage, or a named section','exact','free',0,'owner, agent; channels pass the asker gate','SELECT api.describe(''functions'')','1',NULL,NULL);

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('record_kinds',jsonb_build_object(
  'description','Every channel has one record kind. It decides whether a record is evidence, whether it can have a location and how it enters retrieval.',
  'kinds',jsonb_build_object(
   'life_event','Something that happened to or around the owner (a message, a play, a purchase). Evidence for itself. Takes a source location or one from the track.',
   'reference','Reference text the owner did not live through (a manual, a documentation page). Evidence for itself; excluded from api.search unless asked for. No location.',
   'page_revision','One revision of a written page about the archive. Generated text: never its own evidence, supported only by its provenance sources. No location.',
   'derived','A record computed from other records or from the track (a detected visit, a timeline entry). Generated: supported only by its provenance sources. Keeps a source location, never takes one from the track.'),
  'record_contract','Every record view returns the same columns: record_id, kind, ref_id, message_id, version, is_current, channel, external_id, thread_id, thread_no, content, record_time, ended_at, ingested_at, subject_from, subject_to, subject_from_precision, subject_to_precision, person_id, author_handle, lat, lon, loc_reason, is_outside, heading_path, char_start, char_end, claim, generated, unsupported, provenance, support, expansion.',
  'generated','generated is true for page_revision and derived records. support lists the records a record may be cited for; unsupported is true when a generated record has none.',
  'views',jsonb_build_object('api.records','Current version of every message record','api.record_versions','Every version of every message record'))),
 ('time',jsonb_build_object(
  'record_time','When the record happened (happened_at). For a page revision this is the revision time.',
  'ended_at','End of the record when it has a duration.',
  'ingested_at','When this version was stored. It is the only known time of an edit.',
  'subject','subject_from and subject_to bound the period the record is about, when that differs from when it was written (a note written today about a trip last year). Either end may be absent.',
  'precision',jsonb_build_object('columns',jsonb_build_array('subject_from_precision','subject_to_precision'),'values',jsonb_build_array('exact','day','month','year','approximate','unknown')),
  'time_basis',jsonb_build_object('record','Filter on record_time (default)','subject','Filter on the subject interval','either','A record matches when its record time or its subject interval does'),
  'as_of','Record-time cutoff: api.records_asof(at) returns per (channel, external_id) the version valid at that time. Page revisions are chosen by revision time.',
  'known_by','Ingest-time cutoff: api.records_asof(at, known_by) only considers versions stored by then.',
  'limits','Without known_by, records other than page revisions are returned in their current version: an edit has no time of its own besides its ingest time.',
  'validity','api.applies_at(valid_from, valid_to, at) tests a half-open interval; a missing end is unbounded.')),
 ('location',jsonb_build_object(
  'columns',jsonb_build_array('lat','lon','loc_accuracy_m','loc_method','loc_reason'),
  'loc_method',jsonb_build_object('source','Location supplied by the source with the record','track','Nearest point of the owner''s location track within the channel tolerance'),
  'loc_reason',jsonb_build_object('not_applicable','The record kind has no location: reference, page_revision, and derived records without a source location','no_track_point_within_tolerance','A life event without a source location and without a track point close enough in time'),
  'applicability',jsonb_build_object('life_event','source location, else track','derived','source location only','reference','none','page_revision','none'))),
 ('levels',jsonb_build_object(
  'description','Retrieval capabilities by level. They are capabilities an agent may combine in one request, not modes to switch between; a higher level never replaces a lower one.',
  'levels',jsonb_build_array(
   jsonb_build_object('level',0,'name','exact SQL','description','Read-only SQL over the api views and exact functions. Complete and reproducible.'),
   jsonb_build_object('level',1,'name','discovery','description','Ranked candidate generation: keyword, meaning and fused search, recall legs. Ranked and limited, so not exhaustive unless the limit is NULL.'),
   jsonb_build_object('level',2,'name','expansion, rerank and selection','description','Work on a candidate set: context and link expansion, reranking, deduplication, selection under a budget, grouping.'),
   jsonb_build_object('level',3,'name','librarian','description','Optional bounded controller that plans calls of the lower levels. Never required; everything it does is available directly.')),
  'function_levels','Each entry of the functions list carries its level.'));
-- Signatures come from the system catalogue so they cannot drift from the
-- functions defined so far; a later CREATE OR REPLACE updates its own row.
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.proname=f.name AND f.name<>'catalog' AND f.name<>'describe';
UPDATE tl.api_functions SET signature='catalog() RETURNS jsonb' WHERE name='catalog';
UPDATE tl.api_functions SET signature='describe(subject text DEFAULT NULL::text) RETURNS jsonb' WHERE name='describe';

-- 9. Catalogue and describe.
CREATE FUNCTION tl.catalog_version() RETURNS text LANGUAGE sql STABLE SET search_path=pg_catalog,tl AS $$
 SELECT left(md5(coalesce(string_agg(v.item,'|' ORDER BY v.item),'')),12)
 FROM (SELECT 'f:'||name||':'||version||':'||md5(signature) AS item FROM tl.api_functions UNION ALL SELECT 's:'||subject||':'||version FROM tl.catalog_sections) v $$;
REVOKE EXECUTE ON FUNCTION tl.catalog_version() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.catalog_version() TO throughline_app;

ALTER FUNCTION api.catalog() RENAME TO catalog_identity;
CREATE FUNCTION api.catalog() RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT api.catalog_identity() || jsonb_build_object(
  'functions',(SELECT coalesce(jsonb_agg(to_jsonb(f) ORDER BY f.name),'[]'::jsonb) FROM tl.api_functions f),
  'sections',(SELECT coalesce(jsonb_object_agg(s.subject,s.body),'{}'::jsonb) FROM tl.catalog_sections s),
  'catalog_version',tl.catalog_version()) $$;

-- Maintenance numbers of one channel. Reads private tables, so api.describe
-- calls it only for an owner scope and only for a channel it already found
-- through the gated api.channels view. Not callable by runtime roles.
CREATE FUNCTION tl.channel_maintenance(cid bigint) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,tl AS $$
 SELECT jsonb_build_object(
  'dirty_threads',(SELECT count(*) FROM tl.dirty_threads d JOIN tl.threads t ON t.id=d.thread_id WHERE t.channel_id=cid),
  'closed_conversations',k.closed,
  'embedded_conversations',k.embedded,
  'embedded_share',CASE WHEN k.closed>0 THEN round(k.embedded::numeric/k.closed,4) END)
 FROM (SELECT count(*) FILTER (WHERE c.closed) AS closed,
   count(*) FILTER (WHERE c.closed AND EXISTS(SELECT 1 FROM tl.embeddings e JOIN tl.embedding_models em ON em.model=e.model AND em.active WHERE e.conversation_id=c.id AND e.text_hash=c.text_hash)) AS embedded
  FROM tl.conversations c JOIN tl.threads t ON t.id=c.thread_id WHERE t.channel_id=cid) k $$;
REVOKE EXECUTE ON FUNCTION tl.channel_maintenance(bigint) FROM PUBLIC;

-- api.describe reads message, channel, thread and conversation data ONLY
-- through api views, so the asker gate of those views scopes every answer: a
-- hidden channel and an unknown channel are indistinguishable (both null).
-- Other packages add a subject by creating a function api.describe_<subject>().
CREATE FUNCTION api.describe(subject text DEFAULT NULL) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE
 result jsonb;
 ch record;
 wanted text;
 owner_scope boolean := coalesce(current_setting('throughline.scope_mode',true),'') IN ('','owner');
BEGIN
 IF describe.subject IS NULL THEN
  RETURN jsonb_build_object(
   'subjects',(SELECT jsonb_agg(jsonb_build_object('subject',i.subject,'description',i.description) ORDER BY i.ord,i.subject) FROM (
     SELECT 1 AS ord,'functions' AS subject,'Every registered function with signature, exactness, cost class, level and an example' AS description
     UNION ALL SELECT 1,'function:<name>','One registered function'
     UNION ALL SELECT 1,'channel:<name>','One channel: description, record kind, fields and coverage'
     UNION ALL SELECT 2,s.subject,coalesce(s.body->>'description','Section '||s.subject) FROM tl.catalog_sections s
      WHERE NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.pronargs=0 AND p.proname='describe_'||s.subject)
     UNION ALL SELECT 3,substr(p.proname,10),coalesce(obj_description(p.oid,'pg_proc'),'Computed section '||substr(p.proname,10)) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname='api' AND p.pronargs=0 AND p.proname ~ '^describe_[a-z][a-z0-9_]*$') i),
   'channels',(SELECT coalesce(jsonb_agg(c.name ORDER BY c.name),'[]'::jsonb) FROM api.channels c),
   'functions',(SELECT coalesce(jsonb_agg(f.name ORDER BY f.name),'[]'::jsonb) FROM tl.api_functions f),
   'catalog_version',tl.catalog_version());
 END IF;
 IF describe.subject='functions' THEN
  RETURN (SELECT coalesce(jsonb_agg(to_jsonb(f) ORDER BY f.name),'[]'::jsonb) FROM tl.api_functions f);
 END IF;
 IF describe.subject LIKE 'function:%' THEN
  wanted := substr(describe.subject,10);
  RETURN (SELECT to_jsonb(f) FROM tl.api_functions f WHERE f.name=wanted);
 END IF;
 IF describe.subject LIKE 'channel:%' THEN
  wanted := substr(describe.subject,9);
  SELECT c.* INTO ch FROM api.channels c WHERE c.name=wanted;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT jsonb_build_object(
   'name',ch.name,'plugin',ch.plugin,'description',ch.description,'record_kind',ch.record_kind,
   'is_reference',ch.is_reference,'is_outside',ch.is_outside,'fields',ch.fields,
   'messages',count(*),'first_record_time',min(m.happened_at),'last_record_time',max(m.happened_at),'last_ingested_at',max(m.ingested_at))
   INTO result FROM api.messages m WHERE m.channel_id=ch.id;
  -- Maintenance state is operational detail for the owner, not for an asker.
  IF owner_scope THEN
   result := result || jsonb_build_object('maintenance',tl.channel_maintenance(ch.id) || jsonb_build_object('plugin',
    (SELECT jsonb_build_object('last_success_at',p.last_success_at,'last_error',p.last_error) FROM api.plugins p WHERE p.name=ch.plugin)));
  ELSE
   result := result || jsonb_build_object('maintenance',NULL);
  END IF;
  RETURN result;
 END IF;
 IF describe.subject ~ '^[a-z][a-z0-9_]*$' AND EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.pronargs=0 AND p.proname='describe_'||describe.subject) THEN
  EXECUTE format('SELECT to_jsonb(api.%I())','describe_'||describe.subject) INTO result;
  RETURN result;
 END IF;
 RETURN (SELECT s.body FROM tl.catalog_sections s WHERE s.subject=describe.subject);
END $$;
GRANT EXECUTE ON FUNCTION api.support_of(text,bigint,int),api.records_asof(timestamptz,timestamptz),api.applies_at(timestamptz,timestamptz,timestamptz),api.catalog(),api.catalog_identity(),api.describe(text) TO throughline_app,throughline_reader;
