"""Restricted SQL grammar plus an independently read-only database connection."""

import csv
import io
import json
import time
from contextlib import contextmanager

from pglast.parser import ParseError, parse_sql_json, scan
from psycopg import sql
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from . import allowlist
from .budget import estimate_tokens
from .db import connect
from .execution import Execution
from .scope import Scope

# Additions come from allowlist/*.json, one file per migration.
_EXTRA_FUNCTIONS, _EXTRA_SCALAR = allowlist.load()
FUNCTIONS = (
    {
        "context",
        "search",
        "search_all",
        "search_filtered",
        "search_fused",
        "search_meaning",
        "near",
        "near_place",
        "overlapping",
        "during",
        "person",
        "person_messages",
        "follow",
        "catalog",
    }
    | _EXTRA_FUNCTIONS
    | _EXTRA_SCALAR
)
BUILTINS = {
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "lower",
    "upper",
    "initcap",
    "length",
    "char_length",
    "character_length",
    "octet_length",
    "bit_length",
    "coalesce",
    "nullif",
    "greatest",
    "least",
    "date_trunc",
    "date_bin",
    "extract",
    "date_part",
    "age",
    "now",
    "make_interval",
    "make_date",
    "make_timestamp",
    "make_timestamptz",
    "to_timestamp",
    "to_date",
    "row_number",
    "rank",
    "dense_rank",
    "lag",
    "lead",
    "first_value",
    "last_value",
    "nth_value",
    "ntile",
    "percent_rank",
    "cume_dist",
    "jsonb_agg",
    "json_agg",
    "array_agg",
    "string_agg",
    "bool_or",
    "bool_and",
    "every",
    "percentile_cont",
    "percentile_disc",
    "unnest",
    "jsonb_build_object",
    "jsonb_build_array",
    "json_build_object",
    "json_build_array",
    "jsonb_object_keys",
    "jsonb_array_elements",
    "jsonb_array_elements_text",
    "jsonb_each",
    "jsonb_each_text",
    "jsonb_typeof",
    "jsonb_array_length",
    "json_array_length",
    "to_jsonb",
    "to_json",
    "row_to_json",
    "websearch_to_tsquery",
    "plainto_tsquery",
    "ts_rank_cd",
    "ts_rank",
    "abs",
    "round",
    "ceil",
    "ceiling",
    "floor",
    "trunc",
    "mod",
    "power",
    "sqrt",
    "sign",
    "width_bucket",
    "generate_series",
    "substring",
    "substr",
    "split_part",
    "replace",
    "translate",
    "concat",
    "concat_ws",
    "reverse",
    "starts_with",
    "btrim",
    "ltrim",
    "rtrim",
    "position",
    "strpos",
    "left",
    "right",
    "chr",
    "ascii",
    "md5",
    "array_length",
    "cardinality",
    "array_to_string",
    "string_to_array",
    "array_position",
    "array_remove",
    "timezone",
    "to_char",
    # Added for the web interface (docs/web/backend.md). Every name below is
    # immutable or stable, has no side effect, reads no file, network, setting
    # or catalog, and its output is bounded by its input (and by the row cap,
    # the response budget and the statement timeout like everything else).
    # Binning and arithmetic.
    "floor",
    "ceil",
    "ceiling",
    "width_bucket",
    "date_bin",
    "ln",
    "log",
    "sqrt",
    "power",
    "exp",
    "sign",
    "mod",
    # Aggregates, including the ordered-set ones (WITHIN GROUP).
    "percentile_cont",
    "percentile_disc",
    "mode",
    "stddev_samp",
    "stddev_pop",
    "var_samp",
    "corr",
    "string_agg",
    "bool_or",
    "bool_and",
    # Window functions.
    "lag",
    "lead",
    "first_value",
    "last_value",
    "nth_value",
    "ntile",
    "percent_rank",
    "cume_dist",
    # Text, arrays and JSON inspection.
    "split_part",
    "concat",
    "char_length",
    "cardinality",
    "array_length",
    "jsonb_typeof",
    "jsonb_array_length",
    # Time.
    "to_timestamp",
    "age",
    "justify_interval",
    "make_interval",
    "isfinite",
    # Set-returning: one output row per element of a value the statement
    # already holds, so the row cap and the timeout bound them. In FROM they
    # go through range_function below like generate_series.
    "jsonb_array_elements",
    "jsonb_array_elements_text",
    "jsonb_object_keys",
    "unnest",
}
# FROM functions whose single output column has a fixed name of its own (an OUT
# parameter), whatever the table alias: alias.<name> is that column.
NAMED_OUTPUT = {
    "jsonb_array_elements": "value",
    "jsonb_array_elements_text": "value",
}
# SQL-standard syntax (EXTRACT, AT TIME ZONE, SUBSTRING, TRIM, POSITION,
# OVERLAY, OVERLAPS, LIKE ... ESCAPE, SIMILAR TO) is parsed into calls of these
# pg_catalog functions; no other schema-qualified builtin is accepted.
SQL_SYNTAX = {
    "extract",
    "timezone",
    "substring",
    "btrim",
    "ltrim",
    "rtrim",
    "position",
    "overlay",
    "overlaps",
    "like_escape",
    "similar_to_escape",
}
# Allow-listed api functions that return one scalar instead of rows.
SCALAR_FUNCTIONS = {"catalog"} | _EXTRA_SCALAR
# One result row larger than this (as PostgreSQL row text) is never sent to the
# API process. Row text is at most about twice the size of its JSON encoding.
MAX_ROW_TEXT = 8 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
# No statement of a request runs longer than this, whatever the deadline.
MAX_STATEMENT_MS = 30000
MAX_PARAMS = 200
MAX_STREAM_ROWS = 1_000_000


def normalize_params(params):
    """Validate optional positional SQL parameters ($1, $2, ...)."""
    if params is None:
        return None
    if not isinstance(params, (list, tuple)):
        raise ValueError("params must be a list")
    if len(params) > MAX_PARAMS:
        raise ValueError(f"at most {MAX_PARAMS} query parameters are allowed")
    return list(params)


def _adapt_param(value):
    if isinstance(value, dict):
        return Jsonb(value)
    return value


def bind_params(statement, params):
    """Rewrite PostgreSQL $1..$N parameter tokens to psycopg %s placeholders.

    Uses PostgreSQL's own lexer (pglast.parser.scan), so $1 inside string
    literals or comments is untouched, repeated $1 references work, and literal
    % characters (such as LIKE '%foo%') are safely escaped to %% only when
    parameters are bound.
    """
    params = normalize_params(params)
    if not params:
        return statement, None
    raw = statement.encode("utf-8")
    out = []
    bound = []
    pos = 0
    for tok in scan(statement):
        if tok.name != "PARAM":
            continue
        start = tok.start
        end = tok.end + 1
        index = int(raw[start:end].decode("ascii")[1:])
        if index < 1 or index > len(params):
            raise ValueError(f"parameter ${index} is out of range (1..{len(params)})")
        out.append(raw[pos:start].decode("utf-8").replace("%", "%%"))
        out.append("%s")
        bound.append(_adapt_param(params[index - 1]))
        pos = end
    if not bound:
        return statement, None
    out.append(raw[pos:].decode("utf-8").replace("%", "%%"))
    return "".join(out), tuple(bound)


def validate_sql(statement, *, param_count=0):
    """Reject anything but one allow-listed SELECT; return its bare text."""
    try:
        tree = json.loads(parse_sql_json(statement))
    except ParseError:
        raise ValueError("invalid SQL syntax") from None
    stmts = tree["stmts"]
    if len(stmts) != 1 or "SelectStmt" not in stmts[0]["stmt"]:
        raise ValueError("only one SELECT statement is allowed")

    def strings(nodes):
        return [n["String"]["sval"] for n in nodes]

    # PostgreSQL resolves alias.name to name(alias) when the alias has no such
    # column, and the "whole row" of a FROM function that returns a scalar is
    # that scalar. alias.name on such an item would therefore call any
    # one-argument function, outside the allow-list. Record every FROM function
    # whose row is not known to be composite, with the only column names it has.
    scalar_aliases = {}

    def range_function(value):
        alias = value.get("alias") or {}
        if value.get("coldeflist"):
            raise ValueError("column definition lists are not allowed")
        calls = []
        for item in value.get("functions", []):
            call = item.get("List", {}).get("items", [{}])[0]
            if "FuncCall" not in call:
                raise ValueError("only function calls are allowed in FROM")
            calls.append(strings(call["FuncCall"]["funcname"]))
        if (
            len(calls) == 1
            and len(calls[0]) == 2
            and calls[0][0] == "api"
            and calls[0][1] in FUNCTIONS - SCALAR_FUNCTIONS
        ):
            return
        implicit = {call[-1] for call in calls}
        columns = set(strings(alias.get("colnames", [])))
        if not columns:
            columns = (
                {alias["aliasname"]}
                if alias.get("aliasname") and len(calls) == 1
                else set(implicit)
            )
        if not alias.get("colnames") and len(calls) == 1 and len(calls[0]) == 1:
            named = NAMED_OUTPUT.get(calls[0][0])
            if named:
                # The function's own output column is the only column: the
                # alias names the relation, not a column. Counting the alias
                # as a column would let `x.x` resolve as a call of function x.
                columns = {named}
        if value.get("ordinality"):
            columns.add("ordinality")
        names = {alias["aliasname"]} if alias.get("aliasname") else implicit
        for name in names:
            scalar_aliases.setdefault(name, set()).update(columns)

    def collect(node):
        if isinstance(node, list):
            for v in node:
                collect(v)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key == "RangeFunction":
                    range_function(value)
                collect(value)

    collect(tree)

    def walk(node, ctes=frozenset()):
        if isinstance(node, list):
            for v in node:
                walk(v, ctes)
        elif isinstance(node, dict):
            with_clause = node.get("withClause")
            if isinstance(with_clause, dict):
                # A WITH name is a relation only inside its own statement, and
                # a non-recursive entry sees the entries before it, never
                # itself. Any other unqualified name must still be in schema api.
                entries = with_clause.get("ctes", [])
                names = [e["CommonTableExpr"]["ctename"] for e in entries]
                visible = ctes
                if with_clause.get("recursive"):
                    visible = ctes | frozenset(names)
                for entry, name in zip(entries, names):
                    walk(entry, visible)
                    visible = visible | {name}
                ctes = ctes | frozenset(names)
            for key, value in node.items():
                if key == "withClause":
                    continue
                if key.endswith("Stmt") and key != "SelectStmt":
                    raise ValueError("only SELECT is allowed")
                if key == "ColumnRef":
                    fields = value.get("fields", [])
                    names = [
                        item["String"]["sval"] for item in fields if "String" in item
                    ]
                    if len(names) > 2 and not (len(names) == 3 and names[0] == "api"):
                        raise ValueError("composite attribute notation is not allowed")
                    if (
                        len(names) == 2
                        and names[0] in scalar_aliases
                        and names[1] not in scalar_aliases[names[0]]
                    ):
                        raise ValueError("attribute function notation is not allowed")
                if key == "RangeVar" and (
                    value.get("catalogname")
                    or not (
                        value.get("schemaname") == "api"
                        or (
                            not value.get("schemaname") and value.get("relname") in ctes
                        )
                    )
                ):
                    raise ValueError(
                        "relations must be explicitly in schema api: "
                        f"{value.get('schemaname') or ''}{'.' if value.get('schemaname') else ''}"
                        f"{value.get('relname')} is not readable; use api.<view> "
                        "(api.messages, api.threads, api.people, ch_<channel> views)"
                    )
                if key == "A_Indirection" and any(
                    "String" in item for item in value.get("indirection", [])
                ):
                    raise ValueError("attribute function notation is not allowed")
                if key == "FuncCall":
                    names = strings(value["funcname"])
                    if not (
                        (
                            len(names) == 2
                            and names[0] == "api"
                            and names[1] in FUNCTIONS
                        )
                        or (
                            len(names) == 2
                            and names[0] == "pg_catalog"
                            and names[1] in SQL_SYNTAX
                        )
                        or (len(names) == 1 and names[0] in BUILTINS)
                    ):
                        name = ".".join(names)
                        hint = (
                            f"; did you mean api.{names[-1]}(...)?"
                            if names[-1] in FUNCTIONS
                            else "; api functions are listed by describe functions"
                        )
                        raise ValueError(
                            f"function is outside the query allow-list: {name}{hint}"
                        )
                if key in ("TypeName", "typeName"):
                    names = strings(value.get("names", []))
                    if any(
                        n
                        in (
                            "regclass",
                            "regproc",
                            "regprocedure",
                            "regrole",
                            "regnamespace",
                            # age(xid) and friends read the transaction
                            # counter; no api column has these types.
                            "xid",
                            "xid8",
                        )
                        for n in names
                    ) or (len(names) > 1 and names[0] != "pg_catalog"):
                        raise ValueError("type is outside the query allow-list")
                if key == "A_Expr" and len(value.get("name", [])) > 1:
                    raise ValueError("qualified operators are not allowed")
                if key in ("intoClause", "lockingClause"):
                    raise ValueError("SELECT cannot create or lock tables")
                if key == "ParamRef":
                    number = value.get("number")
                    if not param_count:
                        raise ValueError(
                            "query parameters ($1, $2, ...) require a params list"
                        )
                    if (
                        isinstance(number, bool)
                        or not isinstance(number, int)
                        or number < 1
                        or number > param_count
                    ):
                        raise ValueError(
                            f"parameter ${number} is out of range (1..{param_count})"
                        )
                walk(value, ctes)

    walk(tree)
    raw = statement.encode("utf-8")
    start = stmts[0].get("stmt_location", 0)
    length = stmts[0].get("stmt_len")
    return raw[start : start + length if length else None].decode("utf-8")


@contextmanager
def reader_transaction(scope, deadline_ms=None):
    """The only way to read as throughline_reader.

    Opens a read-only transaction that carries the caller's scope as
    transaction settings (read by the gate functions in the database) and a
    statement timeout of min(30 s, deadline). A scope is required: there is no
    unscoped reader connection anywhere in the service.
    """
    if not isinstance(scope, Scope):
        raise TypeError("reader_transaction requires a Scope")
    timeout = MAX_STATEMENT_MS
    if deadline_ms is not None:
        timeout = max(1, min(MAX_STATEMENT_MS, int(deadline_ms)))
    with connect("reader") as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(
            sql.SQL("SET LOCAL statement_timeout={}").format(sql.Literal(timeout))
        )
        for name, value in scope.gucs().items():
            conn.execute(
                sql.SQL("SET LOCAL {}={}").format(
                    sql.SQL(".").join(map(sql.Identifier, name.split("."))),
                    sql.Literal(value),
                )
            )
        yield conn


def query_ex(
    statement,
    cap=1000,
    *,
    scope,
    params=None,
    deadline_ms=None,
    token_budget=None,
    execution=None,
    materialize=True,
    token_row=None,
    budgets=None,
    dry_run=False,
    approval_id=None,
    strict_exact=False,
    trace=None,
    columns=None,
):
    """Run one validated SELECT under a scope; rows plus execution metadata.

    columns, when a list is passed, receives the output columns in order as
    {name, type} (PostgreSQL type names), also for a result without rows and
    with duplicate names kept; the returned dict keeps its four keys.

    deadline_ms bounds this call (maintenance wait included); the statement
    itself never gets more than 30 s. token_budget stops the result before the
    row that would exceed it (estimate: characters / 4). materialize=False
    skips the deferred-maintenance wait, so a caller that runs several
    statements does it once. A passed Execution receives one stage and the
    timings; otherwise a fresh one is used. Truncation reasons: row_cap,
    response_bytes, row_too_large, token_budget.

    A statement with semantic calls (api.recall, ...) is run by semantic.run
    with token_row, budgets, dry_run, approval_id, strict_exact and trace.
    Every statement is analysed for exactness (QRY-38): aggregates over
    ranked or sampled rows and hit counts at record grain add warnings, and
    strict_exact rejects the former before any work.
    """
    # Fail closed before any work (validation, maintenance wait) is done.
    if not isinstance(scope, Scope):
        raise TypeError("query_ex requires a Scope")
    params = normalize_params(params)
    from . import semantic

    calls = semantic.find_calls(statement)
    if calls:
        return semantic.run(
            statement,
            cap,
            scope=scope,
            params=params,
            token_row=token_row,
            budgets=budgets,
            deadline_ms=deadline_ms,
            token_budget=token_budget,
            dry_run=dry_run,
            approval_id=approval_id,
            strict_exact=strict_exact,
            trace=trace,
            execution=execution,
            materialize=materialize,
            calls=calls,
            columns=columns,
        )
    started = time.perf_counter()
    statement = validate_sql(statement, param_count=len(params or ()))
    bound_sql, bound_values = bind_params(statement, params)
    cap = max(1, min(cap, 10000))
    execution = execution if execution is not None else Execution()
    analysis = handles = None
    try:
        tree = json.loads(parse_sql_json(statement))
        handles = semantic.handle_info(scope, semantic.handle_literals(tree))
        analysis = semantic.analyze(tree, handles=handles)
    except Exception as exc:  # The analysis must never break a valid read.
        execution.warn("exactness analysis unavailable: " + type(exc).__name__)
    if analysis is not None:
        semantic.apply(analysis, execution, strict_exact=strict_exact, handles=handles)
    if dry_run:
        execution.set("dry_run", True)
        execution.set("plan", [])
        execution.set("estimate", dict(cost=0.0, calls=0, tokens=0, parts=[]))
        execution.stage("sql", "ok", dry_run=True)
        return dict(
            rows=[], truncated=False, row_count=0, execution=execution.to_dict()
        )
    materialize_ms = 0.0
    if materialize:
        # Bounded and best effort: never waits on an ingest lock and never
        # fails the read. Dirty threads stay exact through the api views
        # meanwhile.
        from .derived import materialize_for_read, read_wait_s

        wait = None
        if deadline_ms is not None:
            wait = min(read_wait_s(), max(0.0, deadline_ms / 1000))
        if not materialize_for_read(wait):
            execution.warn("deferred thread maintenance did not finish in time")
        materialize_ms = (time.perf_counter() - started) * 1000
    remaining = None
    if deadline_ms is not None:
        remaining = deadline_ms - (time.perf_counter() - started) * 1000
    rows = []
    reason = limit = None
    described = None
    database_started = time.perf_counter()
    try:
        with reader_transaction(scope, remaining) as conn:
            # The statement runs as a subquery so the server, not this
            # process, decides what is sent: a row above MAX_ROW_TEXT arrives
            # as an empty marker instead of being transferred and decoded, and
            # LIMIT stops the query one row past the cap. Rows are streamed one
            # at a time, so at most one bounded row is held before the
            # response budget is checked.
            bounded = (
                "SELECT d.* FROM (\n"
                + bound_sql
                + "\n) q LEFT JOIN LATERAL (SELECT true AS ok, q.*) d"
                # (q.*) is always the subquery's row; a bare q would first
                # match an output column of that name and be ambiguous with
                # d's copy.
                + f" ON octet_length((q.*)::text) <= {MAX_ROW_TEXT} LIMIT {cap + 1}"
            )
            size = 64  # Response wrapper plus the largest row_count encoding.
            tokens = 0
            with conn.cursor(row_factory=tuple_row) as cursor:
                stream = (
                    cursor.stream(bounded, bound_values)
                    if bound_values is not None
                    else cursor.stream(bounded)
                )
                try:
                    for record in stream:
                        if record[0] is None:
                            reason, limit = "row_too_large", MAX_ROW_TEXT
                            break
                        if len(rows) == cap:
                            reason, limit = "row_cap", cap
                            break
                        row = {
                            column.name: value
                            for column, value in zip(cursor.description[1:], record[1:])
                        }
                        encoded = json.dumps(row, default=str, ensure_ascii=False)
                        size += len(encoded.encode("utf-8"))
                        if size > MAX_RESPONSE:
                            reason, limit = "response_bytes", MAX_RESPONSE
                            break
                        if token_budget is not None:
                            tokens += estimate_tokens(encoded)
                            if tokens > token_budget:
                                reason, limit = "token_budget", token_budget
                                break
                        rows.append(row)
                finally:
                    # Closing an unfinished stream cancels the statement.
                    stream.close()
                    if columns is not None and cursor.description:
                        described = [
                            (column.name, column.type_code)
                            for column in cursor.description[1:]
                        ]
    except Exception as exc:
        now = time.perf_counter()
        code = getattr(exc, "sqlstate", None)
        execution.add_timing("materialize", materialize_ms)
        execution.add_timing("database", (now - database_started) * 1000)
        execution.add_timing("total", (now - started) * 1000)
        execution.stage(
            "sql",
            "timeout" if code == "57014" else "failed",
            reason="statement cancelled at the deadline"
            if code == "57014"
            else "database operation rejected",
            code=code,
        )
        raise
    finished = time.perf_counter()
    database_ms = (finished - database_started) * 1000
    execution.add_timing("materialize", materialize_ms)
    execution.add_timing("database", database_ms)
    execution.add_timing("total", (finished - started) * 1000)
    if reason:
        execution.set_truncation(reason, limit)
    execution.stage("sql", rows=len(rows), ms=round(database_ms, 3), truncated=reason)
    if columns is not None:
        if described is None:
            # A result without rows carries no description in single-row
            # mode: plan the statement once more with a zero-row limit.
            try:
                described = _describe(statement, scope)
            except Exception:  # Never fail a read that already succeeded.
                described = []
        columns[:] = _columns(described, scope)
    return dict(
        rows=rows,
        truncated=reason is not None,
        row_count=len(rows),
        execution=execution.to_dict(),
    )


def _columns(described, scope):
    """[{name, type}] for (name, type oid) pairs. Built-in types are named from
    the driver's registry; any other type costs one short catalogue read."""
    from psycopg import postgres

    names = {}
    for _, oid in described:
        info = postgres.types.get(oid)
        if info is not None:
            names[oid] = "_" + info.name if oid == info.array_oid else info.name
    unknown = sorted({oid for _, oid in described if oid not in names})
    if unknown:
        try:
            with reader_transaction(scope, 5000) as conn:
                for row in conn.execute(
                    "SELECT t.oid::int8 AS oid,t.typname FROM pg_catalog.pg_type t WHERE t.oid=ANY(%s::oid[])",
                    (unknown,),
                ):
                    names[row["oid"]] = row["typname"]
        except Exception:  # A type name is never worth failing a read for.
            pass
    return [dict(name=name, type=names.get(oid, "unknown")) for name, oid in described]


def _describe(statement, scope, params=None):
    """(name, type oid) of each output column of a validated statement."""
    bound_sql, bound_values = bind_params(statement, params)
    with reader_transaction(scope, 5000) as conn:
        with conn.cursor(row_factory=tuple_row) as cursor:
            sql_text = "SELECT * FROM (\n" + bound_sql + "\n) q LIMIT 0"
            if bound_values is not None:
                cursor.execute(sql_text, bound_values)
            else:
                cursor.execute(sql_text)
            return [(c.name, c.type_code) for c in cursor.description or []]


def describe_sql(statement, *, scope, params=None):
    """Validate one statement and describe its output without running it.

    The statement is checked by validate_sql, then planned by the database
    under the scope with a zero-row limit, so no row is produced; unknown
    names and type errors surface as the usual psycopg error. Statements with
    semantic calls are not handled here (use a dry run). Returns the columns
    as [{name, type}].
    """
    if not isinstance(scope, Scope):
        raise TypeError("describe_sql requires a Scope")
    params = normalize_params(params)
    validated = validate_sql(statement, param_count=len(params or ()))
    return _columns(_describe(validated, scope, params=params), scope)


def prepare_stream(
    statement,
    *,
    scope,
    params=None,
    row_cap=100_000,
    deadline_ms=None,
    token_row=None,
    budgets=None,
    approval_id=None,
    strict_exact=False,
    materialize=True,
):
    """Validate and prepare a streaming SQL read before headers are sent."""
    if not isinstance(scope, Scope):
        raise TypeError("prepare_stream requires a Scope")
    params = normalize_params(params)
    started = time.perf_counter()
    from . import semantic

    calls = semantic.find_calls(statement)
    if calls:
        resolved = semantic.run(
            statement,
            min(row_cap, 10000),
            scope=scope,
            params=params,
            token_row=token_row,
            budgets=budgets,
            deadline_ms=deadline_ms,
            approval_id=approval_id,
            strict_exact=strict_exact,
            materialize=materialize,
            calls=calls,
            resolve_only=True,
        )
        statement = resolved["statement"]
        materialize = False
    statement = validate_sql(statement, param_count=len(params or ()))
    bound_sql, bound_values = bind_params(statement, params)
    cap = max(1, min(int(row_cap), MAX_STREAM_ROWS))
    execution = Execution()
    analysis = handles = None
    try:
        tree = json.loads(parse_sql_json(statement))
        handles = semantic.handle_info(scope, semantic.handle_literals(tree))
        analysis = semantic.analyze(tree, handles=handles)
    except Exception:
        pass
    if analysis is not None:
        semantic.apply(analysis, execution, strict_exact=strict_exact, handles=handles)
    if materialize:
        from .derived import materialize_for_read, read_wait_s

        wait = None
        if deadline_ms is not None:
            wait = min(read_wait_s(), max(0.0, deadline_ms / 1000))
        materialize_for_read(wait)
    remaining = None
    if deadline_ms is not None:
        remaining = max(1, int(deadline_ms - (time.perf_counter() - started) * 1000))
    return dict(
        bound_sql=bound_sql,
        bound_values=bound_values,
        scope=scope,
        cap=cap,
        remaining_ms=remaining,
    )


def _csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def iter_stream(prepared, *, fmt="ndjson"):
    """Stream validated rows as NDJSON lines or RFC 4180 CSV bytes."""
    bounded = (
        "SELECT d.* FROM (\n"
        + prepared["bound_sql"]
        + "\n) q LEFT JOIN LATERAL (SELECT true AS ok, q.*) d"
        + f" ON octet_length((q.*)::text) <= {MAX_ROW_TEXT} LIMIT {prepared['cap']}"
    )
    bound_values = prepared["bound_values"]
    with reader_transaction(prepared["scope"], prepared["remaining_ms"]) as conn:
        with conn.cursor(row_factory=tuple_row) as cursor:
            stream = (
                cursor.stream(bounded, bound_values)
                if bound_values is not None
                else cursor.stream(bounded)
            )
            try:
                if fmt == "csv":
                    buf = io.StringIO()
                    writer = csv.writer(buf)
                    wrote_header = False
                    for record in stream:
                        if not wrote_header and cursor.description:
                            writer.writerow([col.name for col in cursor.description[1:]])
                            yield buf.getvalue().encode("utf-8")
                            buf.seek(0)
                            buf.truncate(0)
                            wrote_header = True
                        if record[0] is None:
                            break
                        writer.writerow([_csv_cell(v) for v in record[1:]])
                        yield buf.getvalue().encode("utf-8")
                        buf.seek(0)
                        buf.truncate(0)
                    if not wrote_header and cursor.description:
                        writer.writerow([col.name for col in cursor.description[1:]])
                        yield buf.getvalue().encode("utf-8")
                else:
                    columns = None
                    for record in stream:
                        if record[0] is None:
                            break
                        if columns is None:
                            columns = [col.name for col in cursor.description[1:]]
                        row = dict(zip(columns, record[1:]))
                        yield (
                            json.dumps(row, default=str, ensure_ascii=False) + "\n"
                        ).encode("utf-8")
            finally:
                stream.close()


def query(statement, cap=1000, *, params=None):
    """Legacy in-process read as the local owner: exactly three keys."""
    result = query_ex(statement, cap, scope=Scope.local(), params=params)
    return dict(
        rows=result["rows"],
        truncated=result["truncated"],
        row_count=result["row_count"],
    )
