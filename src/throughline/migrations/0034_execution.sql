-- Execution plumbing: traces, operator registry and profiles, per-token
-- budget limits and purge hooks.
--
-- 1. Traces (QRY-36, QRY-44). Written only on request or for multi-statement
--    operations. They hold statements, ids, counts and hashes, never excerpts
--    or prompts, so they are not gated per record and need no purge hook; a
--    trace is readable only by its own token and asker under the policy
--    version it was written with (or by the owner).
CREATE TABLE tl.traces(
 id uuid PRIMARY KEY,
 token_id bigint REFERENCES tl.tokens ON DELETE CASCADE,
 asker text,
 policy_version bigint NOT NULL DEFAULT 0,
 kind text NOT NULL,
 request jsonb,
 execution jsonb,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX traces_token ON tl.traces(token_id,created_at);
CREATE TABLE tl.trace_events(
 trace_id uuid NOT NULL REFERENCES tl.traces ON DELETE CASCADE,
 seq int NOT NULL,
 op text NOT NULL,
 statement text,
 status text,
 rows int,
 ms numeric,
 model text,
 tokens int,
 cost numeric,
 detail jsonb,
 PRIMARY KEY(trace_id,seq));
GRANT SELECT,INSERT ON tl.traces,tl.trace_events TO throughline_app;
GRANT UPDATE(execution) ON tl.traces TO throughline_app;

-- 2. Operator contract (CST-8, LIB-4): what is registered in the API process,
--    and measured profiles the planner chooses implementations from.
CREATE TABLE tl.operators(
 name text PRIMARY KEY,
 implements text NOT NULL,
 implementation text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('leg','stage','ranker','semantic','transform')),
 exact boolean NOT NULL DEFAULT true,
 cost_class text NOT NULL DEFAULT 'free' CHECK(cost_class IN ('free','embedding','model')),
 access text,
 provenance text,
 version text NOT NULL DEFAULT '1');
CREATE TABLE tl.operator_profiles(
 operator text NOT NULL,
 implementation text NOT NULL,
 version text NOT NULL DEFAULT '1',
 measured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 p50_ms numeric,
 p95_ms numeric,
 cost_per_call numeric,
 quality jsonb,
 source text NOT NULL DEFAULT 'bench' CHECK(source IN ('bench','runtime')));
CREATE INDEX operator_profiles_latest ON tl.operator_profiles(operator,version,measured_at DESC);
GRANT SELECT,INSERT,UPDATE ON tl.operators TO throughline_app;
GRANT SELECT,INSERT ON tl.operator_profiles TO throughline_app;

-- Registry joined with the latest profile of each operator's current version.
-- Dispatched by api.describe('operators'); not on the query allow-list.
CREATE FUNCTION api.describe_operators() RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,api,public AS $$
 SELECT coalesce(jsonb_agg(jsonb_build_object(
  'name',o.name,'implements',o.implements,'implementation',o.implementation,'kind',o.kind,
  'exact',o.exact,'cost_class',o.cost_class,'access',o.access,'provenance',o.provenance,'version',o.version,
  'profile',CASE WHEN p.operator IS NULL THEN NULL ELSE jsonb_build_object(
   'measured_at',p.measured_at,'p50_ms',p.p50_ms,'p95_ms',p.p95_ms,'cost_per_call',p.cost_per_call,'quality',p.quality,'source',p.source) END)
  ORDER BY o.implements,o.name),'[]'::jsonb)
 FROM tl.operators o LEFT JOIN LATERAL (
  SELECT * FROM tl.operator_profiles x WHERE x.operator=o.name AND x.version=o.version ORDER BY x.measured_at DESC LIMIT 1) p ON true
$$;
GRANT EXECUTE ON FUNCTION api.describe_operators() TO throughline_reader,throughline_app;

-- 3. Per-token budget limits (CST-4, QRY-41). A request can only lower them.
ALTER TABLE tl.tokens
 ADD COLUMN max_deadline_ms int NOT NULL DEFAULT 30000 CHECK(max_deadline_ms>=0),
 ADD COLUMN max_return_tokens int NOT NULL DEFAULT 50000 CHECK(max_return_tokens>=0),
 ADD COLUMN max_controller_turns int NOT NULL DEFAULT 3 CHECK(max_controller_turns>=0),
 ADD COLUMN allow_escalation boolean NOT NULL DEFAULT false;

-- 4. Purge hooks (ARC-5): a component that keeps text derived from messages
--    registers one statement here; `throughline purge` runs them, in ord
--    order, before its own deletes. $1 is the id of the channel being purged.
CREATE TABLE tl.purge_hooks(name text PRIMARY KEY,ord int NOT NULL,statement text NOT NULL);
CREATE FUNCTION tl.run_purge_hooks(channel_id bigint) RETURNS int LANGUAGE plpgsql SET search_path=pg_catalog,tl AS $$
DECLARE h record; n int:=0;
BEGIN
 FOR h IN SELECT name,statement FROM tl.purge_hooks ORDER BY ord,name LOOP
  EXECUTE h.statement USING channel_id;
  n:=n+1;
 END LOOP;
 RETURN n;
END $$;
-- Hooks are arbitrary statements: only the owner writes or runs them.
REVOKE ALL ON tl.purge_hooks FROM PUBLIC,throughline_app,throughline_reader;
REVOKE EXECUTE ON FUNCTION tl.run_purge_hooks(bigint) FROM PUBLIC,throughline_app,throughline_reader;
