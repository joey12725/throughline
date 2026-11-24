ALTER ROLE throughline_reader NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
DO $$ DECLARE parent text; BEGIN
 FOR parent IN SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles member ON member.oid=m.member WHERE member.rolname='throughline_reader' LOOP
 EXECUTE format('REVOKE %I FROM throughline_reader',parent);
 END LOOP;
END $$;
