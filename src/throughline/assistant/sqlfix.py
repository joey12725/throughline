"""Deterministic SQL help: the Fix rule table, a structural Explain, and the
small parse utilities both use. No model is involved.

Fix is keyed on what the validator and the database report: the validator's
fixed messages (query.validate_sql) and SQLSTATE codes. A rule proposes a
corrected statement when it can build one mechanically (an unknown column
becomes the closest catalogue column, an unknown function the nearest
allow-listed builtin, a missing GROUP BY entry is added), and always explains
what was wrong in one or two sentences.
"""

import re

from pglast import ast, parse_sql
from pglast.stream import RawStream
from pglast.visitors import Visitor

from .. import query

AGGREGATES = {
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "jsonb_agg",
    "json_agg",
    "array_agg",
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
}
WINDOW_ONLY = {
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
}
READ_ONLY = (
    "The query surface is read-only: one SELECT over views and allow-listed "
    "functions in schema api. INSERT, UPDATE, DELETE, DDL, SELECT INTO and "
    "locking clauses are refused; records are written through the write routes, "
    "never through SQL."
)


def distance(a, b):
    """Levenshtein edit distance."""
    a, b = a.lower(), b.lower()
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def closest(name, candidates, limit=3):
    """Up to `limit` candidates nearest to name (ties: shorter first), only
    those within a distance a typo could explain."""
    name = name or ""
    bound = max(2, len(name) // 3)
    scored = []
    for candidate in set(candidates):
        d = distance(name, candidate)
        if (
            candidate.lower().startswith(name.lower())
            or name.lower() in candidate.lower()
        ):
            d = min(d, 1)
        if d <= bound:
            scored.append((d, len(candidate), candidate))
    return [c for _, _, c in sorted(scored)[:limit]]


def parse(statement):
    try:
        found = parse_sql(statement)
    except Exception:
        return None
    return found if len(found) == 1 else found


def deparse(node):
    try:
        return RawStream()(node)
    except Exception:
        return None


def _name(funcname):
    return ".".join(getattr(part, "sval", "") for part in funcname or ())


class _Collect(Visitor):
    def __init__(self):
        self.relations = []
        self.functions = []
        self.columns = []
        self.ctes = set()
        self.output = set()
        self.aliases = {}

    def visit_CommonTableExpr(self, ancestors, node):
        self.ctes.add(node.ctename)

    def visit_RangeVar(self, ancestors, node):
        alias = node.alias.aliasname if node.alias else None
        self.relations.append((node.schemaname, node.relname, alias))
        if alias:
            self.aliases[alias] = (node.schemaname, node.relname)

    def visit_RangeSubselect(self, ancestors, node):
        if node.alias:
            self.aliases[node.alias.aliasname] = (None, None)

    def visit_RangeFunction(self, ancestors, node):
        if node.alias:
            self.aliases[node.alias.aliasname] = (None, None)

    def visit_FuncCall(self, ancestors, node):
        self.functions.append(
            dict(
                name=_name(node.funcname),
                parts=tuple(getattr(p, "sval", "") for p in node.funcname or ()),
                window=node.over is not None,
                star=bool(node.agg_star),
            )
        )

    def visit_ColumnRef(self, ancestors, node):
        names = [getattr(f, "sval", None) for f in node.fields or ()]
        if names and all(isinstance(n, str) for n in names):
            self.columns.append(tuple(names))

    def visit_ResTarget(self, ancestors, node):
        if node.name:
            self.output.add(node.name)


def collect(tree):
    found = _Collect()
    found(tree)
    return found


def allowed_function(parts):
    if len(parts) == 2 and parts[0] == "api":
        return parts[1] in query.FUNCTIONS or parts[1] in (
            "recall",
            "expand",
            "match",
            "librarian",
        )
    if len(parts) == 2 and parts[0] == "pg_catalog":
        return parts[1] in query.SQL_SYNTAX
    return len(parts) == 1 and parts[0] in query.BUILTINS


def _replace_word(statement, old, new):
    """Replace identifier `old` (not inside a string literal) by `new`."""
    pieces = re.split(r"('(?:[^']|'')*')", statement)
    pattern = re.compile(r'(?<![\w."])"?' + re.escape(old) + r'"?(?![\w"])')
    return "".join(
        piece if index % 2 else pattern.sub(new, piece)
        for index, piece in enumerate(pieces)
    )


QUOTED_NAME = re.compile(r'"([^"]{1,128})"')


def _detail_name(detail, kind):
    """The identifier a database message names: column "x", relation "y"."""
    if not detail:
        return None
    match = re.search(kind + r'\s+"([^"]+)"', detail) or re.search(
        kind + r"\s+([A-Za-z_][\w.]*)", detail
    )
    return match.group(1) if match else None


# Spellings from other SQL dialects: (allow-listed replacement taking the same
# arguments, or None with what to write instead).
SYNONYMS = {
    "ifnull": ("coalesce", None),
    "nvl": ("coalesce", None),
    "isnull": ("coalesce", None),
    "len": ("length", None),
    "char_len": ("char_length", None),
    "stddev": ("stddev_samp", None),
    "variance": ("var_samp", None),
    "instr": ("strpos", None),
    "group_concat": (None, "write string_agg(x, ', ')"),
    "listagg": (None, "write string_agg(x, ', ')"),
    "datediff": (None, "subtract the timestamps (b - a is an interval) or use extract(epoch FROM b - a)"),
    "date_format": (None, "write to_char(t, 'YYYY-MM-DD')"),
    "strftime": (None, "write to_char(t, 'YYYY-MM-DD')"),
    "to_date": (None, "cast instead: x::date"),
    "date": (None, "cast instead: x::date"),
    "datetime": (None, "cast instead: x::timestamptz"),
    "json_extract": (None, "use the operators: fields->>'name' (text) or fields->'name' (json)"),
    "trunc": (None, "use floor(x) or round(x, n)"),
    "string_split": (None, "use split_part(text, ',', n) or unnest of an array"),
}
for _prefix in ("regexp_match", "regexp_matches", "regexp_replace", "regexp_like", "regexp_substr"):
    SYNONYMS[_prefix] = (
        None,
        "regular expressions are not allowed (unbounded backtracking); use LIKE, ILIKE, strpos or split_part",
    )


def tree_of(statement):
    found = parse(statement)
    return found if found is not None and not isinstance(found, list) else None


def _median(statement):
    """median(x) rewritten as percentile_cont(0.5) WITHIN GROUP (ORDER BY x)."""
    tree = tree_of(statement)
    if tree is None:
        return None
    changed = []

    class Rewrite(Visitor):
        def visit_FuncCall(self, ancestors, node):
            if _name(node.funcname).lower() == "median" and len(node.args or ()) == 1:
                node.funcname = (ast.String(sval="percentile_cont"),)
                node.agg_order = (ast.SortBy(node=node.args[0], sortby_dir=0, sortby_nulls=0),)
                node.args = (ast.A_Const(val=ast.Float(fval="0.5")),)
                node.agg_within_group = True
                changed.append(1)

    Rewrite()(tree)
    return deparse(tree[0].stmt) if changed else None


class Fixer:
    """The rule table. catalog: an assistant.catalog.Catalog."""

    def __init__(self, catalog):
        self.catalog = catalog

    # -- helpers ---------------------------------------------------------------
    def relation_columns(self, found):
        """Columns of the api relations the statement reads."""
        columns = set()
        for schema, name, _ in found.relations:
            if schema == "api":
                columns.update(c for c, _ in self.catalog.columns(name))
        return columns

    def known_function_names(self):
        return set(query.BUILTINS) | {"api." + f for f in query.FUNCTIONS}

    # -- rules -----------------------------------------------------------------
    def fix(self, statement, message, code, detail=None):
        """[{rule, explanation, statement or None}], best first."""
        statement = (statement or "").strip()
        message = message or ""
        tree = parse(statement)
        single = tree if tree is not None and not isinstance(tree, list) else None
        found = collect(single) if single is not None else None
        out = []

        def add(rule, explanation, fixed=None):
            if fixed is not None and fixed.strip() == statement:
                fixed = None
            out.append(dict(rule=rule, explanation=explanation, statement=fixed))

        if message == "invalid SQL syntax" or code == "42601":
            self._syntax(statement, add)
        if message == "only one SELECT statement is allowed":
            if isinstance(tree, list) and tree:
                selects = [s for s in tree if isinstance(s.stmt, ast.SelectStmt)]
                if selects:
                    add(
                        "single_statement",
                        "Only one statement runs per request. The first SELECT is kept; run the others separately.",
                        deparse(selects[0].stmt),
                    )
                else:
                    add("read_only", READ_ONLY)
            else:
                add("read_only", READ_ONLY)
        if message in ("only SELECT is allowed", "SELECT cannot create or lock tables"):
            fixed = None
            if single is not None and isinstance(single[0].stmt, ast.SelectStmt):
                select = single[0].stmt
                select.intoClause = None
                select.lockingClause = None
                fixed = deparse(select)
            add("read_only", READ_ONLY, fixed)
        if message.startswith("relations must be explicitly in schema api") or code == "42P01":
            self._relations(statement, found, detail, add)
        if message.startswith("function is outside the query allow-list") or code == "42883":
            self._functions(statement, found, detail, add)
        if code == "42703" and found is not None:
            self._columns(statement, found, detail, add)
        if code == "42803" and single is not None:
            self._group_by(single, found, detail, add)
        if code == "42702" and found is not None:
            name = _detail_name(detail, "column reference") or ""
            aliases = sorted(a for a, (s, r) in found.aliases.items() if s == "api")
            hint = f" (for example {aliases[0]}.{name})" if aliases and name else ""
            add(
                "ambiguous_column",
                f"Column {name or 'reference'} exists in more than one relation of the FROM clause: qualify it with its alias{hint}.",
            )
        if code in ("42804", "22P02") or (
            code == "42883" and "operator" in (detail or "")
        ):
            add(
                "cast",
                "The types do not match. Cast explicitly: a declared field read from "
                "fields is text, so write (fields->>'name')::float8 (or ::bigint, "
                "::timestamptz), or use the typed column of the channel view "
                "api.ch_<channel>. A stored value that does not parse fails the cast; "
                "guard it with CASE WHEN jsonb_typeof(fields->'name') = 'number' THEN ... END.",
            )
        if code == "22012":
            add("division_by_zero", "A divisor is zero: write x / nullif(y, 0).")
        if code == "57014":
            add(
                "timeout",
                "The statement ran past its time limit. Bound it: a happened_at window "
                "(happened_at >= now() - interval '90 days'), the channel's own view "
                "instead of api.messages, an aggregate instead of raw rows, a LIMIT.",
            )
        if code == "08P01":
            add(
                "bind_parameters",
                "There are no bind parameters ($1). Write literals inline and double every ' inside a string literal.",
            )
        if message == "type is outside the query allow-list":
            add(
                "type",
                "Casts to regclass, regproc, regrole, regnamespace, xid and to schema-qualified types are refused; use plain types (text, bigint, float8, timestamptz, date, interval, jsonb).",
            )
        if message in (
            "composite attribute notation is not allowed",
            "attribute function notation is not allowed",
        ):
            add(
                "attribute_notation",
                "Name columns directly (alias.column). alias.name on a function result, "
                "(row).field and a.b.c are refused; to read a whole row as JSON write to_jsonb(alias).",
            )
        if message == "column definition lists are not allowed":
            add(
                "column_definition",
                "Remove the AS (column type, ...) list after the function call: api functions declare their own columns.",
            )
        if message == "qualified operators are not allowed":
            add(
                "operator",
                "Write operators plainly (a = b), not OPERATOR(pg_catalog.=).",
            )
        if message == "only function calls are allowed in FROM":
            add(
                "from_items",
                "FROM takes api views, api functions and set-returning builtins such as generate_series and unnest; nothing else.",
            )
        if not out:
            add(
                "explain",
                (
                    f"The database rejected the statement (SQLSTATE {code}). "
                    if code
                    else ""
                )
                + (
                    message + ". "
                    if message and message != "database operation rejected"
                    else ""
                )
                + "Check names against the catalogue (describe) and the types of the columns you compare.",
            )
        return out

    def _syntax(self, statement, add):
        fixed = statement
        fixed = (
            fixed.replace("‘", "'")
            .replace("’", "'")
            .replace("“", '"')
            .replace("”", '"')
        )
        fixed = re.sub(
            r",\s*(FROM|WHERE|GROUP|ORDER|LIMIT)\b", r" \1", fixed, flags=re.IGNORECASE
        )
        fixed = re.sub(r";\s*$", "", fixed)
        # "text" used as a string literal after a comparison.
        fixed = re.sub(
            r'(=|<>|!=|LIKE|ILIKE)\s*"([^"]*)"', r"\1 '\2'", fixed, flags=re.IGNORECASE
        )
        if fixed.count("(") != fixed.count(")"):
            add(
                "parentheses",
                f"The parentheses do not balance ({fixed.count('(')} open, {fixed.count(')')} close).",
            )
        if fixed.count("'") % 2:
            add(
                "quotes",
                "A string literal is not closed: every ' needs its partner, and a ' inside a literal is written ''.",
            )
        if fixed != statement and parse(fixed) is not None:
            add(
                "syntax",
                "Smart quotes, a trailing comma before a clause, or a double-quoted string used as a value were corrected.",
                fixed,
            )
        else:
            add(
                "syntax",
                "The statement does not parse as PostgreSQL SQL. Write one SELECT: SELECT ... FROM api.<view> WHERE ... GROUP BY ... ORDER BY ... LIMIT ...",
            )

    def _relations(self, statement, found, detail, add):
        relations = self.catalog.relations()
        names = set(relations)
        targets = []
        if found is not None:
            targets = [
                (schema, name)
                for schema, name, _ in found.relations
                if schema != "api" and name not in found.ctes
            ]
            targets += [
                ("api", name)
                for schema, name, _ in found.relations
                if schema == "api" and name not in names
            ]
        missing = _detail_name(detail, "relation")
        if missing:
            targets.append(
                tuple(missing.split(".", 1)) if "." in missing else (None, missing)
            )
        fixed = statement
        explained = []
        for schema, name in targets:
            if not name:
                continue
            candidates = []
            if name in names:
                candidates = [name]
            else:
                channel_view = "ch_" + name.replace(".", "_")
                if channel_view in names:
                    candidates = [channel_view]
                else:
                    candidates = closest(name, names)
            if candidates:
                qualified = (schema + "." if schema else "") + name
                fixed = _replace_word(fixed, qualified, "api." + candidates[0])
                explained.append(f"{qualified} -> api.{candidates[0]}")
        add(
            "schema_api",
            "Relations must be written schema-qualified in schema api (api.messages, api.ch_<channel>)."
            + (" Closest names: " + ", ".join(explained) + "." if explained else ""),
            fixed if explained else None,
        )

    def _functions(self, statement, found, detail, add):
        offenders = []
        if found is not None:
            offenders = [f for f in found.functions if not allowed_function(f["parts"])]
        missing = None
        if detail and detail.startswith("function "):
            match = re.match(r"function ([\w.]+)\(", detail)
            missing = match.group(1) if match else None
        names = {f["name"] for f in offenders}
        if missing:
            names.add(missing)
        if not names:
            return
        fixed = statement
        explained = []
        allowed = self.known_function_names()
        if "median" in names and tree_of(fixed) is not None:
            rewritten = _median(fixed)
            if rewritten:
                fixed = rewritten
                names.discard("median")
                explained.append(
                    "median(x) -> percentile_cont(0.5) WITHIN GROUP (ORDER BY x)"
                )
        for name in sorted(names):
            bare = name.split(".")[-1]
            if bare.lower() in SYNONYMS:
                replacement, note = SYNONYMS[bare.lower()]
                if replacement:
                    fixed = re.sub(
                        r"(?<![\w.])" + re.escape(name) + r"\s*\(",
                        replacement + "(",
                        fixed,
                        flags=re.IGNORECASE,
                    )
                    explained.append(f"{name} -> {replacement}")
                else:
                    explained.append(f"{name}: {note}")
                continue
            if bare in query.FUNCTIONS and name != "api." + bare:
                replacement = "api." + bare
            elif name.startswith("pg_catalog.") and bare in query.BUILTINS:
                replacement = bare
            else:
                options = closest(bare, allowed) or closest(name, allowed)
                replacement = options[0] if options else None
            if replacement:
                fixed = re.sub(
                    r"(?<![\w.])" + re.escape(name) + r"\s*\(", replacement + "(", fixed
                )
                explained.append(f"{name} -> {replacement}")
            else:
                explained.append(f"{name} has no allow-listed equivalent")
        add(
            "allow_list",
            "Only allow-listed functions run: api.<name> functions of the catalogue and the plain builtins "
            "(count, avg, date_trunc, percentile_cont, lag, ...). "
            + "; ".join(explained)
            + ".",
            fixed if fixed != statement else None,
        )

    def _columns(self, statement, found, detail, add):
        available = self.relation_columns(found) | found.output
        missing = _detail_name(detail, "column")
        candidates = []
        if missing:
            candidates = [missing]
        else:
            candidates = [
                ref[-1]
                for ref in found.columns
                if ref[-1] not in available and ref[-1] not in found.aliases
            ]
        fixed = statement
        explained = []
        for name in dict.fromkeys(candidates):
            bare = name.split(".")[-1]
            options = closest(bare, available - {bare})
            if options:
                fixed = _replace_word(fixed, bare, options[0])
                explained.append(
                    f"{bare} -> {options[0]}"
                    + (f" (or {', '.join(options[1:])})" if options[1:] else "")
                )
            else:
                explained.append(f"{bare} is not a column of the relations read")
        if not explained:
            return
        add(
            "unknown_column",
            "A column name does not exist in the relations of the statement. "
            + "; ".join(explained)
            + ". Declared fields are typed columns of api.ch_<channel>, or fields->>'name' on api.messages.",
            fixed if fixed != statement else None,
        )

    def _group_by(self, tree, found, detail, add):
        select = tree[0].stmt
        if not isinstance(select, ast.SelectStmt):
            return
        missing = _detail_name(detail, "column")
        plain = []
        for target in select.targetList or ():
            value = target.val
            if isinstance(value, ast.ColumnRef):
                plain.append(value)
        group = list(select.groupClause or ())
        grouped = {deparse(g) for g in group}
        added = []
        for column in plain:
            text = deparse(column)
            if text in grouped:
                continue
            if (
                missing
                and text != missing
                and text.split(".")[-1] != missing.split(".")[-1]
            ):
                continue
            group.append(column)
            grouped.add(text)
            added.append(text)
        if not added:
            add(
                "group_by",
                "Every selected column that is not inside an aggregate must be listed in GROUP BY"
                + (f" ({missing})." if missing else "."),
            )
            return
        select.groupClause = tuple(group)
        add(
            "group_by",
            "Every selected column that is not inside an aggregate must be in GROUP BY: added "
            + ", ".join(added)
            + ".",
            deparse(select),
        )


# -- explain ----------------------------------------------------------------------


def _join_kind(join):
    return {0: "an inner join", 1: "a left join", 2: "a full join", 3: "a right join"}.get(
        int(join.jointype), "a join"
    )


def _position(node, targets):
    """`GROUP BY 1` / `ORDER BY 2` named by the output column they mean."""
    text = deparse(node) or "?"
    if text.isdigit() and 1 <= int(text) <= len(targets):
        target = targets[int(text) - 1]
        return f"`{target.name or deparse(target.val)}`"
    return f"`{text}`"


def explain(statement, catalog=None):
    """A plain-English, structural description of a statement (markdown)."""
    tree = parse(statement)
    if tree is None:
        return "This does not parse as SQL, so it cannot be explained. Use Fix first."
    if isinstance(tree, list):
        return f"This is {len(tree)} statements; only one SELECT runs per request."
    root = tree[0].stmt
    if not isinstance(root, ast.SelectStmt):
        return "This is not a SELECT. " + READ_ONLY
    lines = []
    found = collect(tree)
    if root.withClause:
        names = [c.ctename for c in root.withClause.ctes]
        lines.append(
            "Builds "
            + ", ".join(f"`{n}`" for n in names)
            + " first (WITH), then reads from them."
        )
    if root.op and int(root.op) != 0:
        lines.append(
            "Combines two SELECTs ("
            + str(root.op).split(".")[-1].replace("SETOP_", "").lower()
            + ")."
        )
        return "\n".join("- " + line for line in lines)
    sources, joins = [], []

    def walk_from(item):
        if isinstance(item, ast.RangeVar):
            name = (item.schemaname + "." if item.schemaname else "") + item.relname
            alias = item.alias.aliasname if item.alias else None
            sources.append(f"`{name}`" + (f" as `{alias}`" if alias else ""))
        elif isinstance(item, ast.JoinExpr):
            walk_from(item.larg)
            walk_from(item.rarg)
            joins.append(
                _join_kind(item)
                + (f" on `{deparse(item.quals)}`" if item.quals is not None else "")
            )
        elif isinstance(item, ast.RangeFunction):
            for pair in item.functions or ():
                call = pair[0] if isinstance(pair, (list, tuple)) else pair
                sources.append(f"`{deparse(call)}`")
        elif isinstance(item, ast.RangeSubselect):
            alias = item.alias.aliasname if item.alias else "a subquery"
            sources.append(f"a subquery `{alias}`")

    for item in root.fromClause or ():
        walk_from(item)
    if sources:
        lines.append("Reads " + ", ".join(sources) + ".")
    for join in joins:
        lines.append("Joins with " + join + ".")
    if root.whereClause is not None:
        lines.append(f"Keeps only rows where `{deparse(root.whereClause)}`.")
    outputs = []
    for target in root.targetList or ():
        text = deparse(target.val) or "?"
        outputs.append(f"`{target.name}` = `{text}`" if target.name else f"`{text}`")
    aggregates = sorted(
        {
            f["name"]
            for f in found.functions
            if f["name"] in AGGREGATES and not f["window"]
        }
    )
    windows = sorted({f["name"] for f in found.functions if f["window"]})
    if root.groupClause:
        groups = ", ".join(_position(g, root.targetList or ()) for g in root.groupClause)
        lines.append(
            f"Groups the rows by {groups}"
            + (
                f" and computes {', '.join(aggregates)} per group."
                if aggregates
                else "."
            )
        )
    elif aggregates:
        lines.append(f"Folds all rows into one with {', '.join(aggregates)}.")
    if windows:
        lines.append(
            f"Uses window functions ({', '.join(windows)}) that look at neighbouring rows of the result."
        )
    if root.havingClause is not None:
        lines.append(f"Keeps only groups where `{deparse(root.havingClause)}`.")
    if root.distinctClause:
        lines.append("Removes duplicate rows (DISTINCT).")
    if outputs:
        lines.append(
            "Returns "
            + ", ".join(outputs[:12])
            + (" and more columns." if len(outputs) > 12 else ".")
        )
    if root.sortClause:
        order = ", ".join(
            _position(s.node, root.targetList or ())
            + (" descending" if int(s.sortby_dir) == 2 else "")
            for s in root.sortClause
        )
        lines.append(f"Orders by {order}.")
    if root.limitCount is not None:
        lines.append(f"Returns at most {deparse(root.limitCount)} rows.")
    else:
        lines.append("Has no LIMIT: the request's row cap bounds the result.")
    semantic = sorted(
        {
            f["name"]
            for f in found.functions
            if f["name"] in ("api.recall", "api.expand", "api.match", "api.librarian")
        }
    )
    if semantic:
        lines.append(
            "Calls semantic functions ("
            + ", ".join(semantic)
            + "): these may spend model or embedding budget."
        )
    return "\n".join("- " + line for line in lines)
