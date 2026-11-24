-- Asker scope enforced in the database.
--
-- The service places four settings in every reader transaction:
--   throughline.scope_mode     owner | asker | deny
--   throughline.asker          the asker named by the request
--   throughline.unknown_asker  unrestricted | deny (an asker without a row)
--   throughline.token_id
-- A connection that sets none of them (a direct database connection, the
-- application role) is unrestricted; the service always sets a mode.
--
-- Every api view that exposes per-record columns is built by tl.install_gate:
--   tl.<view>_all    the definition (reads tl tables or other tl.*_all views)
--   tl.<view>_gated  security barrier: SELECT * FROM tl.<view>_all v WHERE <predicate>
--   api.<view>       _all for unrestricted askers UNION ALL _gated for the rest
-- Unrestricted askers keep predicate push-down and the full-text index. For a
-- restricted asker no caller predicate can run below the barrier, so a failing
-- expression (division by zero, a cast) cannot report on hidden rows.

-- 1. Askers, grants and the policy version ---------------------------------
CREATE TABLE tl.askers(
 name text PRIMARY KEY CHECK(length(name) BETWEEN 1 AND 200),
 mode text NOT NULL DEFAULT 'restricted' CHECK(mode IN ('owner','restricted')),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE tl.asker_grants(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 asker text NOT NULL REFERENCES tl.askers ON DELETE CASCADE ON UPDATE CASCADE,
 channel_id bigint REFERENCES tl.channels ON DELETE CASCADE,
 thread_id bigint REFERENCES tl.threads ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(num_nonnulls(channel_id,thread_id)=1),
 UNIQUE NULLS NOT DISTINCT(asker,channel_id,thread_id)
);
CREATE INDEX asker_grants_channel ON tl.asker_grants(channel_id) WHERE channel_id IS NOT NULL;
CREATE INDEX asker_grants_thread ON tl.asker_grants(thread_id) WHERE thread_id IS NOT NULL;
CREATE TABLE tl.policy_state(
 one boolean PRIMARY KEY DEFAULT true CHECK(one),
 version bigint NOT NULL DEFAULT 1
);
INSERT INTO tl.policy_state DEFAULT VALUES;
-- SECURITY DEFINER: the application role may change nothing here, but a token
-- change made through any role must still move the version.
CREATE FUNCTION tl.bump_policy() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tl AS $$ BEGIN
 INSERT INTO tl.policy_state(one,version) VALUES(true,2) ON CONFLICT(one) DO UPDATE SET version=tl.policy_state.version+1;
 RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION tl.bump_policy() FROM PUBLIC;
CREATE TRIGGER bump_policy AFTER INSERT OR UPDATE OR DELETE ON tl.askers FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();
CREATE TRIGGER bump_policy_truncate AFTER TRUNCATE ON tl.askers FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();
CREATE TRIGGER bump_policy AFTER INSERT OR UPDATE OR DELETE ON tl.asker_grants FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();
CREATE TRIGGER bump_policy_truncate AFTER TRUNCATE ON tl.asker_grants FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();
CREATE TRIGGER bump_policy AFTER UPDATE OF askers,revoked_at ON tl.tokens FOR EACH STATEMENT EXECUTE FUNCTION tl.bump_policy();
-- Runtime roles read policies and never write them (owner connection only).
GRANT SELECT ON tl.askers,tl.asker_grants,tl.policy_state TO throughline_app;

-- 2. Gate functions ---------------------------------------------------------
-- They live in schema api because views run their functions as the caller, and
-- the reader has no access to schema tl. None is on the query allow-list.
-- Per-row functions are plpgsql: their plans are cached across calls.
CREATE FUNCTION api.asker_unrestricted() RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE scope text := coalesce(current_setting('throughline.scope_mode',true),''); asker_mode text;
BEGIN
 IF scope IN ('','owner') THEN RETURN true; END IF;
 -- 'deny' and any value this version does not know fail closed.
 IF scope<>'asker' THEN RETURN false; END IF;
 SELECT a.mode INTO asker_mode FROM tl.askers a WHERE a.name=current_setting('throughline.asker',true);
 IF FOUND THEN RETURN asker_mode='owner'; END IF;
 RETURN current_setting('throughline.unknown_asker',true) IS DISTINCT FROM 'deny';
END $$;
CREATE FUNCTION api.asker_sees(channel_id bigint,thread_id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF current_setting('throughline.scope_mode',true) IS DISTINCT FROM 'asker' THEN RETURN false; END IF;
 RETURN EXISTS(SELECT 1 FROM tl.asker_grants g WHERE g.asker=current_setting('throughline.asker',true) AND (g.channel_id=asker_sees.channel_id OR g.thread_id=asker_sees.thread_id));
END $$;
-- Migration 0036 replaces this function (same signature) to add the rule that
-- page revisions and derived records also need every source visible.
CREATE FUNCTION api.row_visible(message_id bigint,channel_id bigint,thread_id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 -- Same rule as api.asker_sees, written out: this runs once per scanned row.
 IF current_setting('throughline.scope_mode',true) IS DISTINCT FROM 'asker' THEN RETURN false; END IF;
 RETURN EXISTS(SELECT 1 FROM tl.asker_grants g WHERE g.asker=current_setting('throughline.asker',true) AND (g.channel_id=row_visible.channel_id OR g.thread_id=row_visible.thread_id));
END $$;
CREATE FUNCTION api.message_visible(message_id bigint) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE cid bigint; tid bigint;
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 SELECT m.channel_id,m.thread_id INTO cid,tid FROM tl.messages m WHERE m.id=message_visible.message_id;
 IF NOT FOUND THEN RETURN false; END IF;
 RETURN coalesce(api.row_visible(message_visible.message_id,cid,tid),false);
END $$;
-- An empty or missing source list proves nothing, so only unrestricted askers
-- see a row that has one.
CREATE FUNCTION api.sources_visible(ids bigint[]) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE i bigint;
BEGIN
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 IF coalesce(cardinality(ids),0)=0 THEN RETURN false; END IF;
 FOREACH i IN ARRAY ids LOOP
  IF NOT api.message_visible(i) THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
END $$;
-- Link evidence that names messages (evidence->'message_ids', a non-empty
-- array) is visible only when every named message is. Anything in that array
-- that is not a message id fails closed.
CREATE FUNCTION api.evidence_visible(evidence jsonb) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
DECLARE ids jsonb := evidence->'message_ids';
BEGIN
 IF ids IS NULL OR jsonb_typeof(ids)<>'array' OR jsonb_array_length(ids)=0 THEN RETURN true; END IF;
 IF api.asker_unrestricted() THEN RETURN true; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(ids) x WHERE jsonb_typeof(x)<>'number' OR x::text !~ '^[0-9]{1,18}$') THEN RETURN false; END IF;
 RETURN api.sources_visible(ARRAY(SELECT x::text::bigint FROM jsonb_array_elements(ids) x));
END $$;
CREATE FUNCTION api.policy_version() RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT coalesce((SELECT version FROM tl.policy_state),0)
$$;
-- What the current asker may read. It describes that asker only: no other
-- asker, no ungranted channel and no total appears in it.
CREATE FUNCTION api.describe_access() RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 WITH me AS (
  SELECT nullif(current_setting('throughline.scope_mode',true),'') scope_mode,
   CASE WHEN current_setting('throughline.scope_mode',true)='asker' THEN current_setting('throughline.asker',true) END asker
 ), registered AS (
  SELECT a.name,a.mode FROM tl.askers a JOIN me ON me.asker=a.name
 )
 SELECT jsonb_build_object(
  'scope_mode',me.scope_mode,
  'asker',me.asker,
  'asker_mode',(SELECT mode FROM registered),
  'unrestricted',api.asker_unrestricted(),
  'granted_channels',CASE WHEN me.asker IS NULL THEN '[]'::jsonb ELSE (SELECT coalesce(jsonb_agg(c.name ORDER BY c.name),'[]') FROM tl.asker_grants g JOIN tl.channels c ON c.id=g.channel_id WHERE g.asker=me.asker) END,
  'granted_threads',CASE WHEN me.asker IS NULL THEN 0 ELSE (SELECT count(*) FROM tl.asker_grants g WHERE g.asker=me.asker AND g.thread_id IS NOT NULL) END,
  'rule','An unrestricted asker reads everything. A restricted asker reads messages, threads, conversations and message locations of granted channels and threads, and links whose message ends and cited evidence messages are all visible; people, identifiers, facts, relations, entities, the location track and plugins are not readable.',
  'policy_version',api.policy_version())
 FROM me
$$;
GRANT EXECUTE ON FUNCTION api.asker_unrestricted(),api.asker_sees(bigint,bigint),api.row_visible(bigint,bigint,bigint),api.message_visible(bigint),api.sources_visible(bigint[]),api.evidence_visible(jsonb),api.policy_version(),api.describe_access() TO throughline_app,throughline_reader;

-- 3. Gate installer ---------------------------------------------------------
CREATE TABLE tl.gates(view_name text PRIMARY KEY,predicate text NOT NULL);
GRANT SELECT ON tl.gates TO throughline_app;
-- predicate: a boolean expression over the row alias v of tl.<view>_all;
--   NULL keeps the stored one; 'false' makes the view unrestricted-only.
-- definition: the SELECT of tl.<view>_all; NULL takes the current api.<view>
--   the first time and keeps tl.<view>_all afterwards. A later definition may
--   only append columns.
-- Names in both texts are resolved with search_path pg_catalog,public, so tl
-- and api objects must be schema-qualified.
-- A gated api view is a UNION ALL and so not automatically updatable. An
-- INSTEAD OF trigger keeps a write on it a privilege question: the runtime
-- roles hold only SELECT and are refused before the trigger is reached, and a
-- role that does hold the privilege is refused by the trigger itself.
CREATE FUNCTION tl.gated_view_read_only() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 RAISE EXCEPTION 'api.% is read-only',TG_TABLE_NAME USING ERRCODE='insufficient_privilege';
END $$;
REVOKE EXECUTE ON FUNCTION tl.gated_view_read_only() FROM PUBLIC;
CREATE FUNCTION tl.install_gate(view_name text,predicate text DEFAULT NULL,definition text DEFAULT NULL) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE pred text; body text;
 all_view text := view_name||'_all'; gated_view text := view_name||'_gated';
BEGIN
 IF view_name IS NULL OR view_name !~ '^[a-z_][a-z0-9_]*$' OR octet_length(view_name)>57 THEN RAISE EXCEPTION 'invalid gate view name: %',view_name; END IF;
 pred := coalesce(predicate,(SELECT g.predicate FROM tl.gates g WHERE g.view_name=install_gate.view_name));
 IF pred IS NULL OR btrim(pred)='' THEN RAISE EXCEPTION 'api.% has no stored gate predicate; pass one',view_name; END IF;
 body := regexp_replace(definition,'[;\s]+$','');
 IF to_regclass(format('tl.%I',all_view)) IS NULL THEN
  IF body IS NULL THEN
   IF to_regclass(format('api.%I',view_name)) IS NULL THEN RAISE EXCEPTION 'api.% does not exist; pass a definition',view_name; END IF;
   IF EXISTS(SELECT 1 FROM tl.gates g WHERE g.view_name=install_gate.view_name) OR to_regclass(format('tl.%I',gated_view)) IS NOT NULL THEN
    RAISE EXCEPTION 'api.% is already gated but tl.% is missing; pass a definition',view_name,all_view;
   END IF;
   body := regexp_replace(pg_get_viewdef(to_regclass(format('api.%I',view_name)),false),'[;\s]+$','');
  END IF;
  EXECUTE format('CREATE VIEW tl.%I AS %s',all_view,body);
 ELSIF body IS NOT NULL THEN
  EXECUTE format('CREATE OR REPLACE VIEW tl.%I AS %s',all_view,body);
 END IF;
 EXECUTE format('CREATE OR REPLACE VIEW tl.%I WITH (security_barrier=true) AS SELECT * FROM tl.%I v WHERE %s',gated_view,all_view,pred);
 EXECUTE format('CREATE OR REPLACE VIEW api.%I AS SELECT * FROM tl.%I WHERE (SELECT api.asker_unrestricted()) UNION ALL SELECT * FROM tl.%I WHERE NOT (SELECT api.asker_unrestricted())',view_name,all_view,gated_view);
 EXECUTE format('CREATE OR REPLACE TRIGGER read_only INSTEAD OF INSERT OR UPDATE OR DELETE ON api.%I FOR EACH ROW EXECUTE FUNCTION tl.gated_view_read_only()',view_name);
 INSERT INTO tl.gates(view_name,predicate) VALUES(install_gate.view_name,pred) ON CONFLICT ON CONSTRAINT gates_pkey DO UPDATE SET predicate=EXCLUDED.predicate;
 EXECUTE format('GRANT SELECT ON api.%I TO throughline_app,throughline_reader',view_name);
 EXECUTE format('GRANT SELECT ON tl.%I,tl.%I TO throughline_app',all_view,gated_view);
END $$;
REVOKE EXECUTE ON FUNCTION tl.install_gate(text,text,text) FROM PUBLIC,throughline_app,throughline_reader;

-- 4. Gates ------------------------------------------------------------------
-- api.messages, every ch_ view, api.visits, api.search_results and the SETOF
-- api.messages functions stay bound: api.message_versions is replaced in place.
SELECT tl.install_gate('message_versions','api.row_visible(v.id, v.channel_id, v.thread_id)');
SELECT tl.install_gate('threads','api.asker_sees(v.channel_id, v.id)');
SELECT tl.install_gate('conversations','api.asker_sees((SELECT t.channel_id FROM tl.threads t WHERE t.id=v.thread_id), v.thread_id)');
-- A channel is listed for a grant on it or on one of its threads.
SELECT tl.install_gate('channels','current_setting(''throughline.scope_mode'',true)=''asker'' AND EXISTS(SELECT 1 FROM tl.asker_grants g LEFT JOIN tl.threads t ON t.id=g.thread_id WHERE g.asker=current_setting(''throughline.asker'',true) AND (g.channel_id=v.id OR t.channel_id=v.id))');
SELECT tl.install_gate('message_locations','api.message_visible(v.message_id)');
-- A link needs a message end, every message end visible, and every message its
-- evidence cites visible. Links between non-message objects are derived rows
-- without a visible-source proof.
SELECT tl.install_gate('links','(v.from_kind=''message'' OR v.to_kind=''message'') AND (v.from_kind<>''message'' OR api.message_visible(v.from_id)) AND (v.to_kind<>''message'' OR api.message_visible(v.to_id)) AND api.evidence_visible(v.evidence)');
DO $$ DECLARE n text; BEGIN
 FOREACH n IN ARRAY ARRAY['people','person_names','identifiers','facts','facts_history','relations','relations_history','not_same','entities','location_track','plugins'] LOOP
  PERFORM tl.install_gate(n,'false');
 END LOOP;
END $$;
-- Record contract view of migration 0032, when present: its definition is
-- repointed at the ungated message view so the gate is applied exactly once.
DO $$ DECLARE body text; BEGIN
 IF to_regclass('api.record_versions') IS NOT NULL THEN
  body := replace(pg_get_viewdef('api.record_versions'::regclass,false),'api.message_versions','tl.message_versions_all');
  PERFORM tl.install_gate('record_versions','api.row_visible(v.message_id, (SELECT x.channel_id FROM tl.messages x WHERE x.id=v.message_id), v.thread_id)',body);
  IF to_regclass('api.records') IS NOT NULL THEN
   CREATE OR REPLACE VIEW api.records AS SELECT * FROM api.record_versions WHERE is_current;
   GRANT SELECT ON api.records TO throughline_app,throughline_reader;
  END IF;
 END IF;
END $$;
-- A hidden message and a missing one are the same answer.
CREATE OR REPLACE FUNCTION api.current_message(message_id bigint) RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 WITH RECURSIVE versions AS (SELECT id,superseded_by FROM tl.messages WHERE id=$1 UNION ALL SELECT m.id,m.superseded_by FROM tl.messages m JOIN versions v ON v.superseded_by=m.id) SELECT id FROM versions WHERE superseded_by IS NULL AND api.message_visible(id)
$$;
