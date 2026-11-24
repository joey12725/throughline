ALTER TABLE tl.identifiers ADD COLUMN original_asserted_by text;
ALTER TABLE tl.facts ADD COLUMN original_asserted_by text;
ALTER TABLE tl.relations ADD COLUMN original_asserted_by text;
-- Old NULL-scheme rows cannot be attributed to a platform from stored data.
-- Preserve them under unique explicit legacy namespaces for owner reconciliation.
UPDATE tl.identifiers SET system=coalesce(system,'legacy_unresolved'),kind=coalesce(kind,'legacy_unresolved_'||id::text) WHERE system IS NULL OR kind IS NULL;
ALTER TABLE tl.identifiers ALTER COLUMN system SET NOT NULL;
ALTER TABLE tl.identifiers ALTER COLUMN kind SET NOT NULL;
GRANT DELETE ON tl.identifiers TO throughline_app;
CREATE OR REPLACE VIEW api.identifiers AS SELECT * FROM tl.identifiers;
CREATE OR REPLACE VIEW api.facts AS SELECT * FROM tl.facts WHERE retired_at IS NULL;
CREATE OR REPLACE VIEW api.facts_history AS SELECT * FROM tl.facts;
CREATE OR REPLACE VIEW api.relations_history AS SELECT * FROM tl.relations;
CREATE OR REPLACE VIEW api.people AS SELECT * FROM tl.people;
CREATE OR REPLACE VIEW api.links AS SELECT * FROM tl.links;
-- Repair references left by versions created before this release.
UPDATE tl.links SET from_id=api.current_message(from_id) WHERE from_kind='message' AND api.current_message(from_id) IS NOT NULL;
UPDATE tl.links SET to_id=api.current_message(to_id) WHERE to_kind='message' AND api.current_message(to_id) IS NOT NULL;
DELETE FROM tl.links a USING tl.links b WHERE a.id>b.id AND a.status='active' AND b.status='active' AND (to_jsonb(a)-ARRAY['id','created_at'])=(to_jsonb(b)-ARRAY['id','created_at']);
