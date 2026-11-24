-- Request and commitment ledger (natural key with corroboration,
-- extraction skips ignored and media-noise turns, evidence class; TBL-3, JOB-3, JOB-7, ACL-9, ARC-6, INT-10, INT-12, CST-1).
--
-- 1. tl.commitment_runs: one row per extraction or audit (ids, hashes and
--    counts only, never text). tl.commitment_run_items: every commitment an
--    extraction found (new, corroborated or already present), the set an
--    audit of that run must partition.
-- 2. tl.commitments: requests and commitments with an exact quote of the
--    pinned source version (a trigger refuses anything else), speaker,
--    effective time, due date and acceptance test. No writable state column:
--    rows are immutable except external_ref (a Plane task reference, never
--    called). The natural key (kind, speaker, day of said_at, sha256 of the
--    normalised statement) is unique; a second source of the same commitment
--    is a 'corroborates' row in tl.commitment_sources.
-- 3. tl.commitment_assessments: immutable, evidence-backed; done-type states
--    need evidence other than the commitment's own sources (detected is not
--    done). The current state is the latest assessment, or 'open'.
-- 4. Audit partition: a deferred constraint trigger checks at commit that the
--    commitments of an audit are exactly those of its extraction run.
-- 5. Gated views api.commitments and api.commitment_records (record prefix
--    r), functions, catalogue, purge.
--
-- Forward-only. tl objects are schema-qualified.

-- 1. Runs ---------------------------------------------------------------------------
CREATE TABLE tl.commitment_runs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 kind text NOT NULL CHECK (kind IN ('extract','audit')),
 population_handle text,
 population_hash text NOT NULL CHECK (population_hash ~ '^[0-9a-f]{64}$'),
 extract_run_id bigint REFERENCES tl.commitment_runs,
 operator text NOT NULL,
 provider text,
 model text,
 operator_version text NOT NULL,
 token_id bigint REFERENCES tl.tokens ON DELETE SET NULL,
 job text NOT NULL DEFAULT 'commitments',
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 finished_at timestamptz,
 status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','partial','failed')),
 counts jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(counts)='object'),
 CHECK ((kind='audit')=(extract_run_id IS NOT NULL)));
CREATE INDEX commitment_runs_extract ON tl.commitment_runs(extract_run_id) WHERE extract_run_id IS NOT NULL;

-- Lower case, whitespace collapsed, leading and trailing punctuation stripped
-- (the claim rule of 0042).
CREATE FUNCTION tl.commitment_statement_norm(statement text) RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT regexp_replace(btrim(regexp_replace(lower(coalesce(statement,'')),'\s+',' ','g')),'^[[:punct:][:space:]]+|[[:punct:][:space:]]+$','','g') $$;
-- Natural key: kind, speaker (person, else handle), UTC day of said_at and the
-- sha256 of the normalised statement.
CREATE FUNCTION tl.commitment_natural_key(kind text,speaker_person_id bigint,speaker_handle text,said_at timestamptz,statement text) RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,tl AS $$
 SELECT encode(sha256(convert_to(concat_ws('|',
  $1,
  coalesce('person:'||$2::text,'handle:'||nullif(lower(btrim($3)),''),'unattributed'),
  to_char($4 AT TIME ZONE 'UTC','YYYY-MM-DD'),
  encode(sha256(convert_to(tl.commitment_statement_norm($5),'UTF8')),'hex')),'UTF8')),'hex') $$;
GRANT EXECUTE ON FUNCTION tl.commitment_statement_norm(text),tl.commitment_natural_key(text,bigint,text,timestamptz,text) TO throughline_app;

-- 2. Commitments --------------------------------------------------------------------
CREATE TABLE tl.commitments(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 run_id bigint REFERENCES tl.commitment_runs,
 kind text NOT NULL CHECK (kind IN ('request','commitment')),
 statement text NOT NULL CHECK (length(statement) BETWEEN 1 AND 2000),
 quote text NOT NULL CHECK (length(quote) BETWEEN 1 AND 4000),
 source_message_id bigint NOT NULL REFERENCES tl.messages ON DELETE CASCADE,
 source_version int NOT NULL,
 char_start int,
 char_end int,
 speaker_person_id bigint REFERENCES tl.people ON DELETE SET NULL,
 speaker_handle text,
 addressee text CHECK (addressee IS NULL OR length(addressee)<=200),
 said_at timestamptz NOT NULL,
 due_at timestamptz,
 due_precision text CHECK (due_precision IN ('exact','day','month','year','approximate','unknown')),
 acceptance_test text CHECK (acceptance_test IS NULL OR length(acceptance_test)<=2000),
 weak boolean NOT NULL DEFAULT false,
 external_ref text CHECK (external_ref IS NULL OR length(btrim(external_ref)) BETWEEN 1 AND 500),
 method text NOT NULL CHECK (method IN ('rule','model','owner')),
 natural_key text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK (due_at IS NOT NULL OR due_precision IS NULL),
 CHECK (NOT weak OR (kind='request' AND acceptance_test IS NULL)));
-- Indexes, not table constraints: they contain expressions.
CREATE UNIQUE INDEX commitments_quote_key ON tl.commitments(source_message_id,char_start,char_end,md5(quote));
CREATE UNIQUE INDEX commitments_natural_key ON tl.commitments(natural_key);
CREATE INDEX commitments_source ON tl.commitments(source_message_id);
CREATE INDEX commitments_speaker ON tl.commitments(speaker_person_id) WHERE speaker_person_id IS NOT NULL;

-- An exact quote is a database guarantee: the quote must be exactly
-- substring(content of the pinned version, char_start+1, char_end-char_start).
-- The statement is stored normalised and the natural key is computed here, so
-- no writer can forge either.
CREATE FUNCTION tl.commitment_quote_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE body text; ver int;
BEGIN
 SELECT m.content,m.version INTO body,ver FROM tl.messages m WHERE m.id=NEW.source_message_id;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'commitment source message % does not exist',NEW.source_message_id USING ERRCODE='foreign_key_violation';
 END IF;
 IF ver IS DISTINCT FROM NEW.source_version THEN
  RAISE EXCEPTION 'commitment source version % is not the version of message % (%)',NEW.source_version,NEW.source_message_id,ver USING ERRCODE='check_violation';
 END IF;
 IF NEW.char_start IS NULL OR NEW.char_end IS NULL OR NEW.char_start<0 OR NEW.char_end<=NEW.char_start OR NEW.char_end>length(body)
  OR substring(body FROM NEW.char_start+1 FOR NEW.char_end-NEW.char_start) IS DISTINCT FROM NEW.quote THEN
  RAISE EXCEPTION 'a commitment quote must be exactly the pinned source text at char_start..char_end' USING ERRCODE='check_violation',
   HINT='quote = substring(content, char_start+1, char_end-char_start) of the source version';
 END IF;
 NEW.statement := tl.commitment_statement_norm(NEW.statement);
 IF NEW.statement='' THEN
  RAISE EXCEPTION 'a commitment needs a statement' USING ERRCODE='check_violation';
 END IF;
 NEW.natural_key := tl.commitment_natural_key(NEW.kind,NEW.speaker_person_id,NEW.speaker_handle,NEW.said_at,NEW.statement);
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.commitment_quote_guard() FROM PUBLIC;
CREATE TRIGGER quote_guard BEFORE INSERT ON tl.commitments FOR EACH ROW EXECUTE FUNCTION tl.commitment_quote_guard();

-- Immutable except external_ref (and speaker_person_id cleared by a person
-- delete, ON DELETE SET NULL).
CREATE FUNCTION tl.commitment_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF (to_jsonb(NEW)-ARRAY['external_ref','speaker_person_id']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['external_ref','speaker_person_id'])
  OR (NEW.speaker_person_id IS DISTINCT FROM OLD.speaker_person_id AND NEW.speaker_person_id IS NOT NULL) THEN
  RAISE EXCEPTION 'commitments are immutable; only external_ref changes (assess to change the state)' USING ERRCODE='insufficient_privilege';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.commitment_immutable() FROM PUBLIC;
CREATE TRIGGER immutable BEFORE UPDATE ON tl.commitments FOR EACH ROW EXECUTE FUNCTION tl.commitment_immutable();

-- Sources: the 'said' source and every corroborating one, version pinned.
CREATE TABLE tl.commitment_sources(
 commitment_id bigint NOT NULL REFERENCES tl.commitments ON DELETE CASCADE,
 message_id bigint NOT NULL REFERENCES tl.messages ON DELETE CASCADE,
 version int NOT NULL,
 relation text NOT NULL CHECK (relation IN ('said','corroborates')),
 run_id bigint REFERENCES tl.commitment_runs,
 char_start int,
 char_end int,
 added_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(commitment_id,message_id));
CREATE INDEX commitment_sources_message ON tl.commitment_sources(message_id);
CREATE FUNCTION tl.commitment_source_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM tl.messages m WHERE m.id=NEW.message_id AND m.version=NEW.version) THEN
  RAISE EXCEPTION 'commitment source % version % does not exist',NEW.message_id,NEW.version USING ERRCODE='foreign_key_violation';
 END IF;
 IF TG_OP='UPDATE' THEN
  RAISE EXCEPTION 'commitment sources are immutable' USING ERRCODE='insufficient_privilege';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.commitment_source_guard() FROM PUBLIC;
CREATE TRIGGER source_guard BEFORE INSERT OR UPDATE ON tl.commitment_sources FOR EACH ROW EXECUTE FUNCTION tl.commitment_source_guard();

CREATE TABLE tl.commitment_run_items(
 run_id bigint NOT NULL REFERENCES tl.commitment_runs ON DELETE CASCADE,
 commitment_id bigint NOT NULL REFERENCES tl.commitments ON DELETE CASCADE,
 outcome text NOT NULL CHECK (outcome IN ('written','corroborated','existing')),
 PRIMARY KEY(run_id,commitment_id));
CREATE INDEX commitment_run_items_commitment ON tl.commitment_run_items(commitment_id);

-- Runs keep their identity: kind, population, extraction run, operator and
-- start never change; a finished run changes only its counts (the purge
-- clears them). Run items are added only to a running extraction, so the set
-- an audit partitions is fixed once the extraction finishes.
CREATE FUNCTION tl.commitment_run_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF (to_jsonb(NEW)-ARRAY['status','finished_at','counts']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','finished_at','counts'])
  OR (OLD.status<>'running' AND (NEW.status IS DISTINCT FROM OLD.status OR NEW.finished_at IS DISTINCT FROM OLD.finished_at)) THEN
  RAISE EXCEPTION 'commitment runs are immutable once finished, except their counts' USING ERRCODE='insufficient_privilege';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.commitment_run_guard() FROM PUBLIC;
CREATE TRIGGER immutable BEFORE UPDATE ON tl.commitment_runs FOR EACH ROW EXECUTE FUNCTION tl.commitment_run_guard();
CREATE FUNCTION tl.commitment_run_item_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM tl.commitment_runs r WHERE r.id=NEW.run_id AND r.kind='extract' AND r.status='running') THEN
  RAISE EXCEPTION 'run items are added only to a running extraction' USING ERRCODE='check_violation';
 END IF;
 IF TG_OP='UPDATE' THEN
  RAISE EXCEPTION 'run items are immutable' USING ERRCODE='insufficient_privilege';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.commitment_run_item_guard() FROM PUBLIC;
CREATE TRIGGER item_guard BEFORE INSERT OR UPDATE ON tl.commitment_run_items FOR EACH ROW EXECUTE FUNCTION tl.commitment_run_item_guard();

-- 3. Assessments --------------------------------------------------------------------
CREATE TABLE tl.commitment_assessments(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 commitment_id bigint NOT NULL REFERENCES tl.commitments ON DELETE CASCADE,
 state text NOT NULL CHECK (state IN ('completed','partially_completed','not_completed','superseded','expired','unclear')),
 evidence_message_ids bigint[] NOT NULL CHECK (cardinality(evidence_message_ids)>0 AND cardinality(evidence_message_ids)<=200),
 evidence_versions int[],
 note text CHECK (note IS NULL OR length(note)<=2000),
 method text NOT NULL CHECK (method IN ('owner','rule','model','agent')),
 token_id bigint REFERENCES tl.tokens ON DELETE SET NULL,
 job text,
 job_version text,
 audit_id bigint REFERENCES tl.commitment_runs,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK (evidence_versions IS NULL OR cardinality(evidence_versions)=cardinality(evidence_message_ids)));
CREATE INDEX commitment_assessments_commitment ON tl.commitment_assessments(commitment_id,created_at DESC,id DESC);
CREATE INDEX commitment_assessments_evidence ON tl.commitment_assessments USING gin(evidence_message_ids);

-- Every evidence id is a message (versions pinned); done-type states need
-- evidence other than the commitment's own sources (any version of them):
-- detected is not done.
CREATE FUNCTION tl.commitment_assessment_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE versions int[];
BEGIN
 IF TG_OP='UPDATE' THEN
  RAISE EXCEPTION 'commitment assessments are immutable; add a new assessment' USING ERRCODE='insufficient_privilege';
 END IF;
 IF EXISTS(SELECT 1 FROM unnest(NEW.evidence_message_ids) e(id) WHERE e.id IS NULL OR NOT EXISTS(SELECT 1 FROM tl.messages m WHERE m.id=e.id)) THEN
  RAISE EXCEPTION 'every evidence id of an assessment must be a message' USING ERRCODE='foreign_key_violation';
 END IF;
 versions := ARRAY(SELECT m.version FROM unnest(NEW.evidence_message_ids) WITH ORDINALITY e(id,o) JOIN tl.messages m ON m.id=e.id ORDER BY e.o);
 IF NEW.evidence_versions IS NULL THEN
  NEW.evidence_versions := versions;
 ELSIF NEW.evidence_versions IS DISTINCT FROM versions THEN
  RAISE EXCEPTION 'evidence_versions must be the versions of the evidence messages' USING ERRCODE='check_violation';
 END IF;
 IF NEW.state IN ('completed','partially_completed','not_completed','superseded') AND NOT EXISTS(
   SELECT 1 FROM unnest(NEW.evidence_message_ids) e(id) JOIN tl.messages m ON m.id=e.id
   WHERE NOT EXISTS(
    SELECT 1 FROM tl.commitments c JOIN tl.messages s ON s.id=c.source_message_id
    WHERE c.id=NEW.commitment_id AND s.channel_id=m.channel_id AND s.external_id=m.external_id)
   AND NOT EXISTS(
    SELECT 1 FROM tl.commitment_sources x JOIN tl.messages s ON s.id=x.message_id
    WHERE x.commitment_id=NEW.commitment_id AND s.channel_id=m.channel_id AND s.external_id=m.external_id)) THEN
  RAISE EXCEPTION 'detected is not done: state % needs evidence other than the commitment''s own sources',NEW.state USING ERRCODE='check_violation';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.commitment_assessment_guard() FROM PUBLIC;
CREATE TRIGGER assessment_guard BEFORE INSERT OR UPDATE ON tl.commitment_assessments FOR EACH ROW EXECUTE FUNCTION tl.commitment_assessment_guard();

-- A restricted scope proposes an assessment (kind commitment_assessment);
-- the owner's accept records it through the same guarded insert, with
-- method agent (never owner).
ALTER TABLE tl.proposals DROP CONSTRAINT proposals_kind_check;
ALTER TABLE tl.proposals ADD CONSTRAINT proposals_kind_check
 CHECK (kind IN ('claim','link','concept','correction','recipe','provenance','link_type','annotation','commitment_assessment'));

-- 4. Audit partition ----------------------------------------------------------------
CREATE TABLE tl.commitment_audit_items(
 audit_id bigint NOT NULL REFERENCES tl.commitment_runs ON DELETE CASCADE,
 commitment_id bigint NOT NULL REFERENCES tl.commitments ON DELETE CASCADE,
 assessment_id bigint NOT NULL REFERENCES tl.commitment_assessments ON DELETE CASCADE,
 PRIMARY KEY(audit_id,commitment_id));
CREATE INDEX commitment_audit_items_commitment ON tl.commitment_audit_items(commitment_id);

-- At commit: the commitment ids of an audit are exactly those its extraction
-- run found (none missing, none extra; the primary key forbids duplicates),
-- and each item's assessment belongs to that commitment and that audit. Every
-- item row checks itself (its commitment is one the run found, its
-- assessment is its own): adding items can only add extras, never missing
-- ones. The full set comparison runs for the audit's run row and for the
-- item with the highest commitment id, so a large audit is compared twice,
-- not once per row, and no session setting can skip a check.
CREATE FUNCTION tl.commitment_audit_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE aid bigint; r tl.commitment_runs; missing bigint[]; extra bigint[]; bad bigint[];
BEGIN
 IF TG_TABLE_NAME='commitment_runs' THEN aid := NEW.id; ELSE aid := NEW.audit_id; END IF;
 SELECT * INTO r FROM tl.commitment_runs WHERE id=aid;
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF r.kind<>'audit' THEN
  RAISE EXCEPTION 'commitment run % is not an audit',aid USING ERRCODE='check_violation';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM tl.commitment_runs e WHERE e.id=r.extract_run_id AND e.kind='extract') THEN
  RAISE EXCEPTION 'audit % must name an extraction run',aid USING ERRCODE='check_violation';
 END IF;
 IF TG_TABLE_NAME='commitment_audit_items' THEN
  IF NOT EXISTS(SELECT 1 FROM tl.commitment_run_items i WHERE i.run_id=r.extract_run_id AND i.commitment_id=NEW.commitment_id) THEN
   RAISE EXCEPTION 'audit % is not a partition of extraction run %: every extracted commitment exactly once',aid,r.extract_run_id
    USING ERRCODE='check_violation',
    DETAIL=jsonb_build_object('missing','[]'::jsonb,'extra',jsonb_build_array(NEW.commitment_id))::text;
  END IF;
  IF NOT EXISTS(SELECT 1 FROM tl.commitment_assessments a WHERE a.id=NEW.assessment_id AND a.commitment_id=NEW.commitment_id AND a.audit_id=aid) THEN
   RAISE EXCEPTION 'audit % items must carry an assessment of that commitment made by that audit',aid
    USING ERRCODE='check_violation',DETAIL=jsonb_build_object('mismatched',jsonb_build_array(NEW.commitment_id))::text;
  END IF;
  IF NEW.commitment_id<(SELECT max(x.commitment_id) FROM tl.commitment_audit_items x WHERE x.audit_id=aid) THEN
   RETURN NULL;
  END IF;
 END IF;
 missing := ARRAY(SELECT i.commitment_id FROM tl.commitment_run_items i WHERE i.run_id=r.extract_run_id
  EXCEPT SELECT x.commitment_id FROM tl.commitment_audit_items x WHERE x.audit_id=aid ORDER BY 1);
 extra := ARRAY(SELECT x.commitment_id FROM tl.commitment_audit_items x WHERE x.audit_id=aid
  EXCEPT SELECT i.commitment_id FROM tl.commitment_run_items i WHERE i.run_id=r.extract_run_id ORDER BY 1);
 IF cardinality(missing)>0 OR cardinality(extra)>0 THEN
  RAISE EXCEPTION 'audit % is not a partition of extraction run %: every extracted commitment exactly once',aid,r.extract_run_id
   USING ERRCODE='check_violation',
   DETAIL=jsonb_build_object('missing',to_jsonb(missing),'extra',to_jsonb(extra))::text;
 END IF;
 -- Assessments made by this audit that no item carries.
 bad := ARRAY(SELECT a.commitment_id FROM tl.commitment_assessments a WHERE a.audit_id=aid
  AND NOT EXISTS(SELECT 1 FROM tl.commitment_audit_items x WHERE x.audit_id=aid AND x.assessment_id=a.id) ORDER BY 1);
 IF cardinality(bad)>0 THEN
  RAISE EXCEPTION 'audit % made assessments that no audit item carries',aid
   USING ERRCODE='check_violation',DETAIL=jsonb_build_object('mismatched',to_jsonb(bad))::text;
 END IF;
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.commitment_audit_check() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER audit_partition AFTER INSERT ON tl.commitment_audit_items
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION tl.commitment_audit_check();
CREATE CONSTRAINT TRIGGER audit_partition AFTER INSERT ON tl.commitment_runs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.kind='audit') EXECUTE FUNCTION tl.commitment_audit_check();
-- An assessment that names an audit must be carried by an item of that audit,
-- checked at commit whenever it is written: an assessment added later to an
-- audit that already committed (no run or item insert in that transaction)
-- is refused too. One indexed lookup per assessment, not a set comparison.
CREATE FUNCTION tl.commitment_audit_assessment_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM tl.commitment_runs r WHERE r.id=NEW.audit_id AND r.kind='audit') THEN
  RAISE EXCEPTION 'assessment % names run %, which is not an audit',NEW.id,NEW.audit_id USING ERRCODE='check_violation';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM tl.commitment_audit_items x WHERE x.audit_id=NEW.audit_id AND x.commitment_id=NEW.commitment_id AND x.assessment_id=NEW.id) THEN
  RAISE EXCEPTION 'audit % made assessments that no audit item carries',NEW.audit_id
   USING ERRCODE='check_violation',DETAIL=jsonb_build_object('mismatched',jsonb_build_array(NEW.commitment_id))::text;
 END IF;
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.commitment_audit_assessment_check() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER audit_partition AFTER INSERT ON tl.commitment_assessments
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.audit_id IS NOT NULL) EXECUTE FUNCTION tl.commitment_audit_assessment_check();
CREATE FUNCTION tl.commitment_audit_items_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 RAISE EXCEPTION 'audit items are immutable' USING ERRCODE='insufficient_privilege';
END $$;
REVOKE EXECUTE ON FUNCTION tl.commitment_audit_items_immutable() FROM PUBLIC;
CREATE TRIGGER immutable BEFORE UPDATE ON tl.commitment_audit_items FOR EACH ROW EXECUTE FUNCTION tl.commitment_audit_items_immutable();

REVOKE ALL ON tl.commitment_runs,tl.commitments,tl.commitment_sources,tl.commitment_run_items,tl.commitment_assessments,tl.commitment_audit_items FROM PUBLIC,throughline_app,throughline_reader;
GRANT SELECT,INSERT,UPDATE ON tl.commitment_runs TO throughline_app;
GRANT SELECT,INSERT ON tl.commitments,tl.commitment_sources,tl.commitment_run_items,tl.commitment_assessments,tl.commitment_audit_items TO throughline_app;
GRANT UPDATE(external_ref) ON tl.commitments TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.commitment_runs_id_seq,tl.commitments_id_seq,tl.commitment_assessments_id_seq TO throughline_app;

-- 5. Gated views --------------------------------------------------------------------
-- A restricted asker sees a commitment only when its source, every
-- corroborating source and every evidence message of every assessment are
-- visible (ACL-9). Speaker persons and natural keys are unrestricted-only.
SELECT tl.install_gate('commitments',
 'api.sources_visible(v.visibility_message_ids)',
 $def$
SELECT c.id,c.kind,c.statement,c.quote,
 c.source_message_id,c.source_version,c.char_start,c.char_end,
 sm.channel_id AS source_channel_id,sch.name AS channel,sm.external_id,sm.thread_id,sm.thread_no,
 coalesce(s.refs,'[]'::jsonb) AS source_refs,
 coalesce(s.ids,ARRAY[c.source_message_id]) AS source_message_ids,
 CASE WHEN (SELECT api.asker_unrestricted()) THEN c.speaker_person_id END AS speaker_person_id,
 c.speaker_handle,c.addressee,c.said_at,c.due_at,c.due_precision,c.acceptance_test,c.weak,
 coalesce(a.state,'open') AS state,
 coalesce(a.n,0)::int AS assessments,
 a.last_assessed_at,
 c.external_ref,c.method,c.run_id,
 CASE WHEN (SELECT api.asker_unrestricted()) THEN c.natural_key END AS natural_key,
 c.created_at,
 ARRAY(SELECT DISTINCT x FROM unnest(coalesce(s.ids,ARRAY[c.source_message_id])||coalesce(a.evidence,'{}'::bigint[])) x ORDER BY x) AS visibility_message_ids
FROM tl.commitments c
JOIN tl.messages sm ON sm.id=c.source_message_id
JOIN tl.channels sch ON sch.id=sm.channel_id
LEFT JOIN LATERAL (
 SELECT array_agg(x.message_id ORDER BY x.message_id) AS ids,
  jsonb_agg(jsonb_build_object('message_id',x.message_id,'version',x.version,'relation',x.relation,'added_at',x.added_at) ORDER BY x.relation DESC,x.message_id) AS refs
 FROM tl.commitment_sources x WHERE x.commitment_id=c.id) s ON true
LEFT JOIN LATERAL (
 SELECT (array_agg(z.state ORDER BY z.created_at DESC,z.id DESC))[1] AS state,count(*) AS n,max(z.created_at) AS last_assessed_at,
  ARRAY(SELECT DISTINCT e FROM tl.commitment_assessments y CROSS JOIN LATERAL unnest(y.evidence_message_ids) e WHERE y.commitment_id=c.id) AS evidence
 FROM tl.commitment_assessments z WHERE z.commitment_id=c.id) a ON true
$def$);

-- Record contract view of commitments (prefix r). Columns, order and types are
-- those of api.record_versions. content = statement and quote; record_time =
-- said_at; subject = due; support = the sources.
SELECT tl.install_gate('commitment_records','EXISTS(SELECT 1 FROM tl.commitments_gated g WHERE g.id=v.ref_id)',$def$
SELECT 'r:'||c.id AS record_id,
 'commitment'::text AS kind,
 c.id AS ref_id,
 c.source_message_id AS message_id,
 c.source_version AS version,
 true AS is_current,
 c.channel,
 c.external_id,
 c.thread_id,
 c.thread_no,
 c.statement||E'\n"'||c.quote||'"' AS content,
 c.said_at AS record_time,
 NULL::timestamptz AS ended_at,
 c.created_at AS ingested_at,
 c.due_at AS subject_from,
 NULL::timestamptz AS subject_to,
 c.due_precision AS subject_from_precision,
 NULL::text AS subject_to_precision,
 c.speaker_person_id AS person_id,
 c.speaker_handle AS author_handle,
 NULL::double precision AS lat,
 NULL::double precision AS lon,
 'not_applicable'::text AS loc_reason,
 false AS is_outside,
 NULL::text[] AS heading_path,
 c.char_start,
 c.char_end,
 jsonb_build_object(
  'kind',c.kind,
  'state',c.state,
  'due',c.due_at,
  'due_precision',c.due_precision,
  'speaker',jsonb_build_object('person_id',c.speaker_person_id,'handle',c.speaker_handle),
  'addressee',c.addressee,
  'acceptance_test',c.acceptance_test,
  'weak',c.weak,
  'assessments',c.assessments,
  'method',c.method,
  'external_ref',c.external_ref,
  'corroborating_sources',greatest(jsonb_array_length(c.source_refs)-1,0)) AS claim,
 true AS generated,
 false AS unsupported,
 jsonb_build_object('method',c.method,'run_id',c.run_id,'job','commitments') AS provenance,
 (SELECT coalesce(jsonb_agg(jsonb_build_object('message_id',(e->>'message_id')::bigint,'version',(e->>'version')::int,'relation',e->>'relation')),'[]'::jsonb)
  FROM jsonb_array_elements(c.source_refs) e) AS support,
 jsonb_build_object('commitment',c.id,'sources',true,'assessments',c.assessments) AS expansion
FROM tl.commitments_all c
$def$);
INSERT INTO tl.record_sources VALUES ('r','api.commitment_records') ON CONFLICT (prefix) DO NOTHING;

-- 6. Functions ------------------------------------------------------------------------
-- Assessments of a commitment, newest first; nothing when it is not visible.
CREATE FUNCTION api.commitment_assessments(commitment_id bigint)
RETURNS TABLE(id bigint,commitment_id bigint,state text,evidence_message_ids bigint[],evidence_versions int[],note text,method text,job text,job_version text,audit_id bigint,created_at timestamptz)
LANGUAGE sql STABLE AS $$
 SELECT a.id,a.commitment_id,a.state,a.evidence_message_ids,a.evidence_versions,a.note,a.method,a.job,a.job_version,a.audit_id,a.created_at
 FROM api.commitments c JOIN tl.commitment_assessments a ON a.commitment_id=c.id
 WHERE c.id=commitment_assessments.commitment_id
 ORDER BY a.created_at DESC,a.id DESC $$;
-- Open requests and commitments (no assessment yet), optionally of one
-- speaker; earliest due first, undated last. max_results NULL = all.
CREATE FUNCTION api.open_commitments(person_id bigint DEFAULT NULL,max_results int DEFAULT 100)
RETURNS SETOF api.commitments LANGUAGE sql STABLE AS $$
 SELECT c.* FROM api.commitments c
 WHERE c.state='open' AND (open_commitments.person_id IS NULL OR c.speaker_person_id=open_commitments.person_id)
 ORDER BY c.due_at NULLS LAST,c.said_at,c.id
 LIMIT open_commitments.max_results $$;
REVOKE EXECUTE ON FUNCTION api.commitment_assessments(bigint),api.open_commitments(bigint,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api.commitment_assessments(bigint),api.open_commitments(bigint,int) TO throughline_app,throughline_reader;

-- 7. Registry, allow-list and catalogue -----------------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('commitment_assessments','sql','commitment_assessments','The assessments of one request or commitment, newest first: state, evidence message ids with pinned versions, note, method and audit; nothing when the commitment is hidden','exact','free',2,'owner, agent; rows pass the asker gate (source, corroboration and every evidence message visible)','SELECT * FROM api.commitment_assessments(1)','1',NULL,NULL),
 ('open_commitments','sql','open_commitments','Open requests and commitments (detected, never assessed), optionally of one speaker person, earliest due first; detected is not done','exact','free',1,'owner, agent; rows pass the asker gate; speaker persons unrestricted only','SELECT * FROM api.open_commitments(NULL,50)','1',2,100);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('commitment_assessments','open_commitments');

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('commitments',jsonb_build_object(
  'description','The request and commitment ledger: exact-quote requests and commitments extracted from messages and transcripts, with speaker, effective time, due date and acceptance test, assessed only with evidence and audited as a partition. Tasks stay in Plane: a commitment carries an optional external_ref and Throughline never calls Plane. Ground truth is never changed.',
  'views',jsonb_build_object(
   'api.commitments','id, kind (request | commitment), statement (normalised), quote, source_message_id, source_version, char_start, char_end, source_refs (said and corroborates), speaker_person_id (unrestricted only), speaker_handle, addressee, said_at, due_at, due_precision, acceptance_test, weak, state, assessments, external_ref, method',
   'api.commitment_records','Commitments in the record contract (prefix r, kind commitment, generated true, support = sources; content = statement and quote; record_time = said_at; subject_from = due)'),
  'states',jsonb_build_array('open','completed','partially_completed','not_completed','superseded','expired','unclear'),
  'rules',jsonb_build_array(
   'An exact quote is a database guarantee: quote = substring(content of the pinned version, char_start+1, char_end-char_start), refused otherwise.',
   'Natural key: kind, speaker, UTC day of said_at and sha256 of the normalised statement (unique). A second source of the same commitment adds a corroborating source, never a duplicate; re-extraction is idempotent.',
   'Extraction skips ignored and media-noise turns (the default scope); speaker from the overlay speaker or attribution; said_at is the effective time.',
   'Detected is not done: the state is open until an assessment says otherwise; completed, partially_completed, not_completed and superseded need evidence other than the commitment''s own sources; every assessment needs at least one evidence message.',
   'Assessments are immutable; the current state is the latest assessment.',
   'An audit of an extraction run assesses every commitment that run found exactly once; the partition is enforced at commit.',
   'A restricted asker sees a commitment only when its source, every corroborating source and every evidence message are visible; restricted assessments become proposals.',
   'evidence_class: derived for rule and owner commitments, inference for model ones.'),
  'operation','extract_requests: sentences {id, text, context {said_at, message_id}} -> request | commitment | none with quote, char_start, char_end, statement, addressee, due, due_precision, acceptance_test, weak; local deterministic lexicon rule by default; a non-local provider is estimated and cost-gated first',
  'routes','POST /v1/commitments/extract; GET /v1/commitments; GET /v1/commitments/{id}; POST /v1/commitments/{id}/assess; POST /v1/commitments/audit; PATCH /v1/commitments/{id}/external-ref; throughline lib commitments extract|list|show|assess|audit',
  'functions',jsonb_build_array('commitment_assessments','open_commitments')));

-- 8. Purge (ARC-5, ACL-9): commitments whose source, corroboration or any
-- assessment evidence is in the purged channel go (assessments and audit
-- items cascade); the counts of their runs are cleared.
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('commitments',33,
  'WITH doomed AS MATERIALIZED (SELECT c.id,c.run_id FROM tl.commitments c
     WHERE EXISTS(SELECT 1 FROM tl.messages m WHERE m.id=c.source_message_id AND m.channel_id=$1)
      OR EXISTS(SELECT 1 FROM tl.commitment_sources s JOIN tl.messages m ON m.id=s.message_id WHERE s.commitment_id=c.id AND m.channel_id=$1)
      OR EXISTS(SELECT 1 FROM tl.commitment_assessments a JOIN tl.messages m ON m.id=ANY(a.evidence_message_ids) WHERE a.commitment_id=c.id AND m.channel_id=$1)),
   runs AS (UPDATE tl.commitment_runs r SET counts=jsonb_build_object(''purged'',true)
     WHERE r.id IN (SELECT d.run_id FROM doomed d
      UNION SELECT i.run_id FROM tl.commitment_run_items i JOIN doomed d ON d.id=i.commitment_id
      UNION SELECT x.audit_id FROM tl.commitment_audit_items x JOIN doomed d ON d.id=x.commitment_id
      UNION SELECT a.audit_id FROM tl.commitment_assessments a JOIN doomed d ON d.id=a.commitment_id))
   DELETE FROM tl.commitments c WHERE c.id IN (SELECT d.id FROM doomed d)');
