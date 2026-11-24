-- Retire recipes that left core.
--
-- Four recipes queried the channels of one particular connector (coding-agent
-- usage and work events) and now ship with that connector instead. Stored
-- recipe versions are content-hashed and stay listed while they are the
-- newest tested version, so each one gets an explicit retired version here;
-- throughline.recipes.sync then drops its recipe:<name> catalogue section.
-- A fresh database has none of them stored and gets nothing.
INSERT INTO tl.recipes(name,version,schema_version,status,summary,body,statements,expect,needs,content_hash)
SELECT r.name, max(r.version)+1, '0075', 'retired',
 'Retired from core; the recipe ships with the plugin that owns its channels.',
 'Retired from core by migration 0075. Install the plugin that owns the channels to get it back.' || chr(10),
 '["SELECT 1"]'::jsonb, '{}'::jsonb, '[]'::jsonb,
 md5('retired:' || r.name || ':' || (max(r.version)+1)::text)
FROM tl.recipes r
WHERE r.name IN ('burn_vs_calendar','subscription_status','work_cadence','work_concentration')
GROUP BY r.name
HAVING (array_agg(r.status ORDER BY r.version DESC))[1] <> 'retired';
