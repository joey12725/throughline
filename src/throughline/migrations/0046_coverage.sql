-- Source coverage and named consumer cursors (PLG-7,
-- BEN-20, LIB-9, JOB-2, JOB-7, QRY-36, ACL-8, ACL-10, INT-1, ONB-2).
--
-- Coverage: what each source says it delivered, for which window, with what
-- status. A report never stops an import: an unknown status degrades to
-- 'partial' with the raw value kept. A gap opens only from an explicit gap or
-- a partial window and closes only on a covering report.
--
-- Consumers: named cursors keyed on arrival (tl.messages.ingested_at, the
-- ingesting transaction's start time), never on message time, so a late
-- backfill is delivered once. The settle guarantee below is enforced by the
-- database for every role.
--
-- Forward-only. Every api function becomes SECURITY DEFINER with a fixed
-- search_path through tl.harden_reader(), so tl objects are schema-qualified.

-- 1. Reason codes (a seed hook) -----------------------------------------------
CREATE TABLE tl.coverage_reasons(
 code text PRIMARY KEY,
 description text NOT NULL,
 severity text NOT NULL CHECK (severity IN ('info','partial')));
GRANT SELECT ON tl.coverage_reasons TO throughline_app;
INSERT INTO tl.seed_hooks(name,ord,statement) VALUES
 ('coverage_reasons',46,$seed$INSERT INTO tl.coverage_reasons(code,description,severity) VALUES
 ('not_reported','The source has not reported coverage for this channel','partial'),
 ('run_failed','The importer run failed before it finished','partial'),
 ('stale','The plugin has not succeeded within twice its interval','partial'),
 ('source_cap','The source returns at most a fixed number of items','partial'),
 ('source_error','The source answered with an error for part of the window','partial'),
 ('rate_limited','The source rate-limited the importer','partial'),
 ('auth_failed','The importer could not authenticate','partial'),
 ('quarantined_rows','Some rows were held back by the importer','partial'),
 ('filtered_by_policy','Rows were left out on purpose by a policy','info'),
 ('listing_lag','The source lists new items with a delay','info'),
 ('partial_history','The source exposes only part of its history','partial'),
 ('unknown_shape','A report field had an unexpected shape; the raw value is kept','partial'),
 ('unrecognised_status','The reported status is not a known one; the raw value is kept','partial'),
 ('unrecognised_reason','A reported reason code is not a known one; it is kept in raw_detail','info'),
 ('retry_pending','Items are queued for a retry','partial'),
 ('empty_verified','The source verified that the window is empty','info')
ON CONFLICT (code) DO UPDATE SET description=EXCLUDED.description,severity=EXCLUDED.severity$seed$);

-- 2. Coverage, reports, gaps, backup runs ----------------------------------------
CREATE TABLE tl.coverage(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 plugin text NOT NULL,
 channel_id bigint REFERENCES tl.channels ON DELETE CASCADE,
 scope_key text NOT NULL DEFAULT '',
 status text NOT NULL CHECK (status IN ('complete','partial','best_effort')),
 reasons text[] NOT NULL DEFAULT '{}',
 raw_status text,
 raw_detail jsonb,
 last_attempt_at timestamptz,
 last_success_at timestamptz,
 covered_from timestamptz,
 covered_to timestamptz,
 high_water_at timestamptz,
 high_water_ingested_at timestamptz,
 rows_delivered bigint,
 pending_retries int NOT NULL DEFAULT 0,
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE NULLS NOT DISTINCT (plugin,channel_id,scope_key));
CREATE INDEX coverage_channel ON tl.coverage(channel_id);
-- Append-only log of what was received (raw, at most 64 KB) and what it was
-- normalised to.
CREATE TABLE tl.coverage_reports(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 plugin text NOT NULL,
 channel_id bigint REFERENCES tl.channels ON DELETE CASCADE,
 scope_key text NOT NULL DEFAULT '',
 received jsonb NOT NULL,
 normalized jsonb NOT NULL,
 at timestamptz NOT NULL DEFAULT now());
CREATE INDEX coverage_reports_channel ON tl.coverage_reports(channel_id);
CREATE TABLE tl.coverage_gaps(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 plugin text NOT NULL,
 channel_id bigint REFERENCES tl.channels ON DELETE CASCADE,
 scope_key text NOT NULL DEFAULT '',
 gap_from timestamptz NOT NULL,
 gap_to timestamptz NOT NULL,
 reason text NOT NULL,
 opened_at timestamptz NOT NULL DEFAULT now(),
 opened_by_report bigint REFERENCES tl.coverage_reports ON DELETE SET NULL,
 closed_at timestamptz,
 closed_by_report bigint REFERENCES tl.coverage_reports ON DELETE SET NULL,
 CHECK (gap_to>=gap_from));
CREATE INDEX coverage_gaps_open ON tl.coverage_gaps(plugin,channel_id,scope_key) WHERE closed_at IS NULL;
-- Written by the backup step (owner connection); read by GET /v1/status.
-- detail holds counts and hashes only, never content.
CREATE TABLE tl.backup_runs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 kind text NOT NULL CHECK (kind IN ('dump','verify','offsite')),
 started_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz,
 status text NOT NULL CHECK (status IN ('ok','failed')),
 file_sha256 text,
 bytes bigint,
 detail jsonb NOT NULL DEFAULT '{}');
GRANT SELECT,INSERT,UPDATE ON tl.coverage TO throughline_app;
GRANT SELECT,INSERT ON tl.coverage_reports TO throughline_app;
GRANT SELECT,INSERT,UPDATE ON tl.coverage_gaps TO throughline_app;
GRANT SELECT ON tl.backup_runs TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.coverage_id_seq,tl.coverage_reports_id_seq,tl.coverage_gaps_id_seq TO throughline_app;

-- 3. Consumers and the settle window ------------------------------------------------
CREATE TABLE tl.consumers(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 name text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
 token_id bigint REFERENCES tl.tokens ON DELETE CASCADE,
 asker text,
 scope jsonb NOT NULL DEFAULT '{}',
 seen_to_ingested_at timestamptz,
 seen_to_message_id bigint,
 marks bigint NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE NULLS NOT DISTINCT (token_id,asker,name));
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.consumers TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.consumers_id_seq TO throughline_app;
CREATE TABLE tl.consumer_settings(
 one boolean PRIMARY KEY DEFAULT true CHECK (one),
 settle_s int NOT NULL DEFAULT 120,
 margin_s int NOT NULL DEFAULT 15,
 CHECK (margin_s>=0 AND settle_s>margin_s AND settle_s>=2));
-- The app role reads it; PUT /v1/consumers/settings (owner tokens only) writes.
GRANT SELECT,UPDATE ON tl.consumer_settings TO throughline_app;
INSERT INTO tl.seed_hooks(name,ord,statement) VALUES
 ('consumer_settings',46,'INSERT INTO tl.consumer_settings(one) VALUES (true) ON CONFLICT (one) DO NOTHING');
DO $$ BEGIN
 EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='coverage_reasons');
 EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='consumer_settings');
END $$;
CREATE INDEX messages_arrival ON tl.messages(ingested_at,id);

-- 4. Settle guarantee ------------------------------------------------------------
-- A transaction that inserts messages must commit within settle_s - margin_s
-- of its start (ingested_at is the transaction start). Checked once per
-- transaction at commit (deferred), for every role. So when a reader at r
-- sees no row with ingested_at <= r - settle_s, none will ever commit, and a
-- cursor that stops at api.consumer_horizon() never skips a late commit.
-- Only restores (rows keep their original ingested_at and consumer state is
-- replaced with them) and migrations that insert messages set
-- throughline.bulk_restore=on.
CREATE FUNCTION tl.consumer_settle_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE s int; m int;
BEGIN
 IF current_setting('throughline.settle_checked',true)='on' THEN RETURN NULL; END IF;
 IF current_setting('throughline.bulk_restore',true)='on' THEN RETURN NULL; END IF;
 SELECT x.settle_s,x.margin_s INTO s,m FROM tl.consumer_settings x WHERE x.one;
 IF NOT FOUND THEN s := 120; m := 15; END IF;
 IF clock_timestamp()-transaction_timestamp() > make_interval(secs=>s-m) THEN
  RAISE EXCEPTION 'ingest transaction exceeded the consumer settle window; retry with a smaller batch'
   USING ERRCODE='54000', DETAIL=format('a transaction that inserts messages must commit within %s s of its start',s-m);
 END IF;
 PERFORM set_config('throughline.settle_checked','on',true);
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.consumer_settle_check() FROM PUBLIC,throughline_app,throughline_reader;
CREATE CONSTRAINT TRIGGER consumer_settle AFTER INSERT ON tl.messages DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION tl.consumer_settle_check();

-- 5. Gated views ------------------------------------------------------------------
-- Every channel has a row: its reported ones, else a default best_effort row
-- with reason not_reported. A plugin that has not succeeded within twice its
-- interval is stale: reason 'stale', and complete degrades to partial.
-- Plugin-level rows (channel_id NULL) are for unrestricted askers only.
SELECT tl.install_gate('coverage','v.channel_id IS NOT NULL AND api.asker_sees(v.channel_id,NULL)',$def$
WITH r AS (
 SELECT c.id,c.plugin,c.channel_id,c.scope_key,c.status,c.reasons,c.raw_status,c.raw_detail,
  c.last_attempt_at,c.last_success_at,c.covered_from,c.covered_to,c.high_water_at,c.high_water_ingested_at,
  c.rows_delivered,c.pending_retries,c.updated_at
 FROM tl.coverage c
 -- An attempt placeholder (not_reported, nothing received) is left out once
 -- the channel has a reported row, e.g. for a scope key, so it never holds a
 -- reported channel at best_effort.
 WHERE NOT (c.channel_id IS NOT NULL AND c.raw_status IS NULL AND c.reasons='{not_reported}'::text[]
  AND EXISTS(SELECT 1 FROM tl.coverage o WHERE o.channel_id=c.channel_id AND o.id<>c.id
   AND NOT (o.raw_status IS NULL AND o.reasons='{not_reported}'::text[])))
 UNION ALL
 SELECT NULL::bigint,ch.plugin,ch.id,''::text,'best_effort'::text,ARRAY['not_reported']::text[],NULL::text,NULL::jsonb,
  NULL::timestamptz,NULL::timestamptz,NULL::timestamptz,NULL::timestamptz,NULL::timestamptz,NULL::timestamptz,
  NULL::bigint,0,NULL::timestamptz
 FROM tl.channels ch WHERE NOT EXISTS(SELECT 1 FROM tl.coverage c WHERE c.channel_id=ch.id))
SELECT r.id,r.plugin,r.channel_id,ch.name AS channel,ch.record_kind,r.scope_key,
 CASE WHEN st.stale AND r.status='complete' THEN 'partial' ELSE r.status END AS status,
 CASE WHEN st.stale AND NOT 'stale'=ANY(r.reasons) THEN r.reasons||'stale'::text ELSE r.reasons END AS reasons,
 r.status AS reported_status,
 r.raw_status,r.raw_detail,r.last_attempt_at,
 st.last_success_at,
 r.covered_from,r.covered_to,r.high_water_at,r.high_water_ingested_at,r.rows_delivered,r.pending_retries,r.updated_at,
 st.stale,
 NOT ('not_reported'=ANY(r.reasons)) AS reported
FROM r
LEFT JOIN tl.channels ch ON ch.id=r.channel_id
LEFT JOIN tl.plugins p ON p.name=r.plugin
CROSS JOIN LATERAL (
 SELECT x.ls AS last_success_at,
  coalesce(x.ls IS NOT NULL AND now()>x.ls+make_interval(secs=>2*greatest(coalesce(p.interval_s,3600),1)),false) AS stale
 FROM (SELECT CASE WHEN r.last_success_at IS NULL THEN p.last_success_at WHEN p.last_success_at IS NULL THEN r.last_success_at
  ELSE greatest(r.last_success_at,p.last_success_at) END AS ls) x) st
$def$);
SELECT tl.install_gate('coverage_gaps','v.channel_id IS NOT NULL AND api.asker_sees(v.channel_id,NULL)',$def$
SELECT g.id,g.plugin,g.channel_id,ch.name AS channel,g.scope_key,g.gap_from,g.gap_to,g.reason,
 g.opened_at,g.opened_by_report,g.closed_at,g.closed_by_report,g.closed_at IS NULL AS open
FROM tl.coverage_gaps g LEFT JOIN tl.channels ch ON ch.id=g.channel_id
$def$);

-- 6. Functions ----------------------------------------------------------------------
-- Coverage per visible channel over a window. status is the worst of the
-- channel's rows (best_effort < partial < complete); gaps are the open gaps
-- overlapping the window. window_status: covered, partly_covered,
-- not_covered or unknown (nothing reported, or best_effort only).
CREATE FUNCTION api.coverage_for(channels text[] DEFAULT NULL,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,kinds text[] DEFAULT NULL)
RETURNS TABLE(channel text,status text,reasons text[],covered_from timestamptz,covered_to timestamptz,last_success_at timestamptz,stale boolean,gaps jsonb,window_status text)
LANGUAGE sql STABLE AS $$
 WITH v AS MATERIALIZED (
  SELECT c.* FROM api.coverage c
  WHERE c.channel_id IS NOT NULL
   AND (coverage_for.channels IS NULL OR c.channel=ANY(coverage_for.channels))
   AND (coverage_for.kinds IS NULL OR c.record_kind=ANY(coverage_for.kinds))),
 rs AS (SELECT v.channel_id,array_agg(DISTINCT x.code ORDER BY x.code) AS reasons FROM v CROSS JOIN LATERAL unnest(v.reasons) x(code) GROUP BY v.channel_id),
 a AS (SELECT v.channel_id,min(v.channel) AS channel,
   min(CASE v.status WHEN 'best_effort' THEN 0 WHEN 'partial' THEN 1 ELSE 2 END) AS srank,
   bool_or(v.reported) AS reported,min(v.covered_from) AS cf,max(v.covered_to) AS ct,
   max(v.last_success_at) AS ls,bool_or(v.stale) AS stale
  FROM v GROUP BY v.channel_id),
 g AS (SELECT gg.channel_id,
   jsonb_agg(jsonb_build_object('id',gg.id,'channel',gg.channel,'scope_key',gg.scope_key,'from',gg.gap_from,'to',gg.gap_to,'reason',gg.reason) ORDER BY gg.gap_from,gg.id) AS gaps,
   bool_or(coverage_for.after_at IS NOT NULL AND coverage_for.before_at IS NOT NULL AND gg.gap_from<=coverage_for.after_at AND gg.gap_to>=coverage_for.before_at) AS whole
  FROM api.coverage_gaps gg
  WHERE gg.closed_at IS NULL AND gg.channel_id IN (SELECT a.channel_id FROM a)
   AND (coverage_for.after_at IS NULL OR gg.gap_to>=coverage_for.after_at)
   AND (coverage_for.before_at IS NULL OR gg.gap_from<=coverage_for.before_at)
  GROUP BY gg.channel_id)
 SELECT a.channel,
  CASE a.srank WHEN 0 THEN 'best_effort' WHEN 1 THEN 'partial' ELSE 'complete' END,
  coalesce(rs.reasons,'{}'::text[]),a.cf,a.ct,a.ls,a.stale,coalesce(g.gaps,'[]'::jsonb),
  CASE
   WHEN NOT a.reported THEN 'unknown'
   WHEN coalesce(g.whole,false) THEN 'not_covered'
   WHEN g.gaps IS NOT NULL THEN 'partly_covered'
   WHEN (coverage_for.after_at IS NOT NULL AND a.ct IS NOT NULL AND coverage_for.after_at>a.ct)
    OR (coverage_for.before_at IS NOT NULL AND a.cf IS NOT NULL AND coverage_for.before_at<a.cf) THEN 'not_covered'
   WHEN a.srank=0 THEN 'unknown'
   WHEN a.srank=1 THEN 'partly_covered'
   WHEN (a.cf IS NOT NULL AND (coverage_for.after_at IS NULL OR coverage_for.after_at<a.cf))
    OR (a.ct IS NOT NULL AND (coverage_for.before_at IS NULL OR coverage_for.before_at>a.ct)) THEN 'partly_covered'
   ELSE 'covered' END
 FROM a LEFT JOIN rs ON rs.channel_id=a.channel_id LEFT JOIN g ON g.channel_id=a.channel_id
 ORDER BY a.channel $$;

-- Rows with ingested_at at or before the horizon are settled: no transaction
-- that would add one can still commit (section 4).
CREATE FUNCTION api.consumer_horizon() RETURNS timestamptz LANGUAGE sql STABLE AS $$
 SELECT now()-make_interval(secs=>coalesce((SELECT s.settle_s FROM tl.consumer_settings s WHERE s.one),120)) $$;

-- The calling consumer of throughline.token_id and throughline.asker.
CREATE FUNCTION tl.consumer_id(name text) RETURNS bigint LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 SELECT k.id FROM tl.consumers k
 WHERE k.name=consumer_id.name
  AND k.token_id IS NOT DISTINCT FROM nullif(current_setting('throughline.token_id',true),'')::bigint
  AND k.asker IS NOT DISTINCT FROM nullif(current_setting('throughline.asker',true),'') $$;
REVOKE EXECUTE ON FUNCTION tl.consumer_id(text) FROM PUBLIC,throughline_app,throughline_reader;

-- Arrivals of a consumer after a cursor and at or before a horizon, gated,
-- in arrival order. Written to be inlined (no SET, not SECURITY DEFINER) so
-- the cursor and the order use messages_arrival.
-- Scope keys: channels, kinds (default life_event and derived), thread_ids,
-- persons (attributed author), include_ignored (default false),
-- include_versions (default true).
CREATE FUNCTION tl.news_candidates(consumer_id bigint,seen_at timestamptz,seen_id bigint,horizon timestamptz,max_rows int)
RETURNS TABLE(message_id bigint,ingested_at timestamptz,version int) LANGUAGE sql STABLE AS $$
 SELECT m.id,m.ingested_at,m.version
 FROM tl.messages m
 WHERE (m.ingested_at,m.id)>(coalesce(news_candidates.seen_at,'-infinity'::timestamptz),coalesce(news_candidates.seen_id,0))
  AND m.ingested_at<=news_candidates.horizon
  AND m.channel_id=ANY((SELECT coalesce(array_agg(ch.id),'{}'::bigint[]) FROM tl.channels ch, tl.consumers k
   WHERE k.id=news_candidates.consumer_id
    AND ch.record_kind=ANY(CASE WHEN jsonb_typeof(k.scope->'kinds')='array' AND jsonb_array_length(k.scope->'kinds')>0
     THEN ARRAY(SELECT jsonb_array_elements_text(k.scope->'kinds')) ELSE ARRAY['life_event','derived'] END)
    AND (jsonb_typeof(k.scope->'channels') IS DISTINCT FROM 'array' OR ch.name IN (SELECT jsonb_array_elements_text(k.scope->'channels'))))::bigint[])
  AND ((SELECT jsonb_typeof(k.scope->'thread_ids') IS DISTINCT FROM 'array' FROM tl.consumers k WHERE k.id=news_candidates.consumer_id)
   OR m.thread_id=ANY((SELECT ARRAY(SELECT x::bigint FROM jsonb_array_elements_text(k.scope->'thread_ids') x) FROM tl.consumers k WHERE k.id=news_candidates.consumer_id)::bigint[]))
  AND ((SELECT jsonb_typeof(k.scope->'persons') IS DISTINCT FROM 'array' FROM tl.consumers k WHERE k.id=news_candidates.consumer_id)
   OR m.person_id=ANY((SELECT ARRAY(SELECT x::bigint FROM jsonb_array_elements_text(k.scope->'persons') x) FROM tl.consumers k WHERE k.id=news_candidates.consumer_id)::bigint[]))
  AND (m.version=1 OR (SELECT coalesce((k.scope->>'include_versions')::boolean,true) FROM tl.consumers k WHERE k.id=news_candidates.consumer_id))
  AND ((SELECT coalesce((k.scope->>'include_ignored')::boolean,false) FROM tl.consumers k WHERE k.id=news_candidates.consumer_id)
   OR NOT CASE WHEN (m.fields->>'ignore_reason') IS NOT NULL OR EXISTS(
     SELECT 1 FROM tl.annotations a WHERE a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id AND a.status='active')
    THEN coalesce((SELECT o.ignored FROM tl.overlay_state(m.channel_id,m.external_id,m.version,m.fields,NULL) o),false)
    ELSE false END)
  AND ((SELECT api.asker_unrestricted()) OR api.row_visible(m.id,m.channel_id,m.thread_id))
 ORDER BY m.ingested_at,m.id
 LIMIT news_candidates.max_rows $$;
REVOKE EXECUTE ON FUNCTION tl.news_candidates(bigint,timestamptz,bigint,timestamptz,int) FROM PUBLIC,throughline_app,throughline_reader;

-- A page of arrivals in arrival order (not a population): rows of
-- api.record_versions after the consumer's cursor and at or before the
-- horizon. The caller's own consumer only; an unknown name returns nothing.
CREATE FUNCTION api.news(consumer text,max_results int DEFAULT 200)
RETURNS TABLE(record_id text,kind text,ref_id bigint,message_id bigint,version int,is_current boolean,channel text,external_id text,
 thread_id bigint,thread_no int,content text,record_time timestamptz,ended_at timestamptz,ingested_at timestamptz,
 subject_from timestamptz,subject_to timestamptz,subject_from_precision text,subject_to_precision text,person_id bigint,author_handle text,
 lat double precision,lon double precision,loc_reason text,is_outside boolean,heading_path text[],char_start int,char_end int,
 claim jsonb,generated boolean,unsupported boolean,provenance jsonb,support jsonb,expansion jsonb,
 is_new_version boolean,cursor jsonb)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE k record; h timestamptz := api.consumer_horizon();
BEGIN
 SELECT c.id,c.seen_to_ingested_at,c.seen_to_message_id INTO k FROM tl.consumers c WHERE c.id=tl.consumer_id(news.consumer);
 IF NOT FOUND THEN RETURN; END IF;
 RETURN QUERY
 SELECT r.record_id,r.kind,r.ref_id,r.message_id,r.version,r.is_current,r.channel,r.external_id,
  r.thread_id,r.thread_no,r.content,r.record_time,r.ended_at,r.ingested_at,
  r.subject_from,r.subject_to,r.subject_from_precision,r.subject_to_precision,r.person_id,r.author_handle,
  r.lat,r.lon,r.loc_reason,r.is_outside,r.heading_path,r.char_start,r.char_end,
  r.claim,r.generated,r.unsupported,r.provenance,r.support,r.expansion,
  n.version>1,jsonb_build_object('ingested_at',n.ingested_at,'message_id',n.message_id)
 FROM tl.news_candidates(k.id,k.seen_to_ingested_at,k.seen_to_message_id,h,greatest(1,least(coalesce(news.max_results,200),5000))) n
 JOIN api.record_versions r ON r.message_id=n.message_id AND r.record_id='m:'||n.message_id
 ORDER BY n.ingested_at,n.message_id;
END $$;

-- What a consumer has left to read, counted over gated rows only.
-- The input is named `name` because the output has a column `consumer`.
CREATE FUNCTION api.pending(name text)
RETURNS TABLE(consumer text,pending bigint,oldest_ingested_at timestamptz,newest_ingested_at timestamptz,horizon timestamptz,seen_to jsonb)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE k record; h timestamptz := api.consumer_horizon();
BEGIN
 SELECT c.id,c.name,c.seen_to_ingested_at,c.seen_to_message_id INTO k FROM tl.consumers c WHERE c.id=tl.consumer_id(pending.name);
 IF NOT FOUND THEN RETURN; END IF;
 RETURN QUERY
 SELECT k.name::text,count(*)::bigint,min(n.ingested_at),max(n.ingested_at),h,
  CASE WHEN k.seen_to_ingested_at IS NULL THEN NULL ELSE jsonb_build_object('ingested_at',k.seen_to_ingested_at,'message_id',k.seen_to_message_id) END
 FROM tl.news_candidates(k.id,k.seen_to_ingested_at,k.seen_to_message_id,h,NULL) n;
END $$;

-- 7. describe('coverage'): the catalogue section plus the caller's channels.
CREATE FUNCTION api.describe_coverage() RETURNS jsonb LANGUAGE sql STABLE AS $$
 SELECT coalesce((SELECT s.body FROM tl.catalog_sections s WHERE s.subject='coverage'),'{}'::jsonb)
  || jsonb_build_object(
   'reason_codes',(SELECT coalesce(jsonb_object_agg(r.code,jsonb_build_object('description',r.description,'severity',r.severity)),'{}'::jsonb) FROM tl.coverage_reasons r),
   'channels',(SELECT coalesce(jsonb_agg(to_jsonb(c) ORDER BY c.channel),'[]'::jsonb) FROM api.coverage_for() c)) $$;
COMMENT ON FUNCTION api.describe_coverage() IS 'Source coverage: statuses, reason codes, rules and the coverage of the channels you may read';

GRANT EXECUTE ON FUNCTION api.coverage_for(text[],timestamptz,timestamptz,text[]),api.consumer_horizon(),api.news(text,int),api.pending(text),api.describe_coverage() TO throughline_app,throughline_reader;

-- 8. Registry, allow-list and catalogue ------------------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('coverage_for','sql','coverage_for','Source coverage of the channels you may read over a window: status (worst of the channel''s rows), reasons, covered range, staleness, open gaps overlapping the window and window_status (covered, partly_covered, not_covered, unknown)','exact','free',0,'owner, agent; channels pass the asker gate','SELECT * FROM api.coverage_for(ARRAY[''synthetic.chat''])','1',NULL,NULL),
 ('news','sql','news','A page of arrivals in arrival order, not a population: records of your named consumer after its cursor and at or before the settle horizon, with is_new_version and a cursor per row (max_results capped at 5000)','exact','free',0,'owner, agent; your own consumers; rows pass the asker gate','SELECT * FROM api.news(''synthetic-consumer'',100)','1',NULL,200),
 ('pending','sql','pending','How many settled arrivals your named consumer has not marked, with the oldest and newest arrival times, the horizon and the cursor','exact','free',0,'owner, agent; your own consumers; counts pass the asker gate','SELECT * FROM api.pending(''synthetic-consumer'')','1',NULL,NULL);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('coverage_for','news','pending');

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('coverage',jsonb_build_object(
  'description','What each source reports it delivered, per channel and scope key: status, reasons, covered window, high water, open gaps. Read api.coverage, api.coverage_gaps or api.coverage_for(channels, after_at, before_at, kinds); GET /v1/coverage; throughline lib coverage.',
  'statuses',jsonb_build_object(
   'complete','The source delivered everything in the window',
   'partial','Something in the window is missing or unknown; see reasons and gaps',
   'best_effort','The source cannot say what is missing (for example no history listing); nothing reported is the default'),
  'rules',jsonb_build_array(
   'unknown degrades to partial: an unknown status, an unexpected field shape or an unknown reason code never stops an import; the raw value is kept and the report is stored.',
   'A plugin that has not succeeded within twice its interval is stale: reason stale, and complete degrades to partial.',
   'A gap is not absence: it marks a window the source did not deliver. A gap opens from an explicit gap or a partial report window and closes only when a report names it or covers its window with status complete and delivered rows or verified true.',
   'A best_effort report records its status but opens no window gap.',
   'not found in covered sources versus not found in coverage: recall says not_found_in_covered_sources only when every channel in scope is complete and covers the window; otherwise not_found_in_coverage with the partial channels and gaps. It never says that something did not happen.',
   'Coverage rows are shown for the channels you may read; plugin-level rows are for unrestricted askers only.'),
  'reporting','POST /v1/plugins/{name}/coverage {reports:[{channel?, scope_key?, status, reasons?, window{from,to}?, high_water?, rows_delivered?, pending_retries?, gaps?, closes?, verified?, detail?}]}; POST /v1/plugins/{name}/coverage/attempt; GET /v1/plugins/{name}/coverage/gaps',
  'functions',jsonb_build_array('coverage_for'))),
 ('consumers',jsonb_build_object(
  'description','Named cursors over arrivals for your token and asker: api.news(name, max_results) returns a page of arrivals in arrival order; api.pending(name) counts what is left; POST /v1/consumers/{name}/mark moves the cursor.',
  'rules',jsonb_build_array(
   'Arrival-keyed: the cursor is (ingested_at, message_id), never message time, so a late backfill of old messages is delivered once.',
   'Horizon: only rows with ingested_at at or before api.consumer_horizon() = now() - settle_s are delivered.',
   'Settle window: every transaction that inserts messages must commit within settle_s - margin_s of its start, for every role; the database refuses a longer one. So a cursor that stops at the horizon never skips a late commit. Restores are exempt (they replace consumer state together with the rows).',
   'Mark after work: read news, do the work, then mark the last cursor. Delivery is at least once (a crash before marking redelivers); marking is exactly once (compare-and-set on the previous cursor; a repeated mark is a no-op with the same answer).',
   'Scope keys: channels, kinds (default life_event and derived), thread_ids, persons (attributed author), include_ignored (default false), include_versions (default true; a new version is flagged is_new_version).'),
  'functions',jsonb_build_array('news','pending')));

-- 9. Purge: coverage reports and gaps of a purged channel (coverage rows cascade).
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('coverage_gaps',60,'DELETE FROM tl.coverage_gaps x WHERE x.channel_id=$1'),
 ('coverage_reports',61,'DELETE FROM tl.coverage_reports r WHERE r.channel_id=$1'),
 -- The channel's coverage state: scope keys and raw detail may carry content,
 -- and a 'complete' status must not outlive the purged messages.
 ('coverage',62,'DELETE FROM tl.coverage c WHERE c.channel_id=$1');
