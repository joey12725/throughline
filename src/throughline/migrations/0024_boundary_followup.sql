-- Follow-up to the review boundary (0010/0020).
--
-- 0010 granted throughline_app EXECUTE on every function it revoked from
-- PUBLIC, including server file access (pg_read_*, pg_ls_*), large-object
-- import/export, dblink and the *_to_xml query runners. The runtime role needs
-- none of those. They are revoked here, and by tl.harden_reader() on every
-- later migration run and after a restore.
--
-- Audit rows gain a structured detail (route parameters, never request bodies)
-- and entities record the token that created them.
ALTER TABLE tl.audit_log ADD COLUMN detail jsonb;
ALTER TABLE tl.entities ADD COLUMN token_id bigint REFERENCES tl.tokens;
CREATE TRIGGER attribute_token BEFORE INSERT ON tl.entities FOR EACH ROW EXECUTE FUNCTION tl.attribute_token();

CREATE OR REPLACE FUNCTION tl.harden_reader() RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,tl,api AS $$
DECLARE f regprocedure; parent text; leaked text;
BEGIN
 REVOKE ALL ON ALL TABLES IN SCHEMA tl FROM throughline_reader;
 REVOKE ALL ON SCHEMA tl FROM throughline_reader;
 REVOKE CREATE ON SCHEMA public FROM PUBLIC,throughline_reader;
 REVOKE UPDATE,DELETE,TRUNCATE ON tl.audit_log,tl.identity_events FROM throughline_app;
 ALTER ROLE throughline_reader NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
 ALTER ROLE throughline_reader SET default_transaction_read_only=on;
 ALTER ROLE throughline_reader SET statement_timeout='30s';
 ALTER ROLE throughline_reader SET search_path=pg_catalog,api;
 ALTER ROLE throughline_reader SET work_mem='4MB';
 FOR parent IN SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles member ON member.oid=m.member WHERE member.rolname='throughline_reader' LOOP
  EXECUTE format('REVOKE %I FROM throughline_reader',parent);
 END LOOP;
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' LOOP
  EXECUTE format('ALTER FUNCTION %s SECURITY DEFINER SET search_path=pg_catalog,api,public',f);
 END LOOP;
 -- PUBLIC grants are inherited, so they are revoked together with the roles.
 -- The app role keeps only what the API calls: transaction/session advisory
 -- locks and set_config (token attribution).
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN ('pg_catalog','public') AND (p.proname ~ '^(lo_|pg_read_|pg_ls_|dblink|query_to_xml|table_to_xml|database_to_xml|schema_to_xml|pg_advisory_|pg_try_advisory_)' OR p.proname IN ('ts_stat','set_config','pg_notify','pg_logical_emit_message','pg_cancel_backend','pg_terminate_backend','cursor_to_xml','cursor_to_xmlschema')) LOOP
  EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC,throughline_reader,throughline_app',f);
 END LOOP;
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='pg_catalog' AND (p.proname ~ '^pg_advisory_' OR p.proname='set_config') LOOP
  EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO throughline_app',f);
 END LOOP;
 EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC,throughline_reader',current_database());
 EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO throughline_app',current_database());
 GRANT SELECT ON ALL TABLES IN SCHEMA api TO throughline_reader,throughline_app;
 GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA api TO throughline_reader,throughline_app;
 -- A role that does not own the catalog functions gets only a warning from
 -- REVOKE. Verify the result so an unapplied boundary stops the migration
 -- instead of passing silently.
 SELECT string_agg(r.rolname||': '||p.oid::regprocedure::text,', ') INTO leaked
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN pg_roles r
 WHERE n.nspname IN ('pg_catalog','public') AND has_function_privilege(r.oid,p.oid,'EXECUTE') AND (
  (r.rolname='throughline_reader' AND (p.proname ~ '^(lo_|pg_read_|pg_ls_|dblink|query_to_xml|table_to_xml|database_to_xml|schema_to_xml|pg_advisory_|pg_try_advisory_)' OR p.proname IN ('ts_stat','set_config','pg_notify','pg_logical_emit_message','pg_cancel_backend','pg_terminate_backend','cursor_to_xml','cursor_to_xmlschema')))
  OR (r.rolname='throughline_app' AND (p.proname ~ '^(lo_|pg_read_|pg_ls_|dblink|query_to_xml|table_to_xml|database_to_xml|schema_to_xml)' OR p.proname IN ('ts_stat','cursor_to_xml','cursor_to_xmlschema'))));
 IF leaked IS NOT NULL THEN
  RAISE EXCEPTION 'role boundary was not applied (migrate with a superuser): %',left(leaked,600);
 END IF;
 IF has_schema_privilege('throughline_reader','tl','USAGE') OR has_database_privilege('throughline_reader',current_database(),'TEMPORARY') OR has_schema_privilege('throughline_reader','public','CREATE') THEN
  RAISE EXCEPTION 'reader boundary was not applied (migrate with a superuser)';
 END IF;
END $$;
REVOKE EXECUTE ON FUNCTION tl.harden_reader() FROM PUBLIC,throughline_app,throughline_reader;
SELECT tl.harden_reader();
