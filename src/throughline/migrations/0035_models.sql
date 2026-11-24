-- Model providers: ledger, scope-checked cache, cost approvals and the egress
-- description.
--
-- 1. Model ledger (CST-3, CST-5). One row per provider call made outside the
--    embedding worker, which keeps tl.embedding_ledger (one row per batch).
--    Rows carry purpose, provider, model, counts and cost: no text.
CREATE TABLE tl.model_ledger(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 at timestamptz NOT NULL DEFAULT now(),
 purpose text NOT NULL,
 provider text NOT NULL,
 model text,
 tokens bigint NOT NULL DEFAULT 0,
 cost numeric NOT NULL DEFAULT 0,
 calls int NOT NULL DEFAULT 1,
 token_id bigint REFERENCES tl.tokens ON DELETE SET NULL,
 trace_id uuid,
 origin text NOT NULL DEFAULT 'query' CHECK(origin IN ('query','worker','job','rule','curator','librarian')),
 origin_ref text);
CREATE INDEX model_ledger_at ON tl.model_ledger(at);
CREATE INDEX model_ledger_token ON tl.model_ledger(token_id,at);
GRANT SELECT,INSERT ON tl.model_ledger TO throughline_app;

-- Everything spent on models: provider calls and embedding batches.
CREATE VIEW tl.spend AS
 SELECT at,purpose,provider,model,tokens,cost,calls,token_id,trace_id,origin,origin_ref FROM tl.model_ledger
 UNION ALL
 SELECT at,'embedding'::text,NULL::text,model,tokens,cost,1,NULL::bigint,NULL::uuid,'worker'::text,NULL::text FROM tl.embedding_ledger;
GRANT SELECT ON tl.spend TO throughline_app;

-- 2. Cache of derived values (CST-5, CST-6). The key covers operator, operator
--    version, model and the exact input (content hashes, or message id with
--    version). source_message_ids names the messages a value was derived from:
--    the API returns such an entry only to a scope that can see all of them
--    (providers.cache.get), and the purge hook below removes it with them.
CREATE TABLE tl.semantic_cache(
 key text PRIMARY KEY,
 operator text NOT NULL,
 operator_version text NOT NULL,
 model text,
 input_hash text,
 value jsonb NOT NULL,
 source_message_ids bigint[] NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz);
CREATE INDEX semantic_cache_sources ON tl.semantic_cache USING gin(source_message_ids);
CREATE INDEX semantic_cache_expiry ON tl.semantic_cache(expires_at) WHERE expires_at IS NOT NULL;
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.semantic_cache TO throughline_app;

INSERT INTO tl.purge_hooks(name,ord,statement) VALUES(
 'semantic_cache',50,
 'DELETE FROM tl.semantic_cache WHERE source_message_ids && ARRAY(SELECT m.id FROM tl.messages m WHERE m.channel_id=$1)');

-- 3. Cost gate (CST-10). Each token has a model-cost limit per request; a
--    request above it needs an approval bound to the token, the asker, the
--    request (statement, inputs with their member counts, budgets) and the
--    policy version, and usable once. approve_limit is the highest cost an
--    agent token may approve itself; the default 0 leaves approval to the owner.
ALTER TABLE tl.tokens
 ADD COLUMN max_model_cost numeric NOT NULL DEFAULT 0.10 CHECK(max_model_cost>=0),
 ADD COLUMN approve_limit numeric NOT NULL DEFAULT 0 CHECK(approve_limit>=0);
CREATE TABLE tl.approvals(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 token_id bigint REFERENCES tl.tokens ON DELETE CASCADE,
 asker text,
 policy_version bigint NOT NULL DEFAULT 0,
 request_hash text NOT NULL,
 estimate jsonb NOT NULL,
 cost numeric NOT NULL,
 limit_cost numeric NOT NULL,
 explanation text NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','used','rejected')),
 approved_by text,
 created_at timestamptz NOT NULL DEFAULT now(),
 decided_at timestamptz);
CREATE INDEX approvals_token ON tl.approvals(token_id,status,created_at);
GRANT SELECT,INSERT ON tl.approvals TO throughline_app;
GRANT UPDATE(status,approved_by,decided_at,estimate,cost,limit_cost,explanation) ON tl.approvals TO throughline_app;

-- 4. Which provider serves each kind, written by the API at start. It holds
--    names of implementations and models only: no host names and no keys.
CREATE TABLE tl.provider_state(
 kind text PRIMARY KEY CHECK(kind IN ('embedding','rerank','semantic','controller')),
 provider text NOT NULL,
 model text,
 local boolean NOT NULL,
 excerpts_leave_host boolean NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now());
GRANT SELECT,INSERT,UPDATE ON tl.provider_state TO throughline_app;

-- Dispatched by api.describe('egress'); not on the query allow-list. A kind
-- without a row has not been reported by a running API yet.
CREATE FUNCTION api.describe_egress() RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
 SELECT jsonb_build_object(
  'providers',coalesce((SELECT jsonb_agg(jsonb_build_object(
    'kind',s.kind,'provider',s.provider,'model',s.model,'local',s.local,
    'excerpts_leave_host',s.excerpts_leave_host,'updated_at',s.updated_at) ORDER BY s.kind)
   FROM tl.provider_state s),'[]'::jsonb),
  'excerpts_leave_host',coalesce((SELECT bool_or(s.excerpts_leave_host) FROM tl.provider_state s),false),
  'rule','A model endpoint that is not loopback receives requests only when its host is in the egress allow-list; it is checked before any connection is opened.')
$$;
GRANT EXECUTE ON FUNCTION api.describe_egress() TO throughline_reader,throughline_app;
