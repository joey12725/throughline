-- Fast passage views and passage_scope without scanning tl.message_versions_all.
--
-- Migration 0037 defined tl.passages_all and tl.passage_records_all by joining
-- tl.message_versions_all (whose materialized dirty_threads CTE prevents
-- PostgreSQL from pushing channel or message_id filters down, forcing a full
-- sequential scan of tl.messages), and tl.passage_scope read both
-- api.message_versions and api.passages (scanning all of tl.messages twice and
-- all of tl.passages twice before filtering to the chosen page revisions).
--
-- This migration keeps the exact column contracts, security barriers, and
-- authorized-revision fallback while reading tl.messages and tl.channels
-- directly.

-- 1. Gated passage views without tl.message_versions_all -----------------------
SELECT tl.install_gate('passages', NULL, $def$
SELECT p.id AS passage_id,
 'p:'||p.id AS record_id,
 p.message_id,
 p.channel_id,
 c.name AS channel,
 p.external_id,
 p.version,
 m.superseded_by IS NULL AS is_current,
 p.ordinal,
 p.heading_path,
 p.char_start,
 p.char_end,
 p.content,
 p.token_estimate,
 m.happened_at AS revision_time,
 m.thread_id,
 p.tsv
FROM tl.passages p
JOIN tl.messages m ON m.id=p.message_id
JOIN tl.channels c ON c.id=p.channel_id
$def$);

SELECT tl.install_gate('passage_records', NULL, $def$
SELECT 'p:'||p.id AS record_id,
 'passage'::text AS kind,
 p.id AS ref_id,
 p.message_id,
 p.version,
 x.superseded_by IS NULL AS is_current,
 c.name AS channel,
 p.external_id,
 x.thread_id,
 coalesce(
  CASE WHEN x.thread_id IS NOT NULL AND x.superseded_by IS NULL
    AND EXISTS(SELECT 1 FROM tl.dirty_threads d WHERE d.thread_id=x.thread_id)
   THEN (SELECT n.no FROM (
    SELECT m1.id, row_number() OVER (ORDER BY m1.happened_at, m1.external_id)::int AS no
    FROM tl.messages m1
    WHERE m1.thread_id=x.thread_id AND m1.superseded_by IS NULL) n WHERE n.id=x.id)
  END, x.thread_no) AS thread_no,
 p.content,
 x.happened_at AS record_time,
 x.ended_at,
 x.ingested_at,
 x.subject_from,
 x.subject_to,
 x.subject_from_precision,
 x.subject_to_precision,
 x.person_id,
 x.author_handle,
 x.lat,
 x.lon,
 x.loc_reason,
 c.is_outside,
 p.heading_path,
 p.char_start,
 p.char_end,
 NULL::jsonb AS claim,
 true AS generated,
 s.support='[]'::jsonb AS unsupported,
 jsonb_build_object('plugin',c.plugin,'channel',c.name,'external_id',p.external_id,'version',p.version,'method','passage','ordinal',p.ordinal) AS provenance,
 s.support,
 jsonb_build_object('page',jsonb_build_array(c.name,p.external_id),'revision',p.message_id,'sources',true) AS expansion
FROM tl.passages p
JOIN tl.messages x ON x.id=p.message_id
JOIN tl.channels c ON c.id=p.channel_id
CROSS JOIN LATERAL (SELECT coalesce(
  (SELECT jsonb_agg(jsonb_build_object('message_id',pv.source_message_id,'version',pv.source_version,'relation',pv.relation) ORDER BY pv.source_message_id)
   FROM tl.provenance pv WHERE pv.derived_kind='passage' AND pv.derived_id=p.id AND pv.origin IN ('source','owner','job')),
  api.support_of('page_revision',p.message_id,p.version)) AS support) s
$def$);

-- 2. Fast tl.passage_scope -----------------------------------------------------
-- Unrestricted callers skip the row_visible / provenance gate and, for current
-- revisions (no as_of / known_by and current_only true), read superseded_by IS
-- NULL directly through the channel indexes instead of scanning every message.
-- Restricted askers materialize the revision and passage visibility gates
-- before evaluating any caller predicate.
CREATE OR REPLACE FUNCTION tl.passage_scope(
 q tsquery,
 channels text[],
 as_of timestamptz,
 current_only boolean,
 after_at timestamptz,
 before_at timestamptz,
 time_basis text,
 thread_ids bigint[],
 message_ids bigint[],
 must_terms text[],
 source_ids bigint[],
 exclude_message_ids bigint[],
 known_by timestamptz)
RETURNS TABLE(passage_id bigint,message_id bigint,text_hash text,tsv tsvector)
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,public AS $$
#variable_conflict use_column
DECLARE
 cur_only boolean := coalesce(passage_scope.current_only, true);
 tb text := coalesce(passage_scope.time_basis, 'record');
BEGIN
 IF api.asker_unrestricted() THEN
  IF passage_scope.as_of IS NULL AND passage_scope.known_by IS NULL AND cur_only THEN
   IF passage_scope.q IS NOT NULL THEN
    RETURN QUERY
     SELECT p.id, p.message_id, p.text_hash, p.tsv
     FROM tl.passages p
     JOIN tl.messages r ON r.id = p.message_id
     JOIN tl.channels c ON c.id = r.channel_id
     WHERE c.record_kind = 'page_revision'
      AND r.superseded_by IS NULL
      AND p.tsv @@ passage_scope.q
      AND (passage_scope.channels IS NULL OR c.name = ANY(passage_scope.channels))
      AND (passage_scope.thread_ids IS NULL OR r.thread_id = ANY(passage_scope.thread_ids))
      AND (passage_scope.message_ids IS NULL OR p.message_id = ANY(passage_scope.message_ids))
      AND (passage_scope.exclude_message_ids IS NULL OR NOT (p.message_id = ANY(passage_scope.exclude_message_ids)))
      AND (passage_scope.must_terms IS NULL OR NOT EXISTS(
       SELECT 1 FROM unnest(passage_scope.must_terms) t(term)
       WHERE numnode(plainto_tsquery('english',t.term))>0 AND NOT p.tsv @@ plainto_tsquery('english',t.term)))
      AND tl.passage_time_ok(r.happened_at,r.ended_at,r.subject_from,r.subject_to,r.subject_from_precision,r.subject_to_precision,passage_scope.after_at,passage_scope.before_at,tb)
      AND (passage_scope.source_ids IS NULL OR EXISTS(
       SELECT 1 FROM tl.provenance pv
       JOIN tl.messages s ON s.id=pv.source_message_id
       JOIN tl.messages k ON k.channel_id=s.channel_id AND k.external_id=s.external_id
       WHERE k.id=ANY(passage_scope.source_ids) AND pv.origin IN ('source','owner','job')
        AND ((pv.derived_kind='passage' AND pv.derived_id=p.id)
         OR (pv.derived_kind='message' AND pv.derived_id=p.message_id AND NOT EXISTS(
          SELECT 1 FROM tl.provenance pp WHERE pp.derived_kind='passage' AND pp.derived_id=p.id AND pp.origin IN ('source','owner','job'))))));
    RETURN;
   END IF;
   RETURN QUERY
    WITH chosen AS MATERIALIZED (
     SELECT r.id
     FROM tl.channels c
     JOIN tl.messages r ON r.channel_id = c.id
     WHERE c.record_kind = 'page_revision'
      AND r.superseded_by IS NULL
      AND (passage_scope.channels IS NULL OR c.name = ANY(passage_scope.channels))
      AND (passage_scope.thread_ids IS NULL OR r.thread_id = ANY(passage_scope.thread_ids))
      AND (passage_scope.message_ids IS NULL OR r.id = ANY(passage_scope.message_ids))
      AND (passage_scope.exclude_message_ids IS NULL OR NOT (r.id = ANY(passage_scope.exclude_message_ids)))
      AND tl.passage_time_ok(r.happened_at,r.ended_at,r.subject_from,r.subject_to,r.subject_from_precision,r.subject_to_precision,passage_scope.after_at,passage_scope.before_at,tb)
    )
    SELECT p.id, p.message_id, p.text_hash, p.tsv
    FROM chosen ch
    JOIN tl.passages p ON p.message_id = ch.id
    WHERE (passage_scope.must_terms IS NULL OR NOT EXISTS(
      SELECT 1 FROM unnest(passage_scope.must_terms) t(term)
      WHERE numnode(plainto_tsquery('english',t.term))>0 AND NOT p.tsv @@ plainto_tsquery('english',t.term)))
     AND (passage_scope.source_ids IS NULL OR EXISTS(
      SELECT 1 FROM tl.provenance pv
      JOIN tl.messages s ON s.id=pv.source_message_id
      JOIN tl.messages k ON k.channel_id=s.channel_id AND k.external_id=s.external_id
      WHERE k.id=ANY(passage_scope.source_ids) AND pv.origin IN ('source','owner','job')
       AND ((pv.derived_kind='passage' AND pv.derived_id=p.id)
        OR (pv.derived_kind='message' AND pv.derived_id=p.message_id AND NOT EXISTS(
         SELECT 1 FROM tl.provenance pp WHERE pp.derived_kind='passage' AND pp.derived_id=p.id AND pp.origin IN ('source','owner','job'))))));
   RETURN;
  END IF;

  -- Unrestricted historical or all-revisions path.
  RETURN QUERY
   WITH revs AS MATERIALIZED (
    SELECT m.id, m.channel_id, m.external_id, m.version, m.happened_at
    FROM tl.channels c
    JOIN tl.messages m ON m.channel_id = c.id
    WHERE c.record_kind = 'page_revision'
     AND (passage_scope.channels IS NULL OR c.name = ANY(passage_scope.channels))
     AND (passage_scope.as_of IS NULL OR m.happened_at <= passage_scope.as_of)
     AND (passage_scope.known_by IS NULL OR m.ingested_at <= passage_scope.known_by)
   ), chosen AS MATERIALIZED (
    SELECT r.id FROM revs r WHERE passage_scope.as_of IS NULL AND NOT cur_only
    UNION ALL
    (SELECT DISTINCT ON (r.channel_id, r.external_id) r.id FROM revs r
     WHERE passage_scope.as_of IS NOT NULL OR cur_only
     ORDER BY r.channel_id, r.external_id,
      CASE WHEN passage_scope.as_of IS NOT NULL THEN r.happened_at END DESC NULLS LAST,
      r.version DESC)
   )
   SELECT p.id, p.message_id, p.text_hash, p.tsv
   FROM chosen ch
   JOIN tl.passages p ON p.message_id = ch.id
   JOIN tl.messages r ON r.id = p.message_id
   WHERE (passage_scope.q IS NULL OR p.tsv @@ passage_scope.q)
    AND (passage_scope.thread_ids IS NULL OR r.thread_id = ANY(passage_scope.thread_ids))
    AND (passage_scope.message_ids IS NULL OR p.message_id = ANY(passage_scope.message_ids))
    AND (passage_scope.exclude_message_ids IS NULL OR NOT (p.message_id = ANY(passage_scope.exclude_message_ids)))
    AND (passage_scope.must_terms IS NULL OR NOT EXISTS(
     SELECT 1 FROM unnest(passage_scope.must_terms) t(term)
     WHERE numnode(plainto_tsquery('english',t.term))>0 AND NOT p.tsv @@ plainto_tsquery('english',t.term)))
    AND tl.passage_time_ok(r.happened_at,r.ended_at,r.subject_from,r.subject_to,r.subject_from_precision,r.subject_to_precision,passage_scope.after_at,passage_scope.before_at,tb)
    AND (passage_scope.source_ids IS NULL OR EXISTS(
     SELECT 1 FROM tl.provenance pv
     JOIN tl.messages s ON s.id=pv.source_message_id
     JOIN tl.messages k ON k.channel_id=s.channel_id AND k.external_id=s.external_id
     WHERE k.id=ANY(passage_scope.source_ids) AND pv.origin IN ('source','owner','job')
      AND ((pv.derived_kind='passage' AND pv.derived_id=p.id)
       OR (pv.derived_kind='message' AND pv.derived_id=p.message_id AND NOT EXISTS(
        SELECT 1 FROM tl.provenance pp WHERE pp.derived_kind='passage' AND pp.derived_id=p.id AND pp.origin IN ('source','owner','job'))))));
  RETURN;
 END IF;

 -- Restricted asker: materialize visibility gates before any caller predicate.
 RETURN QUERY
  WITH revs_visible AS MATERIALIZED (
   SELECT m.id, m.channel_id, c.name AS channel, m.external_id, m.version, m.happened_at, m.ingested_at
   FROM tl.channels c
   JOIN tl.messages m ON m.channel_id = c.id
   WHERE c.record_kind = 'page_revision'
    AND api.row_visible(m.id, m.channel_id, m.thread_id)
  ), revs AS MATERIALIZED (
   SELECT r.id, r.channel_id, r.external_id, r.version, r.happened_at
   FROM revs_visible r
   WHERE (passage_scope.channels IS NULL OR r.channel = ANY(passage_scope.channels))
    AND (passage_scope.as_of IS NULL OR r.happened_at <= passage_scope.as_of)
    AND (passage_scope.known_by IS NULL OR r.ingested_at <= passage_scope.known_by)
  ), chosen AS MATERIALIZED (
   SELECT r.id FROM revs r WHERE passage_scope.as_of IS NULL AND NOT cur_only
   UNION ALL
   (SELECT DISTINCT ON (r.channel_id, r.external_id) r.id FROM revs r
    WHERE passage_scope.as_of IS NOT NULL OR cur_only
    ORDER BY r.channel_id, r.external_id,
     CASE WHEN passage_scope.as_of IS NOT NULL THEN r.happened_at END DESC NULLS LAST,
     r.version DESC)
  ), vis AS MATERIALIZED (
   SELECT p.id AS passage_id, p.message_id, p.text_hash, p.tsv,
    r.thread_id, r.happened_at, r.ended_at, r.subject_from, r.subject_to,
    r.subject_from_precision, r.subject_to_precision
   FROM chosen ch
   JOIN tl.passages p ON p.message_id = ch.id
   JOIN tl.messages r ON r.id = p.message_id
   WHERE NOT EXISTS(
    SELECT 1 FROM tl.provenance s
    WHERE s.derived_kind='passage' AND s.derived_id=p.id
     AND s.origin IN ('source','owner','job')
     AND NOT api.message_visible(s.source_message_id))
  )
  SELECT v.passage_id, v.message_id, v.text_hash, v.tsv
  FROM vis v
  WHERE (passage_scope.q IS NULL OR v.tsv @@ passage_scope.q)
   AND (passage_scope.thread_ids IS NULL OR v.thread_id = ANY(passage_scope.thread_ids))
   AND (passage_scope.message_ids IS NULL OR v.message_id = ANY(passage_scope.message_ids))
   AND (passage_scope.exclude_message_ids IS NULL OR NOT (v.message_id = ANY(passage_scope.exclude_message_ids)))
   AND (passage_scope.must_terms IS NULL OR NOT EXISTS(
    SELECT 1 FROM unnest(passage_scope.must_terms) t(term)
    WHERE numnode(plainto_tsquery('english',t.term))>0 AND NOT v.tsv @@ plainto_tsquery('english',t.term)))
   AND tl.passage_time_ok(v.happened_at,v.ended_at,v.subject_from,v.subject_to,v.subject_from_precision,v.subject_to_precision,passage_scope.after_at,passage_scope.before_at,tb)
   AND (passage_scope.source_ids IS NULL OR EXISTS(
    SELECT 1 FROM tl.provenance pv
    JOIN tl.messages s ON s.id=pv.source_message_id
    JOIN tl.messages k ON k.channel_id=s.channel_id AND k.external_id=s.external_id
    WHERE k.id=ANY(passage_scope.source_ids) AND pv.origin IN ('source','owner','job')
     AND ((pv.derived_kind='passage' AND pv.derived_id=v.passage_id)
      OR (pv.derived_kind='message' AND pv.derived_id=v.message_id AND NOT EXISTS(
       SELECT 1 FROM tl.provenance pp WHERE pp.derived_kind='passage' AND pp.derived_id=v.passage_id AND pp.origin IN ('source','owner','job'))))));
END $$;
REVOKE EXECUTE ON FUNCTION tl.passage_scope(tsquery,text[],timestamptz,boolean,timestamptz,timestamptz,text,bigint[],bigint[],text[],bigint[],bigint[],timestamptz) FROM PUBLIC;
