-- Classifiers in the catalogue (docs/v11/classifiers.md): the receipt kinds
-- the classifier routes issue, and a describe('classifiers') subject so an
-- agent finds the feature from the index.

UPDATE tl.catalog_sections
SET body = jsonb_set(
  body,
  '{receipt_kinds}',
  coalesce(body->'receipt_kinds', '[]'::jsonb) || jsonb_build_array('classifier', 'group_annotation', 'annotation_attribute')
),
version = coalesce(version, 1) + 1
WHERE subject = 'write_contract';

INSERT INTO tl.catalog_sections(subject,body,version) VALUES ('classifiers', jsonb_build_object(
  'summary','Classifiers write annotations automatically. kind message: one Jev or LLM question per arriving message (a JSON definition, no code). kind python: one sandboxed function over a whole thread (conversation, then participants, then messages). An agent''s definition is proposed until the owner approves it; owner annotations always win.',
  'start','GET /v1/classifiers/guide returns the workflow, the Python contract and worked examples.',
  'workflow',jsonb_build_array(
   'GET /v1/annotation-attributes (reuse one, or define a new attribute inside the classifier definition)',
   'POST /v1/classifiers/check {definition}',
   'POST /v1/classifiers/try {definition, sample: {limit: 5}}: writes nothing, spends a few cents',
   'POST /v1/classifiers {definition}: proposed for agents, active for the owner'),
  'tables',jsonb_build_object(
   'tl.annotation_attributes','the attribute registry: value_schema, recall_effect, flag',
   'tl.group_annotations','verdicts on a thread or a participant, copied onto messages (annotations.group_annotation_id)',
   'tl.classifiers','definitions, status, cursor, caps',
   'tl.classifier_threads','python classifiers: the verdict per judged thread'),
  'routes','GET /v1/classifiers/guide; GET /v1/classifiers; GET /v1/classifiers/{name}; GET /v1/classifiers/{name}/results; POST /v1/classifiers/check; POST /v1/classifiers/try; POST /v1/classifiers/draft; POST /v1/classifiers; POST /v1/classifiers/{name}/status|run|backfill|retract-marks; GET|POST /v1/annotation-attributes; GET /v1/threads/{id}/classification; POST /v1/group-annotations; POST /v1/group-annotations/{id}/retract',
  'cli','throughline lib classifiers guide|list|show|check|try|define|approve|pause|resume|reject|backfill|retract-marks|results|run|tick'),
  1)
ON CONFLICT (subject) DO UPDATE SET body=EXCLUDED.body,version=tl.catalog_sections.version+1;
