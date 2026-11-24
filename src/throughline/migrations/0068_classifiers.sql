-- Classifiers: an attribute registry, annotations on whole threads and on the
-- participants of a thread (copied down onto their messages), and two kinds of
-- classifier that write them (docs/v11/classifiers.md):
--   message  one Jev or LLM question per arriving message (no code);
--   python   an agent-written function over a whole thread, run in a sandbox.
--
-- 1. tl.annotation_attributes: every annotation attribute is registered. The
--    six core attributes keep their database value checks; a registered
--    attribute's value is checked against its value_schema by
--    throughline.classifiers.registry before any write. recall_effect
--    'exclude' with a boolean flag field keeps messages whose effective value
--    has flag=true out of default recall: core attributes do this in the
--    overlay (ignore, media_noise); a registered attribute does it through a
--    companion ignore annotation that the classifier engine writes and
--    retracts with it.
-- 2. tl.group_annotations: an annotation on a thread or on one participant of
--    a thread (participant_key, see tl.participant_key). value is what each
--    covered message gets (NULL: a note that covers nothing, e.g. a
--    conversation judged 'partial'); detail holds the group-level verdict.
--    Message annotations derived from one carry group_annotation_id.
-- 3. tl.classifiers, tl.classifier_runs, tl.classifier_threads.

CREATE TABLE tl.annotation_attributes(
 name text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_]{0,62}$'),
 description text NOT NULL DEFAULT '',
 value_schema jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(value_schema)='object'),
 recall_effect text NOT NULL DEFAULT 'none' CHECK (recall_effect IN ('none','exclude')),
 flag text,
 origin text NOT NULL DEFAULT 'owner' CHECK (origin IN ('core','owner','agent')),
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK (recall_effect='none' OR flag IS NOT NULL));

INSERT INTO tl.seed_hooks(name,ord,statement) VALUES
 ('annotation_attributes',5,$seed$INSERT INTO tl.annotation_attributes(name,description,value_schema,recall_effect,flag,origin) VALUES
 ('effective_time','When the record really happened, when its own timestamp is wrong',
  '{"at":{"type":"string","required":true,"description":"ISO time with UTC offset"},"ended_at":{"type":"string"},"precision":{"type":"enum","values":["exact","day","month","year","approximate"],"required":true}}','none',NULL,'core'),
 ('effective_place','Where the record really happened',
  '{"name":{"type":"string","required":true},"entity_id":{"type":"integer"}}','none',NULL,'core'),
 ('location','Coordinates of the record',
  '{"lat":{"type":"number","required":true,"min":-90,"max":90},"lon":{"type":"number","required":true,"min":-180,"max":180},"accuracy_m":{"type":"number","min":0}}','none',NULL,'core'),
 ('ignore','Keep the record out of default recall',
  '{"ignored":{"type":"boolean","required":true},"reason":{"type":"string"}}','exclude','ignored','core'),
 ('media_noise','The record is media that was playing (TV, film, music, video, audiobook), not people talking',
  '{"is_noise":{"type":"boolean","required":true},"score":{"type":"number","min":0,"max":1},"kind":{"type":"enum","values":["tv","movie","music","audiobook","podcast","short_form","youtube","game","other"]},"basis":{"type":"enum","values":["message","participant","thread"]},"evidence":{"type":"string"},"matched_record_id":{"type":"integer"},"classifier":{"type":"string"}}','exclude','is_noise','core'),
 ('speaker','The person who said this transcript turn',
  '{"person_id":{"type":"integer","required":true}}','none',NULL,'core')
 ON CONFLICT (name) DO NOTHING$seed$);
DO $$ BEGIN EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='annotation_attributes'); END $$;

-- Annotations accept any registered attribute (the 0042 column check was
-- unnamed) and a registered attribute's value passes the database check when
-- it is an object; its schema is checked by the writer.
DO $$ DECLARE c text; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid='tl.annotations'::regclass AND contype='c'
  AND pg_get_constraintdef(oid) LIKE '%effective_place%' AND conname<>'annotations_value' LOOP
  EXECUTE format('ALTER TABLE tl.annotations DROP CONSTRAINT %I',c);
 END LOOP;
END $$;
ALTER TABLE tl.annotations ADD CONSTRAINT annotations_attribute_registered
 FOREIGN KEY (attribute) REFERENCES tl.annotation_attributes(name);

CREATE OR REPLACE FUNCTION tl.annotation_value_ok(attribute text,value jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE s timestamptz; e timestamptz;
BEGIN
 IF value IS NULL OR jsonb_typeof(value)<>'object' THEN RETURN false; END IF;
 IF attribute='effective_time' THEN
  IF jsonb_typeof(value->'at') IS DISTINCT FROM 'string' OR value->>'at' !~ '([zZ]|[+-][0-9]{2}(:?[0-9]{2})?)$' THEN RETURN false; END IF;
  IF coalesce(value->>'precision','') NOT IN ('exact','day','month','year','approximate') THEN RETURN false; END IF;
  s := (value->>'at')::timestamptz;
  IF value ? 'ended_at' AND jsonb_typeof(value->'ended_at')<>'null' THEN
   IF jsonb_typeof(value->'ended_at')<>'string' OR value->>'ended_at' !~ '([zZ]|[+-][0-9]{2}(:?[0-9]{2})?)$' THEN RETURN false; END IF;
   e := (value->>'ended_at')::timestamptz;
   IF e<s THEN RETURN false; END IF;
  END IF;
  RETURN true;
 ELSIF attribute='effective_place' THEN
  IF jsonb_typeof(value->'name') IS DISTINCT FROM 'string' OR btrim(value->>'name')='' THEN RETURN false; END IF;
  RETURN NOT value ? 'entity_id' OR jsonb_typeof(value->'entity_id') IN ('number','null');
 ELSIF attribute='location' THEN
  IF jsonb_typeof(value->'lat') IS DISTINCT FROM 'number' OR jsonb_typeof(value->'lon') IS DISTINCT FROM 'number' THEN RETURN false; END IF;
  IF (value->>'lat')::float8 NOT BETWEEN -90 AND 90 OR (value->>'lon')::float8 NOT BETWEEN -180 AND 180 THEN RETURN false; END IF;
  IF value ? 'accuracy_m' AND jsonb_typeof(value->'accuracy_m')<>'null' THEN
   IF jsonb_typeof(value->'accuracy_m')<>'number' THEN RETURN false; END IF;
   RETURN (value->>'accuracy_m')::float8>=0;
  END IF;
  RETURN true;
 ELSIF attribute='ignore' THEN
  RETURN jsonb_typeof(value->'ignored')='boolean' AND (NOT value ? 'reason' OR jsonb_typeof(value->'reason') IN ('string','null'));
 ELSIF attribute='media_noise' THEN
  IF jsonb_typeof(value->'is_noise') IS DISTINCT FROM 'boolean' THEN RETURN false; END IF;
  IF value ? 'score' AND jsonb_typeof(value->'score')<>'null' THEN
   IF jsonb_typeof(value->'score')<>'number' THEN RETURN false; END IF;
   RETURN (value->>'score')::float8 BETWEEN 0 AND 1;
  END IF;
  RETURN true;
 ELSIF attribute='speaker' THEN
  RETURN jsonb_typeof(value->'person_id')='number' AND (value->'person_id')::text ~ '^[0-9]{1,18}$';
 END IF;
 -- A registered attribute: the writer checked its value_schema.
 RETURN true;
EXCEPTION WHEN others THEN RETURN false;
END $$;

-- The participant of a message within its thread: the person when known, else
-- the author identifier (a transcript speaker label is one per recording),
-- else the author handle.
CREATE FUNCTION tl.participant_key(person_id bigint,author_identifier_id bigint,author_handle text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
 SELECT CASE WHEN person_id IS NOT NULL THEN 'p:'||person_id
  WHEN author_identifier_id IS NOT NULL THEN 'i:'||author_identifier_id
  ELSE 'h:'||coalesce(author_handle,'') END $$;

CREATE TABLE tl.classifiers(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 name text UNIQUE NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,62}$'),
 kind text NOT NULL CHECK (kind IN ('message','python')),
 description text NOT NULL DEFAULT '',
 attribute text NOT NULL REFERENCES tl.annotation_attributes(name),
 channels text[] NOT NULL DEFAULT '{}',
 spec jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(spec)='object'),
 code text,
 code_hash text,
 version int NOT NULL DEFAULT 1,
 status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','active','paused','rejected')),
 monthly_cap_usd numeric CHECK (monthly_cap_usd IS NULL OR monthly_cap_usd>=0),
 cursor_message_id bigint NOT NULL DEFAULT 0,
 backfill_since timestamptz,
 backfill_cursor bigint,
 backfill_until_id bigint,
 created_by bigint REFERENCES tl.tokens ON DELETE SET NULL,
 approved_by bigint REFERENCES tl.tokens ON DELETE SET NULL,
 approved_at timestamptz,
 last_run_at timestamptz,
 last_error text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK ((kind='python')=(code IS NOT NULL)));

CREATE TABLE tl.classifier_runs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 classifier_id bigint NOT NULL REFERENCES tl.classifiers ON DELETE CASCADE,
 trigger text NOT NULL DEFAULT 'tick',
 status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','partial','failed')),
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 finished_at timestamptz,
 items int NOT NULL DEFAULT 0,
 marks int NOT NULL DEFAULT 0,
 calls int NOT NULL DEFAULT 0,
 tokens bigint NOT NULL DEFAULT 0,
 cost numeric NOT NULL DEFAULT 0,
 errors jsonb NOT NULL DEFAULT '[]',
 summary jsonb NOT NULL DEFAULT '{}');
CREATE INDEX classifier_runs_by_classifier ON tl.classifier_runs(classifier_id,started_at DESC);

CREATE TABLE tl.group_annotations(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 target_kind text NOT NULL CHECK (target_kind IN ('thread','participant')),
 thread_id bigint NOT NULL REFERENCES tl.threads ON DELETE CASCADE,
 participant_key text,
 attribute text NOT NULL REFERENCES tl.annotation_attributes(name),
 value jsonb CHECK (value IS NULL OR jsonb_typeof(value)='object'),
 detail jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail)='object'),
 confidence real CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
 reason text CHECK (reason IS NULL OR length(reason)<=2000),
 method text NOT NULL CHECK (method IN ('owner','rule','model')),
 origin text NOT NULL CHECK (origin IN ('owner','job')),
 job text,
 job_version text,
 classifier_id bigint REFERENCES tl.classifiers ON DELETE SET NULL,
 token_id bigint REFERENCES tl.tokens ON DELETE SET NULL,
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','retracted')),
 superseded_by bigint REFERENCES tl.group_annotations ON DELETE SET NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK ((target_kind='participant')=(participant_key IS NOT NULL)),
 CHECK ((method='owner')=(origin='owner')),
 CHECK (method='owner' OR (job IS NOT NULL AND job_version IS NOT NULL)));
CREATE INDEX group_annotations_active ON tl.group_annotations(thread_id,attribute) WHERE status='active';
CREATE INDEX group_annotations_classifier ON tl.group_annotations(classifier_id) WHERE status='active';

ALTER TABLE tl.annotations ADD COLUMN group_annotation_id bigint;
CREATE INDEX annotations_group ON tl.annotations(group_annotation_id) WHERE group_annotation_id IS NOT NULL;

CREATE TABLE tl.classifier_threads(
 classifier_id bigint NOT NULL REFERENCES tl.classifiers ON DELETE CASCADE,
 thread_id bigint NOT NULL REFERENCES tl.threads ON DELETE CASCADE,
 judged_max_message_id bigint NOT NULL,
 judged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 version int NOT NULL,
 result jsonb NOT NULL DEFAULT '{}',
 PRIMARY KEY (classifier_id,thread_id));

REVOKE ALL ON tl.annotation_attributes,tl.classifiers,tl.classifier_runs,tl.group_annotations,tl.classifier_threads FROM PUBLIC,throughline_reader;
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.annotation_attributes,tl.classifiers,tl.classifier_runs,tl.group_annotations,tl.classifier_threads TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.classifiers_id_seq,tl.classifier_runs_id_seq,tl.group_annotations_id_seq TO throughline_app;
GRANT EXECUTE ON FUNCTION tl.participant_key(bigint,bigint,text) TO throughline_app;
