-- Purge of a large channel (e.g. 297k reddit.feed rows) never finished: the
-- write_receipts hook from 0048 compared every receipt node against a jsonb
-- array of every purged id inside one jsonpath, so the cost was
-- receipts x nodes x ids. This version walks each receipt once and checks the
-- referenced ids against hashed id sets. It matches the same references as
-- before: an object whose kind is message/link/claim with a numeric id in the
-- purged set, or a message_id/link_id/claim_id (scalar or array) in it.
UPDATE tl.purge_hooks SET statement =
 'WITH msgs AS MATERIALIZED (SELECT to_jsonb(m.id) AS j, m.id FROM tl.messages m WHERE m.channel_id=$1),
   lnk AS MATERIALIZED (SELECT to_jsonb(l.id) AS j FROM tl.links l
     WHERE (l.from_kind=''message'' AND l.from_id IN (SELECT id FROM msgs))
        OR (l.to_kind=''message'' AND l.to_id IN (SELECT id FROM msgs))),
   clm AS MATERIALIZED (SELECT to_jsonb(c.id) AS j FROM tl.claims c
     WHERE c.revision_message_id IN (SELECT id FROM msgs)
        OR EXISTS(SELECT 1 FROM tl.claim_sources s WHERE s.claim_id=c.id AND s.message_id IN (SELECT id FROM msgs))),
   refs AS (
     SELECT r.token_id, r.idempotency_key, x.kind, e.v
     FROM tl.write_receipts r
     CROSS JOIN LATERAL jsonb_path_query(r.response, ''strict $.**'') AS o(node)
     CROSS JOIN LATERAL (VALUES
       (CASE o.node->>''kind'' WHEN ''message'' THEN ''m'' WHEN ''link'' THEN ''l'' WHEN ''claim'' THEN ''c'' END, o.node->''id''),
       (''m'', o.node->''message_id''),
       (''l'', o.node->''link_id''),
       (''c'', o.node->''claim_id'')) AS x(kind, val)
     CROSS JOIN LATERAL jsonb_array_elements(
       CASE WHEN jsonb_typeof(x.val)=''array'' THEN x.val ELSE jsonb_build_array(x.val) END) AS e(v)
     WHERE jsonb_typeof(o.node)=''object'' AND x.kind IS NOT NULL AND x.val IS NOT NULL
       AND jsonb_typeof(e.v)=''number''),
   doomed AS (
     SELECT token_id, idempotency_key FROM refs WHERE kind=''m'' AND v IN (SELECT j FROM msgs)
     UNION SELECT token_id, idempotency_key FROM refs WHERE kind=''l'' AND v IN (SELECT j FROM lnk)
     UNION SELECT token_id, idempotency_key FROM refs WHERE kind=''c'' AND v IN (SELECT j FROM clm))
   DELETE FROM tl.write_receipts r WHERE (r.token_id, r.idempotency_key) IN (SELECT token_id, idempotency_key FROM doomed)'
WHERE name='write_receipts';
