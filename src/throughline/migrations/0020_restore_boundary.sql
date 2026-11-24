-- pg_dump excludes catalog function/database ACLs. Reapply the boundary after
-- a restore and on subsequent migration runs, without touching role passwords.
CREATE FUNCTION tl.harden_reader() RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,tl,api AS $$
DECLARE f regprocedure; parent text;
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
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN ('pg_catalog','public') AND (p.proname ~ '^(lo_|pg_read_|pg_ls_|dblink|query_to_xml|table_to_xml|database_to_xml|schema_to_xml|pg_advisory_|pg_try_advisory_)' OR p.proname IN ('ts_stat','set_config','pg_notify','pg_logical_emit_message','pg_cancel_backend','pg_terminate_backend','cursor_to_xml','cursor_to_xmlschema')) LOOP
  EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC,throughline_reader',f);
  IF f::text ~ '(pg_advisory_|set_config)' THEN EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO throughline_app',f); END IF;
 END LOOP;
 EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC,throughline_reader',current_database());
 EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO throughline_app',current_database());
 GRANT SELECT ON ALL TABLES IN SCHEMA api TO throughline_reader,throughline_app;
 GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA api TO throughline_reader,throughline_app;
END $$;
REVOKE EXECUTE ON FUNCTION tl.harden_reader() FROM PUBLIC,throughline_app,throughline_reader;
SELECT tl.harden_reader();
