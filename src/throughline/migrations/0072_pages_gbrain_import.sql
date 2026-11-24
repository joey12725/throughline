-- Pages: room for the owner's GBrain to move in (docs/v11/pages.md,
-- "Moving GBrain in").
--
-- * Slugs up to eight segments and 255 characters (GBrain paths are deep).
-- * tags (GBrain tags) and source_ref (the imported record a page came from,
--   '<channel>:<external_id>', so a re-run updates the same page).
-- * The kinds GBrain used that the base registry lacks.

ALTER TABLE tl.pages DROP CONSTRAINT IF EXISTS pages_slug_check;
ALTER TABLE tl.pages ADD CONSTRAINT pages_slug_check
  CHECK (slug ~ '^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*){0,7}$' AND length(slug) <= 255);
ALTER TABLE tl.pages ADD COLUMN tags text[] NOT NULL DEFAULT '{}';
ALTER TABLE tl.pages ADD COLUMN source_ref text UNIQUE;
CREATE INDEX pages_tags ON tl.pages USING gin(tags);

INSERT INTO tl.seed_hooks(name,ord,statement) VALUES
 ('page_kinds_gbrain',22,$seed$INSERT INTO tl.page_kinds(kind,description,sections,guidance,half_life_days,status,origin) VALUES
 ('concepts','An idea, framework or concept (from GBrain)',ARRAY['Summary','Related'],'',NULL,'active','core'),
 ('commitments','One commitment or promise (from GBrain)',ARRAY['What','Who','Status'],'',60,'active','core'),
 ('conversations','A conversation worth keeping as a page (from GBrain)',ARRAY['Summary','Notable'],'',180,'active','core'),
 ('reports','A report or analysis (from GBrain)',ARRAY['Summary','Findings'],'',180,'active','core'),
 ('documents','A document or note (from GBrain)',ARRAY[]::text[],'',NULL,'active','core'),
 ('dossiers','A dossier collecting material about a subject (from GBrain)',ARRAY[]::text[],'',NULL,'active','core'),
 ('indexes','An index page listing other pages (from GBrain)',ARRAY[]::text[],'',NULL,'active','core'),
 ('routines','A routine or recurring practice (from GBrain)',ARRAY['What','When','Notes'],'',NULL,'active','core'),
 ('notes','A free-form note',ARRAY[]::text[],'',NULL,'active','core'),
 ('pets','A pet',ARRAY['Who they are','Care','Important details'],'',NULL,'active','core')
 ON CONFLICT (kind) DO NOTHING$seed$);
DO $$ BEGIN EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='page_kinds_gbrain'); END $$;
