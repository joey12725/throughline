-- Transcript-native behaviour (IDN-6, IDN-8, MED-3,
-- ARC-6, scenarios media_contaminated and unenrolled_voice).
--
-- 1. Media-noise run state: one row per recording (thread) judged by the
--    transcripts job, so a recording is judged again only after a new ingest.
--    The flags themselves are overlay annotations (tl.annotations, attribute
--    media_noise, job 'transcripts'); the transcript text is never changed.
-- 2. 'Only enrolled voices become people': an identifier of kind
--    speaker_label never carries a person, for every role. Enforced on the
--    identifier kind itself (a CHECK plus a trigger with a readable message),
--    so it holds with no identifier_schemes row, after a scheme is created at
--    runtime and after tests truncate the tables. Existing label attachments
--    are first turned into owner 'speaker' annotations (curation kept, no
--    person created from a label, an identity_event records it).
-- 3. api.speaker_inbox: unenrolled speaker labels per recording, for
--    unrestricted askers only; not allow-listed for SQL (served by
--    GET /v1/transcripts/speakers).
--
-- Forward-only. tl objects are schema-qualified.

-- 1. Noise state and runs ----------------------------------------------------------
CREATE TABLE tl.transcript_noise_state(
 thread_id bigint PRIMARY KEY REFERENCES tl.threads ON DELETE CASCADE,
 judged_to_ingested_at timestamptz NOT NULL,
 judged_at timestamptz NOT NULL DEFAULT now(),
 turns int NOT NULL DEFAULT 0,
 flagged int NOT NULL DEFAULT 0,
 job_version text NOT NULL);
-- Counts and ids only, never transcript text.
CREATE TABLE tl.transcript_noise_runs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 started_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz,
 status text NOT NULL CHECK (status IN ('running','done','partial','max_items','failed')),
 dry_run boolean NOT NULL DEFAULT false,
 token_id bigint REFERENCES tl.tokens ON DELETE SET NULL,
 summary jsonb NOT NULL DEFAULT '{}');
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.transcript_noise_state TO throughline_app;
GRANT SELECT,INSERT,UPDATE ON tl.transcript_noise_runs TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.transcript_noise_runs_id_seq TO throughline_app;
CREATE INDEX IF NOT EXISTS messages_author_identifier ON tl.messages(author_identifier_id) WHERE author_identifier_id IS NOT NULL;

-- 2. Only enrolled voices become people ----------------------------------------------
-- Re-runnable conversion: every speaker_label identifier that has a person is
-- detached; each attachment becomes an owner 'speaker' annotation on that
-- label's turns (every version: target_version NULL) unless the turn already
-- has an active owner speaker annotation. Returns the annotations written.
CREATE FUNCTION tl.convert_speaker_label_attachments() RETURNS int LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$
DECLARE d jsonb; n int := 0;
BEGIN
 SELECT coalesce(jsonb_agg(jsonb_build_object('identifier_id',i.id,'person_id',i.person_id,'system',i.system,'label',i.value,
   'turns',(SELECT count(*) FROM tl.messages m WHERE m.author_identifier_id=i.id AND m.superseded_by IS NULL)) ORDER BY i.id),'[]'::jsonb)
 INTO d FROM tl.identifiers i WHERE i.kind='speaker_label' AND i.person_id IS NOT NULL;
 IF jsonb_array_length(d)=0 THEN RETURN 0; END IF;
 INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin,reason)
 SELECT DISTINCT ON (m.channel_id,m.external_id) m.channel_id,m.external_id,'speaker',
  jsonb_build_object('person_id',i.person_id),'owner','owner',
  'converted from a speaker_label attachment: only enrolled voices become people'
 FROM tl.identifiers i JOIN tl.messages m ON m.author_identifier_id=i.id
 WHERE i.kind='speaker_label' AND i.person_id IS NOT NULL
  AND NOT EXISTS(SELECT 1 FROM tl.annotations a WHERE a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id
   AND a.attribute='speaker' AND a.status='active' AND a.method='owner')
 ORDER BY m.channel_id,m.external_id,m.version DESC;
 GET DIAGNOSTICS n = ROW_COUNT;
 UPDATE tl.messages m SET person_id=NULL FROM tl.identifiers i
  WHERE m.author_identifier_id=i.id AND i.kind='speaker_label' AND i.person_id IS NOT NULL AND m.person_id IS NOT NULL;
 UPDATE tl.identifiers SET person_id=NULL WHERE kind='speaker_label' AND person_id IS NOT NULL;
 INSERT INTO tl.identity_events(operation,asserted_by,detail) VALUES
  ('speaker_label_conversion','owner',jsonb_build_object('identifiers',d,'annotations',n,
   'rule','only enrolled voices become people; label attachments became owner speaker annotations'));
 RETURN n;
END $$;
REVOKE EXECUTE ON FUNCTION tl.convert_speaker_label_attachments() FROM PUBLIC,throughline_app,throughline_reader;
SELECT tl.convert_speaker_label_attachments();

-- The readable refusal fires before the CHECK (a BEFORE trigger); the CHECK
-- holds even with triggers disabled. voice_profile stays attachable.
CREATE FUNCTION tl.identifiers_unenrolled_voice() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.kind='speaker_label' AND NEW.person_id IS NOT NULL THEN
  RAISE EXCEPTION 'only enrolled voices become people; enrol a voice profile or annotate the speaker'
   USING ERRCODE='check_violation',
   HINT='Assign the label''s turns with POST /v1/transcripts/speakers/assign (owner speaker annotations) or enrol a voice_profile identifier.';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.identifiers_unenrolled_voice() FROM PUBLIC;
CREATE TRIGGER unenrolled_voice BEFORE INSERT OR UPDATE OF person_id,kind ON tl.identifiers
 FOR EACH ROW EXECUTE FUNCTION tl.identifiers_unenrolled_voice();
ALTER TABLE tl.identifiers ADD CONSTRAINT identifiers_unenrolled_voice CHECK (kind <> 'speaker_label' OR person_id IS NULL);

-- 3. Speaker inbox -------------------------------------------------------------------
-- One row per (speaker_label identifier, recording): unclaimed labels whose
-- status is not 'ignored' (IDN-6), with their turn count, spoken seconds
-- (fields end_s - start_s, else ended_at - happened_at), first and last time,
-- the first three turns and whether every turn already has an active
-- speaker annotation. A (label, recording) whose every turn the owner
-- ignored is left out. Unannotated first, newest first. Empty for restricted
-- askers: identity data is unrestricted-only.
CREATE FUNCTION api.speaker_inbox(max_results int DEFAULT 100)
RETURNS TABLE(identifier_id bigint,thread_id bigint,recording_title text,label text,turns bigint,total_s double precision,
 first_at timestamptz,last_at timestamptz,sample_message_ids bigint[],annotated boolean)
LANGUAGE sql STABLE AS $$
 SELECT i.id,m.thread_id,t.title,i.value,count(*)::bigint,
  sum(greatest(0::float8,coalesce(
   CASE WHEN jsonb_typeof(m.fields->'end_s')='number' AND jsonb_typeof(m.fields->'start_s')='number'
    THEN (m.fields->>'end_s')::float8-(m.fields->>'start_s')::float8 END,
   extract(epoch FROM m.ended_at-m.happened_at)::float8,0::float8)))::float8,
  min(m.happened_at),max(coalesce(m.ended_at,m.happened_at)),
  (array_agg(m.id ORDER BY m.thread_no,m.id))[1:3],
  bool_and(s.annotated)
 FROM tl.identifiers i
 JOIN tl.messages m ON m.author_identifier_id=i.id AND m.superseded_by IS NULL
 LEFT JOIN tl.threads t ON t.id=m.thread_id
 CROSS JOIN LATERAL (SELECT EXISTS(SELECT 1 FROM tl.annotations a WHERE a.target_channel_id=m.channel_id
   AND a.target_external_id=m.external_id AND a.attribute='speaker' AND a.status='active') AS annotated,
  EXISTS(SELECT 1 FROM tl.annotations a WHERE a.target_channel_id=m.channel_id
   AND a.target_external_id=m.external_id AND a.attribute='ignore' AND a.status='active' AND a.method='owner'
   AND (a.target_version IS NULL OR a.target_version=m.version) AND a.value->'ignored'='true'::jsonb) AS ignored) s
 WHERE i.kind='speaker_label' AND i.person_id IS NULL AND i.status IS DISTINCT FROM 'ignored'
  AND (SELECT api.asker_unrestricted())
 GROUP BY i.id,m.thread_id,t.title,i.value
 -- A label the owner ignored in this recording (every turn carries an owner
 -- ignore) leaves this recording's row even when the label's identifier is
 -- shared with other recordings and so keeps its status.
 HAVING NOT bool_and(s.ignored)
 ORDER BY bool_and(s.annotated),max(coalesce(m.ended_at,m.happened_at)) DESC,i.id,m.thread_id
 LIMIT greatest(1,least(coalesce(speaker_inbox.max_results,100),1000)) $$;
REVOKE EXECUTE ON FUNCTION api.speaker_inbox(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api.speaker_inbox(int) TO throughline_app,throughline_reader;

-- 4. Catalogue -----------------------------------------------------------------------
-- api.speaker_inbox is level 'owner': it has no tl.api_functions row and is
-- not on the SQL allow-list; GET /v1/transcripts/speakers serves it.
INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('transcripts',jsonb_build_object(
  'description','Transcript-native behaviour for whole-thread recording channels (conversation_whole_thread): per-segment media-noise flags, the speaker inbox and the rule that only enrolled voices become people.',
  'media_noise',jsonb_build_object(
   'operation','classify_media_noise: items {id, text, context {overlapping_media [{record_id, text, title?}], duration_s}} -> media | conversation | uncertain with evidence offsets and a score',
   'local_rule',jsonb_build_array(
    'word 4-gram Jaccard between the turn and the text of media playing at that time (best window) at least THROUGHLINE_MEDIA_NOISE_OVERLAP (0.5) -> media, with matched_record_id',
    'markers (music symbols, [music], [applause]) or lyric-like repetition (repeated 4-grams at least half of at least 40 tokens) -> media',
    'a turn that names the title of a playing work but overlaps little is conversation about media and is kept',
    'otherwise conversation'),
   'flags','A media turn gets an overlay annotation media_noise {is_noise: true, score, evidence, matched_record_id} (method rule for the local model, else model; job transcripts). The text is never changed; flagged turns are excluded by default like ignored ones and come back with include_ignored.',
   'fail_open','A provider error, timeout or approval requirement writes no flag for that batch, marks the run partial with the reason and leaves the recording to be judged again. Nothing is hidden by a failure.',
   'owner','An owner media_noise {is_noise: false} or ignore {ignored: false} on a turn wins: later runs skip it.',
   'calibration','Every recording is covered (min_duration_s defaults to 0, flagged per segment); recordings of an hour or more are judged first and reported in their own length bucket.',
   'routes','POST /v1/transcripts/noise/run {policy, dry_run} (owner); GET /v1/transcripts/noise/status; throughline lib transcripts noise run|status'),
  'speakers',jsonb_build_object(
   'rule','Identifiers of kind speaker_label never carry a person, for any role (CHECK on tl.identifiers). Enrol a voice_profile identifier (attachable as usual) or annotate the speaker.',
   'inbox','GET /v1/transcripts/speakers (unrestricted only): unclaimed speaker labels per recording with turns, seconds, first and last time, sample turns and annotated; ignored labels never appear.',
   'resolve','POST /v1/transcripts/speakers/assign {thread_id, label, person_id} writes owner speaker annotations on the label''s turns in that recording; POST /v1/transcripts/speakers/ignore {thread_id, label, reason} writes owner ignore annotations and sets the identifier status ignored.',
   'late_enrolment','When the source later sends the turns with a voice_profile author, they resolve through the normal identifier path without a re-import.'),
  'grouping','A recording of a whole-thread channel is one episode and one conversation: episodes_of, recall spans, group_by episode and conversations never split it by time gaps.',
  'functions',jsonb_build_array(),
  'levels',jsonb_build_object('speaker_inbox','owner (unrestricted route only; not allow-listed for SQL)')));

-- 5. Purge: noise state of the purged channel's recordings (the annotations
--    go through the overlay hook; runs hold counts only).
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('transcript_noise_state',63,'DELETE FROM tl.transcript_noise_state s USING tl.threads t WHERE t.id=s.thread_id AND t.channel_id=$1');

-- 6. Owner annotation guard, cast made safe ---------------------------------------
-- 0042's tl.annotation_owner_guard cast throughline.token_id to bigint inside
-- an AND whose evaluation order PostgreSQL does not guarantee, so a job that
-- supersedes its own flags on a pooled connection whose setting is '' (it
-- served a token request earlier) could fail with 22P02. Same rules; the
-- cast is guarded by a CASE and only reached for owner annotations.
CREATE OR REPLACE FUNCTION tl.annotation_owner_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE tid bigint;
BEGIN
 IF OLD.method='owner' AND NEW.superseded_by IS NOT NULL AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by
  AND EXISTS(SELECT 1 FROM tl.annotations a WHERE a.id=NEW.superseded_by AND a.method<>'owner') THEN
  RAISE EXCEPTION 'a job never supersedes an owner annotation' USING ERRCODE='insufficient_privilege';
 END IF;
 IF OLD.method='owner' AND NEW.status IS DISTINCT FROM OLD.status THEN
  tid := CASE WHEN current_setting('throughline.token_id',true) ~ '^[0-9]{1,18}$'
   THEN current_setting('throughline.token_id',true)::bigint END;
  IF tid IS NOT NULL AND NOT EXISTS(SELECT 1 FROM tl.tokens k WHERE k.id=tid AND k.kind='owner') THEN
   RAISE EXCEPTION 'only the owner supersedes or retracts an owner annotation' USING ERRCODE='insufficient_privilege';
  END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.annotation_owner_guard() FROM PUBLIC;
