-- 0053 open loops in one-to-one threads the owner never answered.
-- Forward-only. Every api function becomes SECURITY DEFINER with a fixed
-- search_path through tl.harden_reader(), so tl objects are schema-qualified.
--
-- 0047's api.open_loops read only threads with an owner-side message (no
-- person) or threads where both the owner and the person wrote (a person).
-- An ask nobody answered in a thread of its own was therefore never
-- returned: exactly the owner_owes loop the function exists to find, and
-- with a person named, an owner ask alone in its thread was missed too.
-- The threads considered now also include a person's one-to-one threads
-- with the owner whether or not the owner wrote there:
--  * a thread with a visible message of the person whose conversation_kind
--    is 'dm', or whose visible, non-ignored messages are all by the owner
--    side or by that one person;
--  * with a person named, every thread of a one-to-one channel: a channel
--    with a visible message of the person whose visible, non-ignored
--    messages are all by the owner side or by that person (so an owner ask
--    alone in its thread has that person as counterpart).
-- Group threads and threads with unattributed authors are still read only
-- when the owner wrote there. Every decision reads gated rows, so hidden
-- rows never change which threads are read. Signature, result and
-- catalogue row are unchanged.

-- One-to-one threads of a person (every person when person is NULL) with
-- the owner, whether or not the owner wrote there (see the header).
CREATE FUNCTION tl.loop_threads(person bigint) RETURNS bigint[] LANGUAGE sql STABLE AS $$
 WITH o AS MATERIALIZED (SELECT tl.owner_id() AS id),
 cand AS MATERIALIZED (
  SELECT DISTINCT m.thread_id FROM tl.messages m
  WHERE m.superseded_by IS NULL AND m.thread_id IS NOT NULL AND m.person_id IS NOT NULL
   AND (loop_threads.person IS NULL OR m.person_id=loop_threads.person)
   AND m.person_id IS DISTINCT FROM (SELECT o.id FROM o)),
 t AS (
  SELECT r.thread_id,
   coalesce(bool_or(r.conversation_kind='dm'),false) AS dm,
   count(DISTINCT r.party_id) FILTER (WHERE NOT r.is_me) AS others,
   bool_and(r.is_me OR r.party_id IS NOT NULL) AS attributed,
   bool_or(NOT r.is_me AND r.party_id IS NOT NULL
    AND (loop_threads.person IS NULL OR r.party_id=loop_threads.person)) AS has_person
  FROM tl.thread_rows(ARRAY(SELECT cand.thread_id FROM cand)) r GROUP BY r.thread_id),
 ch AS MATERIALIZED (
  SELECT DISTINCT m.channel_id FROM tl.messages m
  WHERE loop_threads.person IS NOT NULL AND m.superseded_by IS NULL AND m.person_id=loop_threads.person
   AND m.person_id IS DISTINCT FROM (SELECT o.id FROM o)),
 one AS MATERIALIZED (
  SELECT ch.channel_id FROM ch
  WHERE EXISTS(SELECT 1 FROM api.appearances a WHERE a.channel_id=ch.channel_id AND NOT a.ignored
    AND NOT a.is_me AND a.party_id=loop_threads.person)
   AND NOT EXISTS(SELECT 1 FROM api.appearances a WHERE a.channel_id=ch.channel_id AND NOT a.ignored
    AND NOT a.is_me AND a.party_id IS DISTINCT FROM loop_threads.person))
 SELECT coalesce(array_agg(z.thread_id ORDER BY z.thread_id),'{}'::bigint[]) FROM (
  SELECT t.thread_id FROM t WHERE t.has_person AND (t.dm OR (t.others=1 AND t.attributed))
  UNION
  SELECT m.thread_id FROM tl.messages m
  WHERE m.channel_id=ANY(ARRAY(SELECT one.channel_id FROM one)) AND m.superseded_by IS NULL AND m.thread_id IS NOT NULL) z $$;
REVOKE EXECUTE ON FUNCTION tl.loop_threads(bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION api.open_loops(person_id bigint DEFAULT NULL,older_than interval DEFAULT '1 day',horizon_days int DEFAULT 30,max_results int DEFAULT 100,
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
  threads := ARRAY(SELECT DISTINCT x FROM unnest(tl.shared_threads(ARRAY[o,open_loops.person_id])
   || tl.loop_threads(open_loops.person_id)) x ORDER BY x);
 ELSE
  threads := ARRAY(SELECT DISTINCT x FROM unnest(
   ARRAY(SELECT a.thread_id FROM tl.party_rows(ARRAY[o]) a WHERE a.thread_id IS NOT NULL)
   || tl.loop_threads(NULL)) x ORDER BY x);
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
