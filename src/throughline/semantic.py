"""Semantic SQL runtime (QRY-31, QRY-36..38, CST-4, CST-10).

A statement may use semantic functions (api.recall, and every operator of
throughline.semantic_ops) as the single function of a FROM item:

    SELECT r.record_id, m.channel
    FROM api.recall(query => 'orchard', k => 10) r
    JOIN api.messages m ON m.id = r.message_id

The read path is unchanged: the statement that finally runs is one validated
SELECT on the reader connection under the caller's scope. Each semantic call
is run first, in Python, under one budget, and produces a result set owned by
the caller; its text is then replaced by api.result_set('<handle>'), keeping
the alias. Semantic functions are never Postgres functions and never on the
allow-list, so a statement that bypassed this runtime fails validation.

run() steps: find the calls (placement rules, constant arguments) and
validate the statement with placeholders before any work; clamp budgets and
open one tracker, Execution and Trace; resolve input_sql sets; collect the
estimates (dry_run stops here); pass the cost gate (approval_required stops
here, nothing spent); run the operators concurrently; splice the real handles,
validate again and run through query.query_ex with the same Execution.

Exactness (QRY-38): analyze() marks relations whose rows are not a
population (ranked functions without an explicit NULL limit, ranked or cut
result sets, LIMIT levels, and everything that reads them) and reports
aggregates, DISTINCT, GROUP BY and window aggregates over them. Under
strict_exact these are rejected. Counting hits of a search or recall at record
grain also warns 'hit_count_not_incident_count': count DISTINCT
episode_id from api.episodes_of instead.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field

from pglast.parser import ParseError, parse_sql_json, scan

from . import semantic_ops

log = logging.getLogger("throughline.semantic")

DEFAULT_MAX_CALLS = 4
MAX_INPUT_ROWS = 10000
PLACEHOLDER = "rs_" + "0" * 32
HANDLE = re.compile(r"^rs_[0-9a-f]{32}$")
AGGREGATES = {
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "array_agg",
    "jsonb_agg",
    "json_agg",
}
AGGREGATE_WARNING = "aggregate over a ranked top-k or sampled set, not a population"
GRAIN_CODE = "hit_count_not_incident_count"
GRAIN_WARNING = (
    GRAIN_CODE + ": a hit count is not an incident count;"
    " count DISTINCT episode_id from api.episodes_of"
)
# Handles produced by these operators hold hits, not incidents.
HIT_OPS = ("recall", "expand", "match")
# Ranked functions when tl.api_functions cannot be read (before 0032).
_DEFAULT_RANKED = {
    "search": (2, "max_results"),
    "search_all": (2, "max_results"),
    "search_filtered": (2, "max_results"),
    "search_meaning": (2, "max_results"),
    "search_fused": (3, "max_results"),
}
_ranked_cache = {"at": 0.0, "value": None}
_ranked_lock = threading.Lock()
RANKED_TTL_S = 60


def max_calls():
    try:
        value = int(os.getenv("THROUGHLINE_MAX_SEMANTIC_CALLS") or DEFAULT_MAX_CALLS)
    except ValueError:
        value = DEFAULT_MAX_CALLS
    return max(1, min(value, 16))


def _reject(name, construct):
    return ValueError(
        f"semantic function api.{name} is not supported here: {construct}"
    )


@dataclass
class Call:
    """One semantic call: name, bound arguments, byte span and FROM alias.

    start/end are byte offsets into the UTF-8 statement (end exclusive);
    replacing statement[start:end] keeps the alias. input_sql maps an input
    parameter to (statement, max_rows) when the set is given as SQL.
    """

    name: str
    args: dict
    start: int
    end: int
    alias: str | None = None
    input_sql: dict = field(default_factory=dict)
    top_level: bool = False

    def describe(self):
        return dict(
            name=self.name,
            alias=self.alias,
            start=self.start,
            end=self.end,
            args={
                k: (v if not isinstance(v, str) or len(v) <= 200 else _sha(v))
                for k, v in self.args.items()
            },
            input_sql={
                k: dict(statement_sha256=_sha(s)[7:], max_rows=n)
                for k, (s, n) in self.input_sql.items()
            },
        )


def _sha(text):
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _tree(statement):
    try:
        return json.loads(parse_sql_json(statement))
    except ParseError:
        raise ValueError("invalid SQL syntax") from None


def _names(nodes):
    return [n["String"]["sval"] for n in nodes or [] if "String" in n]


def _func_name(call):
    names = _names(call.get("funcname"))
    if len(names) == 2 and names[0] == "api":
        return names[1]
    if len(names) == 1:
        return names[0]
    return None


# --- tokens and byte spans -----------------------------------------------------


def _byte_tokens(statement):
    """Tokens as (byte start, byte end exclusive, name); scan() counts chars."""
    offsets = [0]
    for ch in statement:
        offsets.append(offsets[-1] + len(ch.encode("utf-8")))
    tokens = []
    for token in scan(statement):
        tokens.append((offsets[token.start], offsets[token.end + 1], token.name))
    return tokens


def _call_end(tokens, start):
    """Byte offset after the parenthesis closing the call that begins at start."""
    depth = 0
    seen = False
    for begin, end, name in tokens:
        if begin < start:
            continue
        if name == "ASCII_40":
            depth += 1
            seen = True
        elif name == "ASCII_41":
            depth -= 1
            if seen and depth == 0:
                return end
    raise ValueError("invalid SQL syntax")


# --- constant arguments ----------------------------------------------------------


def _constant(node, name):
    """Python value of a constant argument, or ValueError naming the construct."""
    if "A_Const" in node:
        value = node["A_Const"]
        if value.get("isnull"):
            return None
        if "sval" in value:
            return value["sval"].get("sval", "")
        if "ival" in value:
            return int(value["ival"].get("ival", 0))
        if "fval" in value:
            text = value["fval"].get("fval", "0")
            return float(text)
        if "boolval" in value:
            return bool(value["boolval"].get("boolval", False))
        raise _reject(name, "unsupported constant")
    if "TypeCast" in node:
        cast = node["TypeCast"]
        inner = _constant(cast["arg"], name)
        type_names = _names(cast.get("typeName", {}).get("names"))
        base = type_names[-1] if type_names else ""
        if inner is None or cast.get("typeName", {}).get("arrayBounds"):
            return inner
        try:
            if base in ("json", "jsonb") and isinstance(inner, str):
                return json.loads(inner)
            if base in ("int2", "int4", "int8", "integer", "bigint", "smallint"):
                return int(inner)
            if base in ("float4", "float8", "numeric", "real"):
                return float(inner)
            if base == "bool" and isinstance(inner, str):
                return inner.strip().lower() in ("true", "t", "on", "1", "yes")
        except (TypeError, ValueError):
            raise _reject(name, f"constant cannot be cast to {base}") from None
        return inner
    if "A_ArrayExpr" in node:
        return [_constant(e, name) for e in node["A_ArrayExpr"].get("elements", [])]
    if "ColumnRef" in node:
        raise _reject(name, "column-reference argument")
    if "SubLink" in node:
        raise _reject(name, "subquery argument")
    if "FuncCall" in node:
        raise _reject(name, "function call argument")
    if "ParamRef" in node:
        raise _reject(name, "parameter argument")
    raise _reject(name, "non-constant argument")


# --- finding calls -----------------------------------------------------------------

CLAUSES = {
    "targetList": "target list",
    "whereClause": "WHERE",
    "groupClause": "GROUP BY",
    "havingClause": "HAVING",
    "sortClause": "ORDER BY",
    "windowClause": "window",
    "distinctClause": "DISTINCT",
    "valuesLists": "VALUES",
    "limitCount": "LIMIT",
    "limitOffset": "OFFSET",
    "quals": "JOIN ON",
}


class _Finder:
    def __init__(self, statement, ops):
        self.statement = statement
        self.ops = ops
        self.tokens = None
        self.calls = []

    def find(self, tree):
        stmts = tree.get("stmts", [])
        for stmt in stmts:
            node = stmt.get("stmt", {})
            if "SelectStmt" in node:
                self.select(node["SelectStmt"], top=True, recursive=False)
            else:
                self.expr(node, None, False)
        return self.calls

    def _semantic(self, call):
        names = _names(call.get("funcname"))
        if len(names) == 2 and names[0] == "api" and names[1] in self.ops:
            return names[1]
        return None

    def select(self, node, top, recursive):
        with_clause = node.get("withClause")
        if isinstance(with_clause, dict):
            recursive = recursive or bool(with_clause.get("recursive"))
            for entry in with_clause.get("ctes", []):
                query = entry.get("CommonTableExpr", {}).get("ctequery", {})
                if "SelectStmt" in query:
                    self.select(query["SelectStmt"], False, recursive)
                else:
                    self.expr(query, "WITH", recursive)
        for side in ("larg", "rarg"):
            if node.get(side):
                self.select(node[side], False, recursive)
        for item in node.get("fromClause", []) or []:
            self.from_item(item, top, recursive, alone=len(node["fromClause"]) == 1)
        for key, value in node.items():
            if key in ("withClause", "fromClause", "larg", "rarg"):
                continue
            if key in CLAUSES:
                self.expr(value, CLAUSES[key], recursive)
            elif isinstance(value, (dict, list)):
                self.expr(value, key, recursive)

    def from_item(self, item, top, recursive, alone):
        if "RangeFunction" in item:
            self.range_function(item["RangeFunction"], top, recursive, alone)
        elif "JoinExpr" in item:
            join = item["JoinExpr"]
            for side in ("larg", "rarg"):
                if join.get(side):
                    self.from_item(join[side], top, recursive, False)
            if join.get("quals"):
                self.expr(join["quals"], "JOIN ON", recursive)
        elif "RangeSubselect" in item:
            sub = item["RangeSubselect"].get("subquery", {})
            if "SelectStmt" in sub:
                self.select(sub["SelectStmt"], False, recursive)
        elif isinstance(item, dict):
            self.expr(item, "FROM", recursive)

    def range_function(self, value, top, recursive, alone):
        functions = value.get("functions", []) or []
        found = []
        for entry in functions:
            items = entry.get("List", {}).get("items", [])
            call = (
                items[0].get("FuncCall") if items and "FuncCall" in items[0] else None
            )
            if call is None:
                self.expr(entry, "FROM", recursive)
                continue
            name = self._semantic(call)
            if name is None:
                self.expr(call.get("args", []), "function argument", recursive)
                continue
            found.append((name, call))
        for name, call in found:
            if len(functions) > 1:
                raise _reject(name, "ROWS FROM with several functions")
            if value.get("lateral"):
                raise _reject(name, "LATERAL")
            if value.get("ordinality"):
                raise _reject(name, "WITH ORDINALITY")
            if value.get("coldeflist"):
                raise _reject(name, "column definition list")
            if recursive:
                raise _reject(name, "recursive CTE")
            for flag, construct in (
                ("agg_star", "aggregate call"),
                ("agg_distinct", "aggregate call"),
                ("agg_order", "aggregate call"),
                ("agg_filter", "FILTER"),
                ("over", "window"),
                ("agg_within_group", "WITHIN GROUP"),
                ("func_variadic", "VARIADIC"),
            ):
                if call.get(flag):
                    raise _reject(name, construct)
            op = self.ops[name]
            if op.top_level_only and not (top and alone):
                raise _reject(name, "only allowed alone in the top-level FROM")
            alias = (value.get("alias") or {}).get("aliasname")
            self.calls.append(self.bind(name, op, call, alias, top and alone))

    def bind(self, name, op, call, alias, top_alone):
        positional, named = [], {}
        for arg in call.get("args", []) or []:
            if "NamedArgExpr" in arg:
                named_arg = arg["NamedArgExpr"]
                key = named_arg.get("name")
                if key in named:
                    raise _reject(name, f"parameter {key} given twice")
                named[key] = _constant(named_arg["arg"], name)
            else:
                if named:
                    raise _reject(name, "positional argument after a named one")
                positional.append(_constant(arg, name))
        input_sql = {}
        statement = (
            named.pop("input_sql", None) if "input_sql" not in op.params else None
        )
        bound_rows = None
        if "max_rows" in named:
            bound_rows = named["max_rows"]
            if "max_rows" not in op.params:
                named.pop("max_rows")
        if statement is not None:
            if not op.input_params:
                raise _reject(name, "unknown parameter input_sql")
            target = op.input_params[0]
            if target in named or len(positional) > list(op.params).index(target):
                raise _reject(name, f"both {target} and input_sql given")
            input_sql[target] = (
                self.nested(name, statement),
                self.max_rows(name, bound_rows),
            )
        elif bound_rows is not None and "max_rows" not in op.params:
            raise _reject(name, "unknown parameter max_rows")
        args = semantic_ops.bind(op, positional, named)
        for param in op.input_params:
            if param in input_sql:
                args[param] = None
                continue
            value = args.get(param)
            if value is None:
                if param in op.required:
                    raise _reject(name, f"missing parameter {param}")
                continue
            if not isinstance(value, str) or not HANDLE.fullmatch(value):
                raise _reject(name, f"{param} must be a result set handle")
        if self.tokens is None:
            self.tokens = _byte_tokens(self.statement)
        start = call["location"]
        end = _call_end(self.tokens, start)
        return Call(
            name=name,
            args=args,
            start=start,
            end=end,
            alias=alias,
            input_sql=input_sql,
            top_level=top_alone,
        )

    def max_rows(self, name, value):
        if value is None:
            raise _reject(name, "input_sql without max_rows")
        if isinstance(value, bool) or not isinstance(value, int):
            raise _reject(name, "max_rows must be an integer")
        if not 1 <= value <= MAX_INPUT_ROWS:
            raise _reject(name, f"max_rows must be between 1 and {MAX_INPUT_ROWS}")
        return value

    def nested(self, name, statement):
        from .query import validate_sql

        if not isinstance(statement, str):
            raise _reject(name, "input_sql must be text")
        try:
            inner = find_calls(statement)
        except ValueError:
            inner = None
        if inner:
            raise _reject(name, "nested semantic call in input_sql")
        try:
            return validate_sql(statement)
        except ValueError as exc:
            raise _reject(name, f"invalid input_sql ({exc})") from None

    def expr(self, node, construct, recursive):
        if isinstance(node, list):
            for value in node:
                self.expr(value, construct, recursive)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == "SelectStmt":
                self.select(value, False, recursive)
                continue
            if key == "FuncCall":
                name = self._semantic(value)
                if name is not None:
                    raise _reject(name, construct or "expression")
                inner = None
                if value.get("over"):
                    inner = "window"
                elif (_func_name(value) or "") in AGGREGATES or any(
                    value.get(flag)
                    for flag in ("agg_star", "agg_distinct", "agg_order", "agg_filter")
                ):
                    inner = "aggregate argument"
                if inner is not None:
                    self.expr(value, inner, recursive)
                    continue
            if key == "RangeFunction":
                self.range_function(value, False, recursive, False)
                continue
            if isinstance(value, (dict, list)):
                self.expr(value, construct, recursive)


def find_calls(statement, ops=None):
    """Semantic calls of a statement, in text order, checked for placement.

    Each violation raises ValueError('semantic function api.<name> is not
    supported here: <construct>'). Statements without semantic calls return
    [] (and are otherwise left to query.validate_sql).
    """
    ops = semantic_ops.discover() if ops is None else ops
    if not ops or not isinstance(statement, str):
        return []
    lowered = statement.lower()
    if not any(name in lowered for name in ops):
        return []
    tree = _tree(statement)
    calls = _Finder(statement, ops).find(tree)
    if len(calls) > max_calls():
        raise _reject(
            calls[max_calls()].name,
            f"more than {max_calls()} semantic calls in one statement",
        )
    for call in calls:
        if ops[call.name].top_level_only and len(calls) > 1:
            raise _reject(call.name, "only allowed alone in the top-level FROM")
    return sorted(calls, key=lambda c: c.start)


def splice(statement, replacements):
    """Replace each call's byte span by api.result_set('<handle>')."""
    raw = statement.encode("utf-8")
    pieces, last = [], 0
    for call, handle in sorted(replacements, key=lambda pair: pair[0].start):
        if not HANDLE.fullmatch(handle):
            raise ValueError("invalid result set handle")
        pieces.append(raw[last : call.start])
        pieces.append(f"api.result_set('{handle}')".encode())
        last = call.end
    pieces.append(raw[last:])
    return b"".join(pieces).decode("utf-8")


def placeholder(index):
    return "rs_" + format(index, "032x")


# --- taint analysis (QRY-38) ---------------------------------------------------------


def _signature_args(signature):
    """Argument names of 'name(a type, b type DEFAULT x) RETURNS ...'."""
    match = re.match(r"^[^(]*\((.*)\)\s*RETURNS", signature or "", re.S)
    if not match:
        return []
    names, depth, current = [], 0, ""
    for ch in match.group(1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            names.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        names.append(current)
    result = []
    for part in names:
        words = part.strip().split()
        if words and words[0].upper() in ("IN", "INOUT", "VARIADIC"):
            words = words[1:]
        result.append(words[0].strip('"') if words else "")
    return result


def ranked_functions():
    """{name: (1-based limit argument, its name)} from tl.api_functions."""
    now = time.monotonic()
    with _ranked_lock:
        if (
            _ranked_cache["value"] is not None
            and now - _ranked_cache["at"] < RANKED_TTL_S
        ):
            return _ranked_cache["value"]
    try:
        from .db import connect

        with connect("app") as conn:
            rows = conn.execute(
                "SELECT name,ranked_limit_arg,signature FROM tl.api_functions WHERE ranked_limit_arg IS NOT NULL AND kind='sql'"
            ).fetchall()
        value = {}
        for row in rows:
            position = int(row["ranked_limit_arg"])
            names = _signature_args(row["signature"])
            argname = names[position - 1] if 0 < position <= len(names) else None
            value[row["name"]] = (position, argname)
        if not value:
            value = dict(_DEFAULT_RANKED)
    except Exception as exc:
        log.warning("ranked function registry not read: %s", type(exc).__name__)
        value = dict(_DEFAULT_RANKED)
    with _ranked_lock:
        _ranked_cache.update(at=now, value=value)
    return value


def _is_hit_function(name):
    return (
        name == "search"
        or name.startswith("search_")
        or name.startswith("vector_")
        or name in ("search_passages", "entity_search")
    )


@dataclass
class _Rel:
    reason: str | None = None  # why the rows are not a population
    hits: bool = False  # rows are hits of a search, ranking or recall
    episodes: bool = False  # reads api.episodes_of


@dataclass
class Analysis:
    taints: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)  # aggregates over tainted rows
    grain: list = field(default_factory=list)  # hit counts at record grain
    handles: list = field(default_factory=list)  # handles read by api.result_set
    tainted: str | None = None  # the top level's own reason, if any

    @property
    def warnings(self):
        result = []
        if self.findings:
            result.append(AGGREGATE_WARNING)
        if self.grain:
            result.append(GRAIN_WARNING)
        return result


def _limit_is_null(call, position, argname):
    args = call.get("args", []) or []
    positional = [a for a in args if "NamedArgExpr" not in a]
    named = {
        a["NamedArgExpr"].get("name"): a["NamedArgExpr"]["arg"]
        for a in args
        if "NamedArgExpr" in a
    }
    node = None
    if argname and argname in named:
        node = named[argname]
    elif len(positional) >= position:
        node = positional[position - 1]
    if node is None:
        return False, "omitted"
    while "TypeCast" in node:
        node = node["TypeCast"]["arg"]
    if node.get("A_Const", {}).get("isnull"):
        return True, None
    return False, "not NULL"


def _clause_nodes(node):
    """FuncCalls and SubLink subselects of one clause, not inside subqueries."""
    calls, sublinks, columns = [], [], []

    def walk(value):
        if isinstance(value, list):
            for v in value:
                walk(v)
            return
        if not isinstance(value, dict):
            return
        for key, inner in value.items():
            if key == "SubLink":
                sub = inner.get("subselect", {})
                if "SelectStmt" in sub:
                    sublinks.append(sub["SelectStmt"])
                walk(inner.get("testexpr"))
                continue
            if key == "SelectStmt":
                sublinks.append(inner)
                continue
            if key == "FuncCall":
                calls.append(inner)
            if key == "ColumnRef":
                columns.append(_names(inner.get("fields")))
            walk(inner)

    walk(node)
    return calls, sublinks, columns


class _Analyzer:
    def __init__(self, handles, ranked):
        self.handles = handles or {}
        self.ranked = ranked
        self.result = Analysis()

    def level(self, node, env):
        env = dict(env)
        with_clause = node.get("withClause")
        if isinstance(with_clause, dict):
            for entry in with_clause.get("ctes", []):
                cte = entry.get("CommonTableExpr", {})
                query = cte.get("ctequery", {})
                rel = (
                    self.level(query["SelectStmt"], env)
                    if "SelectStmt" in query
                    else _Rel()
                )
                env[cte.get("ctename")] = rel
                if rel.reason:
                    self.result.taints[cte.get("ctename")] = rel.reason
        if node.get("op") not in (None, "SETOP_NONE"):
            parts = [
                self.level(node[side], env)
                for side in ("larg", "rarg")
                if node.get(side)
            ]
            rel = _Rel(
                reason=next((p.reason for p in parts if p.reason), None),
                hits=any(p.hits for p in parts),
                episodes=any(p.episodes for p in parts),
            )
        else:
            items = [self.from_item(item, env) for item in node.get("fromClause") or []]
            rel = _Rel(
                reason=next((r.reason for r in items if r.reason), None),
                hits=any(r.hits for r in items),
                episodes=any(r.episodes for r in items),
            )
            # A filter by a non-population (IN (SELECT ... top-k)) is one too.
            _, where_subs, _ = _clause_nodes(node.get("whereClause"))
            for sub in where_subs:
                inner = self.level(sub, env)
                if inner.reason and not rel.reason:
                    rel.reason = "filtered by " + inner.reason
            self.aggregates(node, rel, env)
        if node.get("limitCount") is not None or node.get("limitOption") in (
            "LIMIT_OPTION_WITH_TIES",
        ):
            rel = _Rel(
                reason=rel.reason or "LIMIT/FETCH level",
                hits=rel.hits,
                episodes=rel.episodes,
            )
        return rel

    def aggregates(self, node, rel, env):
        calls, subs, columns = [], [], []
        for key in ("targetList", "havingClause", "sortClause"):
            c, s, cols = _clause_nodes(node.get(key))
            calls += c
            subs += s
            columns += cols
        for sub in subs:
            self.level(sub, env)
        names = [(_func_name(c) or "", c) for c in calls]
        aggregated = (
            any(n in AGGREGATES for n, _ in names)
            or bool(node.get("distinctClause"))
            or bool(node.get("groupClause"))
        )
        if rel.reason and aggregated:
            self.result.findings.append(rel.reason)
        counts = [c for n, c in names if n == "count"]
        if rel.hits and counts:
            _, _, group_columns = _clause_nodes(node.get("groupClause"))
            episode_grain = rel.episodes and (
                any(cols and cols[-1] == "episode_id" for cols in group_columns)
                or any(
                    cols and cols[-1] == "episode_id"
                    for c in counts
                    for cols in _clause_nodes(c.get("args"))[2]
                )
            )
            if not episode_grain:
                self.result.grain.append("count over hits at record grain")

    def from_item(self, item, env):
        if "RangeVar" in item:
            value = item["RangeVar"]
            name = value.get("relname")
            if not value.get("schemaname") and name in env:
                rel = env[name]
                alias = (value.get("alias") or {}).get("aliasname")
                if alias and rel.reason:
                    self.result.taints[alias] = rel.reason
                return rel
            return _Rel()
        if "JoinExpr" in item:
            join = item["JoinExpr"]
            parts = [
                self.from_item(join[s], env) for s in ("larg", "rarg") if join.get(s)
            ]
            rel = _Rel(
                reason=next((p.reason for p in parts if p.reason), None),
                hits=any(p.hits for p in parts),
                episodes=any(p.episodes for p in parts),
            )
            # JOIN ... ON x IN (SELECT ... top-k) filters like WHERE does.
            _, on_subs, _ = _clause_nodes(join.get("quals"))
            for sub in on_subs:
                inner = self.level(sub, env)
                if inner.reason and not rel.reason:
                    rel.reason = "filtered by " + inner.reason
            alias = (join.get("alias") or {}).get("aliasname")
            if alias and rel.reason:
                self.result.taints[alias] = rel.reason
            return rel
        if "RangeTableSample" in item:
            value = item["RangeTableSample"]
            inner = self.from_item(value.get("relation") or {}, env)
            reason = inner.reason or "TABLESAMPLE"
            relation = (value.get("relation") or {}).get("RangeVar") or {}
            alias = (relation.get("alias") or {}).get("aliasname") or relation.get(
                "relname"
            )
            if alias:
                self.result.taints[alias] = reason
            return _Rel(reason=reason, hits=inner.hits, episodes=inner.episodes)
        if "RangeSubselect" in item:
            value = item["RangeSubselect"]
            sub = value.get("subquery", {})
            rel = self.level(sub["SelectStmt"], env) if "SelectStmt" in sub else _Rel()
            alias = (value.get("alias") or {}).get("aliasname")
            if alias and rel.reason:
                self.result.taints[alias] = rel.reason
            return rel
        if "RangeFunction" in item:
            value = item["RangeFunction"]
            alias = (value.get("alias") or {}).get("aliasname")
            rels, names = [], []
            for entry in value.get("functions", []) or []:
                items = entry.get("List", {}).get("items", [])
                call = (
                    items[0].get("FuncCall")
                    if items and "FuncCall" in items[0]
                    else None
                )
                if call is None:
                    continue
                rels.append(self.function(call))
                names.append(_func_name(call) or "function")
            rel = _Rel(
                reason=next((r.reason for r in rels if r.reason), None),
                hits=any(r.hits for r in rels),
                episodes=any(r.episodes for r in rels),
            )
            if rel.reason:
                self.result.taints[alias or (names[0] if names else "function")] = (
                    rel.reason
                )
            return rel
        return _Rel()

    def function(self, call):
        names = _names(call.get("funcname"))
        if len(names) != 2 or names[0] != "api":
            return _Rel()
        name = names[1]
        if name == "result_set":
            args = call.get("args", []) or []
            handle = None
            if args:
                node = args[0].get("NamedArgExpr", {}).get("arg", args[0])
                while "TypeCast" in node:
                    node = node["TypeCast"]["arg"]
                handle = node.get("A_Const", {}).get("sval", {}).get("sval")
            info = self.handles.get(handle) or {}
            if handle and handle not in self.result.handles:
                self.result.handles.append(handle)
            completeness = info.get("completeness")
            reason = None
            if completeness in ("ranked_sample", "truncated"):
                reason = f"result set {handle} is {completeness}"
            return _Rel(reason=reason, hits=info.get("op") in HIT_OPS)
        if name == "episodes_of":
            return _Rel(episodes=True)
        rel = _Rel(hits=_is_hit_function(name))
        if name in self.ranked:
            position, argname = self.ranked[name]
            is_null, how = _limit_is_null(call, position, argname)
            if not is_null:
                rel.reason = f"api.{name} is ranked and its limit is {how}"
        return rel


def handle_literals(tree):
    """Handles named by api.result_set('<handle>') anywhere in a tree."""
    found = []

    def walk(value):
        if isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            call = value.get("FuncCall")
            if call is not None and _names(call.get("funcname")) == [
                "api",
                "result_set",
            ]:
                for arg in call.get("args", []) or []:
                    node = arg.get("NamedArgExpr", {}).get("arg", arg)
                    while "TypeCast" in node:
                        node = node["TypeCast"]["arg"]
                    text = node.get("A_Const", {}).get("sval", {}).get("sval")
                    if text and HANDLE.fullmatch(text) and text not in found:
                        found.append(text)
            for inner in value.values():
                walk(inner)

    walk(tree)
    return found


def handle_info(scope, handles):
    """{handle: {completeness, op, member_count}} of the caller's readable sets."""
    if not handles:
        return {}
    from . import resultsets

    result = {}
    for handle in handles:
        if handle == PLACEHOLDER:
            continue
        try:
            info = resultsets.info(scope, handle)
        except Exception:
            continue
        result[handle] = dict(
            completeness=info["completeness"],
            op=(info.get("origin") or {}).get("op"),
            member_count=info["member_count"],
        )
    return result


def analyze(statement, *, scope=None, handles=None, ranked=None):
    """Taint, aggregate and grain analysis of one SELECT -> Analysis.

    handles maps a handle to {completeness, op}; when it is None and a scope
    is given, the caller's sets named in the statement are looked up.
    """
    tree = statement if isinstance(statement, dict) else _tree(statement)
    if handles is None:
        handles = handle_info(scope, handle_literals(tree)) if scope is not None else {}
    analyzer = _Analyzer(handles, ranked if ranked is not None else ranked_functions())
    for stmt in tree.get("stmts", []):
        node = stmt.get("stmt", {})
        if "SelectStmt" in node:
            rel = analyzer.level(node["SelectStmt"], {})
            analyzer.result.tainted = rel.reason
    analyzer.result.handles = handle_literals(tree)
    return analyzer.result


def taint(tree, handle_completeness):
    """{relation alias or CTE name: reason} for a parsed statement.

    handle_completeness maps a handle to its completeness (or to
    {completeness, op}).
    """
    handles = {
        h: (v if isinstance(v, dict) else dict(completeness=v))
        for h, v in (handle_completeness or {}).items()
    }
    return analyze(tree, handles=handles).taints


def apply(analysis, execution, *, strict_exact=False, handles=None):
    """Put an analysis into an Execution; strict_exact rejects aggregates."""
    if analysis.findings and strict_exact:
        raise ValueError(
            "strict_exact: "
            + AGGREGATE_WARNING
            + " ("
            + "; ".join(sorted(set(analysis.findings)))
            + ")"
        )
    for warning in analysis.warnings:
        execution.warn(warning)
    if analysis.grain:
        codes = list(execution.to_dict().get("warning_codes") or [])
        if GRAIN_CODE not in codes:
            codes.append(GRAIN_CODE)
        execution.set("warning_codes", codes)
    if analysis.taints:
        execution.set("taint", dict(analysis.taints))
    if analysis.handles:
        known = handles or {}
        execution.set(
            "handles",
            [
                dict(
                    handle=h,
                    completeness=(known.get(h) or {}).get("completeness"),
                    member_count=(known.get(h) or {}).get("member_count"),
                )
                for h in analysis.handles
            ],
        )


# --- input_sql sets --------------------------------------------------------------


def input_set(scope, statement, max_rows, *, deadline_ms=None, origin=None):
    """Run a validated SELECT and save its rows as a result set.

    The statement must return record_id, passage_id, message_id or id.
    Completeness: exhaustive only when it is untainted and max_rows did not
    cut it; ranked_sample when tainted; truncated when cut.
    Returns (handle, completeness, member_count).
    """
    from psycopg import sql

    from . import resultsets
    from .query import reader_transaction, validate_sql

    statement = validate_sql(statement)
    analysis = analyze(statement, scope=scope)
    with reader_transaction(scope, deadline_ms) as conn:
        cursor = conn.execute(
            sql.SQL("SELECT * FROM ({}) q LIMIT 0").format(sql.SQL(statement))
        )
        columns = [d.name for d in cursor.description or []]
        column = next(
            (
                c
                for c in ("record_id", "passage_id", "message_id", "id")
                if c in columns
            ),
            None,
        )
        if column is None:
            raise ValueError("input_sql must return a column id or message_id")
        rows = conn.execute(
            sql.SQL(
                "SELECT q.{} AS v FROM ({}) q WHERE q.{} IS NOT NULL LIMIT {}"
            ).format(
                sql.Identifier(column),
                sql.SQL(statement),
                sql.Identifier(column),
                sql.Literal(int(max_rows) + 1),
            )
        ).fetchall()
    cut = len(rows) > max_rows
    rows = rows[:max_rows]
    prefix = {"passage_id": "p", "message_id": "m", "id": "m"}.get(column)
    records = []
    for row in rows:
        value = row["v"]
        record_id = str(value) if prefix is None else f"{prefix}:{int(value)}"
        records.append(dict(record_id=record_id))
    if analysis.tainted:
        completeness = "ranked_sample"
    elif cut:
        completeness = "truncated"
    else:
        completeness = "exhaustive"
    handle = resultsets.create(
        scope,
        records,
        origin=dict(
            origin or {},
            op="input_sql",
            statement_sha256=_sha(statement)[7:],
            max_rows=int(max_rows),
        ),
        completeness=completeness,
        counts=dict(mechanically_processed=len(records)),
    )
    return handle, completeness, len(records)


# --- run ---------------------------------------------------------------------------


def _budget(budgets, deadline_ms, token_budget):
    from .budget import Budget

    if budgets is None:
        values = {}
    elif isinstance(budgets, Budget):
        values = budgets.to_dict()
    elif hasattr(budgets, "model_dump"):
        values = budgets.model_dump()
    else:
        values = dict(budgets)
    values = {k: v for k, v in values.items() if v is not None}
    if deadline_ms is not None:
        values["deadline_ms"] = (
            min(int(values["deadline_ms"]), int(deadline_ms))
            if values.get("deadline_ms") is not None
            else int(deadline_ms)
        )
    if token_budget is not None:
        values["tokens"] = (
            min(int(values["tokens"]), int(token_budget))
            if values.get("tokens") is not None
            else int(token_budget)
        )
    allowed = set(Budget.__dataclass_fields__)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError("unknown budget: " + ", ".join(sorted(unknown)))
    return Budget(**values)


def _failure(exc):
    text = str(exc) or type(exc).__name__
    return text if len(text) <= 200 else type(exc).__name__


def run(
    statement,
    cap=1000,
    *,
    scope,
    params=None,
    token_row=None,
    budgets=None,
    deadline_ms=None,
    token_budget=None,
    dry_run=False,
    approval_id=None,
    strict_exact=False,
    trace=None,
    execution=None,
    materialize=True,
    calls=None,
    columns=None,
    resolve_only=False,
):
    """Run a statement with semantic calls -> {rows, truncated, row_count, execution}."""
    from . import cost, resultsets
    from .budget import BudgetTracker, clamp
    from .cost import ApprovalRequired
    from .execution import Execution, Trace
    from .query import query_ex, validate_sql
    from .scope import Scope

    if not isinstance(scope, Scope):
        raise TypeError("semantic.run requires a Scope")
    started = time.perf_counter()
    ops = semantic_ops.discover()
    calls = find_calls(statement, ops) if calls is None else calls
    if not calls:
        raise ValueError("statement has no semantic call")
    # (a) Validate with placeholders before any work.
    param_count = len(params or ())
    placeholders = [(call, placeholder(i)) for i, call in enumerate(calls)]
    validate_sql(
        splice(statement, [(c, PLACEHOLDER) for c in calls]),
        param_count=param_count,
    )
    if strict_exact:
        assumed = {
            handle: dict(
                completeness="exhaustive"
                if ops[call.name].exact or call.args.get("exhaustive")
                else "ranked_sample",
                op=call.name,
            )
            for call, handle in placeholders
        }
        apply(
            analyze(splice(statement, placeholders), handles=assumed),
            Execution(),
            strict_exact=True,
        )
    # (b) One budget, one tracker, one Execution and Trace.
    requested = _budget(budgets, deadline_ms, token_budget)
    budget = clamp(requested, token_row)
    tracker = BudgetTracker(budget)
    if execution is None:
        execution = Execution(tracker)
    elif getattr(execution, "tracker", None) is None:
        execution.tracker = tracker
    own_trace = None
    if trace is None or trace is True:
        own_trace = trace = Trace(
            scope,
            "semantic_sql",
            dict(statement_sha256=_sha(statement)[7:], calls=[c.name for c in calls]),
        )
    execution.trace_id = trace.id
    in_investigation = bool(getattr(scope, "investigation_id", None))

    def finish():
        if own_trace is not None and not own_trace.flush(execution):
            execution.trace_id = None
            execution.warn("trace was not written")
        execution.add_timing("semantic", (time.perf_counter() - started) * 1000)

    if materialize:
        from .derived import materialize_for_read, read_wait_s

        remaining = tracker.remaining_ms()
        wait = (
            read_wait_s() if remaining is None else min(read_wait_s(), remaining / 1000)
        )
        if not materialize_for_read(wait):
            execution.warn("deferred thread maintenance did not finish in time")

    contexts = []
    inputs = []
    plan = []
    try:
        for call, _ in placeholders:
            op = ops[call.name]
            args = dict(call.args)
            for param, (nested, max_rows) in call.input_sql.items():
                handle, completeness, count = input_set(
                    scope, nested, max_rows, deadline_ms=tracker.remaining_ms()
                )
                args[param] = handle
                inputs.append(["sql:" + _sha(nested)[7:], count])
                trace.event(
                    "semantic.input_sql",
                    nested,
                    rows=count,
                    detail=dict(handle=handle, completeness=completeness),
                )
                execution.stage(
                    "input_sql",
                    rows=count,
                    handle=handle,
                    completeness=completeness,
                    call=call.name,
                )
            for param in op.input_params:
                if param in call.input_sql or args.get(param) is None:
                    continue
                try:
                    info = resultsets.info(scope, args[param])
                except resultsets.ResultSetNotFound:
                    raise ValueError(
                        f"semantic function api.{call.name}: input result set not found"
                    ) from None
                except resultsets.ResultSetStale:
                    raise ValueError(
                        f"semantic function api.{call.name}: input result set is stale (access policy changed)"
                    ) from None
                inputs.append([args[param], info["member_count"]])
            ctx = semantic_ops.OpContext(
                scope=scope,
                token_row=token_row,
                tracker=tracker,
                execution=execution,
                trace=trace,
                in_investigation=in_investigation,
            )
            estimates = op.estimates(ctx, args)
            contexts.append((call, op, args, ctx, estimates))
            plan.append(
                dict(
                    call.describe(),
                    exact=op.exact,
                    cost_class=op.cost_class,
                    version=op.version,
                    estimate=cost.summarize(estimates),
                )
            )
    except Exception:
        finish()
        raise
    all_estimates = [e for *_, estimates in contexts for e in estimates]
    summary = cost.summarize(all_estimates)
    execution.set("plan", plan)
    execution.set("estimate", summary)
    empty = dict(rows=[], truncated=False, row_count=0)
    if dry_run:
        execution.set("dry_run", True)
        execution.stage("semantic", "ok", dry_run=True, calls=len(calls))
        finish()
        return dict(empty, execution=execution.to_dict())
    # (c) Cost gate: approval_required returns before any model call.
    digest = cost.request_hash(statement, scope, inputs, requested.to_dict())
    try:
        cost.authorize(
            token_row,
            all_estimates,
            scope=scope,
            tracker=tracker,
            caller_limit=requested.model_cost,
            approval_id=approval_id,
            request_hash=digest,
        )
    except ApprovalRequired as exc:
        if resolve_only:
            finish()
            raise ValueError(
                "approval required for semantic query; run via POST /v1/sql to obtain approval_id"
            ) from None
        execution.require_approval(exc.to_dict())
        execution.stage("cost_gate", "ok", approval_required=True)
        trace.event("cost_gate", None, "approval_required", detail=exc.to_dict())
        finish()
        return dict(empty, execution=execution.to_dict())
    # (d) Operators run concurrently under the tracker.
    handles = {}
    failures = []

    def one(call, op, args, ctx):
        begun = time.perf_counter()
        handle = op.run(ctx, args)
        if not isinstance(handle, str) or not HANDLE.fullmatch(handle):
            raise RuntimeError(f"api.{op.name} returned no result set")
        return handle, (time.perf_counter() - begun) * 1000

    pool = ThreadPoolExecutor(max_workers=len(contexts), thread_name_prefix="semantic")
    try:
        futures = [
            (call, op, pool.submit(one, call, op, args, ctx))
            for call, op, args, ctx, _ in contexts
        ]
        for call, op, future in futures:
            remaining = tracker.remaining_ms()
            try:
                handle, ms = future.result(
                    timeout=None if remaining is None else max(0.001, remaining / 1000)
                )
            except FutureTimeout:
                future.cancel()
                failures.append((call, op, "deadline exceeded"))
                continue
            except Exception as exc:
                failures.append((call, op, _failure(exc)))
                continue
            handles[id(call)] = handle
            execution.stage(
                "semantic:" + call.name,
                rows=None,
                ms=round(ms, 3),
                handle=handle,
                alias=call.alias,
                exact=op.exact,
            )
            trace.event(
                "semantic." + call.name,
                None,
                "ok",
                ms=ms,
                detail=dict(handle=handle, alias=call.alias),
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for call, op, reason in failures:
        trace.event("semantic." + call.name, None, "failed", detail=dict(reason=reason))
        if not op.degradable:
            execution.fail(reason, stage="semantic:" + call.name)
            finish()
            raise ValueError(f"semantic function api.{call.name} failed: {reason}")
        execution.stage("semantic:" + call.name, "failed", reason=reason)
        handles[id(call)] = resultsets.create(
            scope,
            [],
            origin=dict(op=call.name, failed=True),
            completeness="truncated",
        )
    # (e) Real handles, validated again, through the unchanged reader path.
    final = splice(statement, [(call, handles[id(call)]) for call in calls])
    try:
        final = validate_sql(final, param_count=param_count)
        info = handle_info(scope, [handles[id(c)] for c in calls])
        execution.set(
            "semantic_calls",
            [
                dict(
                    name=call.name,
                    alias=call.alias,
                    handle=handles[id(call)],
                    completeness=(info.get(handles[id(call)]) or {}).get(
                        "completeness"
                    ),
                    member_count=(info.get(handles[id(call)]) or {}).get(
                        "member_count"
                    ),
                )
                for call in calls
            ],
        )
        if resolve_only:
            finish()
            return dict(statement=final, execution=execution.to_dict())
        remaining = tracker.remaining_ms()
        result = query_ex(
            final,
            cap,
            scope=scope,
            params=params,
            deadline_ms=None if remaining is None else max(1, int(remaining)),
            token_budget=token_budget,
            execution=execution,
            materialize=False,
            strict_exact=strict_exact,
            columns=columns,
        )
        trace.event(
            "sql",
            final,
            "ok",
            rows=result["row_count"],
            detail=dict(truncated=result["truncated"]),
        )
    except Exception as exc:
        trace.event(
            "sql", final, "failed", detail=dict(code=getattr(exc, "sqlstate", None))
        )
        finish()
        raise
    finish()
    result["execution"] = execution.to_dict()
    return result
