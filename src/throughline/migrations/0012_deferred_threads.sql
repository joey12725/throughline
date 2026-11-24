-- Avoid O(batches * thread size) updates during random-order historical loads.
CREATE TABLE tl.dirty_threads(thread_id bigint PRIMARY KEY REFERENCES tl.threads ON DELETE CASCADE);
GRANT SELECT,INSERT,DELETE ON tl.dirty_threads TO throughline_app;
CREATE FUNCTION tl.refresh_derived() RETURNS void LANGUAGE plpgsql AS $$
DECLARE tids bigint[]; cid bigint;
BEGIN
 SELECT array_agg(thread_id ORDER BY thread_id) INTO tids FROM tl.dirty_threads;
 IF tids IS NULL THEN RETURN; END IF;
 FOR cid IN SELECT DISTINCT channel_id FROM tl.threads WHERE id=ANY(tids) ORDER BY channel_id LOOP
  PERFORM pg_advisory_xact_lock(cid);
 END LOOP;
 WITH numbered AS (SELECT id,row_number() OVER(PARTITION BY thread_id ORDER BY happened_at,external_id)::int no FROM tl.messages WHERE superseded_by IS NULL AND thread_id=ANY(tids))
 UPDATE tl.messages m SET thread_no=n.no FROM numbered n WHERE n.id=m.id AND m.thread_no IS DISTINCT FROM n.no;
 PERFORM tl.regroup_conversations(tids);
 DELETE FROM tl.dirty_threads WHERE thread_id=ANY(tids);
END $$;
REVOKE EXECUTE ON FUNCTION tl.refresh_derived() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.refresh_derived() TO throughline_app;
-- All public observations, including direct read-only SQL, see exact dense
-- numbers even while physical maintenance is deferred. Preserve view shape.
DO $$ DECLARE cols text; BEGIN
 SELECT string_agg(CASE WHEN attname='thread_no' THEN 'coalesce(n.no,m.thread_no) AS thread_no' ELSE format('m.%I',attname) END,',' ORDER BY attnum) INTO cols FROM pg_attribute WHERE attrelid='tl.messages'::regclass AND attnum>0 AND NOT attisdropped;
 EXECUTE 'CREATE OR REPLACE VIEW api.message_versions AS WITH numbered AS MATERIALIZED (SELECT m.id,row_number() OVER(PARTITION BY m.thread_id ORDER BY m.happened_at,m.external_id)::int no FROM tl.messages m JOIN tl.dirty_threads d ON d.thread_id=m.thread_id WHERE m.superseded_by IS NULL) SELECT '||cols||',c.name AS channel,c.is_reference,c.is_outside,t.external_id AS thread_external_id,t.title AS thread_title,p.handle AS person_handle,p.display_name FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id LEFT JOIN tl.threads t ON t.id=m.thread_id LEFT JOIN tl.people p ON p.id=m.person_id LEFT JOIN numbered n ON n.id=m.id';
END $$;
