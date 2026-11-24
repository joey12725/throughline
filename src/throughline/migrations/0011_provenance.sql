ALTER TABLE tl.tokens ADD COLUMN location_source boolean NOT NULL DEFAULT false;
CREATE FUNCTION tl.attribute_token() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$ BEGIN
 NEW.token_id := nullif(current_setting('throughline.token_id',true),'')::bigint;
 RETURN NEW;
END $$;
CREATE TRIGGER attribute_token BEFORE INSERT ON tl.links FOR EACH ROW EXECUTE FUNCTION tl.attribute_token();
CREATE TRIGGER attribute_token BEFORE INSERT ON tl.identity_events FOR EACH ROW EXECUTE FUNCTION tl.attribute_token();
REVOKE EXECUTE ON FUNCTION tl.attribute_token() FROM PUBLIC;
CREATE TABLE tl.message_locations(message_id bigint PRIMARY KEY REFERENCES tl.messages,track_id bigint NOT NULL REFERENCES tl.location_track);
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.message_locations TO throughline_app;
CREATE VIEW api.message_locations AS SELECT ml.*,t.source FROM tl.message_locations ml JOIN tl.location_track t ON t.id=ml.track_id;
GRANT SELECT ON api.message_locations TO throughline_app,throughline_reader;
