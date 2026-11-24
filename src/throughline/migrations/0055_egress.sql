-- Label-aware egress: stored content that carries a sensitivity label is not
-- sent to a model provider outside this host unless the owner allowed that
-- label to leave.
--
-- 1. tl.labels.egress (default false): whether content under the label may be
--    sent to a non-local provider. Default deny, like the disclosure rule.
-- 2. tl.message_egress_ok / tl.conversation_egress_ok / tl.messages_egress_held:
--    the rule, for one message, one conversation chunk, or a list of ids.
--    A message is held when a label of its channel, of its thread or its own
--    access_label is a kind with egress=false, or (for a page revision or a
--    derived record) when any source of its lineage is held. The walk is the
--    one of api.row_visible: a cycle, a missing source or more than four
--    levels fail closed.
--
-- The rule decides what the embedding worker, the batch jobs, the reranker
-- and the semantic and controller models may be handed when the configured
-- provider is not local. It never changes what an asker reads: local
-- providers, keyword search and the asker gate are unaffected, and so is the
-- policy version.

ALTER TABLE tl.labels ADD COLUMN egress boolean NOT NULL DEFAULT false;

ALTER TABLE tl.access_events DROP CONSTRAINT access_events_op_check;
ALTER TABLE tl.access_events ADD CONSTRAINT access_events_op_check
 CHECK (op IN ('asker','label','unlabel','label_kind','label_egress','grant','regrant','extend','revoke'));

-- A label kind with egress=false among the labels of a channel, a thread and
-- a message's own access_label.
CREATE FUNCTION tl.egress_held(cid bigint,tid bigint,lbl text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
 SELECT EXISTS(SELECT 1 FROM tl.labels k WHERE NOT k.egress AND (k.label=lbl
  OR EXISTS(SELECT 1 FROM tl.channel_labels l WHERE l.channel_id=cid AND l.label=k.label)
  OR EXISTS(SELECT 1 FROM tl.thread_labels l WHERE l.thread_id=tid AND l.label=k.label))) $$;

CREATE FUNCTION tl.message_egress_ok(mid bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
DECLARE rk text; held boolean;
BEGIN
 SELECT c.record_kind,tl.egress_held(m.channel_id,m.thread_id,m.access_label) INTO rk,held
 FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE m.id=mid;
 -- An id that names no stored message has nothing that could be vouched for.
 IF NOT FOUND OR held THEN RETURN false; END IF;
 IF rk IS NULL OR rk NOT IN ('page_revision','derived') THEN RETURN true; END IF;
 WITH RECURSIVE walk(node,depth,path,cycle) AS (
  SELECT p.source_message_id,1,ARRAY[mid,p.source_message_id],p.source_message_id=mid
  FROM tl.provenance p WHERE p.derived_kind='message' AND p.derived_id=mid AND p.origin IN ('source','owner','job')
  UNION ALL
  SELECT p.source_message_id,w.depth+1,w.path||p.source_message_id,p.source_message_id=ANY(w.path)
  FROM walk w JOIN tl.messages m ON m.id=w.node JOIN tl.channels c ON c.id=m.channel_id
  JOIN tl.provenance p ON p.derived_kind='message' AND p.derived_id=w.node AND p.origin IN ('source','owner','job')
  WHERE NOT w.cycle AND w.depth<=4 AND c.record_kind IN ('page_revision','derived')
 )
 SELECT EXISTS(SELECT 1 FROM walk w LEFT JOIN tl.messages m ON m.id=w.node
  WHERE w.cycle OR w.depth>4 OR m.id IS NULL OR tl.egress_held(m.channel_id,m.thread_id,m.access_label)) INTO held;
 RETURN NOT held;
END $$;

-- A conversation chunk holds the text of every current message of its range,
-- so it may leave only when each of them may.
CREATE FUNCTION tl.conversation_egress_ok(cid bigint) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
 SELECT NOT EXISTS(SELECT 1 FROM tl.conversations c
  JOIN tl.messages m ON m.thread_id=c.thread_id AND m.thread_no BETWEEN c.first_thread_no AND c.last_thread_no AND m.superseded_by IS NULL
  WHERE c.id=cid AND NOT tl.message_egress_ok(m.id)) $$;

-- The ids of a list that may not leave (unknown ids included).
CREATE FUNCTION tl.messages_egress_held(ids bigint[]) RETURNS SETOF bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,tl AS $$
 SELECT i FROM (SELECT DISTINCT i FROM unnest(ids) i WHERE i IS NOT NULL) x WHERE NOT tl.message_egress_ok(i) $$;

REVOKE EXECUTE ON FUNCTION tl.egress_held(bigint,bigint,text),tl.message_egress_ok(bigint),tl.conversation_egress_ok(bigint),tl.messages_egress_held(bigint[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tl.message_egress_ok(bigint),tl.conversation_egress_ok(bigint),tl.messages_egress_held(bigint[]) TO throughline_app;

UPDATE tl.catalog_sections SET body=body||jsonb_build_object(
  'egress','Content under a sensitivity label is embedded by a remote provider only when the owner allowed that label to leave the host (throughline lib access egress LABEL --allow). Held content has no vector and is still found by the keyword, entity, link and structured legs; a local embedding provider embeds everything.'),
 version=version+1
WHERE subject='embeddings';
