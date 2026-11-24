-- Optional librarian (QRY-39..46, LIB-1..9, ACL-10, CST-4, CST-10, INT-12).
--
-- 1. tl.investigations: one row per librarian investigation. It holds ONLY
--    references: the request the caller sent, the plans the controller wrote,
--    result-set handles, note records {generated_text, supporting_refs,
--    uncertainty}, counts, failed avenues, coverage by channel and time
--    (counts), the incompleteness reason and the access policy version. No
--    record excerpt, packet record or controller prompt is stored here or
--    anywhere else: GET /v1/investigations/{id} rebuilds the packet from the
--    result set under the caller's current scope, and answers 409 after a
--    policy change.
-- 2. Registry row api.librarian (semantic, level 3) and catalogue section
--    'librarian'.
-- 3. Purge hook: investigations whose notes cite records of the purged
--    channel are deleted (nothing else holds content).
--
-- Forward-only. Depends only on lower migrations (tl.tokens, tl.purge_hooks,
-- tl.api_functions, tl.catalog_sections).

-- 1. Investigations ------------------------------------------------------------
CREATE TABLE tl.investigations(
 id uuid PRIMARY KEY,
 trace_id uuid,
 token_id bigint REFERENCES tl.tokens ON DELETE CASCADE,
 asker text,
 policy_version bigint NOT NULL,
 request jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(request)='object' AND octet_length(request::text)<=65536),
 status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','complete','degraded','failed')),
 baseline_handle text CHECK (baseline_handle ~ '^rs_[0-9a-f]{32}$'),
 result_handle text CHECK (result_handle ~ '^rs_[0-9a-f]{32}$'),
 plans jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(plans)='array' AND octet_length(plans::text)<=262144),
 notes jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(notes)='array' AND octet_length(notes::text)<=262144),
 counts jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(counts)='object'),
 failed jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(failed)='array' AND octet_length(failed::text)<=65536),
 coverage jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(coverage)='object' AND octet_length(coverage::text)<=65536),
 reason text CHECK (length(reason)<=500),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 finished_at timestamptz);
CREATE INDEX investigations_token ON tl.investigations(token_id,asker,created_at);
REVOKE ALL ON tl.investigations FROM PUBLIC,throughline_reader;
GRANT SELECT,INSERT,UPDATE ON tl.investigations TO throughline_app;

-- 3. Purge (ARC-5): notes are the only generated text an investigation keeps.
INSERT INTO tl.purge_hooks(name,ord,statement) VALUES(
 'investigations',33,
 'DELETE FROM tl.investigations i WHERE EXISTS(
   SELECT 1 FROM jsonb_path_query(i.notes,''$[*].supporting_refs[*].message_id ? (@.type() == "number")'') x(v)
   JOIN tl.messages m ON m.id=(x.v#>>''{}'')::bigint WHERE m.channel_id=$1)');

-- 2. Registry and catalogue -----------------------------------------------------
INSERT INTO tl.api_functions(name,kind,signature,description,exactness,cost_class,level,access,example,version,ranked_limit_arg,default_limit) VALUES
 ('librarian','semantic','librarian(goal text, deadline_ms int, tokens int, request jsonb) RETURNS SETOF api.result_set rows',
  'Optional bounded investigation: a controller plans calls of the shared library (describe, plans, inspect, packets) under one budget, with an independent baseline recall as fallback. Rows are the delivered result set; execution.packet carries the packet-level fields (orientation notes, coverage, unresolved needs, status). Only alone in the top-level FROM; never inside a plan, a nested statement or another investigation',
  'approximate','model',3,'owner, agent; reads under the asker''s scope only; writes only its own result sets, trace, investigation record and pending proposals',
  'SELECT l.record_id,l.kind FROM api.librarian(goal => ''orchard'', deadline_ms => 6000, tokens => 6000) l','1',NULL,NULL);

INSERT INTO tl.catalog_sections(subject,body) VALUES
 ('librarian',jsonb_build_object(
  'description','Optional librarian: a bounded controller over the same library any agent can call. POST /v1/librarian, throughline lib librarian GOAL, or SELECT * FROM api.librarian(goal => ...). Never required; never run by default.',
  'tools',jsonb_build_object(
   'describe','api.describe(subject) under the caller''s scope (GET /v1/describe).',
   'execute','A validated plan of sql, recall, expand, match, transform and inspect nodes (POST /v1/plans); librarian is never a plan op.',
   'inspect','A bounded view of a result set (POST /v1/sets/{handle}/inspect); counts.inspected_by_controller counts what the controller was shown.',
   'deliver','An evidence packet built by the runtime from a live read (POST /v1/packets).'),
  'request',jsonb_build_object(
   'goal','The retrieval objective (required).',
   'question','The original question when different from the goal.',
   'context','Clarification from the calling agent.',
   'scope','Hard restrictions (the recall hard scope); binding for every plan and for the delivered set.',
   'starting_sets','Your result-set handles already obtained.',
   'known_references','Ids or names you already know.',
   'coverage_requirement','{must_include: [record ids or terms], enumerate: bool}; unmet requirements are reported in unresolved_needs.',
   'budgets','deadline_ms, tokens, model_cost, model_calls, controller_turns (default 2); each only lowers the token''s limit.',
   'as_of','Record-time cutoff.','known_by','Ingest-time cutoff.',
   'exclude_seen','Record or message ids not to deliver again.',
   'asker','Ignored: the asker and permissions come only from the authenticated token.'),
  'loop',jsonb_build_array(
   'A baseline recall of the goal starts at once on a worker thread: it is the fallback.',
   'Each controller turn returns {plan} or {deliver}; a plan runs completely without further model turns, branches in parallel.',
   'A new plan after the first needs a stated trigger: unexpected_alias, conflicting_dates, missing_participants, underrepresented_cases, failed_branch, coverage_gap.',
   'The controller sees only bounded inspection views; stored text reaches it as data, never as instructions.',
   'A controller turn is skipped when its measured p95 exceeds the remaining deadline.'),
  'statuses',jsonb_build_object(
   'complete','The controller delivered.',
   'degraded','Controller error, invalid output, timeout, approval requirement or exhausted budget: the baseline recall is delivered and the reason is stated.',
   'failed','The baseline failed too; nothing is delivered.'),
  'guarantees',jsonb_build_array(
   'No recursion: a librarian never starts a librarian (plan op, nested SQL, investigation scope) and never raises its budget.',
   'Turns are counted apart from semantic model calls.',
   'Notes are flagged generated and kept only with verified references to delivered records.',
   'No writes except result sets, the trace, the investigation record and pending proposals.',
   'An investigation stores references only; GET /v1/investigations/{id} rebuilds the packet under your current scope and is 409 after a policy change.',
   'A coverage gap or a partial source is reported as an unresolved need (source coverage partial: <channel>), never as absence.'),
  'escalation','POST /v1/recall may escalate only when the token has allow_escalation, hints.escalate = {librarian: true, budgets: {...}} with a budget is given, and the result has fewer than k records or an uncovered must_term. Plans, SQL and the library function never escalate.'));
