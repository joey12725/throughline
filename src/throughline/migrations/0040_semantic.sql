-- Semantic SQL runtime, plans, packets and episode grain (QRY-31, QRY-36..38,
-- QRY-41, QRY-42, CST-4, CST-10, LIB-5, LIB-9).
--
-- Semantic functions (api.recall and the operators of later migrations) are
-- run by the API process, never by Postgres: they are rows of kind 'semantic'
-- in tl.api_functions and are not on the query allow-list. Before a statement
-- reaches the reader each call is replaced by api.result_set('<handle>').
--
-- api.episodes_of is the episode grain: gap-based sessions of the threads of
-- the given messages, built only from what the asker may see.

-- 1. Episode grain -------------------------------------------------------------
-- Consecutive current messages of a thread (thread_no order) whose happened_at
-- gap is at most the channel's conversation_gap_s form one session; a channel
-- with conversation_whole_thread is one session per thread. The id is
-- 'ep:<thread_id>:<first message id of the session>' ('ep:m:<id>' for a
-- message without a thread). It reads only the gated api.message_versions, so
-- hidden messages neither appear nor join, bridge or count in a session.
CREATE FUNCTION api.episodes_of(message_ids bigint[])
RETURNS TABLE(message_id bigint,episode_id text,thread_id bigint,first_message_id bigint,started_at timestamptz,ended_at timestamptz,message_count int)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,api,public AS $$
BEGIN
 IF coalesce(cardinality(episodes_of.message_ids),0)>10000 THEN
  RAISE EXCEPTION 'api.episodes_of takes at most 10000 message ids (got %)',cardinality(episodes_of.message_ids)
   USING ERRCODE='program_limit_exceeded';
 END IF;
 RETURN QUERY
 WITH wanted AS (SELECT DISTINCT w.id FROM unnest(episodes_of.message_ids) AS w(id) WHERE w.id IS NOT NULL),
 asked AS (
  SELECT v.id,v.thread_id,v.channel_id,v.external_id,v.happened_at,coalesce(v.ended_at,v.happened_at) AS last_at
  FROM api.message_versions v JOIN wanted w ON w.id=v.id),
 ordered AS (
  SELECT v.id,v.thread_id,v.channel_id,v.external_id,v.thread_no,v.happened_at,coalesce(v.ended_at,v.happened_at) AS last_at,
   c.conversation_gap_s,c.conversation_whole_thread,
   lag(v.happened_at) OVER(PARTITION BY v.thread_id ORDER BY v.thread_no,v.happened_at,v.id) AS prev
  FROM api.message_versions v
  JOIN tl.channels c ON c.id=v.channel_id
  WHERE v.superseded_by IS NULL
   AND v.thread_id IN (SELECT a.thread_id FROM asked a WHERE a.thread_id IS NOT NULL)),
 grouped AS (
  SELECT o.*,sum(CASE WHEN o.prev IS NULL OR (NOT o.conversation_whole_thread AND o.happened_at-o.prev>make_interval(secs=>o.conversation_gap_s)) THEN 1 ELSE 0 END)
   OVER(PARTITION BY o.thread_id ORDER BY o.thread_no,o.happened_at,o.id) AS grp
  FROM ordered o),
 sessions AS (
  SELECT g.thread_id,g.grp,(array_agg(g.id ORDER BY g.thread_no,g.happened_at,g.id))[1] AS first_id,
   min(g.happened_at) AS s_at,max(g.last_at) AS e_at,count(*)::int AS n
  FROM grouped g GROUP BY g.thread_id,g.grp),
 members AS (
  SELECT g.thread_id,g.channel_id,g.external_id,s.first_id,s.s_at,s.e_at,s.n
  FROM grouped g JOIN sessions s ON s.thread_id=g.thread_id AND s.grp=g.grp)
 -- A superseded version belongs to the session of its current version.
 SELECT a.id,
  CASE WHEN a.thread_id IS NULL OR m.first_id IS NULL THEN 'ep:m:'||a.id ELSE 'ep:'||a.thread_id||':'||m.first_id END,
  a.thread_id,
  coalesce(m.first_id,a.id),
  coalesce(m.s_at,a.happened_at),
  coalesce(m.e_at,a.last_at),
  coalesce(m.n,1)
 FROM asked a
 LEFT JOIN members m ON m.thread_id=a.thread_id AND m.channel_id=a.channel_id AND m.external_id=a.external_id
 ORDER BY a.id;
END $$;
REVOKE EXECUTE ON FUNCTION api.episodes_of(bigint[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api.episodes_of(bigint[]) TO throughline_app,throughline_reader;

-- 2. Registry -------------------------------------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('recall','semantic','recall(query text, k int DEFAULT 20, channels text[], kinds text[], person bigint, persons bigint[], after timestamptz, before timestamptz, time_basis text, as_of timestamptz, known_by timestamptz, raw_only boolean, expand boolean, strategy text, ranker text, tokens int, deadline_ms int, group_by text, exhaustive boolean, include_ignored boolean, options jsonb) RETURNS SETOF api.result_set rows',
  'Semantic function: recall records of every kind under a hard scope, run by the API before the statement and read as api.result_set rows of the saved set. Only as the single function of a FROM item, with constant arguments. A ranked recall is a sample (ranked_sample); exhaustive => true enumerates (exhaustive or truncated)',
  'approximate','embedding',1,'owner, agent; rows pass the asker gate; the set belongs to the caller',
  'SELECT r.record_id,r.score FROM api.recall(query => ''orchard'', k => 10) r ORDER BY r.ordinal','1',NULL,NULL),
 ('episodes_of','sql','episodes_of',
  'Episode of each given message: gap-based sessions of its thread (conversation_gap_s; one per thread for conversation_whole_thread channels) built from visible current messages only. episode_id = ep:<thread_id>:<first message id>; count(DISTINCT episode_id) counts incidents, count(*) counts hits. At most 10,000 ids',
  'exact','free',2,'owner, agent; rows pass the asker gate',
  'SELECT * FROM api.episodes_of(ARRAY[1,2]::bigint[])','1',NULL,NULL);
UPDATE tl.api_functions f SET signature=f.name||'('||pg_get_function_arguments(p.oid)||') RETURNS '||pg_get_function_result(p.oid)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.proname=f.name AND f.name='episodes_of';

-- 3. Catalogue -------------------------------------------------------------------
INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('semantic_sql',jsonb_build_object(
  'description','Semantic functions (api.recall; api.expand, api.match and api.librarian where installed) compose with SQL. The API runs each call first, under one budget, saves its output as one of your result sets and replaces the call by api.result_set(''<handle>'') before the statement runs on the read-only reader path. They are never Postgres functions and never on the allow-list.',
  'subset',jsonb_build_array(
   'A semantic call is allowed only as the single function of a FROM item of a non-recursive query level: SELECT ... FROM api.recall(...) r JOIN ... (also inside a CTE or a FROM subquery).',
   'Arguments are constants only: positional or named (name => value), casts of constants and ARRAY[...] of constants.',
   'At most THROUGHLINE_MAX_SEMANTIC_CALLS (default 4) calls per statement.',
   'Input sets: input => ''rs_<32 hex>'' (one of your handles) or input_sql => ''<SELECT>'' with max_rows => 1..10000. The nested statement must pass validation, hold no semantic call and return a column record_id, passage_id, message_id or id.',
   'api.librarian is allowed only alone in the top-level FROM.'),
  'rejections',jsonb_build_array(
   'in the target list, WHERE, JOIN ON, GROUP BY, HAVING, ORDER BY, window or aggregate arguments, VALUES, LIMIT',
   'LATERAL, WITH ORDINALITY, a column definition list, ROWS FROM with several functions',
   'inside WITH RECURSIVE',
   'column-reference, subquery, function-call or parameter arguments',
   'unknown, repeated or missing parameters; positional arguments after named ones',
   'more than the allowed number of calls',
   'input_sql without max_rows, max_rows outside 1..10000, an invalid nested statement, a nested semantic call',
   'a handle that is not yours, expired or from an earlier access policy'),
  'error','Each rejection is HTTP 400 ''semantic function api.<name> is not supported here: <construct>'' and happens before any work: nothing runs, nothing is spent.',
  'execution','Steps: validate with placeholders; clamp budgets (one tracker for every call); resolve input_sql sets; estimate; dry_run stops here (execution.plan, execution.estimate); cost gate: over the limit the response has no rows, execution.status approval_required and execution.approval, and nothing is spent; run the operators concurrently; splice the real handles; validate again; run the statement. execution.semantic_calls lists each call''s handle and completeness; execution.trace_id names the trace.',
  'approval','An approval is bound to the token, asker, statement, every input with its member count, the budgets and the access policy version, and is used once: POST /v1/sql again with approval_id.',
  'taint',jsonb_build_object(
   'rules',jsonb_build_array(
    'A relation is not a population when it is a ranked function (tl.api_functions.ranked_limit_arg) whose limit is omitted (the default applies) or anything but an explicit NULL,',
    'api.result_set of a set whose completeness is ranked_sample or truncated,',
    'a query level with LIMIT or FETCH, a table read with TABLESAMPLE,',
    'or any subquery or CTE that reads such a relation (also through a WHERE ... IN (SELECT ...) or JOIN ... ON ... IN (SELECT ...) filter); taint propagates outward through references and joins.'),
   'aggregates','count, sum, avg, min, max, array_agg, jsonb_agg, json_agg, DISTINCT, GROUP BY and window aggregates over a tainted relation add the warning ''aggregate over a ranked top-k or sampled set, not a population''; with strict_exact they are rejected (400).',
   'input_sql','An input_sql set is exhaustive only when its statement is untainted and max_rows did not cut it; ranked_sample when tainted; truncated when cut.',
   'grain','count(*) or count(x) over hits of a search or ranked function or of a recall, expand or match set, at record grain, adds the warning code hit_count_not_incident_count (execution.warning_codes): count DISTINCT episode_id from api.episodes_of instead. It is a warning only, even with strict_exact.'),
  'examples',jsonb_build_array(
   'SELECT r.record_id,r.score,m.channel FROM api.recall(query => ''orchard'', k => 10) r JOIN api.messages m ON m.id=r.message_id ORDER BY r.ordinal',
   'WITH hits AS (SELECT * FROM api.recall(query => ''orchard'', exhaustive => true)) SELECT channel,count(*) FROM hits GROUP BY channel',
   'SELECT count(*) FROM api.search(''orchard'', NULL) s',
   'SELECT count(DISTINCT e.episode_id) FROM api.episodes_of(ARRAY(SELECT id FROM api.search_filtered(''orchard'', NULL))) e'),
  'request','POST /v1/sql {statement, row_cap, deadline_ms, token_budget, budgets:{deadline_ms, tokens, model_cost, model_calls}, dry_run, approval_id, strict_exact, trace}')),
 ('plans',jsonb_build_object(
  'description','A plan is a DAG of operations run by the API under one budget (POST /v1/plans, throughline lib plan run FILE).',
  'shape','{nodes:[{id, op, args, inputs}]}: ops sql, recall, expand, match, transform, inspect. librarian is never accepted. inputs name earlier nodes: {param: node} for recall, expand and match, [node, ...] for transform and inspect.',
  'sql','args {statement, max_rows}: {{id}} in the statement is replaced by the quoted handle of node id (SELECT * FROM api.result_set({{n1}}) ...). The statement is validated with placeholders and may not hold semantic calls. Its rows (record_id, passage_id, message_id or id) become a result set whose completeness follows the taint rules.',
  'limits','At most THROUGHLINE_MAX_PLAN_NODES (default 24) nodes; no cycles; arguments are checked against each operator''s parameters before anything runs.',
  'execution','Independent branches run concurrently. Before dispatch each ready branch gets its share (remaining / ready branches) of the model-cost budget, reserved from the one tracker. A failed branch is isolated: its dependants are skipped and listed, the rest stands (status degraded). Nothing is dispatched after the deadline. Deferred maintenance is waited for once per plan.',
  'response','{handles:{node: handle}, previews:{node: {completeness, member_count, sample}}, execution} in plan order; dry_run returns the validated plan and estimates only; over the cost limit execution.status is approval_required.')),
 ('envelope',jsonb_build_object(
  'description','Every read returns one envelope.',
  'shape','{records | rows, execution, result_set?, manifest?}',
  'records','Recall, expand, match, sets, plans and packets return records with identical keys (the record contract plus span, score, role, groups, discovery).',
  'rows','POST /v1/sql returns rows (whatever the SELECT returns), truncated and row_count.',
  'execution','status (complete | degraded | failed | approval_required), truncation, completeness, stages, timings_ms, incomplete, budget, warnings, trace_id; semantic statements add plan, estimate, semantic_calls and handles.',
  'result_set','The handle of the saved set, when one was saved.',
  'manifest','What was left out (groups, omitted records with a continuation), when a budget cut the answer.',
  'packets','POST /v1/packets {handle, notes, unresolved, token_budget} -> an evidence packet: records copied from one live read of the set (record_id, revision_id, kind, original_excerpt, span, event_time, recorded_time, ingested_time, applicable_time, source_refs, claim_metadata, generated, unsupported, evidence_class, attribution, discovery_reasons), orientation notes (generated: true, each with verified supporting_refs), coverage, unresolved_needs, truncation_and_omissions, cost_and_latency, continuation_ref. execution_status and evidence adequacy are reported separately.')),
 ('grain',jsonb_build_object(
  'description','A hit count is not an incident count. Search, ranking and recall return records (hits); one incident usually spans several records.',
  'units',jsonb_build_object(
   'record','One message version, passage, claim, link or entity: what search and recall return.',
   'episode','A gap-based session of one thread: consecutive messages whose happened_at gap is at most the channel''s conversation_gap_s (a whole thread for conversation_whole_thread channels). api.episodes_of(message_ids) gives each message its episode_id.',
   'day','A calendar day of record_time (date_trunc(''day'', ...)).',
   'conversation','A stored conversation chunk of a thread (search_results.conversation_id), used by the meaning search.'),
  'counting','To count incidents: SELECT count(DISTINCT e.episode_id) FROM api.episodes_of(ARRAY(SELECT message_id FROM ...)) e. count(*) over hits counts records and adds the warning code hit_count_not_incident_count.',
  'ids','episode_id = ep:<thread_id>:<first message id of the session>, or ep:m:<message id> for a message without a thread. Ids are the same on every call while the thread does not change.',
  'stability','Episodes are recomputed on every call: a late message that bridges two sessions merges them (the merged episode keeps the earlier session''s id; the later id disappears), and a new message after a gap starts a new episode. Store message ids, not episode ids, when the grouping must survive new data.',
  'access','Episodes are built only from messages the asker may see: hidden messages neither appear, nor bridge sessions, nor count in message_count.'));
