ALTER TABLE tl.identifiers ADD COLUMN original_value_norm text;
CREATE OR REPLACE VIEW api.identifiers AS SELECT * FROM tl.identifiers;
