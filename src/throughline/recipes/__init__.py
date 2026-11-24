"""Versioned, execution-tested recipes (INT-9, LIB-3).

A recipe is a Markdown file in this package: a JSON front matter block
between two lines of three dashes, then the body (adaptation notes and
troubleshooting). Front matter keys:

  name            lower-case identifier, equal to the file stem
  version         integer; bump it whenever anything in the file changes
  schema_version  the migration number the recipe was written against ("0045")
  status          tested | proposed | retired
  summary         one sentence
  needs           what must exist for the recipe to run, each one of
                  "function:<name>" (a catalogue row), "subject:<name>"
                  (api.describe(<name>) is not null), "view:<name>" (api view)
                  or "route:<METHOD> <path>"
  statements      one SQL statement or a short sequence (at most 8). A later
                  statement may name {{handle}}: the result set handle of the
                  previous statement's (first) semantic call, or {{<alias>}}
                  for a semantic call with that FROM alias
  expect          what the last statement returns on the test corpus:
                  {"min_rows": n} and/or {"columns": [...]}

sync(conn) stores each (name, version) once in tl.recipes and writes the
catalogue section recipe:<name> of every listed recipe. A file whose content
changed while its version did not raises RecipeError, so a stored version is
never silently rewritten. run(target, recipe) executes a recipe through the
reader path (a Scope) or over HTTP (a client) and reports passed, failed or
skipped (a need is absent: skipped, never failed).
"""

import hashlib
import json
import re
from pathlib import Path

DIRECTORY = Path(__file__).parent
NAME = re.compile(r"^[a-z][a-z0-9_]*$")
STATUSES = ("tested", "proposed", "retired")
NEED = re.compile(
    r"^(function|subject|view):([a-z][a-z0-9_:]*)$|^route:([A-Z]+) (/\S+)$"
)
TEMPLATE = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
HANDLE = re.compile(r"rs_[0-9a-f]{32}")
MAX_STATEMENTS = 8
KEYS = {
    "name",
    "version",
    "schema_version",
    "status",
    "summary",
    "needs",
    "statements",
    "expect",
}


class RecipeError(ValueError):
    """A recipe file is malformed or changed without a version bump."""


# --- files ------------------------------------------------------------------


def parse(text, source="recipe"):
    """Parse one recipe file into a dict (front matter keys plus body)."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise RecipeError(f"{source}: front matter must start with ---")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RecipeError(f"{source}: front matter is not closed with ---")
    try:
        meta = json.loads(text[4:end])
    except ValueError as exc:
        raise RecipeError(f"{source}: front matter is not JSON ({exc})") from None
    if not isinstance(meta, dict):
        raise RecipeError(f"{source}: front matter must be an object")
    unknown = set(meta) - KEYS
    missing = {
        "name",
        "version",
        "schema_version",
        "status",
        "summary",
        "statements",
    } - set(meta)
    if unknown or missing:
        raise RecipeError(
            f"{source}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}"
        )
    name, version = meta["name"], meta["version"]
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise RecipeError(f"{source}: name must be a lower-case identifier")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RecipeError(f"{source}: version must be a positive integer")
    if not isinstance(meta["schema_version"], str) or not re.fullmatch(
        r"[0-9]{4}", meta["schema_version"]
    ):
        raise RecipeError(f"{source}: schema_version must be a migration number")
    if meta["status"] not in STATUSES:
        raise RecipeError(f"{source}: status must be one of {STATUSES}")
    if not isinstance(meta["summary"], str) or not meta["summary"].strip():
        raise RecipeError(f"{source}: summary required")
    statements = meta["statements"]
    if isinstance(statements, str):
        statements = [statements]
    if (
        not isinstance(statements, list)
        or not 1 <= len(statements) <= MAX_STATEMENTS
        or not all(isinstance(s, str) and s.strip() for s in statements)
    ):
        raise RecipeError(f"{source}: statements must be 1..{MAX_STATEMENTS} strings")
    needs = meta.get("needs", [])
    if not isinstance(needs, list) or not all(
        isinstance(n, str) and NEED.fullmatch(n) for n in needs
    ):
        raise RecipeError(f"{source}: needs must be a list of need strings")
    expect = meta.get("expect", {})
    if not isinstance(expect, dict) or set(expect) - {"min_rows", "columns"}:
        raise RecipeError(f"{source}: expect may hold min_rows and columns only")
    if "min_rows" in expect and (
        not isinstance(expect["min_rows"], int) or expect["min_rows"] < 0
    ):
        raise RecipeError(f"{source}: expect.min_rows must be a non-negative integer")
    if "columns" in expect and (
        not isinstance(expect["columns"], list)
        or not all(isinstance(c, str) for c in expect["columns"])
    ):
        raise RecipeError(f"{source}: expect.columns must be a list of names")
    body = text[end + 5 :].strip() + "\n"
    recipe = dict(
        name=name,
        version=version,
        schema_version=meta["schema_version"],
        status=meta["status"],
        summary=meta["summary"].strip(),
        needs=list(needs),
        statements=[s.strip() for s in statements],
        expect=dict(expect),
        body=body,
    )
    recipe["content_hash"] = content_hash(recipe)
    return recipe


def content_hash(recipe):
    """Hash of everything stored for one version (not of the file bytes)."""
    keys = (
        "name",
        "version",
        "schema_version",
        "status",
        "summary",
        "needs",
        "statements",
        "expect",
        "body",
    )
    canonical = json.dumps(
        {k: recipe[k] for k in keys}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_files(directory=None):
    """{name: recipe} of every *.md file of a directory (default: this package)."""
    directory = Path(directory) if directory is not None else DIRECTORY
    found = {}
    for path in sorted(directory.glob("*.md")):
        recipe = parse(path.read_text(encoding="utf-8"), path.name)
        if recipe["name"] != path.stem:
            raise RecipeError(f"{path.name}: name must equal the file name")
        found[recipe["name"]] = recipe
    return found


# --- database ---------------------------------------------------------------


def _section(recipe):
    return dict(
        description=recipe["summary"],
        name=recipe["name"],
        version=recipe["version"],
        schema_version=recipe["schema_version"],
        status=recipe["status"],
        needs=recipe["needs"],
        statements=recipe["statements"],
        expect=recipe["expect"],
        body=recipe["body"],
    )


def sync(conn, directory=None):
    """Store new recipe versions and refresh the recipe:<name> sections.

    Runs on the owner connection inside the caller's transaction. Does nothing
    before migration 0045. Raises RecipeError when a stored (name, version)
    differs from its file. Returns {inserted: [...], listed: [...]}.
    """
    if not conn.execute(
        "SELECT to_regclass('tl.recipes') IS NOT NULL AS ok"
    ).fetchone()["ok"]:
        return dict(inserted=[], listed=[])
    files = load_files(directory)
    inserted = []
    for recipe in files.values():
        newer = conn.execute(
            "SELECT max(version) AS v FROM tl.recipes WHERE name=%s",
            (recipe["name"],),
        ).fetchone()["v"]
        if newer is not None and newer > recipe["version"]:
            raise RecipeError(
                f"recipe {recipe['name']} version {recipe['version']} is older than stored version {newer}"
            )
        stored = conn.execute(
            "SELECT content_hash FROM tl.recipes WHERE name=%s AND version=%s",
            (recipe["name"], recipe["version"]),
        ).fetchone()
        if stored:
            if stored["content_hash"] != recipe["content_hash"]:
                raise RecipeError(
                    f"recipe {recipe['name']} changed without a version bump "
                    f"(version {recipe['version']} is already stored)"
                )
            continue
        conn.execute(
            "INSERT INTO tl.recipes(name,version,schema_version,status,summary,body,statements,expect,needs,content_hash)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)",
            (
                recipe["name"],
                recipe["version"],
                recipe["schema_version"],
                recipe["status"],
                recipe["summary"],
                recipe["body"],
                json.dumps(recipe["statements"]),
                json.dumps(recipe["expect"]),
                json.dumps(recipe["needs"]),
                recipe["content_hash"],
            ),
        )
        inserted.append((recipe["name"], recipe["version"]))
    # One catalogue section per listed (tested, current) recipe; sections of
    # recipes no longer listed go away, so describe never shows a proposal.
    listed = conn.execute(
        "SELECT name,version,schema_version,status,summary,needs,statements,expect,body FROM api.recipes"
    ).fetchall()
    names = [r["name"] for r in listed]
    conn.execute(
        "DELETE FROM tl.catalog_sections WHERE subject LIKE 'recipe:%%' AND NOT (substr(subject,8) = ANY(%s))",
        (names,),
    )
    for row in listed:
        body = json.dumps(_section(row), ensure_ascii=False)
        conn.execute(
            "INSERT INTO tl.catalog_sections(subject,body,version) VALUES (%s,%s::jsonb,%s)"
            " ON CONFLICT (subject) DO UPDATE SET body=EXCLUDED.body,version=EXCLUDED.version"
            " WHERE tl.catalog_sections.body IS DISTINCT FROM EXCLUDED.body OR tl.catalog_sections.version IS DISTINCT FROM EXCLUDED.version",
            ("recipe:" + row["name"], body, row["version"]),
        )
    return dict(inserted=inserted, listed=names)


# --- execution --------------------------------------------------------------


class _ScopeTarget:
    """Runs statements through the reader path under a Scope."""

    def __init__(self, scope, token_row=None):
        self.scope = scope
        self.token_row = token_row

    def sql(self, statement):
        from ..query import query_ex

        return query_ex(statement, 1000, scope=self.scope, token_row=self.token_row)

    def describe(self, subject):
        from psycopg import sql

        statement = (
            sql.SQL("SELECT api.describe({}) AS d")
            .format(sql.Literal(subject))
            .as_string()
        )
        return self.sql(statement)["rows"][0]["d"]

    def routes(self):
        from ..api import create_app

        return _app_routes(create_app())


class _HttpTarget:
    """Runs statements over HTTP: a client with .request(method, url, ...)
    (httpx or a FastAPI TestClient), or a callable request(method, path,
    data=None, params=None) returning parsed JSON (throughline.cli.request)."""

    def __init__(self, target):
        self.target = target

    def _call(self, method, path, data=None, params=None):
        if callable(getattr(self.target, "request", None)):
            response = self.target.request(
                method, "/v1/" + path, json=data, params=params
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
            return response.json()
        return self.target(method, path, data, params)

    def sql(self, statement):
        return self._call("POST", "sql", {"statement": statement})

    def describe(self, subject):
        return self._call("GET", "describe", params={"subject": subject})

    def routes(self):
        app = getattr(self.target, "app", None)
        if app is not None and hasattr(app, "routes"):
            return _app_routes(app)
        document = self._call("GET", "openapi.json")
        return {
            (method.upper(), path)
            for path, operations in document.get("paths", {}).items()
            for method in operations
        }


def _app_routes(app):
    def flattened(routes):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from flattened(inner.routes)
            else:
                yield route

    return {
        (method, route.path)
        for route in flattened(app.routes)
        for method in (getattr(route, "methods", None) or ())
    }


def _target(target):
    from ..scope import Scope

    if isinstance(target, Scope):
        return _ScopeTarget(target)
    if isinstance(target, (_ScopeTarget, _HttpTarget)):
        return target
    return _HttpTarget(target)


def missing_needs(target, recipe):
    """The needs of a recipe that the target does not offer."""
    target = _target(target)
    missing, routes = [], None
    for need in recipe.get("needs") or []:
        kind, _, value = need.partition(":")
        try:
            if kind == "function":
                present = target.describe("function:" + value) is not None
            elif kind == "subject":
                present = target.describe(value) is not None
            elif kind == "view":
                target.sql(f"SELECT 1 AS present FROM api.{value} LIMIT 0")
                present = True
            elif kind == "route":
                if routes is None:
                    routes = target.routes()
                method, _, path = value.partition(" ")
                present = (method, path) in routes
            else:
                present = False
        except Exception:
            present = False
        if not present:
            missing.append(need)
    return missing


def render(statement, handles):
    """Replace {{name}} by a saved handle; an unknown name or a value that is
    not a result set handle raises (nothing else is ever spliced into SQL)."""

    def replace(match):
        key = match.group(1)
        if key not in handles:
            raise RecipeError(f"no handle named {key} from an earlier statement")
        if not isinstance(handles[key], str) or not HANDLE.fullmatch(handles[key]):
            raise RecipeError(f"{key} is not a result set handle")
        return handles[key]

    return TEMPLATE.sub(replace, statement)


def _check(expect, result):
    rows = result.get("rows") or []
    problems = []
    if "min_rows" in expect and len(rows) < expect["min_rows"]:
        problems.append(f"{len(rows)} rows, expected at least {expect['min_rows']}")
    if expect.get("columns"):
        if not rows:
            problems.append("no rows to check columns against")
        else:
            absent = [c for c in expect["columns"] if c not in rows[0]]
            if absent:
                problems.append(f"missing columns {absent}")
    return problems


def run(target, recipe):
    """Execute one recipe; returns a report, never raises for a failed step.

    target: a Scope (reader path), a client with .request (HTTP) or a
    request(method, path, data, params) callable. recipe: a name (read from
    this package's files) or a parsed recipe dict. Status 'skipped' when a
    need is absent, 'failed' when a statement errs or the expectation is not
    met, else 'passed'.
    """
    if isinstance(recipe, str):
        files = load_files()
        if recipe not in files:
            raise RecipeError(f"no recipe named {recipe}")
        recipe = files[recipe]
    target = _target(target)
    report = dict(
        name=recipe["name"],
        version=recipe["version"],
        status="passed",
        missing=[],
        steps=[],
        problems=[],
    )
    missing = missing_needs(target, recipe)
    if missing:
        report.update(status="skipped", missing=missing)
        return report
    handles, result = {}, None
    for index, statement in enumerate(recipe["statements"]):
        try:
            text = render(statement, handles)
            result = target.sql(text)
        except Exception as exc:
            report["status"] = "failed"
            report["problems"].append(
                f"statement {index + 1}: {type(exc).__name__}: {str(exc)[:300]}"
            )
            return report
        execution = result.get("execution") or {}
        calls = execution.get("semantic_calls") or []
        step_handles = {}
        for call in calls:
            handle = call.get("handle")
            if not handle:
                continue
            step_handles.setdefault("handle", handle)
            if call.get("alias"):
                step_handles.setdefault(call["alias"], handle)
        handles.update(step_handles)
        rows = result.get("rows") or []
        report["steps"].append(
            dict(
                statement=index + 1,
                row_count=len(rows),
                columns=list(rows[0]) if rows else [],
                handles=sorted(step_handles),
                completeness=execution.get("completeness"),
                status=execution.get("status"),
            )
        )
    problems = _check(recipe.get("expect") or {}, result or {})
    if problems:
        report.update(status="failed", problems=problems)
    return report
