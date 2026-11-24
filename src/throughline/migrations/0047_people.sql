-- Person-centric read model (QRY-6, IDN-1, IDN-5,
-- ACL-8, INT-10).
--
-- 1. Attribution guarantee: only a confirmed author identifier sets
--    tl.messages.person_id. Suspected and ignored accounts are excluded from
--    every tool (legacy search, recall, SQL, person functions) until the
--    owner confirms them; names alone are never links. person_id is identity
--    state, not content (tl.immutable lets it change).
-- 2. Exchange index: replies resolved once into tl.exchanges, keyed by
--    external keys so new versions keep their rows.
-- 3. api.appearances: one gated row per current life_event or derived record
--    with the owner side (is_me), direction and attribution status.
-- 4. Person functions: the brief first, then timeline, arc, dms, between,
--    last contact, open loops, interactions, around, day, together,
--    mentions, circle, ties and rhythm. They read gated views only, so a
--    restricted asker's counts, ranks and lists reflect granted rows only;
--    identity data (names, identifiers, facts, relations, speaker
--    annotations, matched terms) is returned to unrestricted askers only.
--
-- Forward-only. Every api function becomes SECURITY DEFINER with a fixed
-- search_path through tl.harden_reader(), so tl objects are schema-qualified.

-- 1. Attribution guarantee ------------------------------------------------------
CREATE INDEX messages_person ON tl.messages(person_id) WHERE person_id IS NOT NULL;
CREATE INDEX messages_author_identifier ON tl.messages(author_identifier_id) WHERE author_identifier_id IS NOT NULL;
CREATE INDEX messages_outgoing ON tl.messages(id) WHERE superseded_by IS NULL AND (fields->>'direction')='outgoing';
CREATE INDEX messages_happened ON tl.messages(happened_at) WHERE superseded_by IS NULL;

-- A message whose author identifier is not confirmed carries no person.
CREATE FUNCTION tl.attribution_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM tl.identifiers i WHERE i.id=NEW.author_identifier_id AND i.status IS DISTINCT FROM 'confirmed') THEN
  NEW.person_id := NULL;
 END IF;
 RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION tl.attribution_guard() FROM PUBLIC;
CREATE TRIGGER attribution_guard BEFORE INSERT OR UPDATE OF person_id,author_identifier_id ON tl.messages
 FOR EACH ROW WHEN (NEW.person_id IS NOT NULL AND NEW.author_identifier_id IS NOT NULL)
 EXECUTE FUNCTION tl.attribution_guard();

-- A status or person change of an identifier re-attributes its messages in
-- one statement: its person when confirmed, else none.
CREATE FUNCTION tl.identifier_reattribute() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 UPDATE tl.messages m SET person_id=CASE WHEN NEW.status='confirmed' THEN NEW.person_id END
 WHERE m.author_identifier_id=NEW.id
  AND m.person_id IS DISTINCT FROM CASE WHEN NEW.status='confirmed' THEN NEW.person_id END;
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.identifier_reattribute() FROM PUBLIC;
CREATE TRIGGER identifier_reattribute AFTER UPDATE OF status,person_id ON tl.identifiers
 FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status OR OLD.person_id IS DISTINCT FROM NEW.person_id)
 EXECUTE FUNCTION tl.identifier_reattribute();

-- Clears person_id where the author identifier is not confirmed and records
-- an identity event with the count (run once below; kept for repair).
CREATE FUNCTION tl.clear_unconfirmed_attribution(actor text DEFAULT 'migration') RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE n bigint;
BEGIN
 WITH cleared AS (
  UPDATE tl.messages m SET person_id=NULL
  FROM tl.identifiers i
  WHERE i.id=m.author_identifier_id AND i.status<>'confirmed' AND m.person_id IS NOT NULL
  RETURNING m.id)
 SELECT count(*) INTO n FROM cleared;
 INSERT INTO tl.identity_events(operation,asserted_by,detail) VALUES ('clear_unconfirmed_attribution',actor,
  jsonb_build_object('messages_cleared',n,'migration','0047_people',
   'rule','only a confirmed author identifier sets messages.person_id; suspected and ignored accounts are excluded until confirmed'));
 RETURN n;
END $$;
REVOKE EXECUTE ON FUNCTION tl.clear_unconfirmed_attribution(text) FROM PUBLIC,throughline_app,throughline_reader;
SELECT tl.clear_unconfirmed_attribution('migration');

-- 2. Exchange index ------------------------------------------------------------
-- Message R responds to message T when an active reply_to link goes from R
-- to T, or R is the next turn after T in the same thread (consecutive
-- thread_no among current, non-ignored versions) within the window
-- (THROUGHLINE_EXCHANGE_WINDOW_S, default 1800 s), and R and T have different
-- authors. Author identity: the owner side is any message with is_me (owner
-- attribution or fields.direction 'outgoing'); otherwise the attributed
-- person when both are attributed, else author_handle. thread_id is R's.
CREATE TABLE tl.exchanges(
 responder_channel_id bigint NOT NULL,
 responder_external_id text NOT NULL,
 target_channel_id bigint NOT NULL,
 target_external_id text NOT NULL,
 thread_id bigint,
 method text NOT NULL CHECK (method IN ('reply','next_turn')),
 gap_s int,
 PRIMARY KEY (responder_channel_id,responder_external_id,target_channel_id,target_external_id));
CREATE INDEX exchanges_target ON tl.exchanges(target_channel_id,target_external_id);
CREATE INDEX exchanges_thread ON tl.exchanges(thread_id);
-- indexed_to: the newest ingested_at the last rebuild saw. dirty: something
-- that decides exchanges changed since (new messages, reply_to links,
-- attribution, ignore annotations, thread order).
CREATE TABLE tl.exchange_state(
 thread_id bigint PRIMARY KEY REFERENCES tl.threads ON DELETE CASCADE,
 indexed_to timestamptz,
 indexed_at timestamptz,
 window_s int,
 dirty boolean NOT NULL DEFAULT true);
CREATE INDEX exchange_state_dirty ON tl.exchange_state(thread_id) WHERE dirty;
GRANT SELECT ON tl.exchanges TO throughline_app;
GRANT SELECT,UPDATE ON tl.exchange_state TO throughline_app;
INSERT INTO tl.exchange_state(thread_id,dirty) SELECT t.id,true FROM tl.threads t ON CONFLICT (thread_id) DO NOTHING;

-- Staleness markers. Rows are locked in thread order so concurrent writers
-- never deadlock on them.
CREATE FUNCTION tl.exchange_mark_inserted() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 INSERT INTO tl.exchange_state AS s(thread_id,dirty)
 SELECT DISTINCT n.thread_id,true FROM new_rows n WHERE n.thread_id IS NOT NULL ORDER BY n.thread_id
 ON CONFLICT (thread_id) DO UPDATE SET dirty=true WHERE NOT s.dirty;
 RETURN NULL;
END $$;
CREATE FUNCTION tl.exchange_mark_updated() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE ids bigint[];
BEGIN
 ids := ARRAY(SELECT DISTINCT x.t FROM (
   SELECT n.thread_id AS t FROM new_rows n JOIN old_rows o ON o.id=n.id
   WHERE n.person_id IS DISTINCT FROM o.person_id OR n.thread_no IS DISTINCT FROM o.thread_no
    OR n.superseded_by IS DISTINCT FROM o.superseded_by OR n.thread_id IS DISTINCT FROM o.thread_id
   UNION
   SELECT o.thread_id FROM new_rows n JOIN old_rows o ON o.id=n.id WHERE n.thread_id IS DISTINCT FROM o.thread_id) x
  WHERE x.t IS NOT NULL ORDER BY 1);
 IF cardinality(ids)>0 THEN
  PERFORM 1 FROM tl.exchange_state s WHERE s.thread_id=ANY(ids) AND NOT s.dirty ORDER BY s.thread_id FOR UPDATE;
  UPDATE tl.exchange_state s SET dirty=true WHERE s.thread_id=ANY(ids) AND NOT s.dirty;
 END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION tl.exchange_mark_messages(message_ids bigint[]) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE ids bigint[];
BEGIN
 ids := ARRAY(SELECT DISTINCT c.thread_id FROM tl.messages m
  JOIN tl.messages c ON c.channel_id=m.channel_id AND c.external_id=m.external_id
  WHERE m.id=ANY(message_ids) AND c.thread_id IS NOT NULL ORDER BY 1);
 IF cardinality(ids)>0 THEN
  PERFORM 1 FROM tl.exchange_state s WHERE s.thread_id=ANY(ids) AND NOT s.dirty ORDER BY s.thread_id FOR UPDATE;
  UPDATE tl.exchange_state s SET dirty=true WHERE s.thread_id=ANY(ids) AND NOT s.dirty;
 END IF;
END $$;
CREATE FUNCTION tl.exchange_mark_link() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 IF TG_OP IN ('UPDATE','DELETE') AND OLD.from_kind='message' THEN
  PERFORM tl.exchange_mark_messages(ARRAY[OLD.from_id]);
 END IF;
 IF TG_OP IN ('INSERT','UPDATE') AND NEW.from_kind='message' THEN
  PERFORM tl.exchange_mark_messages(ARRAY[NEW.from_id]);
 END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION tl.exchange_mark_annotation() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 PERFORM tl.exchange_mark_messages(ARRAY(SELECT m.id FROM tl.messages m
  WHERE m.channel_id=NEW.target_channel_id AND m.external_id=NEW.target_external_id AND m.superseded_by IS NULL));
 RETURN NULL;
END $$;
CREATE FUNCTION tl.exchange_mark_owner() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
BEGIN
 -- Who the owner is decides is_me, so every thread is re-indexed.
 UPDATE tl.exchange_state s SET dirty=true WHERE NOT s.dirty;
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.exchange_mark_inserted(),tl.exchange_mark_updated(),tl.exchange_mark_messages(bigint[]),tl.exchange_mark_link(),tl.exchange_mark_annotation(),tl.exchange_mark_owner() FROM PUBLIC;
CREATE TRIGGER exchange_mark_inserted AFTER INSERT ON tl.messages REFERENCING NEW TABLE AS new_rows
 FOR EACH STATEMENT EXECUTE FUNCTION tl.exchange_mark_inserted();
CREATE TRIGGER exchange_mark_updated AFTER UPDATE ON tl.messages REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
 FOR EACH STATEMENT EXECUTE FUNCTION tl.exchange_mark_updated();
CREATE TRIGGER exchange_mark_link_insert AFTER INSERT ON tl.links
 FOR EACH ROW WHEN (NEW.type='reply_to') EXECUTE FUNCTION tl.exchange_mark_link();
CREATE TRIGGER exchange_mark_link_update AFTER UPDATE ON tl.links
 FOR EACH ROW WHEN ((NEW.type='reply_to' OR OLD.type='reply_to') AND (NEW.type IS DISTINCT FROM OLD.type OR NEW.status IS DISTINCT FROM OLD.status OR NEW.from_id IS DISTINCT FROM OLD.from_id OR NEW.to_id IS DISTINCT FROM OLD.to_id))
 EXECUTE FUNCTION tl.exchange_mark_link();
CREATE TRIGGER exchange_mark_link_delete AFTER DELETE ON tl.links
 FOR EACH ROW WHEN (OLD.type='reply_to') EXECUTE FUNCTION tl.exchange_mark_link();
CREATE TRIGGER exchange_mark_annotation AFTER INSERT OR UPDATE OF status ON tl.annotations
 FOR EACH ROW WHEN (NEW.attribute IN ('ignore','media_noise')) EXECUTE FUNCTION tl.exchange_mark_annotation();
CREATE TRIGGER exchange_mark_owner AFTER UPDATE OF is_owner,merged_into ON tl.people
 FOR EACH ROW WHEN (OLD.is_owner IS DISTINCT FROM NEW.is_owner OR (NEW.is_owner AND OLD.merged_into IS DISTINCT FROM NEW.merged_into))
 EXECUTE FUNCTION tl.exchange_mark_owner();

-- Rebuild one thread's exchanges set-based (the caller commits per thread).
-- Returns the number of exchanges written.
CREATE FUNCTION tl.rebuild_exchanges(thread bigint,window_s int DEFAULT 1800) RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE hw timestamptz; n int := 0; o bigint; w int := greatest(coalesce(rebuild_exchanges.window_s,1800),0);
BEGIN
 IF NOT EXISTS(SELECT 1 FROM tl.threads t WHERE t.id=rebuild_exchanges.thread) THEN
  DELETE FROM tl.exchanges e WHERE e.thread_id=rebuild_exchanges.thread;
  RETURN 0;
 END IF;
 INSERT INTO tl.exchange_state(thread_id,dirty) VALUES (rebuild_exchanges.thread,true) ON CONFLICT (thread_id) DO NOTHING;
 -- Holding the state row makes a concurrent ingest mark the thread dirty
 -- again only after this rebuild commits.
 PERFORM 1 FROM tl.exchange_state s WHERE s.thread_id=rebuild_exchanges.thread FOR UPDATE;
 o := (SELECT p.id FROM tl.people p WHERE p.is_owner AND p.merged_into IS NULL ORDER BY p.id LIMIT 1);
 SELECT max(m.ingested_at) INTO hw FROM tl.messages m WHERE m.thread_id=rebuild_exchanges.thread;
 DELETE FROM tl.exchanges e WHERE e.thread_id=rebuild_exchanges.thread;
 WITH cur AS MATERIALIZED (
  SELECT m.id,m.channel_id,m.external_id,m.thread_no,m.happened_at,m.person_id,m.author_handle,
   coalesce(m.person_id=o,false) OR coalesce(m.fields->>'direction'='outgoing',false) AS is_me
  FROM tl.messages m
  WHERE m.thread_id=rebuild_exchanges.thread AND m.superseded_by IS NULL
   AND NOT CASE WHEN (m.fields->>'ignore_reason') IS NOT NULL OR EXISTS(SELECT 1 FROM tl.annotations a
     WHERE a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id AND a.status='active')
    THEN coalesce((SELECT s.ignored FROM tl.overlay_state(m.channel_id,m.external_id,m.version,m.fields,NULL) s),false)
    ELSE false END),
 seq AS (
  SELECT c.*,lag(c.id) OVER (ORDER BY c.thread_no NULLS LAST,c.happened_at,c.id) AS prev_id FROM cur c),
 turns AS (
  SELECT r.channel_id AS rc,r.external_id AS rx,t.channel_id AS tc,t.external_id AS tx,'next_turn'::text AS method,
   extract(epoch FROM r.happened_at-t.happened_at) AS gap
  FROM seq r JOIN cur t ON t.id=r.prev_id
  WHERE r.happened_at>=t.happened_at AND r.happened_at-t.happened_at<=make_interval(secs=>w)
   AND NOT CASE WHEN r.is_me OR t.is_me THEN r.is_me AND t.is_me
    WHEN r.person_id IS NOT NULL AND t.person_id IS NOT NULL THEN r.person_id=t.person_id
    ELSE r.author_handle IS NOT DISTINCT FROM t.author_handle END),
 targets AS (
  SELECT DISTINCT r.id AS rid,tc.id AS tid
  FROM cur r
  JOIN tl.messages rv ON rv.channel_id=r.channel_id AND rv.external_id=r.external_id
  JOIN tl.links l ON l.from_kind='message' AND l.from_id=rv.id AND l.to_kind='message' AND l.type='reply_to' AND l.status='active'
  JOIN tl.messages tv ON tv.id=l.to_id
  JOIN tl.messages tc ON tc.channel_id=tv.channel_id AND tc.external_id=tv.external_id AND tc.superseded_by IS NULL),
 replies AS (
  SELECT r.channel_id AS rc,r.external_id AS rx,t.channel_id AS tc,t.external_id AS tx,'reply'::text AS method,
   extract(epoch FROM r.happened_at-t.happened_at) AS gap
  FROM targets x
  JOIN cur r ON r.id=x.rid
  JOIN tl.messages t ON t.id=x.tid
  CROSS JOIN LATERAL (SELECT coalesce(t.person_id=o,false) OR coalesce(t.fields->>'direction'='outgoing',false) AS is_me,
   CASE WHEN (t.fields->>'ignore_reason') IS NOT NULL OR EXISTS(SELECT 1 FROM tl.annotations a
     WHERE a.target_channel_id=t.channel_id AND a.target_external_id=t.external_id AND a.status='active')
    THEN coalesce((SELECT s.ignored FROM tl.overlay_state(t.channel_id,t.external_id,t.version,t.fields,NULL) s),false)
    ELSE false END AS ignored) ts
  WHERE NOT ts.ignored AND t.id<>r.id
   AND NOT CASE WHEN r.is_me OR ts.is_me THEN r.is_me AND ts.is_me
    WHEN r.person_id IS NOT NULL AND t.person_id IS NOT NULL THEN r.person_id=t.person_id
    ELSE r.author_handle IS NOT DISTINCT FROM t.author_handle END),
 found AS (
  SELECT DISTINCT ON (z.rc,z.rx,z.tc,z.tx) z.* FROM (SELECT * FROM replies UNION ALL SELECT * FROM turns) z
  ORDER BY z.rc,z.rx,z.tc,z.tx,(z.method='reply') DESC)
 INSERT INTO tl.exchanges(responder_channel_id,responder_external_id,target_channel_id,target_external_id,thread_id,method,gap_s)
 SELECT f.rc,f.rx,f.tc,f.tx,rebuild_exchanges.thread,f.method,greatest(least(f.gap,2147483647),-2147483647)::int FROM found f
 ON CONFLICT (responder_channel_id,responder_external_id,target_channel_id,target_external_id)
 DO UPDATE SET thread_id=EXCLUDED.thread_id,method=EXCLUDED.method,gap_s=EXCLUDED.gap_s;
 GET DIAGNOSTICS n = ROW_COUNT;
 UPDATE tl.exchange_state s SET indexed_to=hw,indexed_at=now(),window_s=w,dirty=false WHERE s.thread_id=rebuild_exchanges.thread;
 RETURN n;
END $$;
REVOKE EXECUTE ON FUNCTION tl.rebuild_exchanges(bigint,int) FROM PUBLIC,throughline_reader;
GRANT EXECUTE ON FUNCTION tl.rebuild_exchanges(bigint,int) TO throughline_app;

-- 3. api.appearances --------------------------------------------------------------
-- The overlay of one visible message (effective time, ignored, speaker), for
-- the appearances view; NULL for a message the asker may not see.
CREATE FUNCTION api.appearance_overlay(message_id bigint) RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
DECLARE m record;
BEGIN
 IF NOT api.message_visible(appearance_overlay.message_id) THEN RETURN NULL; END IF;
 SELECT x.channel_id,x.external_id,x.version,x.fields INTO m FROM tl.messages x WHERE x.id=appearance_overlay.message_id;
 IF NOT FOUND THEN RETURN NULL; END IF;
 RETURN tl.overlay_fold((
  SELECT jsonb_agg(jsonb_build_object('attribute',a.attribute,'value',a.value,'method',a.method,'created_at',a.created_at,'id',a.id,'reason',a.reason))
  FROM tl.annotations a
  WHERE a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id AND a.status='active'
   AND (a.target_version IS NULL OR a.target_version=m.version)),
  CASE WHEN jsonb_typeof(m.fields->'ignore_reason')='string' THEN m.fields->>'ignore_reason' END);
END $$;

-- party_id is the id every person function uses to place a message on a
-- side: the owner's person id when is_me, else the confirmed attribution.
SELECT tl.install_gate('appearances','api.row_visible(v.message_id,v.channel_id,v.thread_id)',$def$
SELECT m.id AS message_id,
 m.version,
 ch.name AS channel,
 m.thread_id,
 m.thread_no,
 CASE WHEN ch.conversation_whole_thread THEN 'turn' ELSE 'message' END AS kind,
 m.happened_at AS record_time,
 coalesce((ov.f->>'effective_time')::timestamptz,m.happened_at) AS effective_time,
 m.person_id,
 CASE WHEN s.is_me THEN o.id ELSE m.person_id END AS party_id,
 CASE WHEN (SELECT api.asker_unrestricted()) THEN (ov.f->>'speaker_person_id')::bigint END AS annotated_person_id,
 m.author_handle,
 s.is_me,
 CASE WHEN s.is_me THEN 'outgoing' WHEN m.person_id IS NOT NULL THEN 'incoming' WHEN s.dir IS NOT NULL THEN s.dir ELSE 'unknown' END AS direction,
 CASE WHEN m.person_id IS NOT NULL THEN 'confirmed'
  WHEN s.is_me THEN 'self_direction'
  WHEN (ov.f->>'speaker_person_id') IS NOT NULL AND (SELECT api.asker_unrestricted()) THEN 'annotated'
  WHEN i.status='suspected' THEN 'suspected'
  ELSE 'unknown' END AS attribution_status,
 coalesce(CASE WHEN jsonb_typeof(m.fields->'identity_quality')='string' THEN m.fields->>'identity_quality' END,
  CASE WHEN i.status='confirmed' THEN 'identifier' END,'unknown') AS identity_quality,
 coalesce(CASE WHEN jsonb_typeof(m.fields->'time_precision')='string' THEN m.fields->>'time_precision' END,
  m.subject_from_precision,'exact') AS time_precision,
 CASE WHEN jsonb_typeof(m.fields->'conversation_kind')='string' THEN m.fields->>'conversation_kind' END AS conversation_kind,
 coalesce((ov.f->>'ignored')::boolean,false) AS ignored,
 m.channel_id,
 m.external_id
FROM tl.messages m
JOIN tl.channels ch ON ch.id=m.channel_id
LEFT JOIN tl.identifiers i ON i.id=m.author_identifier_id
LEFT JOIN (SELECT p.id FROM tl.people p WHERE p.is_owner AND p.merged_into IS NULL ORDER BY p.id LIMIT 1) o ON true
CROSS JOIN LATERAL (SELECT CASE WHEN jsonb_typeof(m.fields->'direction')='string' THEN m.fields->>'direction' END AS dir) d
CROSS JOIN LATERAL (SELECT coalesce(m.person_id=o.id,false) OR coalesce(d.dir='outgoing',false) AS is_me,d.dir) s
CROSS JOIN LATERAL (SELECT CASE WHEN (m.fields->>'ignore_reason') IS NOT NULL OR EXISTS(SELECT 1 FROM tl.annotations a
   WHERE a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id AND a.status='active')
  THEN api.appearance_overlay(m.id) END AS f) ov
WHERE m.superseded_by IS NULL AND ch.record_kind IN ('life_event','derived')
$def$);

-- Attribution of messages (any version: the current version of the same
-- source key answers). Rows only for visible life_event and derived records.
CREATE FUNCTION api.attribution(message_ids bigint[])
RETURNS TABLE(message_id bigint,is_me boolean,direction text,attribution_status text,identity_quality text,time_precision text)
LANGUAGE sql STABLE AS $$
 WITH k AS MATERIALIZED (
  SELECT x.id,c.id AS cur_id
  FROM tl.messages x
  JOIN tl.messages c ON c.channel_id=x.channel_id AND c.external_id=x.external_id AND c.superseded_by IS NULL
  WHERE x.id=ANY(attribution.message_ids)),
 a AS MATERIALIZED (
  SELECT ap.message_id,ap.is_me,ap.direction,ap.attribution_status,ap.identity_quality,ap.time_precision
  FROM api.appearances ap WHERE ap.message_id=ANY(ARRAY(SELECT k.cur_id FROM k)))
 SELECT k.id,a.is_me,a.direction,a.attribution_status,a.identity_quality,a.time_precision
 FROM k JOIN a ON a.message_id=k.cur_id
 ORDER BY k.id $$;

-- The suspected person behind messages whose author identifier is suspected
-- (unrestricted askers only; identity data).
CREATE FUNCTION api.suspected_attributions(message_ids bigint[])
RETURNS TABLE(message_id bigint,identifier_id bigint,suspected_person_id bigint,system text,kind text)
LANGUAGE sql STABLE AS $$
 SELECT a.message_id,i.id,i.person_id,i.system,i.kind
 FROM api.appearances a
 JOIN tl.messages m ON m.id=a.message_id
 JOIN tl.identifiers i ON i.id=m.author_identifier_id
 WHERE (SELECT api.asker_unrestricted()) AND a.message_id=ANY(suspected_attributions.message_ids) AND i.status='suspected'
 ORDER BY a.message_id $$;

-- 4. Helpers (owner-side only; called by the api functions) ---------------------------
CREATE FUNCTION tl.owner_id() RETURNS bigint LANGUAGE sql STABLE AS $$
 SELECT p.id FROM tl.people p WHERE p.is_owner AND p.merged_into IS NULL ORDER BY p.id LIMIT 1 $$;

-- Visible, non-ignored appearances whose party is one of persons.
CREATE FUNCTION tl.party_rows(persons bigint[]) RETURNS SETOF api.appearances LANGUAGE sql STABLE AS $$
 SELECT a.* FROM api.appearances a
 WHERE a.message_id=ANY(ARRAY(
   SELECT m.id FROM tl.messages m WHERE m.superseded_by IS NULL AND m.person_id=ANY(party_rows.persons)
   UNION
   SELECT m.id FROM tl.messages m WHERE (SELECT tl.owner_id())=ANY(party_rows.persons)
    AND m.superseded_by IS NULL AND (m.fields->>'direction')='outgoing'))
  AND a.party_id=ANY(party_rows.persons) AND NOT a.ignored $$;

-- Visible, non-ignored appearances of threads.
CREATE FUNCTION tl.thread_rows(thread_ids bigint[]) RETURNS SETOF api.appearances LANGUAGE sql STABLE AS $$
 SELECT a.* FROM api.appearances a
 WHERE a.message_id=ANY(ARRAY(SELECT m.id FROM tl.messages m WHERE m.thread_id=ANY(thread_rows.thread_ids) AND m.superseded_by IS NULL))
  AND NOT a.ignored $$;

-- Visible exchanges touching messages (either end), both ends visible and
-- not ignored, with the parties of both ends.
CREATE FUNCTION tl.exchanges_of(message_ids bigint[])
RETURNS TABLE(responder_id bigint,target_id bigint,responder_party bigint,target_party bigint,responder_time timestamptz,target_time timestamptz,thread_id bigint,channel text,method text,gap_s int)
LANGUAGE sql STABLE AS $$
 WITH k AS MATERIALIZED (SELECT DISTINCT m.channel_id,m.external_id FROM tl.messages m WHERE m.id=ANY(exchanges_of.message_ids)),
 e AS MATERIALIZED (
  SELECT x.* FROM k JOIN tl.exchanges x ON x.responder_channel_id=k.channel_id AND x.responder_external_id=k.external_id
  UNION
  SELECT x.* FROM k JOIN tl.exchanges x ON x.target_channel_id=k.channel_id AND x.target_external_id=k.external_id),
 ends AS MATERIALIZED (
  SELECT m.id FROM (SELECT e.responder_channel_id AS c,e.responder_external_id AS x FROM e
   UNION SELECT e.target_channel_id,e.target_external_id FROM e) z
  JOIN tl.messages m ON m.channel_id=z.c AND m.external_id=z.x AND m.superseded_by IS NULL),
 a AS MATERIALIZED (
  SELECT ap.message_id,ap.channel_id,ap.external_id,ap.party_id,ap.effective_time,ap.channel
  FROM api.appearances ap WHERE ap.message_id=ANY(ARRAY(SELECT ends.id FROM ends)) AND NOT ap.ignored)
 SELECT r.message_id,t.message_id,r.party_id,t.party_id,r.effective_time,t.effective_time,e.thread_id,r.channel,e.method,e.gap_s
 FROM e
 JOIN a r ON r.channel_id=e.responder_channel_id AND r.external_id=e.responder_external_id
 JOIN a t ON t.channel_id=e.target_channel_id AND t.external_id=e.target_external_id $$;

-- Threads where every listed person has a visible, non-ignored message by
-- party_id.
CREATE FUNCTION tl.shared_threads(persons bigint[]) RETURNS bigint[] LANGUAGE sql STABLE AS $$
 WITH p AS (SELECT DISTINCT x FROM unnest(shared_threads.persons) x WHERE x IS NOT NULL)
 SELECT coalesce(array_agg(z.thread_id ORDER BY z.thread_id),'{}'::bigint[]) FROM (
  SELECT a.thread_id FROM tl.party_rows(ARRAY(SELECT p.x FROM p)) a WHERE a.thread_id IS NOT NULL
  GROUP BY a.thread_id HAVING count(DISTINCT a.party_id)=(SELECT count(*) FROM p)) z $$;

-- One-to-one threads of a person with the owner: a thread with a message of
-- the person whose conversation_kind is 'dm', or whose party ids are exactly
-- {owner, person}.
CREATE FUNCTION tl.dm_threads(person bigint) RETURNS bigint[] LANGUAGE sql STABLE AS $$
 WITH o AS MATERIALIZED (SELECT tl.owner_id() AS id),
 mine AS MATERIALIZED (SELECT DISTINCT a.thread_id FROM tl.party_rows(ARRAY[dm_threads.person]) a WHERE a.thread_id IS NOT NULL),
 t AS (
  SELECT r.thread_id,bool_or(r.conversation_kind='dm') AS dm,
   array_agg(DISTINCT r.party_id) FILTER (WHERE r.party_id IS NOT NULL) AS parties
  FROM tl.thread_rows(ARRAY(SELECT mine.thread_id FROM mine)) r GROUP BY r.thread_id)
 SELECT coalesce(array_agg(t.thread_id ORDER BY t.thread_id),'{}'::bigint[]) FROM t CROSS JOIN o
 WHERE dm_threads.person IS DISTINCT FROM o.id
  AND (coalesce(t.dm,false) OR (o.id IS NOT NULL AND t.parties @> ARRAY[o.id,dm_threads.person] AND t.parties <@ ARRAY[o.id,dm_threads.person])) $$;

-- Every record that places a person in contact, over the whole history:
--  side 'from'      a message by the person (party_id)
--  side 'to'        an owner-side message in the person's one-to-one threads,
--                   or a message by someone else that responds to the person
--  side 'exchange'  one exchange with the person on either end
--                   (message_id = responder, target_id = target)
CREATE FUNCTION tl.contact_rows(person bigint)
RETURNS TABLE(message_id bigint,side text,effective_time timestamptz,thread_id bigint,channel text,party_id bigint,other_party bigint,target_id bigint)
LANGUAGE sql STABLE AS $$
 WITH o AS MATERIALIZED (SELECT tl.owner_id() AS id),
 f AS MATERIALIZED (SELECT a.* FROM tl.party_rows(ARRAY[contact_rows.person]) a),
 th AS MATERIALIZED (SELECT r.* FROM tl.thread_rows(ARRAY(SELECT DISTINCT f.thread_id FROM f WHERE f.thread_id IS NOT NULL)) r),
 dm AS MATERIALIZED (
  SELECT th.thread_id FROM th CROSS JOIN o
  WHERE contact_rows.person IS DISTINCT FROM o.id
  GROUP BY th.thread_id,o.id
  HAVING coalesce(bool_or(th.conversation_kind='dm'),false)
   OR (o.id IS NOT NULL
    AND array_agg(DISTINCT th.party_id) FILTER (WHERE th.party_id IS NOT NULL) @> ARRAY[o.id,contact_rows.person]
    AND array_agg(DISTINCT th.party_id) FILTER (WHERE th.party_id IS NOT NULL) <@ ARRAY[o.id,contact_rows.person])),
 x AS MATERIALIZED (
  SELECT e.* FROM tl.exchanges_of(ARRAY(SELECT f.message_id FROM f)) e
  WHERE (e.responder_party=contact_rows.person OR e.target_party=contact_rows.person)
   AND e.responder_party IS DISTINCT FROM e.target_party),
 t AS (
  SELECT th.message_id,th.effective_time,th.thread_id,th.channel,th.party_id
  FROM th JOIN dm ON dm.thread_id=th.thread_id WHERE th.is_me AND th.party_id IS DISTINCT FROM contact_rows.person
  UNION
  SELECT x.responder_id,x.responder_time,x.thread_id,x.channel,x.responder_party
  FROM x WHERE x.target_party=contact_rows.person)
 SELECT f.message_id,'from'::text,f.effective_time,f.thread_id,f.channel,f.party_id,NULL::bigint,NULL::bigint FROM f
 UNION ALL
 SELECT t.message_id,'to'::text,t.effective_time,t.thread_id,t.channel,t.party_id,NULL::bigint,NULL::bigint FROM t
 UNION ALL
 SELECT x.responder_id,'exchange'::text,x.responder_time,x.thread_id,x.channel,x.responder_party,
  CASE WHEN x.responder_party=contact_rows.person THEN x.target_party ELSE x.responder_party END,x.target_id FROM x $$;

-- Episodes (the grain of api.episodes_of: gap-based sessions of a thread by
-- the channel's conversation_gap_s, a whole-thread channel being one; ids
-- 'ep:<thread>:<first message>') of every current visible message of threads.
CREATE FUNCTION tl.episodes_in(thread_ids bigint[])
RETURNS TABLE(message_id bigint,episode_id text,thread_id bigint,started_at timestamptz,ended_at timestamptz)
LANGUAGE sql STABLE AS $$
 WITH ordered AS (
  SELECT v.id,v.thread_id,v.thread_no,v.happened_at,coalesce(v.ended_at,v.happened_at) AS last_at,
   c.conversation_gap_s,c.conversation_whole_thread,
   lag(v.happened_at) OVER (PARTITION BY v.thread_id ORDER BY v.thread_no,v.happened_at,v.id) AS prev
  FROM api.message_versions v JOIN tl.channels c ON c.id=v.channel_id
  WHERE v.superseded_by IS NULL AND v.thread_id=ANY(episodes_in.thread_ids)),
 grouped AS (
  SELECT o.*,sum(CASE WHEN o.prev IS NULL OR (NOT o.conversation_whole_thread AND o.happened_at-o.prev>make_interval(secs=>o.conversation_gap_s)) THEN 1 ELSE 0 END)
   OVER (PARTITION BY o.thread_id ORDER BY o.thread_no,o.happened_at,o.id) AS grp
  FROM ordered o),
 sessions AS (
  SELECT g.thread_id,g.grp,(array_agg(g.id ORDER BY g.thread_no,g.happened_at,g.id))[1] AS first_id,
   min(g.happened_at) AS s_at,max(g.last_at) AS e_at
  FROM grouped g GROUP BY g.thread_id,g.grp)
 SELECT g.id,'ep:'||g.thread_id||':'||s.first_id,g.thread_id,s.s_at,s.e_at
 FROM grouped g JOIN sessions s ON s.thread_id=g.thread_id AND s.grp=g.grp $$;

-- Per episode of the threads, the parties with messages in the window.
CREATE FUNCTION tl.episode_parties(thread_ids bigint[],after_at timestamptz,before_at timestamptz)
RETURNS TABLE(episode_id text,thread_id bigint,channel text,started_at timestamptz,ended_at timestamptz,party_id bigint,messages bigint)
LANGUAGE sql STABLE AS $$
 WITH ep AS MATERIALIZED (SELECT * FROM tl.episodes_in(episode_parties.thread_ids)),
 r AS MATERIALIZED (
  SELECT a.message_id,a.thread_id,a.channel,a.party_id FROM tl.thread_rows(episode_parties.thread_ids) a
  WHERE a.party_id IS NOT NULL
   AND (episode_parties.after_at IS NULL OR a.effective_time>=episode_parties.after_at)
   AND (episode_parties.before_at IS NULL OR a.effective_time<episode_parties.before_at))
 SELECT ep.episode_id,r.thread_id,min(r.channel),min(ep.started_at),max(ep.ended_at),r.party_id,count(*)
 FROM r JOIN ep ON ep.message_id=r.message_id
 GROUP BY ep.episode_id,r.thread_id,r.party_id $$;

CREATE FUNCTION tl.people_period_label(grain text,p timestamp) RETURNS text LANGUAGE sql IMMUTABLE AS $$
 SELECT CASE people_period_label.grain
  WHEN 'day' THEN to_char(people_period_label.p,'YYYY-MM-DD')
  WHEN 'week' THEN to_char(people_period_label.p,'IYYY-"W"IW')
  WHEN 'month' THEN to_char(people_period_label.p,'YYYY-MM')
  WHEN 'quarter' THEN to_char(people_period_label.p,'YYYY-"Q"Q')
  ELSE to_char(people_period_label.p,'YYYY') END $$;

CREATE FUNCTION tl.people_timezone(tz text) RETURNS text LANGUAGE sql STABLE AS $$
 SELECT coalesce(nullif(btrim(people_timezone.tz),''),nullif(current_setting('throughline.timezone',true),''),'UTC') $$;

-- Default lexicons (POSIX regular expressions, case-insensitive). Routes pass
-- THROUGHLINE_ASK_PATTERNS / THROUGHLINE_CLOSING_PATTERNS when configured.
CREATE FUNCTION tl.default_ask_patterns() RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
 SELECT ARRAY['\m(can|could|would|will) you\M','\mplease\M','\mlet me know\M','\many (update|news)\M',
  '\mwhen (can|will|could) you\M','\mremind me\M','\mdo you (have|know|want|mind)\M','\mwhat do you think\M',
  '\mare you (free|around|available)\M','\mget back to me\M','\mcan we\M']::text[] $$;
CREATE FUNCTION tl.default_closing_patterns() RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
 SELECT ARRAY['^\W*(thanks|thank you|thx|ty|ok|okay|k|kk|cool|great|got it|no worries|nvm|sounds good|perfect|all good|no problem|np|done)\M',
  '\mnever ?mind\M','\mforget (it|about it)\M','\mall sorted\M','\mno need\M']::text[] $$;

REVOKE EXECUTE ON FUNCTION tl.owner_id(),tl.party_rows(bigint[]),tl.thread_rows(bigint[]),tl.exchanges_of(bigint[]),
 tl.shared_threads(bigint[]),tl.dm_threads(bigint),tl.contact_rows(bigint),tl.episodes_in(bigint[]),
 tl.episode_parties(bigint[],timestamptz,timestamptz),tl.people_period_label(text,timestamp),tl.people_timezone(text),
 tl.default_ask_patterns(),tl.default_closing_patterns() FROM PUBLIC;

-- 5. Person functions -----------------------------------------------------------------
-- Times are effective times (the overlay's effective time, else record
-- time); after_at is inclusive, before_at exclusive. max_results NULL means
-- no limit. Ignored records are excluded.

-- The index state of the threads you may read: stale_threads still need a
-- rebuild (routes refresh within a budget first and disclose the rest).
CREATE FUNCTION api.exchange_index_status()
RETURNS TABLE(stale_threads bigint,indexed_threads bigint,indexed_to timestamptz,indexed_at timestamptz,window_s int)
LANGUAGE sql STABLE AS $$
 SELECT count(*) FILTER (WHERE s.thread_id IS NULL OR s.dirty OR s.indexed_to IS NULL),
  count(*) FILTER (WHERE s.thread_id IS NOT NULL AND NOT s.dirty AND s.indexed_to IS NOT NULL),
  min(s.indexed_to) FILTER (WHERE NOT s.dirty),
  max(s.indexed_at),
  max(s.window_s)
 FROM api.threads t
 LEFT JOIN tl.exchange_state s ON s.thread_id=t.id
 WHERE EXISTS(SELECT 1 FROM tl.messages m WHERE m.thread_id=t.id) $$;

CREATE FUNCTION api.person_timeline(person_id bigint,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,max_results int DEFAULT 200)
RETURNS TABLE(message_id bigint,side text,effective_time timestamptz,record_time timestamptz,channel text,thread_id bigint,thread_no int,
 party_id bigint,is_me boolean,direction text,attribution_status text,content text)
LANGUAGE sql STABLE AS $$
 WITH c AS MATERIALIZED (
  SELECT DISTINCT ON (r.message_id) r.message_id,r.side FROM tl.contact_rows(person_timeline.person_id) r
  WHERE r.side IN ('from','to')
   AND (person_timeline.after_at IS NULL OR r.effective_time>=person_timeline.after_at)
   AND (person_timeline.before_at IS NULL OR r.effective_time<person_timeline.before_at)
  ORDER BY r.message_id,r.side)
 SELECT a.message_id,c.side,a.effective_time,a.record_time,a.channel,a.thread_id,a.thread_no,a.party_id,a.is_me,a.direction,a.attribution_status,m.content
 FROM api.appearances a
 JOIN c ON c.message_id=a.message_id
 JOIN tl.messages m ON m.id=a.message_id
 WHERE a.message_id=ANY(ARRAY(SELECT c.message_id FROM c))
 ORDER BY a.effective_time DESC,a.message_id DESC
 LIMIT CASE WHEN person_timeline.max_results IS NULL THEN NULL ELSE greatest(person_timeline.max_results,0) END $$;

-- Contact arc: per period (UTC) messages from and to the person and exchanges
-- over the whole history, with the first, peak (most messages from + to,
-- earliest on ties) and last periods and the days since the last contact.
CREATE FUNCTION api.person_arc(person_id bigint,grain text DEFAULT 'month')
RETURNS TABLE(period text,period_start timestamptz,messages_from bigint,messages_to bigint,exchanges bigint,
 first_period text,peak_period text,last_period text,last_contact_at timestamptz,gap_days numeric)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE step interval;
BEGIN
 IF person_arc.grain IS NULL OR person_arc.grain NOT IN ('week','month','quarter','year') THEN
  RAISE EXCEPTION 'grain must be week, month, quarter or year' USING ERRCODE='invalid_parameter_value';
 END IF;
 step := CASE person_arc.grain WHEN 'week' THEN interval '1 week' WHEN 'month' THEN interval '1 month'
  WHEN 'quarter' THEN interval '3 months' ELSE interval '1 year' END;
 RETURN QUERY
 WITH r AS MATERIALIZED (
  SELECT x.side,x.message_id,x.effective_time,date_trunc(person_arc.grain,x.effective_time AT TIME ZONE 'UTC') AS p
  FROM tl.contact_rows(person_arc.person_id) x),
 b AS (SELECT min(r.p) AS lo,max(r.p) AS hi,max(r.effective_time) FILTER (WHERE r.side IN ('from','to')) AS last_at FROM r),
 series AS (SELECT g AS p FROM b CROSS JOIN LATERAL generate_series(b.lo,b.hi,step) g WHERE b.lo IS NOT NULL),
 counts AS (
  SELECT s.p,
   count(DISTINCT r.message_id) FILTER (WHERE r.side='from') AS f,
   count(DISTINCT r.message_id) FILTER (WHERE r.side='to') AS t,
   count(*) FILTER (WHERE r.side='exchange') AS e
  FROM series s LEFT JOIN r ON r.p=s.p GROUP BY s.p),
 peak AS (SELECT c.p FROM counts c ORDER BY c.f+c.t DESC,c.e DESC,c.p LIMIT 1)
 SELECT tl.people_period_label(person_arc.grain,c.p),c.p AT TIME ZONE 'UTC',c.f,c.t,c.e,
  tl.people_period_label(person_arc.grain,b.lo),
  tl.people_period_label(person_arc.grain,(SELECT peak.p FROM peak)),
  tl.people_period_label(person_arc.grain,b.hi),
  b.last_at,
  round((extract(epoch FROM now()-b.last_at)/86400)::numeric,2)
 FROM counts c CROSS JOIN b
 ORDER BY c.p;
END $$;

-- Both sides of the person's one-to-one threads with the owner.
CREATE FUNCTION api.dms_with(person_id bigint,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,max_results int DEFAULT 200)
RETURNS TABLE(message_id bigint,side text,effective_time timestamptz,channel text,thread_id bigint,thread_no int,
 party_id bigint,is_me boolean,direction text,attribution_status text,content text)
LANGUAGE sql STABLE AS $$
 SELECT a.message_id,
  CASE WHEN a.party_id=dms_with.person_id THEN 'from' WHEN a.is_me THEN 'to' ELSE 'other' END,
  a.effective_time,a.channel,a.thread_id,a.thread_no,a.party_id,a.is_me,a.direction,a.attribution_status,m.content
 FROM tl.thread_rows(tl.dm_threads(dms_with.person_id)) a
 JOIN tl.messages m ON m.id=a.message_id
 WHERE (dms_with.after_at IS NULL OR a.effective_time>=dms_with.after_at)
  AND (dms_with.before_at IS NULL OR a.effective_time<dms_with.before_at)
 ORDER BY a.effective_time DESC,a.message_id DESC
 LIMIT CASE WHEN dms_with.max_results IS NULL THEN NULL ELSE greatest(dms_with.max_results,0) END $$;

-- Threads where every listed person (at least two) has a message by party_id.
CREATE FUNCTION api.between_threads(persons bigint[])
RETURNS TABLE(thread_id bigint,channel text,messages bigint,last_at timestamptz)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE p bigint[] := ARRAY(SELECT DISTINCT x FROM unnest($1) x WHERE x IS NOT NULL ORDER BY 1);
BEGIN
 IF cardinality(p)<2 THEN
  RAISE EXCEPTION 'between needs at least two persons' USING ERRCODE='invalid_parameter_value';
 END IF;
 RETURN QUERY
 SELECT a.thread_id,min(a.channel),count(*),max(a.effective_time)
 FROM tl.thread_rows(tl.shared_threads(p)) a
 WHERE a.party_id=ANY(p)
 GROUP BY a.thread_id
 ORDER BY a.thread_id;
END $$;

-- Dyad (or group) scope: messages by ANY listed person in the threads where
-- every listed person has a message, so both sides come back (never
-- author-only).
CREATE FUNCTION api.between(persons bigint[],after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,max_results int DEFAULT 200)
RETURNS TABLE(message_id bigint,effective_time timestamptz,channel text,thread_id bigint,thread_no int,
 party_id bigint,is_me boolean,direction text,attribution_status text,content text)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE p bigint[] := ARRAY(SELECT DISTINCT x FROM unnest($1) x WHERE x IS NOT NULL ORDER BY 1);
BEGIN
 IF cardinality(p)<2 THEN
  RAISE EXCEPTION 'between needs at least two persons' USING ERRCODE='invalid_parameter_value';
 END IF;
 RETURN QUERY
 SELECT a.message_id,a.effective_time,a.channel,a.thread_id,a.thread_no,a.party_id,a.is_me,a.direction,a.attribution_status,m.content
 FROM tl.thread_rows(tl.shared_threads(p)) a
 JOIN tl.messages m ON m.id=a.message_id
 WHERE a.party_id=ANY(p)
  AND ($2 IS NULL OR a.effective_time>=$2)
  AND ($3 IS NULL OR a.effective_time<$3)
 ORDER BY a.effective_time DESC,a.message_id DESC
 LIMIT CASE WHEN $4 IS NULL THEN NULL ELSE greatest($4,0) END;
END $$;

-- The newest relevant records: last from them, last to them, last exchange
-- (never the newest record of a shared thread).
CREATE FUNCTION api.last_contact(person_id bigint)
RETURNS TABLE(last_from_at timestamptz,last_from_message_id bigint,last_to_at timestamptz,last_to_message_id bigint,
 last_exchange_at timestamptz,last_exchange_message_id bigint,last_contact_at timestamptz,last_contact_message_id bigint)
LANGUAGE sql STABLE AS $$
 WITH r AS MATERIALIZED (SELECT * FROM tl.contact_rows(last_contact.person_id)),
 f AS (SELECT r.effective_time,r.message_id FROM r WHERE r.side='from' ORDER BY r.effective_time DESC,r.message_id DESC LIMIT 1),
 t AS (SELECT r.effective_time,r.message_id FROM r WHERE r.side='to' ORDER BY r.effective_time DESC,r.message_id DESC LIMIT 1),
 x AS (SELECT r.effective_time,r.message_id FROM r WHERE r.side='exchange' ORDER BY r.effective_time DESC,r.message_id DESC LIMIT 1),
 c AS (SELECT r.effective_time,r.message_id FROM r WHERE r.side IN ('from','to') ORDER BY r.effective_time DESC,r.message_id DESC LIMIT 1)
 SELECT (SELECT f.effective_time FROM f),(SELECT f.message_id FROM f),
  (SELECT t.effective_time FROM t),(SELECT t.message_id FROM t),
  (SELECT x.effective_time FROM x),(SELECT x.message_id FROM x),
  (SELECT c.effective_time FROM c),(SELECT c.message_id FROM c) $$;

-- Open loops: an ask (ends with '?' or matches the request lexicon) older
-- than older_than with no later message by the other party in the thread.
-- direction they_owe: the owner asked; owner_owes: the other party asked.
-- Wound-down chats are left out: the asker's own later closing or
-- acknowledgement message, or no activity in the thread for horizon_days.
-- person_id NULL: every thread with an owner-side message.
CREATE FUNCTION api.open_loops(person_id bigint DEFAULT NULL,older_than interval DEFAULT '1 day',horizon_days int DEFAULT 30,max_results int DEFAULT 100,
 ask_patterns text[] DEFAULT NULL,closing_patterns text[] DEFAULT NULL)
RETURNS TABLE(message_id bigint,direction text,asked_at timestamptz,asker_party_id bigint,counterpart_party_id bigint,
 channel text,thread_id bigint,age_days numeric,last_thread_activity_at timestamptz,content text)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE o bigint := tl.owner_id(); threads bigint[];
 asks text[] := coalesce(open_loops.ask_patterns,tl.default_ask_patterns());
 closes text[] := coalesce(open_loops.closing_patterns,tl.default_closing_patterns());
 age interval := coalesce(open_loops.older_than,interval '1 day');
 horizon int := coalesce(open_loops.horizon_days,30);
BEGIN
 IF o IS NULL THEN RETURN; END IF;
 IF open_loops.person_id IS NOT NULL THEN
  IF open_loops.person_id=o THEN RETURN; END IF;
  threads := tl.shared_threads(ARRAY[o,open_loops.person_id]);
 ELSE
  threads := ARRAY(SELECT DISTINCT a.thread_id FROM tl.party_rows(ARRAY[o]) a WHERE a.thread_id IS NOT NULL);
 END IF;
 RETURN QUERY
 WITH r AS MATERIALIZED (
  SELECT a.message_id,a.thread_id,a.channel,a.effective_time,a.party_id,a.is_me,a.author_handle,m.content
  FROM tl.thread_rows(threads) a JOIN tl.messages m ON m.id=a.message_id),
 lt AS (SELECT r.thread_id,max(r.effective_time) AS at FROM r GROUP BY r.thread_id),
 q AS (
  SELECT r.*,
   CASE WHEN r.is_me THEN 'they_owe' ELSE 'owner_owes' END AS dir
  FROM r
  WHERE (btrim(r.content) ~ '\?$' OR r.content ~* ANY(asks))
   AND r.effective_time<now()-age
   AND (open_loops.person_id IS NULL OR r.is_me OR r.party_id=open_loops.person_id))
 SELECT q.message_id,q.dir,q.effective_time,q.party_id,
  CASE WHEN q.is_me THEN open_loops.person_id ELSE o END,
  q.channel,q.thread_id,round((extract(epoch FROM now()-q.effective_time)/86400)::numeric,2),l.at,q.content
 FROM q JOIN lt l ON l.thread_id=q.thread_id
 WHERE l.at>=now()-make_interval(days=>horizon)
  -- No later message by the other party.
  AND NOT EXISTS(SELECT 1 FROM r x WHERE x.thread_id=q.thread_id
   AND (x.effective_time>q.effective_time OR (x.effective_time=q.effective_time AND x.message_id>q.message_id))
   AND CASE WHEN q.is_me THEN NOT x.is_me AND (open_loops.person_id IS NULL OR x.party_id=open_loops.person_id)
    ELSE x.is_me END)
  -- Not wound down by the asker's own later closing message.
  AND NOT EXISTS(SELECT 1 FROM r x WHERE x.thread_id=q.thread_id
   AND (x.effective_time>q.effective_time OR (x.effective_time=q.effective_time AND x.message_id>q.message_id))
   AND CASE WHEN q.is_me THEN x.is_me WHEN q.party_id IS NOT NULL THEN x.party_id=q.party_id
    ELSE NOT x.is_me AND x.party_id IS NULL AND x.author_handle IS NOT DISTINCT FROM q.author_handle END
   AND x.content ~* ANY(closes))
 ORDER BY q.effective_time DESC,q.message_id DESC
 LIMIT CASE WHEN open_loops.max_results IS NULL THEN NULL ELSE greatest(open_loops.max_results,0) END;
END $$;

-- Counts per period (day, week or month; UTC) of messages from and to the
-- person and exchanges, with the channels of the period's messages. Periods
-- run from after_at (else the first contact) to before_at (else the last).
CREATE FUNCTION api.interactions(person_id bigint,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,grain text DEFAULT 'week')
RETURNS TABLE(period text,period_start timestamptz,messages_from bigint,messages_to bigint,exchanges bigint,channels text[])
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE step interval;
BEGIN
 IF interactions.grain IS NULL OR interactions.grain NOT IN ('day','week','month') THEN
  RAISE EXCEPTION 'grain must be day, week or month' USING ERRCODE='invalid_parameter_value';
 END IF;
 step := CASE interactions.grain WHEN 'day' THEN interval '1 day' WHEN 'week' THEN interval '1 week' ELSE interval '1 month' END;
 RETURN QUERY
 WITH r AS MATERIALIZED (
  SELECT x.side,x.message_id,x.channel,date_trunc(interactions.grain,x.effective_time AT TIME ZONE 'UTC') AS p
  FROM tl.contact_rows(interactions.person_id) x
  WHERE (interactions.after_at IS NULL OR x.effective_time>=interactions.after_at)
   AND (interactions.before_at IS NULL OR x.effective_time<interactions.before_at)),
 b AS (SELECT coalesce(date_trunc(interactions.grain,interactions.after_at AT TIME ZONE 'UTC'),min(r.p)) AS lo,
   coalesce(date_trunc(interactions.grain,(interactions.before_at-interval '1 microsecond') AT TIME ZONE 'UTC'),max(r.p)) AS hi FROM r),
 series AS (SELECT g AS p FROM b CROSS JOIN LATERAL generate_series(b.lo,b.hi,step) g WHERE b.lo IS NOT NULL AND b.hi IS NOT NULL LIMIT 10000)
 SELECT tl.people_period_label(interactions.grain,s.p),s.p AT TIME ZONE 'UTC',
  count(DISTINCT r.message_id) FILTER (WHERE r.side='from'),
  count(DISTINCT r.message_id) FILTER (WHERE r.side='to'),
  count(r.message_id) FILTER (WHERE r.side='exchange'),
  coalesce(array_agg(DISTINCT r.channel ORDER BY r.channel) FILTER (WHERE r.side IN ('from','to')),'{}'::text[])
 FROM series s LEFT JOIN r ON r.p=s.p
 GROUP BY s.p ORDER BY s.p;
END $$;

-- Records across channels (transcripts included) whose effective time is
-- within window_s seconds of at, closest first.
CREATE FUNCTION api.around(at timestamptz,window_s int DEFAULT 1800,persons bigint[] DEFAULT NULL,max_results int DEFAULT 100,include_ignored boolean DEFAULT false)
RETURNS TABLE(message_id bigint,channel text,kind text,thread_id bigint,thread_no int,effective_time timestamptz,record_time timestamptz,
 offset_s int,party_id bigint,is_me boolean,direction text,attribution_status text,ignored boolean,content text)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE w interval := make_interval(secs=>least(greatest(coalesce(around.window_s,1800),0),31536000));
 lo timestamptz := around.at-w; hi timestamptz := around.at+w; ids bigint[];
BEGIN
 IF around.at IS NULL THEN RETURN; END IF;
 ids := ARRAY(
  SELECT m.id FROM tl.messages m WHERE m.superseded_by IS NULL AND m.happened_at BETWEEN lo AND hi
  UNION
  SELECT m.id FROM tl.annotations a
  JOIN tl.messages m ON m.channel_id=a.target_channel_id AND m.external_id=a.target_external_id AND m.superseded_by IS NULL
  WHERE a.attribute='effective_time' AND a.status='active' AND (a.value->>'at')::timestamptz BETWEEN lo AND hi);
 RETURN QUERY
 SELECT a.message_id,a.channel,a.kind,a.thread_id,a.thread_no,a.effective_time,a.record_time,
  extract(epoch FROM a.effective_time-around.at)::int,a.party_id,a.is_me,a.direction,a.attribution_status,a.ignored,m.content
 FROM api.appearances a JOIN tl.messages m ON m.id=a.message_id
 WHERE a.message_id=ANY(ids) AND a.effective_time BETWEEN lo AND hi
  AND (around.include_ignored OR NOT a.ignored)
  AND (around.persons IS NULL OR a.party_id=ANY(around.persons))
 ORDER BY abs(extract(epoch FROM a.effective_time-around.at)),a.effective_time,a.message_id
 LIMIT CASE WHEN around.max_results IS NULL THEN NULL ELSE greatest(around.max_results,0) END;
END $$;

-- Records of one local day (tz, default throughline.timezone, else UTC) by
-- effective time, in time order.
CREATE FUNCTION api.day(on_date date,tz text DEFAULT NULL,persons bigint[] DEFAULT NULL,max_results int DEFAULT 500,include_ignored boolean DEFAULT false)
RETURNS TABLE(message_id bigint,channel text,kind text,thread_id bigint,thread_no int,effective_time timestamptz,local_time timestamp,record_time timestamptz,
 party_id bigint,is_me boolean,direction text,attribution_status text,ignored boolean,content text)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE z text := tl.people_timezone(day.tz); lo timestamptz; hi timestamptz; ids bigint[];
BEGIN
 IF day.on_date IS NULL THEN RETURN; END IF;
 lo := day.on_date::timestamp AT TIME ZONE z;
 hi := (day.on_date+1)::timestamp AT TIME ZONE z;
 ids := ARRAY(
  SELECT m.id FROM tl.messages m WHERE m.superseded_by IS NULL AND m.happened_at>=lo AND m.happened_at<hi
  UNION
  SELECT m.id FROM tl.annotations a
  JOIN tl.messages m ON m.channel_id=a.target_channel_id AND m.external_id=a.target_external_id AND m.superseded_by IS NULL
  WHERE a.attribute='effective_time' AND a.status='active' AND (a.value->>'at')::timestamptz>=lo AND (a.value->>'at')::timestamptz<hi);
 RETURN QUERY
 SELECT a.message_id,a.channel,a.kind,a.thread_id,a.thread_no,a.effective_time,a.effective_time AT TIME ZONE z,a.record_time,
  a.party_id,a.is_me,a.direction,a.attribution_status,a.ignored,m.content
 FROM api.appearances a JOIN tl.messages m ON m.id=a.message_id
 WHERE a.message_id=ANY(ids) AND a.effective_time>=lo AND a.effective_time<hi
  AND (day.include_ignored OR NOT a.ignored)
  AND (day.persons IS NULL OR a.party_id=ANY(day.persons))
 ORDER BY a.effective_time,a.message_id
 LIMIT CASE WHEN day.max_results IS NULL THEN NULL ELSE greatest(day.max_results,0) END;
END $$;

-- Episodes where every listed person has a message (by party_id) in the
-- window; messages counts the listed persons' messages.
CREATE FUNCTION api.together(persons bigint[],after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,max_results int DEFAULT 100)
RETURNS TABLE(episode_id text,thread_id bigint,channel text,started_at timestamptz,ended_at timestamptz,messages bigint,participants bigint[])
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE p bigint[] := ARRAY(SELECT DISTINCT x FROM unnest($1) x WHERE x IS NOT NULL ORDER BY 1);
BEGIN
 IF cardinality(p)<2 THEN
  RAISE EXCEPTION 'together needs at least two persons' USING ERRCODE='invalid_parameter_value';
 END IF;
 RETURN QUERY
 SELECT e.episode_id,e.thread_id,min(e.channel),min(e.started_at),max(e.ended_at),sum(e.messages)::bigint,p
 FROM tl.episode_parties(tl.shared_threads(p),$2,$3) e
 WHERE e.party_id=ANY(p)
 GROUP BY e.episode_id,e.thread_id
 HAVING count(DISTINCT e.party_id)=cardinality(p)
 ORDER BY min(e.started_at) DESC,e.episode_id
 LIMIT CASE WHEN $4 IS NULL THEN NULL ELSE greatest($4,0) END;
END $$;

-- Messages that contain one of the person's names or handles as a whole
-- word and are not by the person. matched_term is identity data
-- (unrestricted askers only).
CREATE FUNCTION api.mentions(person_id bigint,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,max_results int DEFAULT 100)
RETURNS TABLE(message_id bigint,effective_time timestamptz,channel text,thread_id bigint,party_id bigint,matched_term text,content text)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE terms text[]; ids bigint[]; bare text[];
BEGIN
 terms := ARRAY(SELECT DISTINCT ON (lower(t.term)) t.term FROM (
   SELECT p.handle AS term FROM tl.people p WHERE p.id=mentions.person_id
   UNION ALL SELECT p.display_name FROM tl.people p WHERE p.id=mentions.person_id
   UNION ALL SELECT n.name FROM tl.person_names n WHERE n.person_id=mentions.person_id AND n.retired_at IS NULL) t
  WHERE t.term IS NOT NULL AND length(btrim(t.term))>=2 ORDER BY lower(t.term),t.term);
 IF cardinality(terms)=0 THEN RETURN; END IF;
 ids := ARRAY(SELECT DISTINCT m.id FROM tl.messages m CROSS JOIN unnest(terms) t(term)
  WHERE m.superseded_by IS NULL AND numnode(phraseto_tsquery('english',t.term))>0
   AND m.tsv @@ phraseto_tsquery('english',t.term));
 -- A name made only of English stop words (Will, May) has no text-search
 -- form: those terms are matched by whole-word pattern instead.
 bare := ARRAY(SELECT '\m'||regexp_replace(t.term,'([!$()*+.:<=>?[\\\]^{|}-])','\\\1','g')||'\M'
  FROM unnest(terms) t(term) WHERE numnode(phraseto_tsquery('english',t.term))=0);
 IF cardinality(bare)>0 THEN
  ids := ids||ARRAY(SELECT m.id FROM tl.messages m WHERE m.superseded_by IS NULL AND m.content ~* ANY(bare));
 END IF;
 RETURN QUERY
 SELECT a.message_id,a.effective_time,a.channel,a.thread_id,a.party_id,
  CASE WHEN (SELECT api.asker_unrestricted()) THEN hit.term END,m.content
 FROM api.appearances a
 JOIN tl.messages m ON m.id=a.message_id
 CROSS JOIN LATERAL (SELECT t.term FROM unnest(terms) t(term)
  WHERE m.content ~* ('\m'||regexp_replace(t.term,'([!$()*+.:<=>?[\\\]^{|}-])','\\\1','g')||'\M')
  ORDER BY length(t.term) DESC,t.term LIMIT 1) hit
 WHERE a.message_id=ANY(ids) AND NOT a.ignored
  AND a.party_id IS DISTINCT FROM mentions.person_id
  AND (mentions.after_at IS NULL OR a.effective_time>=mentions.after_at)
  AND (mentions.before_at IS NULL OR a.effective_time<mentions.before_at)
 ORDER BY a.effective_time DESC,a.message_id DESC
 LIMIT CASE WHEN mentions.max_results IS NULL THEN NULL ELSE greatest(mentions.max_results,0) END;
END $$;

-- A person's ties (default the owner): exchanges (both directions, between
-- party ids) plus shared conversations (distinct episodes where both have
-- messages) in the window. relative_strength = strength / the sum of
-- strength over the person's ties (NULL when that sum is 0). Rank by
-- strength (relative mode: relative_strength), then person id.
CREATE FUNCTION api.circle(person_id bigint DEFAULT NULL,mode text DEFAULT 'absolute',after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,max_results int DEFAULT 50)
RETURNS TABLE(other_person_id bigint,exchanges bigint,shared_conversations bigint,strength bigint,relative_strength numeric,rank bigint)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE x bigint := coalesce(circle.person_id,tl.owner_id());
BEGIN
 IF circle.mode IS NULL OR circle.mode NOT IN ('absolute','relative') THEN
  RAISE EXCEPTION 'mode must be absolute or relative' USING ERRCODE='invalid_parameter_value';
 END IF;
 IF x IS NULL THEN RETURN; END IF;
 RETURN QUERY
 WITH ex AS (
  SELECT r.other_party AS other,count(*) AS n FROM tl.contact_rows(x) r
  WHERE r.side='exchange' AND r.other_party IS NOT NULL AND r.other_party<>x
   AND (circle.after_at IS NULL OR r.effective_time>=circle.after_at)
   AND (circle.before_at IS NULL OR r.effective_time<circle.before_at)
  GROUP BY r.other_party),
 ep AS MATERIALIZED (
  SELECT e.episode_id,e.party_id FROM tl.episode_parties(
   ARRAY(SELECT DISTINCT a.thread_id FROM tl.party_rows(ARRAY[x]) a WHERE a.thread_id IS NOT NULL),circle.after_at,circle.before_at) e),
 sh AS (
  SELECT e.party_id AS other,count(DISTINCT e.episode_id) AS n FROM ep e
  WHERE e.party_id<>x AND e.episode_id IN (SELECT y.episode_id FROM ep y WHERE y.party_id=x)
  GROUP BY e.party_id),
 s AS (
  SELECT coalesce(ex.other,sh.other) AS other,coalesce(ex.n,0) AS ex_n,coalesce(sh.n,0) AS sh_n,
   coalesce(ex.n,0)+coalesce(sh.n,0) AS strength
  FROM ex FULL JOIN sh ON sh.other=ex.other),
 rel AS (
  SELECT s.*,s.strength::numeric/nullif(sum(s.strength) OVER (),0) AS rs FROM s WHERE s.strength>0),
 ranked AS (
  SELECT rel.*,CASE WHEN circle.mode='relative'
    THEN row_number() OVER (ORDER BY rel.rs DESC NULLS LAST,rel.other)
    ELSE row_number() OVER (ORDER BY rel.strength DESC,rel.other) END AS rk
  FROM rel)
 SELECT ranked.other,ranked.ex_n,ranked.sh_n,ranked.strength,round(ranked.rs,6),ranked.rk
 FROM ranked
 WHERE circle.max_results IS NULL OR ranked.rk<=greatest(circle.max_results,0)
 ORDER BY ranked.rk;
END $$;

-- The pair breakdown of a tie.
CREATE FUNCTION api.ties(a bigint,b bigint,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL)
RETURNS TABLE(person_a bigint,person_b bigint,exchanges bigint,a_responded bigint,b_responded bigint,shared_conversations bigint,
 shared_threads bigint,strength bigint,first_exchange_at timestamptz,last_exchange_at timestamptz)
LANGUAGE sql STABLE AS $$
 WITH ex AS MATERIALIZED (
  SELECT r.* FROM tl.contact_rows(ties.a) r
  WHERE r.side='exchange' AND r.other_party=ties.b AND ties.a IS DISTINCT FROM ties.b
   AND (ties.after_at IS NULL OR r.effective_time>=ties.after_at)
   AND (ties.before_at IS NULL OR r.effective_time<ties.before_at)),
 ep AS MATERIALIZED (
  SELECT e.* FROM tl.episode_parties(CASE WHEN ties.a IS DISTINCT FROM ties.b THEN tl.shared_threads(ARRAY[ties.a,ties.b]) ELSE '{}'::bigint[] END,
   ties.after_at,ties.before_at) e WHERE e.party_id IN (ties.a,ties.b)),
 both_ep AS (SELECT e.episode_id,min(e.thread_id) AS thread_id FROM ep e GROUP BY e.episode_id HAVING count(DISTINCT e.party_id)=2)
 SELECT ties.a,ties.b,
  (SELECT count(*) FROM ex),
  (SELECT count(*) FROM ex WHERE ex.party_id=ties.a),
  (SELECT count(*) FROM ex WHERE ex.party_id=ties.b),
  (SELECT count(*) FROM both_ep),
  (SELECT count(DISTINCT both_ep.thread_id) FROM both_ep),
  (SELECT count(*) FROM ex)+(SELECT count(*) FROM both_ep),
  (SELECT min(ex.effective_time) FROM ex),
  (SELECT max(ex.effective_time) FROM ex) $$;

-- When a person writes: messages by the person per ISO weekday (1 Monday)
-- and hour, and per month of the year, in tz (default throughline.timezone,
-- else UTC).
CREATE FUNCTION api.rhythm(person_id bigint,after_at timestamptz DEFAULT NULL,before_at timestamptz DEFAULT NULL,tz text DEFAULT NULL)
RETURNS TABLE(dimension text,weekday int,hour int,month int,messages bigint)
LANGUAGE sql STABLE AS $$
 WITH r AS MATERIALIZED (
  SELECT a.effective_time AT TIME ZONE tl.people_timezone(rhythm.tz) AS lt FROM tl.party_rows(ARRAY[rhythm.person_id]) a
  WHERE (rhythm.after_at IS NULL OR a.effective_time>=rhythm.after_at)
   AND (rhythm.before_at IS NULL OR a.effective_time<rhythm.before_at))
 SELECT * FROM (
  SELECT 'weekday_hour'::text,extract(isodow FROM r.lt)::int,extract(hour FROM r.lt)::int,NULL::int,count(*) FROM r GROUP BY 2,3
  UNION ALL
  SELECT 'month'::text,NULL::int,NULL::int,extract(month FROM r.lt)::int,count(*) FROM r GROUP BY 4) z
 ORDER BY 1 DESC,2,3,4 $$;

-- The default entry point: who the person is (identity parts for
-- unrestricted askers only) and the shape of the contact, in one row.
CREATE FUNCTION api.person_brief(person_id bigint)
RETURNS TABLE(person bigint,handle text,display_name text,is_owner boolean,merged_into bigint,names text[],identifiers jsonb,
 first_contact_at timestamptz,first_contact_message_id bigint,last_contact_at timestamptz,last_contact_message_id bigint,
 message_count bigint,messages_to bigint,exchange_count bigint,top_channels jsonb,open_loop_count bigint,circle_rank bigint,
 recent_threads jsonb,facts jsonb,relations jsonb,coverage jsonb)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE u boolean := api.asker_unrestricted(); o bigint := tl.owner_id();
 h text; dn text; own boolean; mi bigint;
BEGIN
 IF u THEN
  SELECT x.handle,x.display_name,x.is_owner,x.merged_into INTO h,dn,own,mi FROM tl.people x WHERE x.id=person_brief.person_id;
 END IF;
 RETURN QUERY
 WITH r AS MATERIALIZED (SELECT * FROM tl.contact_rows(person_brief.person_id)),
 ft AS (SELECT DISTINCT ON (r.message_id) r.* FROM r WHERE r.side IN ('from','to') ORDER BY r.message_id,r.side),
 firstc AS (SELECT ft.effective_time,ft.message_id FROM ft ORDER BY ft.effective_time,ft.message_id LIMIT 1),
 lastc AS (SELECT ft.effective_time,ft.message_id FROM ft ORDER BY ft.effective_time DESC,ft.message_id DESC LIMIT 1),
 chans AS (SELECT ft.channel,count(*) AS n FROM ft GROUP BY ft.channel ORDER BY count(*) DESC,ft.channel LIMIT 5),
 threads AS (SELECT ft.thread_id,min(ft.channel) AS channel,max(ft.effective_time) AS last_at,count(*) AS n
  FROM ft WHERE ft.thread_id IS NOT NULL GROUP BY ft.thread_id ORDER BY max(ft.effective_time) DESC,ft.thread_id LIMIT 5)
 SELECT person_brief.person_id,
  h,dn,own,mi,
  CASE WHEN u THEN ARRAY(SELECT n.name FROM tl.person_names n WHERE n.person_id=person_brief.person_id AND n.retired_at IS NULL ORDER BY n.is_primary DESC NULLS LAST,n.id) END,
  CASE WHEN u THEN (SELECT coalesce(jsonb_object_agg(s.system,s.statuses),'{}'::jsonb) FROM (
    SELECT c.system,jsonb_object_agg(c.status,c.n) AS statuses FROM (
     SELECT i.system,i.status,count(*) AS n FROM tl.identifiers i WHERE i.person_id=person_brief.person_id GROUP BY i.system,i.status) c
    GROUP BY c.system) s) END,
  (SELECT firstc.effective_time FROM firstc),(SELECT firstc.message_id FROM firstc),
  (SELECT lastc.effective_time FROM lastc),(SELECT lastc.message_id FROM lastc),
  (SELECT count(*) FROM ft WHERE ft.side='from'),
  (SELECT count(*) FROM ft WHERE ft.side='to'),
  (SELECT count(*) FROM r WHERE r.side='exchange'),
  (SELECT coalesce(jsonb_agg(jsonb_build_object('channel',chans.channel,'messages',chans.n) ORDER BY chans.n DESC,chans.channel),'[]'::jsonb) FROM chans),
  (SELECT count(*) FROM api.open_loops(person_brief.person_id,interval '1 day',30,NULL) l),
  CASE WHEN o IS NOT NULL AND person_brief.person_id IS DISTINCT FROM o THEN
   (SELECT c.rank FROM api.circle(o,'absolute',NULL,NULL,NULL) c WHERE c.other_person_id=person_brief.person_id) END,
  (SELECT coalesce(jsonb_agg(jsonb_build_object('thread_id',threads.thread_id,'channel',threads.channel,'last_at',threads.last_at,'messages',threads.n) ORDER BY threads.last_at DESC,threads.thread_id),'[]'::jsonb) FROM threads),
  CASE WHEN u THEN (SELECT coalesce(jsonb_agg(jsonb_build_object('key',f.key,'value',f.value,'as_of',f.as_of) ORDER BY f.key,f.id),'[]'::jsonb)
   FROM tl.facts f WHERE f.person_id=person_brief.person_id AND f.retired_at IS NULL) END,
  CASE WHEN u THEN (SELECT coalesce(jsonb_agg(jsonb_build_object('kind',x.kind,'other_person_id',x.other,'direction',x.dir) ORDER BY x.kind,x.other),'[]'::jsonb) FROM (
    SELECT rl.kind,rl.to_id AS other,'outgoing'::text AS dir FROM tl.relations rl WHERE rl.from_id=person_brief.person_id AND rl.ended_at IS NULL
    UNION ALL SELECT rl.kind,rl.from_id,'incoming' FROM tl.relations rl WHERE rl.to_id=person_brief.person_id AND rl.ended_at IS NULL) x) END,
  (SELECT coalesce(jsonb_agg(to_jsonb(cv) ORDER BY cv.channel),'[]'::jsonb)
   FROM api.coverage_for(coalesce(ARRAY(SELECT DISTINCT ft.channel FROM ft),'{}'::text[])) cv);
END $$;

GRANT EXECUTE ON FUNCTION api.appearance_overlay(bigint),api.attribution(bigint[]),api.suspected_attributions(bigint[]),
 api.exchange_index_status(),api.person_timeline(bigint,timestamptz,timestamptz,int),api.person_arc(bigint,text),
 api.dms_with(bigint,timestamptz,timestamptz,int),api.between_threads(bigint[]),api.between(bigint[],timestamptz,timestamptz,int),
 api.last_contact(bigint),api.open_loops(bigint,interval,int,int,text[],text[]),api.interactions(bigint,timestamptz,timestamptz,text),
 api.around(timestamptz,int,bigint[],int,boolean),api.day(date,text,bigint[],int,boolean),api.together(bigint[],timestamptz,timestamptz,int),
 api.mentions(bigint,timestamptz,timestamptz,int),api.circle(bigint,text,timestamptz,timestamptz,int),api.ties(bigint,bigint,timestamptz,timestamptz),
 api.rhythm(bigint,timestamptz,timestamptz,text),api.person_brief(bigint)
 TO throughline_app,throughline_reader;

-- 6. Registry, allow-list and catalogue ------------------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('person_brief','sql','person_brief','The default first step for any person question: handle, names and identifier counts by system and status (unrestricted askers only), first and last contact, messages from and to, exchanges, top channels, open loops, circle rank among the owner''s ties, recent threads, facts and relations (unrestricted), and coverage of the channels involved','exact','free',0,'owner, agent; counts over rows that pass the asker gate; identity parts unrestricted only','SELECT * FROM api.person_brief(1)','1',NULL,NULL),
 ('person_timeline','sql','person_timeline','Messages from the person and to the person (owner-side messages in their one-to-one threads, responses to them), newest first; side from | to','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.person_timeline(1,NULL,NULL,50)','1',4,200),
 ('person_arc','sql','person_arc','Contact arc: per period (week, month, quarter, year; UTC) messages from, to and exchanges over the whole history, with first, peak and last periods and gap_days (days since the last contact)','exact','free',0,'owner, agent; counts over rows that pass the asker gate','SELECT * FROM api.person_arc(1,''month'')','1',NULL,NULL),
 ('dms_with','sql','dms_with','Both sides of the person''s one-to-one threads with the owner (conversation_kind dm, or party ids exactly owner and person), newest first','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.dms_with(1,NULL,NULL,50)','1',4,200),
 ('between_threads','sql','between_threads','Threads where every listed person (at least two) has a message by party_id','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.between_threads(ARRAY[1,2]::bigint[])','1',NULL,NULL),
 ('between','sql','between','Dyad or group scope: messages by any listed person in the threads where every listed person has a message (both sides, never author-only), newest first','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.between(ARRAY[1,2]::bigint[],NULL,NULL,50)','1',4,200),
 ('last_contact','sql','last_contact','The newest relevant records of a person: last from them, last to them, last exchange and the last contact (never the newest record of a shared thread)','exact','free',0,'owner, agent; rows pass the asker gate','SELECT * FROM api.last_contact(1)','1',NULL,NULL),
 ('open_loops','sql','open_loops','Unanswered asks (ends with ? or matches the request lexicon) older than older_than with no later message by the other party; direction owner_owes | they_owe; wound-down chats excluded (the asker closed it, or no activity for horizon_days)','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.open_loops(NULL,''1 day'',30,50)','1',4,100),
 ('interactions','sql','interactions','Counts per period (day, week, month; UTC) of messages from and to the person and exchanges, with channels','exact','free',0,'owner, agent; counts over rows that pass the asker gate','SELECT * FROM api.interactions(1,NULL,NULL,''week'')','1',NULL,NULL),
 ('around','sql','around','Records across channels (transcripts included) within window_s seconds of a time, by effective time, closest first; ignored excluded unless include_ignored','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.around(''2020-01-01T00:00:00Z'',1800,NULL,50)','1',4,100),
 ('day','sql','day','Records of one local day by effective time (tz default THROUGHLINE_TIMEZONE, else UTC), in time order; ignored excluded unless include_ignored','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.day(''2020-01-01'',''UTC'',NULL,100)','1',4,500),
 ('together','sql','together','Episodes (api.episodes_of grain) where every listed person has a message by party_id, newest first','exact','free',1,'owner, agent; rows pass the asker gate','SELECT * FROM api.together(ARRAY[1,2]::bigint[],NULL,NULL,50)','1',4,100),
 ('mentions','sql','mentions','Messages that contain one of the person''s names or handles as a whole word and are not by the person; matched_term for unrestricted askers only','exact','free',1,'owner, agent; rows pass the asker gate; matched term unrestricted only','SELECT * FROM api.mentions(1,NULL,NULL,50)','1',4,100),
 ('circle','sql','circle','A person''s ties (default the owner): exchanges plus shared conversations (episodes with both) = strength; relative_strength = strength / the sum over the person''s ties; rank by strength, then person id','exact','free',1,'owner, agent; counts and ranks over rows that pass the asker gate','SELECT * FROM api.circle(NULL,''absolute'',NULL,NULL,20)','1',5,50),
 ('ties','sql','ties','The pair breakdown of a tie: exchanges by direction, shared conversations and threads, strength, first and last exchange','exact','free',0,'owner, agent; counts over rows that pass the asker gate','SELECT * FROM api.ties(1,2)','1',NULL,NULL),
 ('rhythm','sql','rhythm','When a person writes: messages per ISO weekday and hour and per month of the year, in a time zone','exact','free',0,'owner, agent; counts over rows that pass the asker gate','SELECT * FROM api.rhythm(1,NULL,NULL,''UTC'')','1',NULL,NULL),
 ('attribution','sql','attribution','Attribution of messages: is_me, direction, attribution_status (confirmed, suspected, unknown, annotated, self_direction), identity_quality and time_precision','exact','free',2,'owner, agent; rows pass the asker gate','SELECT * FROM api.attribution(ARRAY[1]::bigint[])','1',NULL,NULL),
 ('suspected_attributions','sql','suspected_attributions','The suspected identifier and person behind messages whose author account is suspected (excluded from person attribution until confirmed)','exact','free',0,'owner, agent; unrestricted askers only','SELECT * FROM api.suspected_attributions(ARRAY[1]::bigint[])','1',NULL,NULL),
 ('exchange_index_status','sql','exchange_index_status','The exchange index over the threads you may read: stale_threads still awaiting a rebuild, indexed threads, indexed_to and indexed_at','exact','free',0,'owner, agent; threads pass the asker gate','SELECT * FROM api.exchange_index_status()','1',NULL,NULL);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='api' AND p.proname=f.name AND f.name IN ('person_brief','person_timeline','person_arc','dms_with','between_threads','between',
  'last_contact','open_loops','interactions','around','day','together','mentions','circle','ties','rhythm','attribution','suspected_attributions','exchange_index_status');

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('people',jsonb_build_object(
  'description','Person-centric reads. Start with api.person_brief(person_id) (GET /v1/people/{id}/brief), then the specific function. Every function reads gated views: a restricted asker''s counts, ranks and lists cover granted rows only, and identity data (handles, names, identifiers, facts, relations, speaker annotations, matched terms) is returned to unrestricted askers only.',
  'first_step','person_brief',
  'roles',jsonb_build_object(
   'author','Messages whose author is the person (recall person_role author; api.person_messages).',
   'between','Messages by ANY listed person in threads where every listed person has a message (api.between; recall person_role between). Both sides of a dyad, never author-only.',
   'involved','Messages in threads where the person has a message, plus messages linked to the person (recall person_role involved).'),
  'owner_side','is_me: attributed to the owner, or fields.direction outgoing. party_id is the owner''s person id when is_me, else the confirmed attribution; every person function places a message on a side by party_id, so the owner''s own outgoing messages count for the owner even without a person_id.',
  'attribution_statuses',jsonb_build_object(
   'confirmed','person_id is set: the author identifier is confirmed',
   'self_direction','is_me from fields.direction outgoing only',
   'annotated','a speaker annotation names a person (unrestricted askers only)',
   'suspected','the author identifier is suspected: excluded from person attribution until confirmed',
   'unknown','no attribution'),
  'attribution_rule','Only a confirmed author identifier sets messages.person_id. Suspected and ignored accounts are excluded from every tool (legacy search, recall, SQL, person functions) until confirmed (POST /v1/identity/identifiers/{id}/status); names alone are never links.',
  'exchange','Message R responds to message T when an active reply_to link goes from R to T (any gap), or R is the next turn after T in the same thread (consecutive thread_no among current, non-ignored versions) within THROUGHLINE_EXCHANGE_WINDOW_S (default 1800 s) and by a different author (owner side by is_me; else the attributed person when both are attributed, else author_handle). Resolved once into an index keyed by external keys; routes refresh it within THROUGHLINE_PEOPLE_REFRESH_MS and disclose stale threads as execution.incomplete exchange_index_stale; SQL callers read api.exchange_index_status().',
  'contact','from: messages by the person. to: owner-side messages in the person''s one-to-one threads, and messages by others that respond to the person. exchanges: exchanges with the person on either end.',
  'units',jsonb_build_object(
   'times','effective time (overlay, else record time); after_at inclusive, before_at exclusive',
   'window_s','seconds','older_than','interval','horizon_days','days','gap_days','days (2 decimals)',
   'strength','exchanges + shared conversations (count)','relative_strength','fraction of the person''s total strength (0..1)',
   'periods','UTC calendar periods: week (ISO), month, quarter, year, day','rhythm','ISO weekday 1 = Monday, hour 0-23, month 1-12 in tz'),
  'filters','api.appearances exposes is_me, direction, attribution_status, identity_quality, time_precision and conversation_kind per record; result-set filter takes identity_quality, time_precision and attribution_status lists.',
  'functions',jsonb_build_array('person_brief','person_timeline','person_arc','dms_with','between','between_threads','last_contact','open_loops',
   'interactions','around','day','together','mentions','circle','ties','rhythm','attribution','suspected_attributions','exchange_index_status')));

-- 7. Purge: exchanges whose responder or target is in the purged channel
-- (exchange_state rows cascade with the threads).
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES
 ('exchanges',63,'DELETE FROM tl.exchanges e WHERE e.responder_channel_id=$1 OR e.target_channel_id=$1');
