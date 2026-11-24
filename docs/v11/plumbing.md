# Execution plumbing

Modules `throughline.scope`, `throughline.query`, `throughline.budget`,
`throughline.execution`, `throughline.operators`, `throughline.tokens`,
`throughline.allowlist`, `throughline.api.deps`, `throughline.api.routes`,
`throughline.cli_commands`; migration `0034_execution.sql`.

This is the shared machinery every v1.1 component runs on: who is asking, how
a read reaches the database, how long and how large it may be, what is
reported back, and where new routes, commands, allow-list names, operators and
purge steps are registered without editing a shared file.

## Scope (ACL-8, ACL-5)

`Scope(token_id, token_kind, asker, mode, investigation_id=None)` is frozen
and built once per request by `deps.scope_of(token)`. `mode` is `owner`,
`asker` or `deny`.

| Token | Asker header | Scope |
|---|---|---|
| owner | none | `owner` |
| owner | names one | `asker`; an unregistered asker follows `THROUGHLINE_UNKNOWN_ASKER` (`unrestricted` by default, `deny`) |
| plugin | any | `owner` (plugins write; they do not read through the gate) |
| agent, empty askers list | none | `owner` (legacy full-read token) |
| agent, askers list | names a listed one | `asker`, unregistered asker denied |
| agent, askers list | none, one entry | bound to that entry |
| agent, askers list | none, several entries | 403 |
| anything else | | `deny` (fail closed) |

`Scope.local()` is the in-process owner scope used by the CLI and by
`query.query()`.

`scope.gucs()` becomes these transaction settings in every reader
transaction; the gate functions in the database read nothing else:
`throughline.scope_mode`, `throughline.asker`, `throughline.token_id`,
`throughline.unknown_asker`, `throughline.investigation`. Validated SQL cannot
read or change them (`current_setting` and `set_config` are not on the
allow-list).

## The reader path

`query.reader_transaction(scope, deadline_ms=None)` is the only code in `src`
that opens `connect("reader")`. It starts a `READ ONLY` transaction, sets
`statement_timeout = min(30000, deadline_ms)` and one `SET LOCAL` per setting
above (values as `psycopg.sql.Literal`). Without a `Scope` it raises
`TypeError`. A test scans the source tree to keep it that way, and checks that
every `query_ex` call passes `scope=`.

`query.query_ex(statement, cap=1000, *, scope, deadline_ms=None,
token_budget=None, execution=None, materialize=True)` validates and streams
exactly as `query()` always did and returns
`{rows, truncated, row_count, execution}`.

- Truncation reasons: `row_cap`, `response_bytes`, `row_too_large`,
  `token_budget` (rows are counted as `ceil(len(json) / 4)` tokens; the result
  stops before the row that would exceed the budget).
- `deadline_ms` bounds the whole call; the statement gets what is left (never
  more than 30 s). A cancelled statement raises with SQLSTATE `57014` and the
  stage is recorded as `timeout`.
- `materialize=False` skips the deferred-maintenance wait, so an operation
  that runs several statements waits once and passes `False` afterwards.
- A passed `Execution` receives one `sql` stage and the timings `materialize`,
  `database` and `total` (perf counter; the parts never exceed the total).

`query.query(statement, cap=1000)` is unchanged: owner scope, exactly
`{rows, truncated, row_count}`.

## Allow-list additions

`query.FUNCTIONS` and `query.SCALAR_FUNCTIONS` are the built-in names plus
every `src/throughline/allowlist/NNNN_name.json`:

```json
{"functions": ["record_versions"], "scalar": ["describe"]}
```

Names match `^[a-z_][a-z0-9_]*$` and must be api functions. Scalar names are
added to both sets. A malformed file, an unknown key or a name in both lists
stops the import: an allow-list is never guessed. Add one file per migration.

## Budgets (CST-4)

`budget.Budget(deadline_ms, tokens, model_cost, model_calls,
controller_turns)`; `None` means "not requested".
`budget.clamp(budget, token_row)` returns what the request really gets: never
more than the token's columns `max_deadline_ms` (default 30000),
`max_return_tokens` (50000), `max_model_cost` (0.10) and
`max_controller_turns` (3). Columns are read with `.get`, so a database
behind on migrations gets the defaults.

`budget.BudgetTracker(budget)` is shared by everything a request runs and is
thread-safe: `remaining_ms()`, `expired()`, `reserve(cost, calls)` /
`release(...)` for model calls, `spend_tokens(n)`, `take_turn()`,
`snapshot()`. `estimate_tokens(value) = ceil(len / 4)`.

Per-token limits are set by the owner:

```
throughline lib token-limits 3                       # show
throughline lib token-limits 3 --set max_deadline_ms=5000 --set allow_escalation=true
```

`tokens.set_limits(token_id, **values)` accepts every limit column that
exists on `tl.tokens` (`max_*`, `approve_limit`, `allow_escalation`),
discovered from the catalog, so a later migration's limit is settable without
code changes. Negative numbers and unknown names are refused.

## Execution metadata (QRY-36, CST-9)

`execution.Execution` accumulates what happened; `to_dict()` is the wire form:

| Key | Meaning |
|---|---|
| `status` | `complete`, `degraded` (something was skipped or failed but the rest stands), `failed`, `approval_required` |
| `truncation` | `{reason, limit}` or null |
| `completeness` | `exhaustive`, `partial`, `unknown`, ... |
| `stages` | `[{name, status, ...}]` in run order |
| `timings_ms` | named timings, summed per name |
| `incomplete` | `[{stage, reason}]` |
| `budget` | tracker snapshot or null |
| `warnings` | distinct messages |
| `trace_id` | id of the written trace or null |

`POST /v1/sql` returns it as `execution` and accepts `deadline_ms`,
`token_budget` and `trace` (CLI: `throughline sql --deadline-ms N
--token-budget N`).

## Traces (QRY-36, QRY-44)

`execution.Trace(scope, kind, request)` collects
`event(op, statement, status, rows, ms, model, tokens, cost, detail)` and
`flush(execution)` writes `tl.traces` and `tl.trace_events` with the app role;
it never raises (a failure is logged and reported as a warning). A trace holds
statements, ids, counts and hashes, never excerpts or prompts: any detail
string longer than 200 characters is replaced by `sha256:<hex>`.

A trace is written only when asked (`trace: true`) or by a multi-statement
operation; the request's audit row then carries `trace_id` in its detail. A
plain SQL request still leaves exactly one audit row with `detail ==
{route}`.

`GET /v1/traces/{id}` (CLI `throughline lib trace ID`): the owner reads any
trace; an agent reads only traces of the same token and asker (otherwise 404)
written under the current policy version (otherwise 409).

## App-role routes

Routes that use the app role read tl tables below the database gate. Each one
calls `deps.require_unrestricted(token)` first, which resolves
`api.asker_unrestricted()` once per request inside the request's reader
transaction (before that function exists, only an owner-mode scope counts as
unrestricted) and answers 403 to a restricted scope before any lookup, so no
existence is confirmed. This covers `GET /v1/channels`, `/v1/status`, all of
`/v1/identity/*` and `/v1/registry/*`, `POST`/`PATCH /v1/links`,
`/v1/entities`, `/v1/entities/merge`, `/v1/embed`, `/v1/reparse`, ingest and
plugin routes.

**Rule for new routes:** every new app-role route calls
`require_unrestricted(token)` unless it serves only the caller's own working
state (its sessions, result sets, proposals, approvals, traces); such a
handler is decorated with `@deps.own_working_state` (below the route
decorator). Everything a restricted asker may read goes through
`deps.audited` or `query_ex` with `scope_of(token)`. A test enumerates every
route of `create_app()` (discovered modules included), and fails for a
handler that uses the app role (`authenticated_connection` or `connect`,
directly or through a helper in the same module) without either.

A multi-statement operation passes one `Trace` to each `audited(...,
trace=trace)` call and finishes with `deps.flush_trace(token, trace,
execution)`, which writes the trace, puts its id in the audit detail and in
the `Execution`, and never raises.

## Registration points

| What | Where | Picked up by |
|---|---|---|
| HTTP routes | `src/throughline/api/routes/<name>.py` exporting `router` (full paths) | `create_app`, in module-name order, with authentication attached to every route |
| CLI commands | `src/throughline/cli_commands/<name>.py` exporting `register(group)` | mounted under `throughline lib` |
| Allow-list | `src/throughline/allowlist/NNNN_name.json` | `query` at import |
| Operators | `operators.register(OperatorSpec(...))` | `operators.sync` at API start |
| Purge steps | rows in `tl.purge_hooks` | `throughline purge` |

Shared request plumbing (`authenticate`, `Token`, `require`, `plugin_access`,
`audit`, `authenticated_connection`, `audited`, `scope_of`,
`require_unrestricted`, `own_working_state`, `flush_trace`) lives in `throughline.api.deps` and is re-exported by
`throughline.api`. Route modules import from `deps`, never from the
application.

The measured CLI help (root, `token`, `identity`, `track`) has a fixed budget
(INT-1). The only root addition is `lib` ("Library operations."); nothing is
ever added to the root or to those three groups.

## Operator contract (CST-8, LIB-4)

`operators.OperatorSpec(name, implements, implementation, kind, inputs,
outputs, exact, access, provenance, cost_class, cancellable, cache_key,
version, estimate)`; `kind` is `leg`, `stage`, `ranker`, `semantic` or
`transform`.

- `register(spec)` rejects a duplicate name.
- `equivalents(implements, exact_only=True)`: implementations of one logical
  operation in registration order; the first is the reference.
- `choose(implements, profiles, allow_approximate=False)`: the reference,
  unless another implementation has a recorded quality not worse than the
  reference's and a lower p95. Missing or worse quality never wins;
  approximate implementations are considered only when allowed.
- `describe()`, `sync(conn)` (upserts `tl.operators`), `load_profiles(conn)`,
  `record_profile(...)` (`tl.operator_profiles`, source `bench` or
  `runtime`).
- `api.describe_operators()` returns the registry joined with each operator's
  latest profile.

## Purge hooks (ARC-5)

A component that keeps text derived from messages registers one statement:

```sql
INSERT INTO tl.purge_hooks(name, ord, statement)
VALUES ('passages', 20, 'DELETE FROM tl.passages p USING tl.messages m WHERE m.id=p.message_id AND m.channel_id=$1');
```

`throughline purge CHANNEL --confirm` calls `tl.run_purge_hooks(channel_id)`
first, in `ord` order (then name), while the channel's messages can still be
joined; `$1` is the channel id. A failing hook aborts the whole purge. Only
the owner role may write or run hooks. Traces hold no content and need none.

## Test isolation

`tests/conftest.py` empties every table in schema `tl` before each test
(`TRUNCATE ... RESTART IDENTITY CASCADE`, built from `pg_tables`) except the
schema-owned keep-list `schema_migrations`, `api_functions`,
`catalog_sections`, `record_sources`, `gates`, `purge_hooks`, `policy_state`,
`recipes`, and resets `tl.policy_state` to version 1. A new table needs no
fixture change; a new table whose rows are written by its migration must be
added to `KEEP_TABLES`.
