-- Sensitivity labels and disclosure policy in the database gate (
-- ACL-1, ACL-2, ACL-3 label policies, ACL-5, ACL-7, ACL-8, ACL-10, FLT-3).
--
-- 1. Label kinds (tl.labels, a seed hook) and labels on channels and threads.
--    A tl.messages.access_label value counts as a label when it names a
--    tl.labels row, so labels apply to stored history without a re-import.
-- 2. Grants carry the labels they permit, a purpose, an expiry and their
--    author; tl.access_events is the append-only audit of label and grant
--    changes.
-- 3. api.asker_sees and api.row_visible (same signatures) admit a row for a
--    restricted asker only through an unexpired grant on its channel or
--    thread whose labels cover every label of the channel, the thread and the
--    message. Labelled content is denied by default inside a granted channel.
--    Every gated view, search function, recall leg, set, packet, person
--    function, coverage view and consumer already calls these two, so they
--    inherit the rule; model judgement is never asked whether to disclose.
-- 4. api.policy_version() stays monotonic while grants expire: it adds the
--    number of expired grants to tl.policy_state.version, and the grant
--    trigger compensates when an expired grant is changed or deleted.
-- 5. api.describe_access() shows the asker its own grants with their labels,
--    purpose and expiry.
--
-- Forward-only. Owner scope is unaffected. Every api function becomes
-- SECURITY DEFINER with a fixed search_path through tl.harden_reader(), so tl
-- objects are schema-qualified.

-- 1. Labels --------------------------------------------------------------------
CREATE TABLE tl.labels(
 label text PRIMARY KEY CHECK (label ~ '^[a-z][a-z0-9_]{0,40}$'),
 description text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE tl.channel_labels(
 channel_id bigint NOT NULL REFERENCES tl.channels ON DELETE CASCADE,
 label text NOT NULL REFERENCES tl.labels ON UPDATE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(channel_id,label));
CREATE TABLE tl.thread_labels(
 thread_id bigint NOT NULL REFERENCES tl.threads ON DELETE CASCADE,
 label text NOT NULL REFERENCES tl.labels ON UPDATE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(thread_id,label));
CREATE INDEX channel_labels_label ON tl.channel_labels(label);
CREATE INDEX thread_labels_label ON tl.thread_labels(label);
-- Owner writes; the app role reads (owner connection only, like grants).
REVOKE ALL ON tl.labels,tl.channel_labels,tl.thread_labels FROM PUBLIC,throughline_app,throughline_reader;
GRANT SELECT ON tl.labels,tl.channel_labels,tl.thread_labels TO throughline_app;

-- Seed (a seed hook): the shipped label kinds. The owner may add more.
INSERT INTO tl.seed_hooks(name,ord,statement) VALUES
 ('labels',51,$seed$INSERT INTO tl.labels(label,description) VALUES
 ('intimate','Intimate or romantic content'),
 ('medical','Health and medical content'),
 ('financial','Money, accounts and financial content'),
 ('work','Work content, for work askers only when granted'),
 ('third_party_confidence','Something a third party said in confidence')
 ON CONFLICT (label) DO NOTHING$seed$);
DO $$ BEGIN EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='labels'); END $$;

-- Labels on channels and threads change what restricted askers read.
CREATE TRIGGER bump_policy AFTER INSERT OR UPDATE OR DELETE ON tl.channel_labels FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();
CREATE TRIGGER bump_policy_truncate AFTER TRUNCATE ON tl.channel_labels FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();
CREATE TRIGGER bump_policy AFTER INSERT OR UPDATE OR DELETE ON tl.thread_labels FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();
CREATE TRIGGER bump_policy_truncate AFTER TRUNCATE ON tl.thread_labels FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();
-- A label kind changes visibility only through messages whose access_label
-- names it (channel and thread labels reference the kind, so they cannot
-- outlive it). The version moves when such a message exists; re-seeding an
-- empty database therefore leaves the version alone.
CREATE FUNCTION tl.bump_policy_label_kind() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM tl.messages m WHERE m.access_label IN (CASE WHEN TG_OP<>'INSERT' THEN OLD.label END,CASE WHEN TG_OP<>'DELETE' THEN NEW.label END)) THEN
  INSERT INTO tl.policy_state(one,version) VALUES(true,2) ON CONFLICT(one) DO UPDATE SET version=tl.policy_state.version+1;
 END IF;
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.bump_policy_label_kind() FROM PUBLIC;
CREATE TRIGGER bump_policy AFTER INSERT OR DELETE OR UPDATE OF label ON tl.labels FOR EACH ROW EXECUTE FUNCTION tl.bump_policy_label_kind();
-- A label kind is never renamed, and is not removed while a stored message's
-- access_label names it: either would turn labelled history into unlabelled
-- history and open it to every channel grant. Fail closed; the owner first
-- relabels those messages (or adds the new kind and keeps the old one).
CREATE FUNCTION tl.label_kind_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF TG_OP='UPDATE' THEN
  IF NEW.label IS DISTINCT FROM OLD.label THEN
   RAISE EXCEPTION 'label kind % cannot be renamed',OLD.label USING ERRCODE='integrity_constraint_violation',
    HINT='add the new kind and label the rows with it; keep the old kind while rows carry it';
  END IF;
  RETURN NEW;
 END IF;
 IF EXISTS(SELECT 1 FROM tl.messages m WHERE m.access_label=OLD.label) THEN
  RAISE EXCEPTION 'label kind % is still named by stored messages',OLD.label USING ERRCODE='foreign_key_violation';
 END IF;
 RETURN OLD;
END $$;
REVOKE EXECUTE ON FUNCTION tl.label_kind_guard() FROM PUBLIC;
CREATE TRIGGER label_kind_guard BEFORE DELETE OR UPDATE OF label ON tl.labels FOR EACH ROW EXECUTE FUNCTION tl.label_kind_guard();
CREATE TRIGGER bump_policy_truncate AFTER TRUNCATE ON tl.labels FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();

-- 2. Grants with labels, purpose and expiry; the audit ---------------------------
ALTER TABLE tl.asker_grants
 ADD COLUMN labels text[] NOT NULL DEFAULT '{}',
 ADD COLUMN purpose text CHECK (purpose IS NULL OR length(purpose)<=1000),
 ADD COLUMN expires_at timestamptz,
 ADD COLUMN created_by text;
CREATE INDEX asker_grants_expires ON tl.asker_grants(expires_at) WHERE expires_at IS NOT NULL;
-- A grant names only known label kinds, each once, in a canonical order.
CREATE FUNCTION tl.grant_labels_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE unknown text;
BEGIN
 NEW.labels := ARRAY(SELECT DISTINCT x FROM unnest(coalesce(NEW.labels,'{}'::text[])) x ORDER BY x);
 SELECT string_agg(x,', ') INTO unknown FROM unnest(NEW.labels) x WHERE NOT EXISTS(SELECT 1 FROM tl.labels l WHERE l.label=x);
 IF unknown IS NOT NULL THEN
  RAISE EXCEPTION 'unknown label: %',unknown USING ERRCODE='foreign_key_violation';
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.grant_labels_guard() FROM PUBLIC;
CREATE TRIGGER grant_labels_guard BEFORE INSERT OR UPDATE OF labels ON tl.asker_grants FOR EACH ROW EXECUTE FUNCTION tl.grant_labels_guard();

CREATE TABLE tl.access_events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 op text NOT NULL CHECK (op IN ('asker','label','unlabel','label_kind','grant','regrant','extend','revoke')),
 asker text,
 detail jsonb NOT NULL DEFAULT '{}',
 actor text NOT NULL,
 at timestamptz NOT NULL DEFAULT now());
CREATE INDEX access_events_asker ON tl.access_events(asker,at);
CREATE FUNCTION tl.access_events_append_only() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 RAISE EXCEPTION 'tl.access_events is append-only' USING ERRCODE='insufficient_privilege';
END $$;
REVOKE EXECUTE ON FUNCTION tl.access_events_append_only() FROM PUBLIC;
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON tl.access_events FOR EACH ROW EXECUTE FUNCTION tl.access_events_append_only();
REVOKE ALL ON tl.access_events FROM PUBLIC,throughline_app,throughline_reader;
GRANT SELECT ON tl.access_events TO throughline_app;

-- 3. The label rule --------------------------------------------------------------
-- An unexpired grant of the asker on the channel or the thread whose labels
-- cover every label of the channel, of the thread and (when it names a label
-- kind) of the message. The most permissive matching grant decides (EXISTS).
-- Owner-only helper: the api gate functions (SECURITY DEFINER) call it.
CREATE FUNCTION tl.grant_allows(asker text,channel_id bigint,thread_id bigint,message_label text) RETURNS boolean LANGUAGE plpgsql STABLE SET search_path=pg_catalog,tl AS $$
BEGIN
 -- One statement, so its plan is cached across the per-row calls. A grant
 -- admits the row when no label of the channel or the thread is missing
 -- from it, and the message label is allowed or names no label kind.
 RETURN EXISTS(SELECT 1 FROM tl.asker_grants g WHERE g.asker=grant_allows.asker
   AND (g.channel_id=grant_allows.channel_id OR g.thread_id=grant_allows.thread_id)
   AND (g.expires_at IS NULL OR g.expires_at>now())
   AND NOT EXISTS(SELECT 1 FROM tl.channel_labels l WHERE l.channel_id=grant_allows.channel_id AND l.label<>ALL(g.labels))
   AND NOT EXISTS(SELECT 1 FROM tl.thread_labels l WHERE l.thread_id=grant_allows.thread_id AND l.label<>ALL(g.labels))
   AND (grant_allows.message_label IS NULL OR grant_allows.message_label=ANY(g.labels)
    OR NOT EXISTS(SELECT 1 FROM tl.labels k WHERE k.label=grant_allows.message_label)));
END $$;
REVOKE EXECUTE ON FUNCTION tl.grant_allows(text,bigint,bigint,text) FROM PUBLIC,throughline_app,throughline_reader;

CREATE OR REPLACE FUNCTION api.asker_sees(channel_id bigint,thread_id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF current_setting('throughline.scope_mode',true) IS DISTINCT FROM 'asker' THEN RETURN false; END IF;
 RETURN tl.grant_allows(current_setting('throughline.asker',true),asker_sees.channel_id,asker_sees.thread_id,NULL);
END $$;

-- The 0036 body, with the grant test replaced by the label rule for the row
-- itself (its own access_label included) and for every source of the walk.
CREATE OR REPLACE FUNCTION api.row_visible(message_id bigint,channel_id bigint,thread_id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE rk text; hidden boolean; lbl text; who text := current_setting('throughline.asker',true);
BEGIN
 IF current_setting('throughline.scope_mode',true) IS DISTINCT FROM 'asker' THEN RETURN false; END IF;
 -- The channel's record kind and the message's own label in one lookup.
 SELECT c.record_kind,m.access_label INTO rk,lbl FROM tl.channels c LEFT JOIN tl.messages m ON m.id=row_visible.message_id WHERE c.id=row_visible.channel_id;
 IF rk IS NULL THEN RETURN false; END IF;
 -- tl.grant_allows written out: this runs once per scanned row.
 IF NOT EXISTS(SELECT 1 FROM tl.asker_grants g WHERE g.asker=who
   AND (g.channel_id=row_visible.channel_id OR g.thread_id=row_visible.thread_id)
   AND (g.expires_at IS NULL OR g.expires_at>now())
   AND NOT EXISTS(SELECT 1 FROM tl.channel_labels l WHERE l.channel_id=row_visible.channel_id AND l.label<>ALL(g.labels))
   AND NOT EXISTS(SELECT 1 FROM tl.thread_labels l WHERE l.thread_id=row_visible.thread_id AND l.label<>ALL(g.labels))
   AND (lbl IS NULL OR lbl=ANY(g.labels) OR NOT EXISTS(SELECT 1 FROM tl.labels k WHERE k.label=lbl))) THEN
  RETURN false;
 END IF;
 IF rk NOT IN ('page_revision','derived') THEN RETURN true; END IF;
 WITH RECURSIVE walk(node,depth,path,cycle) AS (
  SELECT p.source_message_id,1,ARRAY[row_visible.message_id,p.source_message_id],p.source_message_id=row_visible.message_id
  FROM tl.provenance p WHERE p.derived_kind='message' AND p.derived_id=row_visible.message_id AND p.origin IN ('source','owner','job')
  UNION ALL
  SELECT p.source_message_id,w.depth+1,w.path||p.source_message_id,p.source_message_id=ANY(w.path)
  FROM walk w JOIN tl.messages m ON m.id=w.node JOIN tl.channels c ON c.id=m.channel_id
  JOIN tl.provenance p ON p.derived_kind='message' AND p.derived_id=w.node AND p.origin IN ('source','owner','job')
  WHERE NOT w.cycle AND w.depth<=4 AND c.record_kind IN ('page_revision','derived')
 )
 SELECT EXISTS(SELECT 1 FROM walk w LEFT JOIN tl.messages m ON m.id=w.node
  WHERE w.cycle OR w.depth>4 OR m.id IS NULL OR NOT tl.grant_allows(who,m.channel_id,m.thread_id,m.access_label)) INTO hidden;
 RETURN NOT hidden;
END $$;

-- A channel is listed for a grant on it or on one of its threads that admits
-- the channel (or that thread) under the label rule, and only while the grant
-- is unexpired.
SELECT tl.install_gate('channels','current_setting(''throughline.scope_mode'',true)=''asker'' AND EXISTS(SELECT 1 FROM tl.asker_grants g LEFT JOIN tl.threads t ON t.id=g.thread_id WHERE g.asker=current_setting(''throughline.asker'',true) AND (g.channel_id=v.id OR t.channel_id=v.id) AND api.asker_sees(v.id,g.thread_id))');

-- Active link-type names (vocabulary, no content) are listed to an asker
-- holding at least one unexpired grant; an asker whose grants have all
-- expired sees none, like an asker with no grant (0042 checked any grant).
SELECT tl.install_gate('link_types',
 'v.status=''active'' AND current_setting(''throughline.scope_mode'',true)=''asker'' AND EXISTS(SELECT 1 FROM tl.asker_grants g WHERE g.asker=current_setting(''throughline.asker'',true) AND (g.expires_at IS NULL OR g.expires_at>now()))',
 $def$
SELECT t.type,t.description,t.inverse,t.status,t.origin,
 coalesce((SELECT array_agg(s.alias ORDER BY s.alias) FROM tl.link_type_synonyms s WHERE s.type=t.type),'{}'::text[]) AS synonyms
FROM tl.link_types t
$def$);

-- 4. Monotonic policy version under expiry --------------------------------------
-- version = tl.policy_state.version + (grants with expires_at <= now()).
-- An expiry passing raises the second term by one, which invalidates saved
-- sets, sessions and investigations exactly like a revocation. A statement on
-- tl.asker_grants adds 1 + (OLD rows already expired at the change) to the
-- stored version: deleting or extending an expired grant lowers the second
-- term by one per row and the compensation keeps the sum moving forward by at
-- least one. Inserting or updating to a past expiry raises both terms.
DROP TRIGGER bump_policy ON tl.asker_grants;
CREATE FUNCTION tl.bump_policy_grants() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE expired bigint := 0;
BEGIN
 IF TG_OP IN ('UPDATE','DELETE') THEN
  -- clock_timestamp: a grant expired by now was counted by every reader
  -- whose transaction began after its expiry.
  SELECT count(*) INTO expired FROM old_grants o WHERE o.expires_at<=clock_timestamp();
 END IF;
 INSERT INTO tl.policy_state(one,version) VALUES(true,2+expired) ON CONFLICT(one) DO UPDATE SET version=tl.policy_state.version+1+expired;
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.bump_policy_grants() FROM PUBLIC;
CREATE TRIGGER bump_policy_grants AFTER INSERT ON tl.asker_grants FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy_grants();
CREATE TRIGGER bump_policy_grants_update AFTER UPDATE ON tl.asker_grants REFERENCING OLD TABLE AS old_grants FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy_grants();
CREATE TRIGGER bump_policy_grants_delete AFTER DELETE ON tl.asker_grants REFERENCING OLD TABLE AS old_grants FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy_grants();
-- TRUNCATE has no transition table: count the expired grants before the
-- rows go (the 0033 AFTER TRUNCATE trigger then adds the usual one).
CREATE FUNCTION tl.bump_policy_grants_truncate() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE expired bigint;
BEGIN
 SELECT count(*) INTO expired FROM tl.asker_grants g WHERE g.expires_at<=clock_timestamp();
 IF expired>0 THEN
  INSERT INTO tl.policy_state(one,version) VALUES(true,1+expired) ON CONFLICT(one) DO UPDATE SET version=tl.policy_state.version+expired;
 END IF;
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.bump_policy_grants_truncate() FROM PUBLIC;
CREATE TRIGGER bump_policy_grants_before_truncate BEFORE TRUNCATE ON tl.asker_grants FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy_grants_truncate();

CREATE OR REPLACE FUNCTION api.policy_version() RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT coalesce((SELECT version FROM tl.policy_state),0)
  + (SELECT count(*) FROM tl.asker_grants g WHERE g.expires_at<=now())
$$;

-- 5. Access description --------------------------------------------------------
-- The asker's own grants only: each grant's target (a channel by name, a
-- thread by id), the labels on that target (for a thread, its channel's as
-- well), the labels the grant allows, its purpose and expiry. No other asker, no ungranted channel, no total.
CREATE OR REPLACE FUNCTION api.describe_access() RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 WITH me AS (
  SELECT nullif(current_setting('throughline.scope_mode',true),'') scope_mode,
   CASE WHEN current_setting('throughline.scope_mode',true)='asker' THEN current_setting('throughline.asker',true) END asker
 ), registered AS (
  SELECT a.name,a.mode FROM tl.askers a JOIN me ON me.asker=a.name
 ), mine AS (
  SELECT g.*,c.name AS channel,(g.expires_at IS NOT NULL AND g.expires_at<=now()) AS expired
  FROM tl.asker_grants g JOIN me ON me.asker=g.asker LEFT JOIN tl.channels c ON c.id=g.channel_id
 )
 SELECT jsonb_build_object(
  'scope_mode',me.scope_mode,
  'asker',me.asker,
  'asker_mode',(SELECT mode FROM registered),
  'unrestricted',api.asker_unrestricted(),
  'granted_channels',(SELECT coalesce(jsonb_agg(m.channel ORDER BY m.channel),'[]') FROM mine m WHERE m.channel_id IS NOT NULL AND NOT m.expired),
  'granted_threads',(SELECT count(*) FROM mine m WHERE m.thread_id IS NOT NULL AND NOT m.expired),
  'grants',(SELECT coalesce(jsonb_agg(jsonb_build_object(
     'channel',m.channel,
     'thread_id',m.thread_id,
     -- A thread grant must also cover the labels of the thread's channel.
     'target_labels',to_jsonb(CASE WHEN m.channel_id IS NOT NULL
       THEN ARRAY(SELECT l.label FROM tl.channel_labels l WHERE l.channel_id=m.channel_id ORDER BY l.label)
       ELSE ARRAY(SELECT x.label FROM (SELECT l.label FROM tl.thread_labels l WHERE l.thread_id=m.thread_id
         UNION SELECT l.label FROM tl.channel_labels l JOIN tl.threads t ON t.channel_id=l.channel_id WHERE t.id=m.thread_id) x ORDER BY x.label) END),
     'allowed_labels',to_jsonb(m.labels),
     'purpose',m.purpose,
     'expires_at',m.expires_at,
     'expired',m.expired) ORDER BY m.channel NULLS LAST,m.thread_id),'[]') FROM mine m),
  'rule','An unrestricted asker reads everything. A restricted asker reads messages, threads, conversations and message locations of granted channels and threads, and links whose message ends and cited evidence messages are all visible. A page revision or derived record is readable only when, in addition, every source it cites is visible (checked through sources of sources to depth 4); a passage only when its revision and its own sources are; the lineage of a record only when the record and all its sources are. Labelled content is denied by default: a row is readable only through an unexpired grant whose allowed labels cover every label of its channel, its thread and the message itself. A conversation is listed, and ranked by meaning, only when every message in it is readable. People, identifiers, facts, relations, entities, the location track and plugins are not readable.',
  'policy_version',api.policy_version())
 FROM me
$$;

-- 6. Catalogue -------------------------------------------------------------------
INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('disclosure',jsonb_build_object(
  'description','Sensitivity labels and disclosure grants, enforced in the database gate for restricted askers. Model judgement never decides what is disclosed; it only phrases what the gate already returned.',
  'labels','Label kinds: intimate, medical, financial, work, third_party_confidence (the owner may add more). A channel or thread carries labels; a message carries one through access_label when that value names a label kind, so labels apply to stored history.',
  'default_deny','A labelled row is hidden from a restricted asker even inside a granted channel, unless an unexpired grant on its channel or thread allows every label of the channel, the thread and the message. The most permissive matching grant decides.',
  'derived','A page revision or derived record citing a labelled message is hidden unless that label is allowed as well (the source rule inherits it).',
  'expiry','A grant with expires_at stops admitting rows at that time; api.policy_version() rises by one when it passes, so saved sets, sessions and investigations made before are refused.',
  'audit','Every label and grant change is recorded with its actor and purpose (owner only).',
  'describe','api.describe_access() lists the asker''s own grants with the labels on each target, the labels each grant allows, purpose and expiry.',
  'functions',jsonb_build_array()))
ON CONFLICT (subject) DO UPDATE SET body=EXCLUDED.body,version=tl.catalog_sections.version+1;

-- 7. Mixed conversations -----------------------------------------------------------
-- A conversation chunk is built from the text of every current message in
-- its thread range, so its token_estimate, text_hash and embedding describe
-- a message the asker may not see (a denied access_label, or a page whose
-- source is hidden). For a restricted asker a chunk counts only when every
-- current message in its range is visible under the row rule: otherwise it
-- is not listed in api.conversations and the meaning legs (api.vector_records,
-- api.search_meaning) do not rank it, so a visible neighbour can never rank
-- for words that only a hidden message holds. Unrestricted askers are not
-- affected. The visible messages of a dropped chunk are still found by the
-- lexical legs.
CREATE FUNCTION api.conversation_visible(thread_id bigint,first_no int,last_no int) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE cid bigint;
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 IF current_setting('throughline.scope_mode',true) IS DISTINCT FROM 'asker' THEN RETURN false; END IF;
 SELECT t.channel_id INTO cid FROM tl.threads t WHERE t.id=conversation_visible.thread_id;
 IF NOT FOUND OR NOT api.asker_sees(cid,conversation_visible.thread_id) THEN RETURN false; END IF;
 RETURN NOT EXISTS(SELECT 1 FROM tl.messages m
  WHERE m.thread_id=conversation_visible.thread_id
   AND m.thread_no BETWEEN conversation_visible.first_no AND conversation_visible.last_no
   AND m.superseded_by IS NULL
   AND NOT coalesce(api.row_visible(m.id,m.channel_id,m.thread_id),false));
END $$;
REVOKE EXECUTE ON FUNCTION api.conversation_visible(bigint,int,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api.conversation_visible(bigint,int,int) TO throughline_app,throughline_reader;

SELECT tl.install_gate('conversations','api.asker_sees((SELECT t.channel_id FROM tl.threads t WHERE t.id=v.thread_id), v.thread_id) AND api.conversation_visible(v.thread_id,v.first_thread_no,v.last_thread_no)');

-- The meaning legs: the ranked chunks pass the same test. Patched in place
-- (their bodies are otherwise those of 0010/0030 and 0038); a body that no
-- longer has the expected shape fails the migration instead of leaving the
-- side channel open.
DO $$
DECLARE
 f record; body text; patched text;
BEGIN
 FOR f IN SELECT * FROM (VALUES
  ('api.search_meaning(vector,integer,bigint,text,timestamp with time zone,timestamp with time zone,text,text,text)'::regprocedure,
   'WHERE EXISTS(SELECT 1 FROM eligible m WHERE m.cid=c.id)'),
  ('api.vector_records(vector,integer,text[],text[],bigint[],text,timestamp with time zone,timestamp with time zone,text,double precision,double precision,double precision,bigint[],boolean,timestamp with time zone,timestamp with time zone,text[],bigint[],bigint[],text,text)'::regprocedure,
   'WHERE EXISTS(SELECT 1 FROM eligible g WHERE g.cid=c.id)')
 ) AS x(fn,anchor) LOOP
  body := pg_get_functiondef(f.fn);
  IF position(f.anchor IN body)=0 OR position('api.conversation_visible' IN body)>0 THEN
   RAISE EXCEPTION '0051: % does not have the expected ranking clause',f.fn;
  END IF;
  patched := replace(body,f.anchor,f.anchor||' AND api.conversation_visible(c.thread_id,c.first_thread_no,c.last_thread_no)');
  EXECUTE patched;
 END LOOP;
END $$;
