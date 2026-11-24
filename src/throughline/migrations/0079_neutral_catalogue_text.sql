-- Neutral wording in catalogue text that older migrations wrote.
--
-- 0047 described person_arc with a person's name from the original
-- deployment, and 0048 listed a connector-specific identity quality (now
-- bridge_id, see throughline.identity_quality; the old name still ranks the
-- same). Fresh databases already get the new wording from those files; this
-- brings upgraded databases to the same text.
UPDATE tl.api_functions
 SET description=regexp_replace(description,'^[A-Za-z]+''s arc: per period','Contact arc: per period')
 WHERE name='person_arc' AND description ~ '^[A-Za-z]+''s arc: per period';

UPDATE tl.catalog_sections
 SET body=regexp_replace(body::text,'default native_id, [a-z]+_id, fingerprint, unknown','default native_id, bridge_id, fingerprint, unknown')::jsonb
 WHERE subject='message_aliases' AND body::text ~ 'default native_id, [a-z]+_id, fingerprint, unknown';
