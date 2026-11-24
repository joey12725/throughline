-- Claims, the closed link-type registry and the annotation overlay
-- (BRN-7, BRN-9..14, BRN-17, QRY-11, ACL-9, ARC-6, IDN-5, IDN-6, LIB-6, LNK-1,
-- LNK-2).
--
-- Ground truth (message content, payloads, the track) is never changed here.
-- Claims and annotations are derived records stored beside it; links keep
-- their history through the 0036 trigger. Nothing here calls a model.
--
-- Forward-only. Every api function becomes SECURITY DEFINER with a fixed
-- search_path through tl.harden_reader(), so tl objects are schema-qualified.

-- 0. Seed hooks ---------------------------------------------------------------
-- A seeding migration stores its idempotent seed statement here and runs it
-- once; tl.reseed() runs every hook again (in ord order), so a test database
-- whose tables were truncated gets its registries back. Hooks are arbitrary
-- statements: only the owner writes or runs them.
CREATE TABLE tl.seed_hooks(name text PRIMARY KEY,ord int NOT NULL,statement text NOT NULL);
REVOKE ALL ON tl.seed_hooks FROM PUBLIC,throughline_app,throughline_reader;
CREATE FUNCTION tl.reseed() RETURNS int LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$
DECLARE h record; n int := 0;
BEGIN
 FOR h IN SELECT s.name,s.statement FROM tl.seed_hooks s ORDER BY s.ord,s.name LOOP
  EXECUTE h.statement;
  n := n+1;
 END LOOP;
 RETURN n;
END $$;
REVOKE EXECUTE ON FUNCTION tl.reseed() FROM PUBLIC,throughline_app,throughline_reader;

-- 1. Status history of claims and annotations ---------------------------------
-- Their rows are never edited: only status and superseded_by change, and each
-- change leaves the image before it here (written by trigger only). As-of
-- views (api.claims_asof, the known_by cutoff of the overlay) replay it.
CREATE TABLE tl.status_history(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 kind text NOT NULL CHECK (kind IN ('claim','annotation')),
 ref_id bigint NOT NULL,
 old_status text NOT NULL,
 old_superseded_by bigint,
 new_status text NOT NULL,
 note text,
 token_id bigint,
 changed_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE INDEX status_history_ref ON tl.status_history(kind,ref_id,changed_at,id);
REVOKE ALL ON tl.status_history FROM PUBLIC,throughline_app,throughline_reader;
GRANT SELECT ON tl.status_history TO throughline_app;

-- UPDATE may change only status and superseded_by; DELETE is refused unless
-- an owner connection runs a purge (throughline.purge on).
CREATE FUNCTION tl.derived_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE note text;
BEGIN
 IF current_setting('throughline.purge',true)='on' AND session_user NOT IN ('throughline_app','throughline_reader') THEN
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'tl.% rows are never deleted; retract or supersede them',TG_TABLE_NAME USING ERRCODE='insufficient_privilege';
 END IF;
 IF (to_jsonb(NEW)-ARRAY['status','superseded_by']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','superseded_by']) THEN
  RAISE EXCEPTION 'tl.% content is never edited; supersede it',TG_TABLE_NAME USING ERRCODE='check_violation';
 END IF;
 IF NEW.status IS DISTINCT FROM OLD.status OR NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
  note := nullif(current_setting('throughline.status_note',true),'');
  INSERT INTO tl.status_history(kind,ref_id,old_status,old_superseded_by,new_status,note,token_id)
  VALUES (CASE TG_TABLE_NAME WHEN 'claims' THEN 'claim' ELSE 'annotation' END,OLD.id,OLD.status,OLD.superseded_by,NEW.status,left(note,500),
   CASE WHEN current_setting('throughline.token_id',true) ~ '^[0-9]{1,18}$' THEN current_setting('throughline.token_id',true)::bigint END);
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.derived_guard() FROM PUBLIC;

-- 2. Closed link-type registry ---------------------------------------
CREATE TABLE tl.link_types(
 type text PRIMARY KEY CHECK (type ~ '^[a-z][a-z0-9_]{0,62}$'),
 description text,
 inverse text CHECK (inverse IS NULL OR inverse ~ '^[a-z][a-z0-9_]{0,62}$'),
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','candidate','retired')),
 origin text NOT NULL CHECK (origin IN ('core','plugin','owner','legacy','source_candidate')),
 plugin text,
 first_seen timestamptz,
 last_written_while_retired timestamptz,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE tl.link_type_synonyms(
 alias text PRIMARY KEY CHECK (alias ~ '^[a-z][a-z0-9_]{0,62}$'),
 type text NOT NULL REFERENCES tl.link_types ON UPDATE CASCADE,
 origin text NOT NULL CHECK (origin IN ('core','owner')),
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK (alias<>type));
CREATE INDEX link_type_synonyms_type ON tl.link_type_synonyms(type);
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.link_types,tl.link_type_synonyms TO throughline_app;
ALTER TABLE tl.links ADD COLUMN type_as_written text;
CREATE INDEX links_type ON tl.links(type);

-- Total: always a valid registry name. Lower case, every run of other
-- characters becomes '_', edges stripped, at most 63 characters; a name that
-- does not start with a letter gets 't_'; nothing left (punctuation-only or
-- non-ASCII-only input) becomes 't_' and 12 hex digits of sha256 of the raw.
CREATE FUNCTION tl.normalize_link_type(raw text) RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
DECLARE t text;
BEGIN
 t := left(btrim(regexp_replace(lower(btrim(raw)),'[^a-z0-9]+','_','g'),'_'),63);
 IF t='' THEN RETURN 't_'||left(encode(sha256(convert_to(raw,'UTF8')),'hex'),12); END IF;
 IF t !~ '^[a-z]' THEN t := left('t_'||t,63); END IF;
 RETURN t;
END $$;
-- The canonical type of a raw name (synonym target, else a registered type of
-- any status), or NULL when the registry does not know it.
CREATE FUNCTION tl.resolve_link_type(raw text) RETURNS text LANGUAGE sql STABLE SET search_path=pg_catalog,tl AS $$
 SELECT coalesce(
  (SELECT s.type FROM tl.link_type_synonyms s WHERE s.alias=tl.normalize_link_type(raw)),
  (SELECT t.type FROM tl.link_types t WHERE t.type=tl.normalize_link_type(raw))) $$;
GRANT EXECUTE ON FUNCTION tl.normalize_link_type(text),tl.resolve_link_type(text) TO throughline_app;

-- Seed (a seed hook): every type the core, the shipped plugins and the tests
-- use, and core synonyms for obvious variants.
INSERT INTO tl.seed_hooks(name,ord,statement) VALUES
 ('link_types',10,$seed$INSERT INTO tl.link_types(type,description,inverse,status,origin) VALUES
 ('about','The from end is about the to end (a topic, place, person or work)','subject_of','active','core'),
 ('added_to','Added to a collection or playlist',NULL,'active','core'),
 ('at_place','Happened at a place entity',NULL,'active','core'),
 ('author','Written or made by','authored','active','core'),
 ('cites','Cites a source record','cited_by','active','core'),
 ('consumed','Watched, read, played or listened to',NULL,'active','core'),
 ('created_by','Created by a person or account',NULL,'active','core'),
 ('derived_from','Computed from another record','source_of','active','core'),
 ('episode_of','An episode of a show or series','has_episode','active','core'),
 ('found_in','Found in a source or collection',NULL,'active','core'),
 ('liked','Liked or favourited',NULL,'active','core'),
 ('mentions','Mentions another record or entity','mentioned_in','active','core'),
 ('next','Followed by',NULL,'active','core'),
 ('part_of','Part of a larger whole','has_part','active','core'),
 ('participant','Took part in',NULL,'active','core'),
 ('pick_of','Picked from a feed or selection',NULL,'active','core'),
 ('played_from','Played from a playlist, album or context',NULL,'active','core'),
 ('posted_in','Posted in a community or channel',NULL,'active','core'),
 ('present','Was present at',NULL,'active','core'),
 ('published_by','Published by a channel, author or publisher','published','active','core'),
 ('recipient','Sent to',NULL,'active','core'),
 ('related_to','Related in an unspecified way','related_to','active','core'),
 ('reply_to','A reply to another message','replied_by','active','core'),
 ('represents','Represents an entity',NULL,'active','core'),
 ('same_as','The same thing recorded twice','same_as','active','core'),
 ('saved','Saved or bookmarked',NULL,'active','core'),
 ('subscribed_to','Subscribed to a channel or feed',NULL,'active','core'),
 ('tagged','Tagged with',NULL,'active','core'),
 ('transcript_of','A transcript of a recording or video','has_transcript','active','core'),
 ('upload_of','An upload of a work',NULL,'active','core'),
 ('voted','Voted on',NULL,'active','core'),
 ('synthetic','Synthetic link type used by the test suite',NULL,'active','core'),
 ('fake_about','Synthetic link type used by the test suite',NULL,'active','core'),
 ('fake_source','Synthetic link type used by the test suite',NULL,'active','core'),
 ('near','Synthetic link type used by the test suite',NULL,'active','core'),
 ('far','Synthetic link type used by the test suite',NULL,'active','core'),
 ('legacy','Synthetic link type used by the test suite',NULL,'active','core')
ON CONFLICT (type) DO NOTHING$seed$),
 ('link_type_synonyms',11,$seed$INSERT INTO tl.link_type_synonyms(alias,type,origin) VALUES
 ('replies_to','reply_to','core'),('replyto','reply_to','core'),('in_reply_to','reply_to','core'),('reply_of','reply_to','core'),
 ('partof','part_of','core'),('is_part_of','part_of','core'),('belongs_to','part_of','core'),
 ('mention','mentions','core'),('mentioned','mentions','core'),
 ('cite','cites','core'),('citation','cites','core'),
 ('authored_by','author','core'),('written_by','author','core'),
 ('sameas','same_as','core'),('duplicate_of','same_as','core'),
 ('transcription_of','transcript_of','core'),
 ('relates_to','related_to','core'),('related','related_to','core')
ON CONFLICT (alias) DO NOTHING$seed$);
SELECT tl.reseed();

-- Existing data stays valid (migration only, not a hook): raw names are
-- normalised and obvious variants folded (link_history keeps the images), and
-- every remaining type is registered active with origin legacy.
UPDATE tl.links SET type_as_written=type,type=tl.normalize_link_type(type) WHERE type IS DISTINCT FROM tl.normalize_link_type(type);
UPDATE tl.links l SET type_as_written=coalesce(l.type_as_written,l.type),type=s.type FROM tl.link_type_synonyms s WHERE s.alias=l.type;
INSERT INTO tl.link_types(type,status,origin,first_seen)
SELECT l.type,'active','legacy',min(l.created_at) FROM tl.links l GROUP BY l.type
ON CONFLICT (type) DO NOTHING;

-- The gate. Writer: throughline.token_id (tl.tokens) and the login role.
-- Decided by METHOD FIRST:
--  synonym -> folded; active -> kept;
--  1 rule/model on an unregistered type -> refused, whatever the role;
--  2 method owner or an owner token -> registered active (origin owner);
--  3 no token, not a runtime role (migrations, owner imports), method owner
--    or source -> registered active (origin owner);
--  4 plugin token, method source, declared in its manifest link_types ->
--    registered active (origin plugin);
--  5 plugin token, method source, undeclared -> accepted, candidate;
--  6 plugin token, method source, retired -> accepted, last_written_while_retired;
--  7 everyone else -> refused.
CREATE FUNCTION tl.link_type_gate() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE raw text := NEW.type; t text; canon text; st text; tid bigint; tkind text; tplugin text; m text := coalesce(NEW.method,'source');
BEGIN
 IF TG_OP='UPDATE' AND NEW.type IS NOT DISTINCT FROM OLD.type THEN RETURN NEW; END IF;
 t := tl.normalize_link_type(raw);
 IF t IS NULL THEN RETURN NEW; END IF;  -- NOT NULL refuses it
 IF t IS DISTINCT FROM raw THEN NEW.type_as_written := raw; END IF;
 SELECT s.type INTO canon FROM tl.link_type_synonyms s WHERE s.alias=t;
 IF canon IS NOT NULL THEN
  NEW.type := canon;
  NEW.type_as_written := raw;
  RETURN NEW;
 END IF;
 NEW.type := t;
 SELECT lt.status INTO st FROM tl.link_types lt WHERE lt.type=t;
 IF st='active' THEN RETURN NEW; END IF;
 IF current_setting('throughline.token_id',true) ~ '^[0-9]{1,18}$' THEN
  tid := current_setting('throughline.token_id',true)::bigint;
  SELECT k.kind,k.plugin INTO tkind,tplugin FROM tl.tokens k WHERE k.id=tid;
 END IF;
 IF m IN ('rule','model') THEN
  RAISE EXCEPTION 'unregistered link type: %; register it or propose it',t USING ERRCODE='check_violation',
   HINT='rule and model links may use registered link types only (GET /v1/link-types); propose a new type with POST /v1/proposals kind link_type';
 END IF;
 IF m='owner' OR tkind='owner' OR (tid IS NULL AND session_user NOT IN ('throughline_app','throughline_reader') AND m IN ('owner','source')) THEN
  IF st IS NULL THEN
   INSERT INTO tl.link_types(type,status,origin,first_seen) VALUES (t,'active','owner',now()) ON CONFLICT (type) DO NOTHING;
  ELSIF st='candidate' THEN
   UPDATE tl.link_types SET status='active',origin='owner' WHERE type=t;
  ELSE
   UPDATE tl.link_types SET last_written_while_retired=now() WHERE type=t;
  END IF;
  RETURN NEW;
 END IF;
 IF tkind='plugin' AND m='source' THEN
  IF st='retired' THEN
   UPDATE tl.link_types SET last_written_while_retired=now() WHERE type=t;
   RETURN NEW;
  END IF;
  IF EXISTS(SELECT 1 FROM tl.plugins p
   CROSS JOIN LATERAL jsonb_array_elements_text(CASE WHEN jsonb_typeof(p.manifest->'link_types')='array' THEN p.manifest->'link_types' ELSE '[]'::jsonb END) d(name)
   WHERE p.name=tplugin AND tl.normalize_link_type(d.name)=t) THEN
   INSERT INTO tl.link_types(type,status,origin,plugin,first_seen) VALUES (t,'active','plugin',tplugin,now())
   ON CONFLICT (type) DO UPDATE SET status='active',origin='plugin',plugin=EXCLUDED.plugin;
   RETURN NEW;
  END IF;
  INSERT INTO tl.link_types(type,status,origin,plugin,first_seen) VALUES (t,'candidate','source_candidate',tplugin,now()) ON CONFLICT (type) DO NOTHING;
  RETURN NEW;
 END IF;
 RAISE EXCEPTION 'unregistered link type: %; register it or propose it',t USING ERRCODE='check_violation',
  HINT=CASE WHEN st='retired' THEN 'this link type is retired; use its replacement (GET /v1/link-types)' ELSE 'register it (owner, POST /v1/link-types) or propose it (POST /v1/proposals kind link_type)' END;
END $$;
REVOKE EXECUTE ON FUNCTION tl.link_type_gate() FROM PUBLIC;
CREATE TRIGGER link_type_gate BEFORE INSERT OR UPDATE OF type ON tl.links FOR EACH ROW EXECUTE FUNCTION tl.link_type_gate();

-- api.links shows type_as_written (append-only re-install).
SELECT tl.install_gate('links',NULL,'SELECT * FROM tl.links');

-- The vocabulary. Restricted askers see active types once they hold any
-- grant; candidates and retired types are unrestricted-only. No per-type
-- counts here: counts over links would count hidden rows.
SELECT tl.install_gate('link_types',
 'v.status=''active'' AND current_setting(''throughline.scope_mode'',true)=''asker'' AND EXISTS(SELECT 1 FROM tl.asker_grants g WHERE g.asker=current_setting(''throughline.asker'',true))',
 $def$
SELECT t.type,t.description,t.inverse,t.status,t.origin,
 coalesce((SELECT array_agg(s.alias ORDER BY s.alias) FROM tl.link_type_synonyms s WHERE s.type=t.type),'{}'::text[]) AS synonyms
FROM tl.link_types t
$def$);

-- 3. Claims -------------------------------------------------------------------
-- Lower case, whitespace collapsed, leading and trailing punctuation stripped.
CREATE FUNCTION tl.claim_statement_norm(statement text) RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT regexp_replace(btrim(regexp_replace(lower(coalesce(statement,'')),'\s+',' ','g')),'^[[:punct:][:space:]]+|[[:punct:][:space:]]+$','','g') $$;
-- Natural key: subject, day of valid_from, type, attribution and the
-- normalised statement. Two writers saying the same thing about the same
-- subject meet on one key.
CREATE FUNCTION tl.claim_natural_key(subject_kind text,subject_id bigint,subject_label text,valid_from timestamptz,claim_type text,attributed_person_id bigint,attributed_handle text,statement text) RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,tl AS $$
 SELECT encode(sha256(convert_to(concat_ws('|',
  $1,
  coalesce($2::text,'label:'||lower(btrim($3)),''),
  coalesce(to_char($4 AT TIME ZONE 'UTC','YYYY-MM-DD'),'undated'),
  $5,
  coalesce('person:'||$6::text,'handle:'||lower(btrim($7)),'unattributed'),
  tl.claim_statement_norm($8)),'UTF8')),'hex') $$;
GRANT EXECUTE ON FUNCTION tl.claim_statement_norm(text),tl.claim_natural_key(text,bigint,text,timestamptz,text,bigint,text,text) TO throughline_app;

CREATE TABLE tl.claims(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 subject_kind text NOT NULL CHECK (subject_kind IN ('page','entity','person','message','concept')),
 subject_id bigint,
 subject_label text,
 page_channel text,
 page_external_id text,
 revision_message_id bigint REFERENCES tl.messages ON DELETE CASCADE,
 passage_id bigint,
 statement text NOT NULL CHECK (length(btrim(statement))>0 AND length(statement)<=20000),
 claim_type text NOT NULL CHECK (claim_type IN ('observation','inference','hypothesis','judgment')),
 confidence real CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
 uncertainty text,
 attributed_person_id bigint REFERENCES tl.people,
 attributed_handle text,
 object_claim_id bigint REFERENCES tl.claims ON DELETE SET NULL,
 valid_from timestamptz,
 valid_to timestamptz,
 valid_from_precision text CHECK (valid_from_precision IN ('exact','day','month','year','approximate','unknown')),
 valid_to_precision text CHECK (valid_to_precision IN ('exact','day','month','year','approximate','unknown')),
 method text NOT NULL CHECK (method IN ('source','rule','model','owner')),
 job text,
 job_version text,
 token_id bigint REFERENCES tl.tokens,
 asker text,
 accepted_by bigint REFERENCES tl.tokens,
 no_source_reason text,
 unsupported boolean NOT NULL DEFAULT false,
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','contradicted','retracted')),
 superseded_by bigint REFERENCES tl.claims ON DELETE SET NULL,
 natural_key text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK (subject_id IS NOT NULL OR coalesce(length(btrim(subject_label)),0)>0),
 CHECK (claim_type<>'judgment' OR attributed_person_id IS NOT NULL OR coalesce(length(btrim(attributed_handle)),0)>0),
 CHECK (unsupported=(no_source_reason IS NOT NULL)),
 CHECK (no_source_reason IS NULL OR length(btrim(no_source_reason))>0),
 CHECK (method NOT IN ('rule','model') OR (job IS NOT NULL AND job_version IS NOT NULL)),
 CONSTRAINT claims_validity_order CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to>=valid_from),
 CONSTRAINT claims_validity_precision CHECK ((valid_from IS NOT NULL OR valid_from_precision IS NULL OR valid_from_precision='unknown') AND (valid_to IS NOT NULL OR valid_to_precision IS NULL OR valid_to_precision='unknown')));
CREATE UNIQUE INDEX claims_natural_key_active ON tl.claims(natural_key) WHERE status='active';
CREATE INDEX claims_subject ON tl.claims(subject_kind,subject_id);
CREATE INDEX claims_revision ON tl.claims(revision_message_id) WHERE revision_message_id IS NOT NULL;
CREATE FUNCTION tl.claim_key() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$
BEGIN
 NEW.natural_key := tl.claim_natural_key(NEW.subject_kind,NEW.subject_id,NEW.subject_label,NEW.valid_from,NEW.claim_type,NEW.attributed_person_id,NEW.attributed_handle,NEW.statement);
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.claim_key() FROM PUBLIC;
CREATE TRIGGER claim_key BEFORE INSERT ON tl.claims FOR EACH ROW EXECUTE FUNCTION tl.claim_key();
CREATE TRIGGER derived_guard BEFORE UPDATE OR DELETE ON tl.claims FOR EACH ROW EXECUTE FUNCTION tl.derived_guard();

CREATE TABLE tl.claim_sources(
 claim_id bigint NOT NULL REFERENCES tl.claims ON DELETE CASCADE,
 message_id bigint NOT NULL REFERENCES tl.messages ON DELETE CASCADE,
 version int NOT NULL,
 span jsonb CHECK (span IS NULL OR jsonb_typeof(span)='object'),
 relation text NOT NULL DEFAULT 'supports' CHECK (relation IN ('supports','contradicts','context')),
 added_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(claim_id,message_id));
CREATE INDEX claim_sources_message ON tl.claim_sources(message_id);
CREATE TABLE tl.claim_relations(
 from_claim bigint NOT NULL REFERENCES tl.claims ON DELETE CASCADE,
 to_claim bigint NOT NULL REFERENCES tl.claims ON DELETE CASCADE,
 relation text NOT NULL CHECK (relation IN ('supersedes','contradicts','split_from','merged_from','refines','promoted_from','endorsed_by_owner')),
 note text CHECK (note IS NULL OR length(note)<=2000),
 token_id bigint REFERENCES tl.tokens,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(from_claim,to_claim,relation),
 CHECK (from_claim<>to_claim));
CREATE INDEX claim_relations_to ON tl.claim_relations(to_claim);

-- Message ids a proposal names (payload and evidence): keys message_id,
-- revision_message_id, source_message_id, target_message_id; arrays
-- message_ids, sources, source_ids, source_message_ids (numbers or objects);
-- link ends {kind: message, id}. Purge and the reference check use it.
CREATE FUNCTION tl.json_message_ids(j jsonb) RETURNS bigint[] LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,tl AS $$
DECLARE found bigint[] := '{}'; k text; v jsonb; e jsonb;
BEGIN
 IF j IS NULL THEN RETURN found; END IF;
 IF jsonb_typeof(j)='array' THEN
  FOR v IN SELECT x FROM jsonb_array_elements(j) x LOOP found := found||tl.json_message_ids(v); END LOOP;
  RETURN found;
 END IF;
 IF jsonb_typeof(j)<>'object' THEN RETURN found; END IF;
 IF j->>'kind'='message' AND jsonb_typeof(j->'id')='number' AND (j->'id')::text ~ '^[0-9]{1,18}$' THEN
  found := found||((j->'id')::text)::bigint;
 END IF;
 FOR k,v IN SELECT x.key,x.value FROM jsonb_each(j) x LOOP
  IF k IN ('message_id','revision_message_id','source_message_id','target_message_id') AND jsonb_typeof(v)='number' AND v::text ~ '^[0-9]{1,18}$' THEN
   found := found||(v::text)::bigint;
  ELSIF k IN ('message_ids','sources','source_ids','source_message_ids') AND jsonb_typeof(v)='array' THEN
   FOR e IN SELECT x FROM jsonb_array_elements(v) x LOOP
    IF jsonb_typeof(e)='number' AND e::text ~ '^[0-9]{1,18}$' THEN found := found||(e::text)::bigint;
    ELSIF jsonb_typeof(e)='object' AND jsonb_typeof(e->'id')='number' AND NOT e ? 'kind' AND (e->'id')::text ~ '^[0-9]{1,18}$' THEN found := found||((e->'id')::text)::bigint;
    ELSE found := found||tl.json_message_ids(e);
    END IF;
   END LOOP;
  ELSIF jsonb_typeof(v) IN ('object','array') THEN
   found := found||tl.json_message_ids(v);
  END IF;
 END LOOP;
 RETURN found;
END $$;
GRANT EXECUTE ON FUNCTION tl.json_message_ids(jsonb) TO throughline_app;

CREATE TABLE tl.proposals(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 kind text NOT NULL CHECK (kind IN ('claim','link','concept','correction','recipe','provenance','link_type','annotation')),
 payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object' AND octet_length(payload::text)<=262144),
 evidence jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence) IN ('object','array') AND octet_length(evidence::text)<=262144),
 origin text NOT NULL CHECK (origin IN ('agent','librarian','curator')),
 token_id bigint REFERENCES tl.tokens,
 asker text,
 investigation_id uuid,
 message_ids bigint[] NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
 decided_by bigint REFERENCES tl.tokens,
 decided_at timestamptz,
 result jsonb,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE INDEX proposals_token ON tl.proposals(token_id,asker,created_at);
CREATE INDEX proposals_messages ON tl.proposals USING gin(message_ids);
CREATE FUNCTION tl.proposal_message_ids() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$
BEGIN
 NEW.message_ids := ARRAY(SELECT DISTINCT x FROM unnest(tl.json_message_ids(NEW.payload)||tl.json_message_ids(NEW.evidence)) x ORDER BY x);
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.proposal_message_ids() FROM PUBLIC;
CREATE TRIGGER proposal_message_ids BEFORE INSERT OR UPDATE OF payload,evidence ON tl.proposals FOR EACH ROW EXECUTE FUNCTION tl.proposal_message_ids();

REVOKE ALL ON tl.claims,tl.claim_sources,tl.claim_relations,tl.proposals FROM PUBLIC,throughline_app,throughline_reader;
GRANT SELECT,INSERT,UPDATE ON tl.claims,tl.proposals TO throughline_app;
GRANT SELECT,INSERT ON tl.claim_sources,tl.claim_relations TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.claims_id_seq,tl.proposals_id_seq,tl.status_history_id_seq TO throughline_app;

-- 4. Annotation overlay ---------------------------------------------
CREATE FUNCTION tl.annotation_value_ok(attribute text,value jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
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
 RETURN false;
EXCEPTION WHEN others THEN RETURN false;
END $$;

CREATE TABLE tl.annotations(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 target_channel_id bigint NOT NULL REFERENCES tl.channels,
 target_external_id text NOT NULL,
 target_version int,
 attribute text NOT NULL CHECK (attribute IN ('effective_time','effective_place','location','ignore','media_noise','speaker')),
 value jsonb NOT NULL,
 valid_from timestamptz,
 valid_to timestamptz,
 valid_from_precision text CHECK (valid_from_precision IN ('exact','day','month','year','approximate','unknown')),
 valid_to_precision text CHECK (valid_to_precision IN ('exact','day','month','year','approximate','unknown')),
 method text NOT NULL CHECK (method IN ('owner','rule','model')),
 origin text NOT NULL CHECK (origin IN ('owner','job')),
 job text,
 job_version text,
 token_id bigint REFERENCES tl.tokens,
 confidence real CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
 reason text CHECK (reason IS NULL OR length(reason)<=2000),
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','retracted')),
 superseded_by bigint REFERENCES tl.annotations ON DELETE SET NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CONSTRAINT annotations_value CHECK (tl.annotation_value_ok(attribute,value)),
 CHECK ((method='owner')=(origin='owner')),
 CHECK (method='owner' OR (job IS NOT NULL AND job_version IS NOT NULL)),
 CONSTRAINT annotations_validity_order CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to>=valid_from),
 CONSTRAINT annotations_validity_precision CHECK ((valid_from IS NOT NULL OR valid_from_precision IS NULL OR valid_from_precision='unknown') AND (valid_to IS NOT NULL OR valid_to_precision IS NULL OR valid_to_precision='unknown')));
CREATE INDEX annotations_active ON tl.annotations(target_channel_id,target_external_id) WHERE status='active';
CREATE INDEX annotations_target ON tl.annotations(target_channel_id,target_external_id,created_at);
CREATE TRIGGER derived_guard BEFORE UPDATE OR DELETE ON tl.annotations FOR EACH ROW EXECUTE FUNCTION tl.derived_guard();
-- A job never supersedes an owner annotation, and a non-owner token never
-- changes the status of one (supersede or retract).
CREATE FUNCTION tl.annotation_owner_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF OLD.method='owner' AND NEW.superseded_by IS NOT NULL AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by
  AND EXISTS(SELECT 1 FROM tl.annotations a WHERE a.id=NEW.superseded_by AND a.method<>'owner') THEN
  RAISE EXCEPTION 'a job never supersedes an owner annotation' USING ERRCODE='insufficient_privilege';
 END IF;
 IF OLD.method='owner' AND NEW.status IS DISTINCT FROM OLD.status
  AND current_setting('throughline.token_id',true) ~ '^[0-9]{1,18}$'
  AND NOT EXISTS(SELECT 1 FROM tl.tokens k WHERE k.id=current_setting('throughline.token_id',true)::bigint AND k.kind='owner') THEN
  RAISE EXCEPTION 'only the owner supersedes or retracts an owner annotation' USING ERRCODE='insufficient_privilege';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.annotation_owner_guard() FROM PUBLIC;
CREATE TRIGGER owner_guard BEFORE UPDATE OF status,superseded_by ON tl.annotations FOR EACH ROW EXECUTE FUNCTION tl.annotation_owner_guard();
REVOKE ALL ON tl.annotations FROM PUBLIC,throughline_app,throughline_reader;
GRANT SELECT,INSERT,UPDATE ON tl.annotations TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.annotations_id_seq TO throughline_app;

-- Provenance accepts the annotation kind (the 0036 check was unnamed).
DO $$ DECLARE c text; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid='tl.provenance'::regclass AND contype='c' AND pg_get_constraintdef(oid) LIKE '%derived_kind%' LOOP
  EXECUTE format('ALTER TABLE tl.provenance DROP CONSTRAINT %I',c);
 END LOOP;
END $$;
ALTER TABLE tl.provenance ADD CONSTRAINT provenance_derived_kind CHECK (derived_kind IN ('message','passage','claim','link','entity','annotation'));

-- Effective overlay of one record from a list of annotation rows (jsonb
-- objects with attribute, value, method, created_at, id, reason) and the
-- record's own fields.ignore_reason. Per attribute: owner > source field >
-- rule > model, the latest within a method. ignored: an owner ignore decides
-- alone; otherwise an ignore (source, rule, model) or a media_noise (owner,
-- rule, model) asserting true.
-- No SET clause, so the planner can inline it into its callers (it names
-- only pg_catalog objects).
CREATE FUNCTION tl.overlay_fold(anns jsonb,source_ignore text) RETURNS jsonb LANGUAGE sql STABLE AS $$
 WITH a AS (
  SELECT e->>'attribute' AS attr,e->'value' AS val,
   CASE e->>'method' WHEN 'owner' THEN 0 WHEN 'rule' THEN 2 ELSE 3 END AS r,
   (e->>'created_at')::timestamptz AS ts,(e->>'id')::bigint AS id,e->>'reason' AS reason
  FROM jsonb_array_elements(CASE WHEN jsonb_typeof(anns)='array' THEN anns ELSE '[]'::jsonb END) e
  UNION ALL
  SELECT 'ignore',jsonb_build_object('ignored',true,'reason',source_ignore),1,NULL::timestamptz,NULL::bigint,source_ignore
  WHERE nullif(btrim(source_ignore),'') IS NOT NULL),
 top AS (SELECT DISTINCT ON (a.attr) a.* FROM a ORDER BY a.attr,a.r,a.ts DESC NULLS LAST,a.id DESC NULLS LAST),
 v AS (SELECT
  (SELECT t.r FROM top t WHERE t.attr='ignore') AS ig_r,
  (SELECT (t.val->>'ignored')::boolean FROM top t WHERE t.attr='ignore') AS ig,
  (SELECT coalesce(nullif(t.val->>'reason',''),t.reason,'ignored') FROM top t WHERE t.attr='ignore') AS ig_reason,
  (SELECT (t.val->>'is_noise')::boolean FROM top t WHERE t.attr='media_noise') AS noise,
  (SELECT t.val FROM top t WHERE t.attr='effective_time') AS et,
  (SELECT t.val FROM top t WHERE t.attr='effective_place') AS ep,
  (SELECT t.val FROM top t WHERE t.attr='location') AS loc,
  (SELECT t.val FROM top t WHERE t.attr='speaker') AS sp),
 d AS (SELECT v.*,
  CASE WHEN v.ig_r=0 THEN coalesce(v.ig,false) ELSE coalesce(v.ig,false) OR coalesce(v.noise,false) END AS ignored FROM v)
 SELECT jsonb_build_object(
  'ignored',d.ignored,
  'ignore_reasons',CASE WHEN NOT d.ignored THEN '[]'::jsonb ELSE to_jsonb(array_remove(ARRAY[
    CASE WHEN coalesce(d.ig,false) THEN d.ig_reason END,
    CASE WHEN coalesce(d.ig_r,1)<>0 AND coalesce(d.noise,false) THEN 'media_noise' END],NULL)) END,
  'media_noise',coalesce(d.noise,false),
  'effective_time',d.et->'at',
  'effective_end',CASE WHEN jsonb_typeof(d.et->'ended_at')='string' THEN d.et->'ended_at' END,
  'precision',d.et->'precision',
  'effective_place',d.ep->'name',
  'lat',d.loc->'lat',
  'lon',d.loc->'lon',
  'speaker_person_id',d.sp->'person_id',
  'annotation_ids',(SELECT coalesce(jsonb_agg(a.id ORDER BY a.id),'[]'::jsonb) FROM a WHERE a.id IS NOT NULL))
 FROM d $$;
REVOKE EXECUTE ON FUNCTION tl.overlay_fold(jsonb,text) FROM PUBLIC;

-- Effective state of one record version for recall_scope. Reads tl.annotations
-- through its index (the base view of recall_scope already decided
-- visibility). known_by: only annotations created by then, in the state they
-- had then. The version limits annotations to target_version NULL or equal.
CREATE FUNCTION tl.overlay_state(channel_id bigint,external_id text,version int,fields jsonb,known_by timestamptz)
RETURNS TABLE(ignored boolean,effective_time timestamptz,effective_end timestamptz,"precision" text) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE f jsonb;
BEGIN
 -- plpgsql: the statement plan is cached across the per-row calls.
 f := tl.overlay_fold((
  SELECT jsonb_agg(jsonb_build_object('attribute',a.attribute,'value',a.value,'method',a.method,'created_at',a.created_at,'id',a.id,'reason',a.reason))
  FROM tl.annotations a
  WHERE a.target_channel_id=overlay_state.channel_id AND a.target_external_id=overlay_state.external_id
   AND (a.target_version IS NULL OR a.target_version=overlay_state.version)
   AND a.attribute IN ('ignore','media_noise','effective_time')
   AND CASE WHEN overlay_state.known_by IS NULL THEN a.status='active'
    ELSE a.created_at<=overlay_state.known_by AND coalesce((SELECT h.old_status FROM tl.status_history h WHERE h.kind='annotation' AND h.ref_id=a.id AND h.changed_at>overlay_state.known_by ORDER BY h.changed_at,h.id LIMIT 1),a.status)='active' END),
  CASE WHEN jsonb_typeof(overlay_state.fields->'ignore_reason')='string' THEN overlay_state.fields->>'ignore_reason' END);
 ignored := (f->>'ignored')::boolean;
 effective_time := (f->>'effective_time')::timestamptz;
 effective_end := (f->>'effective_end')::timestamptz;
 "precision" := f->>'precision';
 RETURN NEXT;
END $$;
REVOKE EXECUTE ON FUNCTION tl.overlay_state(bigint,text,int,jsonb,timestamptz) FROM PUBLIC,throughline_app,throughline_reader;

-- 5. Gated views ----------------------------------------------------------------
-- A claim is visible to a restricted asker only when every source and its
-- revision are visible; a claim without sources (unsupported) never is.
-- The asker that proposed a claim is shown to unrestricted askers only (one
-- asker never learns another's name through a shared source).
SELECT tl.install_gate('claims_history',
 'api.sources_visible(v.source_message_ids) AND (v.revision_message_id IS NULL OR api.message_visible(v.revision_message_id))',
 $def$
SELECT c.id,c.subject_kind,c.subject_id,c.subject_label,c.page_channel,c.page_external_id,
 c.revision_message_id,c.passage_id,c.statement,c.claim_type,c.confidence,c.uncertainty,
 c.attributed_person_id,c.attributed_handle,c.object_claim_id,
 c.valid_from,c.valid_to,c.valid_from_precision,c.valid_to_precision,
 c.method,c.job,c.job_version,c.token_id,
 CASE WHEN (SELECT api.asker_unrestricted()) THEN c.asker END AS asker,
 c.accepted_by,c.no_source_reason,c.unsupported,c.status,c.superseded_by,c.natural_key,c.created_at,
 coalesce(s.ids,'{}'::bigint[]) AS source_message_ids,
 coalesce(s.refs,'[]'::jsonb) AS source_refs
FROM tl.claims c
LEFT JOIN LATERAL (
 SELECT array_agg(x.message_id ORDER BY x.message_id) AS ids,
  jsonb_agg(jsonb_build_object('message_id',x.message_id,'version',x.version,'relation',x.relation,'span',x.span,'added_at',x.added_at) ORDER BY x.message_id) AS refs
 FROM tl.claim_sources x WHERE x.claim_id=c.id) s ON true
$def$);
SELECT tl.install_gate('claims',
 'api.sources_visible(v.source_message_ids) AND (v.revision_message_id IS NULL OR api.message_visible(v.revision_message_id))',
 $def$SELECT * FROM tl.claims_history_all WHERE status='active'$def$);

-- Record contract view of claims (prefix c). Columns, order and types are
-- those of api.record_versions.
SELECT tl.install_gate('claim_records','EXISTS(SELECT 1 FROM tl.claims_history_gated g WHERE g.id=v.ref_id)',$def$
SELECT 'c:'||c.id AS record_id,
 'claim'::text AS kind,
 c.id AS ref_id,
 c.revision_message_id AS message_id,
 r.version,
 c.status='active' AS is_current,
 coalesce(rc.name,c.page_channel) AS channel,
 coalesce(r.external_id,c.page_external_id) AS external_id,
 r.thread_id,
 r.thread_no,
 c.statement AS content,
 c.created_at AS record_time,
 NULL::timestamptz AS ended_at,
 c.created_at AS ingested_at,
 c.valid_from AS subject_from,
 c.valid_to AS subject_to,
 c.valid_from_precision AS subject_from_precision,
 c.valid_to_precision AS subject_to_precision,
 c.attributed_person_id AS person_id,
 c.attributed_handle AS author_handle,
 NULL::double precision AS lat,
 NULL::double precision AS lon,
 'not_applicable'::text AS loc_reason,
 false AS is_outside,
 NULL::text[] AS heading_path,
 NULL::int AS char_start,
 NULL::int AS char_end,
 jsonb_build_object(
  'type',c.claim_type,
  'attribution',CASE WHEN c.attributed_person_id IS NULL AND c.attributed_handle IS NULL THEN NULL ELSE jsonb_build_object('person_id',c.attributed_person_id,'handle',c.attributed_handle) END,
  'confidence',c.confidence,
  'uncertainty',c.uncertainty,
  'status',c.status,
  'method',c.method,
  'corroborating_sources',jsonb_array_length(s.support),
  'subject',jsonb_build_object('kind',c.subject_kind,'id',c.subject_id,'label',c.subject_label),
  'object_claim_id',c.object_claim_id,
  'no_source_reason',c.no_source_reason) AS claim,
 true AS generated,
 c.unsupported OR s.support='[]'::jsonb AS unsupported,
 jsonb_build_object('method',c.method,'job',c.job,'job_version',c.job_version,'accepted_by',c.accepted_by,'natural_key',c.natural_key) AS provenance,
 s.support,
 jsonb_build_object('claim',c.id,'sources',true,'subject',jsonb_build_object('kind',c.subject_kind,'id',c.subject_id),'superseded_by',c.superseded_by) AS expansion
FROM tl.claims c
LEFT JOIN tl.messages r ON r.id=c.revision_message_id
LEFT JOIN tl.channels rc ON rc.id=r.channel_id
CROSS JOIN LATERAL (
 SELECT coalesce(jsonb_agg(jsonb_build_object('message_id',x.message_id,'version',x.version,'relation',x.relation) ORDER BY x.message_id) FILTER (WHERE x.relation='supports'),'[]'::jsonb) AS support
 FROM tl.claim_sources x WHERE x.claim_id=c.id) s
$def$);
INSERT INTO tl.record_sources VALUES ('c','api.claim_records') ON CONFLICT (prefix) DO NOTHING;

-- Annotations joined to the current version of their target. A restricted
-- asker sees an annotation when that message is visible; speaker annotations
-- (identity) are unrestricted-only.
SELECT tl.install_gate('annotations',
 'v.message_id IS NOT NULL AND v.attribute<>''speaker'' AND api.row_visible(v.message_id,v.target_channel_id,v.thread_id)',
 $def$
SELECT a.id,a.target_channel_id,ch.name AS channel,a.target_external_id,a.target_version,
 m.id AS message_id,m.thread_id,a.attribute,a.value,
 a.valid_from,a.valid_to,a.valid_from_precision,a.valid_to_precision,
 a.method,a.origin,a.job,a.job_version,a.token_id,a.confidence,a.reason,a.status,a.superseded_by,a.created_at
FROM tl.annotations a
JOIN tl.channels ch ON ch.id=a.target_channel_id
LEFT JOIN tl.messages m ON m.channel_id=a.target_channel_id AND m.external_id=a.target_external_id AND m.superseded_by IS NULL
$def$);

-- 6. Functions ------------------------------------------------------------------
-- A claim's sources (current versions) with thread context, in one call.
-- Nothing when the claim is not visible.
CREATE FUNCTION api.claim_sources(claim_id bigint,before int DEFAULT 0,after int DEFAULT 0) RETURNS SETOF api.messages LANGUAGE sql STABLE AS $$
 WITH c AS (SELECT h.id FROM api.claims_history h WHERE h.id=claim_sources.claim_id),
 anchors AS (SELECT DISTINCT api.current_message(x.message_id) AS id FROM tl.claim_sources x JOIN c ON c.id=x.claim_id)
 SELECT DISTINCT ON (m.id) m.*
 FROM anchors a
 JOIN api.messages s ON s.id=a.id
 JOIN api.messages m ON m.id=s.id OR (m.thread_id=s.thread_id
  AND m.thread_no BETWEEN s.thread_no-least(greatest(claim_sources.before,0),50) AND s.thread_no+least(greatest(claim_sources.after,0),50))
 ORDER BY m.id $$;

-- Claims stored by `at`, with the status they had then and the sources added
-- by then.
CREATE FUNCTION api.claims_asof(at timestamptz) RETURNS SETOF api.claims_history LANGUAGE sql STABLE AS $$
 SELECT r.* FROM api.claims_history h
 CROSS JOIN LATERAL (SELECT
  (SELECT jsonb_build_object('status',x.old_status,'superseded_by',x.old_superseded_by) FROM tl.status_history x
   WHERE x.kind='claim' AND x.ref_id=h.id AND x.changed_at>claims_asof.at ORDER BY x.changed_at,x.id LIMIT 1) AS state,
  (SELECT jsonb_build_object(
    'source_message_ids',coalesce(jsonb_agg(e->'message_id' ORDER BY (e->>'message_id')::bigint),'[]'::jsonb),
    'source_refs',coalesce(jsonb_agg(e ORDER BY (e->>'message_id')::bigint),'[]'::jsonb))
   FROM jsonb_array_elements(h.source_refs) e WHERE (e->>'added_at')::timestamptz<=claims_asof.at) AS refs) t
 CROSS JOIN LATERAL jsonb_populate_record(NULL::api.claims_history,to_jsonb(h)||coalesce(t.state,'{}'::jsonb)||t.refs) r
 WHERE h.created_at<=claims_asof.at
 ORDER BY h.id $$;

-- The effective overlay of messages, read through the gated views only.
CREATE FUNCTION api.effective(message_ids bigint[]) RETURNS TABLE(message_id bigint,effective_time timestamptz,effective_end timestamptz,effective_time_precision text,effective_place text,lat float8,lon float8,ignored boolean,ignore_reasons text[],media_noise boolean,speaker_person_id bigint,annotation_ids bigint[]) LANGUAGE sql STABLE AS $$
 SELECT m.id,
  (o.f->>'effective_time')::timestamptz,
  (o.f->>'effective_end')::timestamptz,
  o.f->>'precision',
  o.f->>'effective_place',
  (o.f->>'lat')::float8,
  (o.f->>'lon')::float8,
  (o.f->>'ignored')::boolean,
  ARRAY(SELECT jsonb_array_elements_text(o.f->'ignore_reasons')),
  (o.f->>'media_noise')::boolean,
  CASE WHEN (SELECT api.asker_unrestricted()) THEN (o.f->>'speaker_person_id')::bigint END,
  ARRAY(SELECT jsonb_array_elements_text(o.f->'annotation_ids')::bigint)
 FROM api.message_versions m
 CROSS JOIN LATERAL (SELECT tl.overlay_fold((
   SELECT jsonb_agg(jsonb_build_object('attribute',a.attribute,'value',a.value,'method',a.method,'created_at',a.created_at,'id',a.id,'reason',a.reason))
   FROM api.annotations a
   WHERE a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id AND a.status='active'
    AND (a.target_version IS NULL OR a.target_version=m.version)),
  CASE WHEN jsonb_typeof(m.fields->'ignore_reason')='string' THEN m.fields->>'ignore_reason' END) AS f) o
 WHERE m.id=ANY(effective.message_ids)
 ORDER BY m.id $$;

-- The lineage of a claim or an annotation is readable only when the object
-- itself is (0037 body plus the two kinds).
CREATE OR REPLACE FUNCTION api.derived_visible(kind text,id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 RETURN CASE derived_visible.kind
  WHEN 'message' THEN api.message_visible(derived_visible.id)
  WHEN 'link' THEN EXISTS(SELECT 1 FROM tl.links l WHERE l.id=derived_visible.id AND api.link_image_visible(to_jsonb(l)))
  WHEN 'entity' THEN false
  WHEN 'passage' THEN EXISTS(SELECT 1 FROM api.passages p WHERE p.passage_id=derived_visible.id)
  WHEN 'claim' THEN EXISTS(SELECT 1 FROM api.claims_history c WHERE c.id=derived_visible.id)
  WHEN 'annotation' THEN EXISTS(SELECT 1 FROM api.annotations a WHERE a.id=derived_visible.id)
  ELSE true END;
END $$;

GRANT EXECUTE ON FUNCTION api.claim_sources(bigint,int,int),api.claims_asof(timestamptz),api.effective(bigint[]) TO throughline_app,throughline_reader;

-- 7. Eligibility --------------------------------------------------------
-- time_basis 'effective' is accepted (person_role rules unchanged).
CREATE OR REPLACE FUNCTION tl.recall_check(person_role text,time_basis text) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
BEGIN
 IF coalesce(person_role,'author') NOT IN ('author','involved') THEN RAISE EXCEPTION 'person_role must be author or involved' USING ERRCODE='invalid_parameter_value'; END IF;
 IF coalesce(time_basis,'record') NOT IN ('record','subject','either','effective') THEN RAISE EXCEPTION 'time_basis must be record, subject, either or effective' USING ERRCODE='invalid_parameter_value'; END IF;
 RETURN true;
END $$;

-- The 0038 hard scope plus the overlay:
--  * a record whose effective overlay is ignored is eligible only when kinds
--    holds the pseudo-kind 'ignored' (removed before kind matching; kinds of
--    only 'ignored' means the default kinds plus ignored);
--  * time_basis 'effective' tests [coalesce(effective_time, record_time),
--    coalesce(effective_end, ended_at, effective_time, record_time)] with the
--    record rule of tl.recall_time_ok, which never sees 'effective';
--  * known_by applies only annotations made by then; the version limit keeps
--    annotations for every version or for the eligible version.
-- tl.overlay_state is not called for a record without annotations and
-- without an ignore_reason field.
CREATE OR REPLACE FUNCTION tl.recall_scope(message_ids bigint[],kinds text[],channels text[],persons bigint[],person_role text,after_at timestamptz,before_at timestamptz,time_basis text,lat float8,lon float8,meters float8,thread_ids bigint[],include_outside boolean,as_of timestamptz,known_by timestamptz,must_terms text[],exclude_message_ids bigint[])
RETURNS TABLE(message_id bigint,record_time timestamptz,thread_id bigint,thread_no int) LANGUAGE sql STABLE SET search_path=pg_catalog,api,public AS $$
 WITH base AS (
  SELECT m.id,m.channel_id,m.thread_id,m.thread_no,m.happened_at AS record_time,m.ended_at,m.person_id,m.lat,m.lon,m.is_outside
  FROM api.message_versions m
  WHERE recall_scope.as_of IS NULL AND recall_scope.known_by IS NULL AND m.superseded_by IS NULL
   AND (recall_scope.message_ids IS NULL OR m.id=ANY(recall_scope.message_ids))
  UNION ALL
  SELECT r.message_id,x.channel_id,r.thread_id,r.thread_no,r.record_time,r.ended_at,r.person_id,r.lat,r.lon,r.is_outside
  FROM api.records_asof(coalesce(recall_scope.as_of,'infinity'::timestamptz),recall_scope.known_by) r
  JOIN tl.messages x ON x.id=r.message_id
  WHERE (recall_scope.as_of IS NOT NULL OR recall_scope.known_by IS NOT NULL)
   AND (recall_scope.message_ids IS NULL OR r.message_id=ANY(recall_scope.message_ids))
 ), opts AS MATERIALIZED (
  SELECT coalesce('ignored'=ANY(recall_scope.kinds),false) AS include_ignored,
   CASE WHEN recall_scope.kinds IS NULL THEN ARRAY['life_event','derived']
    WHEN 'ignored'=ANY(recall_scope.kinds) AND cardinality(array_remove(recall_scope.kinds,'ignored'))=0 THEN ARRAY['life_event','derived']
    ELSE array_remove(recall_scope.kinds,'ignored') END AS kinds,
   coalesce(recall_scope.time_basis,'record')='effective' AS effective
 )
 SELECT b.id,b.record_time,b.thread_id,b.thread_no
 FROM base b JOIN tl.messages x ON x.id=b.id JOIN tl.channels c ON c.id=b.channel_id CROSS JOIN opts
 -- The tl.overlay_state rule, evaluated in line: a per-row call of the
 -- SECURITY DEFINER helper costs ten times the work itself. CASE guarantees
 -- the overlay is not computed on the fast path (a lateral WHERE would
 -- become a join filter evaluated after the work).
 CROSS JOIN LATERAL (SELECT CASE WHEN (NOT opts.include_ignored OR opts.effective)
   AND ((x.fields->>'ignore_reason') IS NOT NULL OR EXISTS(
    SELECT 1 FROM tl.annotations a WHERE a.target_channel_id=x.channel_id AND a.target_external_id=x.external_id
     AND (a.status='active' OR recall_scope.known_by IS NOT NULL)))
  THEN tl.overlay_fold((
   SELECT jsonb_agg(jsonb_build_object('attribute',a.attribute,'value',a.value,'method',a.method,'created_at',a.created_at,'id',a.id,'reason',a.reason))
   FROM tl.annotations a
   WHERE a.target_channel_id=x.channel_id AND a.target_external_id=x.external_id
    AND (a.target_version IS NULL OR a.target_version=x.version)
    AND a.attribute IN ('ignore','media_noise','effective_time')
    AND CASE WHEN recall_scope.known_by IS NULL THEN a.status='active'
     ELSE a.created_at<=recall_scope.known_by AND coalesce((SELECT h.old_status FROM tl.status_history h WHERE h.kind='annotation' AND h.ref_id=a.id AND h.changed_at>recall_scope.known_by ORDER BY h.changed_at,h.id LIMIT 1),a.status)='active' END),
   CASE WHEN jsonb_typeof(x.fields->'ignore_reason')='string' THEN x.fields->>'ignore_reason' END) END AS st) ov
 WHERE tl.recall_check(recall_scope.person_role,recall_scope.time_basis)
  AND c.record_kind<>'page_revision'
  AND c.record_kind=ANY(opts.kinds)
  AND (recall_scope.channels IS NULL OR c.name=ANY(recall_scope.channels))
  AND (recall_scope.thread_ids IS NULL OR b.thread_id=ANY(recall_scope.thread_ids))
  AND (recall_scope.include_outside IS NOT FALSE OR NOT coalesce(b.is_outside,false))
  AND (recall_scope.exclude_message_ids IS NULL OR b.id<>ALL(recall_scope.exclude_message_ids))
  AND (opts.include_ignored OR NOT coalesce((ov.st->>'ignored')::boolean,false))
  AND (recall_scope.persons IS NULL OR b.person_id=ANY(recall_scope.persons)
   OR (recall_scope.person_role='involved' AND (
    EXISTS(SELECT 1 FROM api.messages o WHERE o.thread_id=b.thread_id AND o.person_id=ANY(recall_scope.persons))
    OR EXISTS(SELECT 1 FROM api.links l WHERE l.status='active' AND (
     (l.from_kind='message' AND l.from_id=b.id AND l.to_kind='person' AND l.to_id=ANY(recall_scope.persons))
     OR (l.to_kind='message' AND l.to_id=b.id AND l.from_kind='person' AND l.from_id=ANY(recall_scope.persons)))))))
  AND CASE WHEN opts.effective THEN tl.recall_time_ok(coalesce((ov.st->>'effective_time')::timestamptz,b.record_time),coalesce((ov.st->>'effective_end')::timestamptz,b.ended_at,(ov.st->>'effective_time')::timestamptz,b.record_time),NULL,NULL,NULL,NULL,recall_scope.after_at,recall_scope.before_at,'record')
   ELSE tl.recall_time_ok(b.record_time,b.ended_at,x.subject_from,x.subject_to,x.subject_from_precision,x.subject_to_precision,recall_scope.after_at,recall_scope.before_at,recall_scope.time_basis) END
  AND (recall_scope.lat IS NULL OR recall_scope.lon IS NULL OR (b.lat IS NOT NULL AND b.lon IS NOT NULL
   AND earth_distance(api.earth_point(recall_scope.lat,recall_scope.lon),api.earth_point(b.lat,b.lon))<=coalesce(recall_scope.meters,100)))
  AND (recall_scope.must_terms IS NULL OR NOT EXISTS(
   SELECT 1 FROM unnest(recall_scope.must_terms) t(term)
   WHERE numnode(plainto_tsquery('english',t.term))>0 AND NOT (x.tsv @@ plainto_tsquery('english',t.term)))) $$;

-- 8. Registry, allow-list and catalogue -------------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('claim_sources','sql','claim_sources','The source messages of a claim (current versions) with before/after thread context, in one call; nothing when the claim or any source is hidden','exact','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.claim_sources(1,1,1)','1',NULL,NULL),
 ('claims_asof','sql','claims_asof','Claims stored by a time, with the status they had then and the sources added by then','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.claims_asof(''2020-01-01Z'')','1',NULL,NULL),
 ('effective','sql','effective','The effective overlay of messages: effective time and place, location, ignored with reasons, media noise, speaker (unrestricted askers only) and the annotations that apply','exact','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.effective(ARRAY[1]::bigint[])','1',NULL,NULL);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('claim_sources','claims_asof','effective');

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('claims',jsonb_build_object(
  'description','Throughline-owned claims: typed statements about a subject (page, entity, person, message, concept) with sources, attribution, uncertainty, validity and supersession. Claims are derived records; ground truth is never changed.',
  'views',jsonb_build_object(
   'api.claims','Active claims: statement, claim_type, confidence, uncertainty, attribution, validity, method, status, source_message_ids, source_refs',
   'api.claims_history','Every claim, superseded, contradicted and retracted ones included',
   'api.claim_records','Claims in the record contract (prefix c, generated true; support is the supporting sources; unsupported when there are none)'),
  'types',jsonb_build_object('observation','What a source shows','inference','What follows from sources','hypothesis','A possible explanation','judgment','An opinion; always attributed to its speaker'),
  'rules',jsonb_build_array(
   'claim_type is mandatory and independent of confidence.',
   'Sources are message ids with the version pinned at write time; a claim without sources needs no_source_reason, is stored unsupported and is never presented as supported.',
   'Natural key: subject, day of valid_from, type, attribution and the normalised statement. A second source of the same claim corroborates it instead of duplicating it.',
   'An owner restating a claim a model or job made supersedes it with an owner claim that keeps both sets of sources; owner claims are protected from non-owner tokens.',
   'Content is never edited: supersede, contradict, split, merge and retract create relations and change status only.',
   'A restricted asker sees a claim only when every source (and its revision) is visible; unsupported claims are unrestricted-only.',
   'Agents, the librarian and the curator propose (POST /v1/proposals); an accepted proposal keeps its originating method and job, never owner.'),
  'functions',jsonb_build_array('claim_sources','claims_asof'))),
 ('link_types',jsonb_build_object(
  'description','The closed link-type registry. Every link type is registered; free-text link types are refused or folded.',
  'view','api.link_types: type, description, inverse, status (active, candidate, retired), origin (core, plugin, owner, legacy, source_candidate), synonyms. Restricted askers see active types only; counts are owner-only (GET /v1/link-types?with_counts=true).',
  'rule','free-text link types are refused or folded',
  'normalisation','A raw type is lower-cased, runs of other characters become _, edges are stripped and it is cut to 63 characters; a name not starting with a letter gets t_; nothing left becomes t_ and a hash. The raw text is kept in links.type_as_written.',
  'writers',jsonb_build_array(
   'Synonyms are folded into their canonical type for every writer.',
   'Rule and model links (including the curator on the owner connection) may use registered types only.',
   'Owner tokens, owner-method links and token-less owner connections writing source or owner links register a new type.',
   'A plugin source link registers a type its manifest link_types declares; an undeclared one is accepted and held as a candidate; a retired one is folded into its merge target, else accepted and reported as retired_but_written. Imports are never stopped by the vocabulary.',
   'Agent tokens and every other writer are refused for unregistered types; they propose them (POST /v1/proposals kind link_type).'),
  'merge','POST /v1/link-types/{type}/merge {into}: creates the synonym, re-types every link (history kept), supersedes duplicates (same ends and method) so the oldest stays active, and retires the type.')),
 ('overlay',jsonb_build_object(
  'description','Annotations overlay messages without changing them: effective time, effective place, location, ignore, media noise and speaker. Overlays never rewrite ground truth.',
  'attributes',jsonb_build_object(
   'effective_time','{at, ended_at?, precision}: when the record really happened',
   'effective_place','{name, entity_id?}',
   'location','{lat, lon, accuracy_m}',
   'ignore','{ignored: boolean, reason}',
   'media_noise','{is_noise: boolean, score?, evidence?, matched_record_id?}',
   'speaker','{person_id} (unrestricted askers only)'),
  'precedence','owner > source field (fields.ignore_reason) > rule > model, the latest within a method. An owner ignore {ignored:false} restores a record a source or a job flagged; later jobs never override an owner annotation.',
  'keys','Annotations are keyed by channel and external_id; target_version NULL applies to every version, so re-import and new versions keep the curation.',
  'recall','Records whose effective overlay is ignored are excluded by default from every recall leg; kinds with the pseudo-kind ignored (include_ignored) brings them back. time_basis effective filters on the effective interval. Under known_by only annotations made by then apply; under as_of alone the current annotations for the version valid then apply. The legacy api.search functions are unchanged.',
  'functions',jsonb_build_array('effective'),
  'view','api.annotations: annotations of messages the asker may see'));

-- 9. Purge (ARC-5, ACL-9): derived text of purged messages must not survive.
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('claims',30,
  'WITH doomed AS MATERIALIZED (SELECT c.id FROM tl.claims c WHERE c.revision_message_id IN (SELECT m.id FROM tl.messages m WHERE m.channel_id=$1)
    OR EXISTS(SELECT 1 FROM tl.claim_sources s JOIN tl.messages m ON m.id=s.message_id WHERE s.claim_id=c.id AND m.channel_id=$1)),
   prov AS (DELETE FROM tl.provenance p WHERE p.derived_kind=''claim'' AND p.derived_id IN (SELECT d.id FROM doomed d)),
   hist AS (DELETE FROM tl.status_history h WHERE h.kind=''claim'' AND h.ref_id IN (SELECT d.id FROM doomed d))
   DELETE FROM tl.claims c WHERE c.id IN (SELECT d.id FROM doomed d)'),
 ('proposals',31,
  'DELETE FROM tl.proposals p WHERE p.message_ids && ARRAY(SELECT m.id FROM tl.messages m WHERE m.channel_id=$1)
    OR EXISTS(SELECT 1 FROM tl.channels ch WHERE ch.id=$1 AND (
     jsonb_path_exists(p.payload,''$.** ? (@.channel == $c || @.page_channel == $c)'',jsonb_build_object(''c'',ch.name))
     OR jsonb_path_exists(p.evidence,''$.** ? (@.channel == $c || @.page_channel == $c)'',jsonb_build_object(''c'',ch.name))))'),
 ('annotations',32,
  'WITH doomed AS MATERIALIZED (SELECT a.id FROM tl.annotations a WHERE a.target_channel_id=$1),
   prov AS (DELETE FROM tl.provenance p WHERE p.derived_kind=''annotation'' AND p.derived_id IN (SELECT d.id FROM doomed d)),
   hist AS (DELETE FROM tl.status_history h WHERE h.kind=''annotation'' AND h.ref_id IN (SELECT d.id FROM doomed d))
   DELETE FROM tl.annotations a WHERE a.id IN (SELECT d.id FROM doomed d)');
