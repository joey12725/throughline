-- Deferred thread maintenance that never stalls readers or ingest.
--
-- 1. Regrouping rewrites only chunks whose span, text or closure changed, so
--    materializing a large thread after a small append touches a few rows.
CREATE OR REPLACE FUNCTION tl.regroup_conversations(thread_ids bigint[] DEFAULT NULL) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 CREATE TEMP TABLE regrouped ON COMMIT DROP AS
 WITH ordered AS (
 SELECT m.id,m.thread_id,m.thread_no,m.happened_at,m.ended_at,m.content,
 c.conversation_gap_s,c.conversation_whole_thread,c.conversation_max_tokens,
 lag(coalesce(m.ended_at,m.happened_at)) OVER(PARTITION BY m.thread_id ORDER BY m.thread_no) prev
 FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id
 WHERE m.superseded_by IS NULL AND (thread_ids IS NULL OR m.thread_id=ANY(thread_ids))
 ), grouped AS (
 SELECT *,sum(CASE WHEN prev IS NULL OR (NOT conversation_whole_thread AND happened_at-prev>make_interval(secs=>conversation_gap_s)) THEN 1 ELSE 0 END) OVER(PARTITION BY thread_id ORDER BY thread_no) grp FROM ordered
 ), positioned AS (
 SELECT *,sum(length(content)+1) OVER(PARTITION BY thread_id,grp ORDER BY thread_no)-length(content)-1 pos FROM grouped
 ), ranges AS (
 SELECT *,max(pos+length(content)) OVER(PARTITION BY thread_id,grp) total_length,conversation_max_tokens*4 size FROM positioned
 ), fragments AS (
 SELECT p.thread_id,p.thread_no,p.grp,p.happened_at,p.ended_at,p.conversation_gap_s,p.pos,length(p.content) content_length,p.size,k*(p.size-64) offset_no,
 substring(p.content FROM greatest(k*(p.size-64)-p.pos,0)::int+1 FOR greatest(least(p.pos+length(p.content),k*(p.size-64)+p.size)-greatest(p.pos,k*(p.size-64)),0)::int)
 || CASE WHEN p.pos+length(p.content)<p.total_length AND p.pos+length(p.content)>=k*(p.size-64) AND p.pos+length(p.content)<k*(p.size-64)+p.size THEN E'\n' ELSE '' END fragment
 FROM ranges p CROSS JOIN LATERAL generate_series(
 greatest(0,floor((p.pos-p.size)::numeric/(p.size-64))::bigint+1),
 (least(p.pos+length(p.content)+1,p.total_length)-1)/(p.size-64)) k
 ), chunks AS (
 SELECT thread_id,grp,offset_no,
 min(thread_no) FILTER(WHERE pos+content_length>offset_no AND pos<offset_no+size)::int first_thread_no,
 max(thread_no) FILTER(WHERE pos+content_length>offset_no AND pos<offset_no+size)::int last_thread_no,
 min(happened_at) FILTER(WHERE pos+content_length>offset_no AND pos<offset_no+size) started_at,
 max(coalesce(ended_at,happened_at)) FILTER(WHERE pos+content_length>offset_no AND pos<offset_no+size) ended_at,
 max(conversation_gap_s) gap,string_agg(fragment,'' ORDER BY thread_no) content
 FROM fragments GROUP BY thread_id,grp,offset_no
 ) SELECT thread_id,first_thread_no,last_thread_no,
 row_number() OVER(PARTITION BY thread_id ORDER BY grp,offset_no)::int chunk_no,
 started_at,ended_at,ceil(length(content)/4.0)::int token_estimate,
 md5(content) text_hash,content,ended_at+make_interval(secs=>gap)<now() closed FROM chunks;
 INSERT INTO tl.conversations(thread_id,first_thread_no,last_thread_no,chunk_no,started_at,ended_at,token_estimate,text_hash,content,closed)
 SELECT thread_id,first_thread_no,last_thread_no,chunk_no,started_at,ended_at,token_estimate,text_hash,content,closed FROM regrouped
 ON CONFLICT(thread_id,chunk_no) DO UPDATE SET first_thread_no=EXCLUDED.first_thread_no,last_thread_no=EXCLUDED.last_thread_no,started_at=EXCLUDED.started_at,ended_at=EXCLUDED.ended_at,token_estimate=EXCLUDED.token_estimate,text_hash=EXCLUDED.text_hash,content=EXCLUDED.content,closed=EXCLUDED.closed
 WHERE (tl.conversations.first_thread_no,tl.conversations.last_thread_no,tl.conversations.started_at,tl.conversations.ended_at,tl.conversations.token_estimate,tl.conversations.text_hash,tl.conversations.closed)
 IS DISTINCT FROM (EXCLUDED.first_thread_no,EXCLUDED.last_thread_no,EXCLUDED.started_at,EXCLUDED.ended_at,EXCLUDED.token_estimate,EXCLUDED.text_hash,EXCLUDED.closed);
 DELETE FROM tl.conversations c WHERE (thread_ids IS NULL OR c.thread_id=ANY(thread_ids)) AND NOT EXISTS(SELECT 1 FROM regrouped r WHERE (r.thread_id,r.chunk_no)=(c.thread_id,c.chunk_no));
 DROP TABLE regrouped;
END $$;

-- 2. One dirty thread per call and per transaction: the caller holds a single
--    channel lock for the duration of one thread, never a set of them, so a
--    backfill cannot be blocked behind unrelated channels and lock order cannot
--    deadlock. wait=false gives up at once when ingest holds the channel.
--    SECURITY DEFINER: the try-lock family is not granted to runtime roles
--    after a restore, and the caller needs no rights beyond EXECUTE.
CREATE FUNCTION tl.renumber_threads(tids bigint[]) RETURNS void LANGUAGE sql SET search_path=pg_catalog,tl AS $$
 WITH numbered AS (SELECT id,row_number() OVER(PARTITION BY thread_id ORDER BY happened_at,external_id)::int no FROM tl.messages WHERE superseded_by IS NULL AND thread_id=ANY(tids))
 UPDATE tl.messages m SET thread_no=n.no FROM numbered n WHERE n.id=m.id AND m.thread_no IS DISTINCT FROM n.no
$$;
CREATE FUNCTION tl.refresh_thread(tid bigint,wait boolean DEFAULT true) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl,pg_temp AS $$
DECLARE cid bigint;
BEGIN
 SELECT channel_id INTO cid FROM tl.threads WHERE id=tid;
 IF cid IS NULL THEN RETURN true; END IF;
 IF wait THEN PERFORM pg_advisory_xact_lock(cid);
 ELSIF NOT pg_try_advisory_xact_lock(cid) THEN RETURN false;
 END IF;
 -- Decide under the lock: another process may have finished this thread.
 IF NOT EXISTS(SELECT 1 FROM tl.dirty_threads WHERE thread_id=tid) THEN RETURN true; END IF;
 PERFORM tl.renumber_threads(ARRAY[tid]);
 PERFORM tl.regroup_conversations(ARRAY[tid]);
 DELETE FROM tl.dirty_threads WHERE thread_id=tid;
 RETURN true;
END $$;
REVOKE EXECUTE ON FUNCTION tl.renumber_threads(bigint[]),tl.refresh_thread(bigint,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.renumber_threads(bigint[]),tl.refresh_thread(bigint,boolean) TO throughline_app;

-- 3. Stored conversations of a dirty thread describe an older message order.
--    Every read surface uses this view, so such a thread reports no
--    conversation (NULL fields, no meaning-search hit) instead of a wrong one
--    until it is materialized. Thread numbers stay exact through
--    api.message_versions throughout.
CREATE VIEW tl.live_conversations AS SELECT c.* FROM tl.conversations c WHERE NOT EXISTS(SELECT 1 FROM tl.dirty_threads d WHERE d.thread_id=c.thread_id);
GRANT SELECT ON tl.live_conversations TO throughline_app;
CREATE OR REPLACE VIEW api.conversations AS SELECT id,thread_id,first_thread_no,last_thread_no,chunk_no,started_at,ended_at,token_estimate,text_hash,closed FROM tl.live_conversations;
-- Repoint whatever api functions currently read the table, keeping every other
-- attribute (arguments, SECURITY DEFINER, search_path) exactly as defined.
DO $$ DECLARE f oid; BEGIN
 FOR f IN SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.prokind='f' AND p.prosrc LIKE '%tl.conversations%' LOOP
  EXECUTE replace(pg_get_functiondef(f),'tl.conversations','tl.live_conversations');
 END LOOP;
END $$;
