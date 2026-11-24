-- Keep only bounded fragments in the chunk aggregation. In particular, do
-- not carry an entire large source message through every expanded chunk row.
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
 ON CONFLICT(thread_id,chunk_no) DO UPDATE SET first_thread_no=EXCLUDED.first_thread_no,last_thread_no=EXCLUDED.last_thread_no,started_at=EXCLUDED.started_at,ended_at=EXCLUDED.ended_at,token_estimate=EXCLUDED.token_estimate,text_hash=EXCLUDED.text_hash,content=EXCLUDED.content,closed=EXCLUDED.closed;
 DELETE FROM tl.conversations c WHERE (thread_ids IS NULL OR c.thread_id=ANY(thread_ids)) AND NOT EXISTS(SELECT 1 FROM regrouped r WHERE (r.thread_id,r.chunk_no)=(c.thread_id,c.chunk_no));
 DROP TABLE regrouped;
END $$;
