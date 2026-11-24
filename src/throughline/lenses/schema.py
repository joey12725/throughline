"""Schema validation, typed column projection, JSON Schema generation, and TOML/JSON loading for lenses."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

LENS_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,43}$")
COLUMN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

COLUMN_TYPES = {
    "text": "text",
    "integer": "bigint",
    "number": "double precision",
    "boolean": "boolean",
    "timestamp": "timestamptz",
    "json": "jsonb",
}

GRAINS = ("message", "conversation", "thread", "day")
WORKERS = ("code", "jev", "llm", "agent", "librarian")
OUTPUTS = ("table", "pages")

# Columns always projected by api.lens_<name>; user columns cannot collide with these.
RESERVED_COLUMNS = frozenset(
    {
        "id",
        "table_name",
        "natural_key",
        "window_key",
        "version",
        "superseded_by",
        "row_status",
        "confidence",
        "method",
        "evidence",
        "no_source_reason",
        "job",
        "job_version",
        "owner_locked",
        "owner_correction_note",
        "source_message_ids",
        "generated",
        "unsupported",
        "data",
        "created_at",
        "updated_at",
    }
)


class LensSpecError(ValueError):
    """Raised when a lens definition or extracted row violates its schema."""


def validate_lens_name(name: Any) -> str:
    if not isinstance(name, str) or not LENS_NAME_RE.fullmatch(name.strip()):
        raise LensSpecError(
            "lens name must match ^[a-z][a-z0-9_]{0,43}$ (lowercase letters, digits, underscores)"
        )
    return name.strip()


def validate_columns(columns: Any) -> list[dict[str, Any]]:
    """Validate and normalize a table's column schema (accepts list of dicts or {name: type_or_spec})."""
    if isinstance(columns, dict):
        items = []
        for col_name, spec in columns.items():
            if isinstance(spec, str):
                items.append({"name": col_name, "type": spec})
            elif isinstance(spec, dict):
                items.append({"name": col_name, **spec})
            else:
                raise LensSpecError(f"column '{col_name}' spec must be a type string or object")
        columns = items
    if not isinstance(columns, list) or not columns:
        raise LensSpecError("schema/columns must be a non-empty list of column definitions")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in columns:
        if not isinstance(raw, dict):
            raise LensSpecError("each column definition must be an object")
        col_name = raw.get("name")
        if not isinstance(col_name, str) or not COLUMN_NAME_RE.fullmatch(col_name):
            raise LensSpecError(
                f"invalid column name {col_name!r}: must match ^[a-z][a-z0-9_]{{0,62}}$"
            )
        if col_name in RESERVED_COLUMNS:
            raise LensSpecError(f"column '{col_name}' uses a reserved name")
        if col_name in seen:
            raise LensSpecError(f"duplicate column name '{col_name}'")
        seen.add(col_name)
        col_type = raw.get("type")
        if col_type not in COLUMN_TYPES:
            raise LensSpecError(
                f"column '{col_name}' type must be one of {', '.join(sorted(COLUMN_TYPES))}"
            )
        entry: dict[str, Any] = {
            "name": col_name,
            "type": col_type,
            "nullable": bool(raw.get("nullable", True)),
            "description": str(raw.get("description") or "").strip(),
        }
        if raw.get("enum") is not None:
            enum = raw["enum"]
            if not isinstance(enum, list) or not enum:
                raise LensSpecError(f"column '{col_name}' enum must be a non-empty list")
            entry["enum"] = list(enum)
        normalized.append(entry)
    return normalized


ITEM_CONTEXT_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def validate_item_context(item_context: Any) -> dict[str, Any]:
    """Validate worker_config.item_context: {state_key: {sql, one?, cap?}}.

    Each entry is a read-only SELECT run once per work item; ``{{name}}``
    placeholders are bound (as quoted SQL literals) from the item's candidate
    fields, then the statement goes through the normal query guard."""
    if not isinstance(item_context, dict) or not item_context:
        raise LensSpecError("worker_config.item_context must be a non-empty object")
    for key, entry in item_context.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", key):
            raise LensSpecError(f"item_context key {key!r} must be an identifier")
        if key in ("policy", "candidate"):
            raise LensSpecError(f"item_context key {key!r} is reserved")
        if isinstance(entry, str):
            entry = {"sql": entry}
            item_context[key] = entry
        if not isinstance(entry, dict) or not isinstance(entry.get("sql"), str) or not entry["sql"].strip():
            raise LensSpecError(f"item_context.{key} needs a non-empty 'sql' string")
        unknown = set(entry) - {"sql", "one", "cap"}
        if unknown:
            raise LensSpecError(f"item_context.{key} has unknown options: {', '.join(sorted(unknown))}")
        cap = entry.get("cap", 20)
        if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 50:
            raise LensSpecError(f"item_context.{key}.cap must be an integer 1..50")
    return item_context


def validate_lens_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete lens definition (TBL-1: schema, prompt, optional query, schedule + runner fields)."""
    if not isinstance(spec, dict):
        raise LensSpecError("lens definition must be an object")
    name = validate_lens_name(spec.get("name"))
    output = str(spec.get("output") or "table").strip().lower()
    if output not in OUTPUTS:
        raise LensSpecError(f"output must be one of {', '.join(OUTPUTS)}")
    raw_columns = spec.get("columns") if spec.get("columns") is not None else spec.get("schema")
    if output == "pages":
        # The Librarian answers with page actions, not rows (docs/v11/pages.md).
        if raw_columns:
            raise LensSpecError("a lens with output 'pages' has no columns: its result is page actions")
        columns: list[dict[str, Any]] = []
    else:
        columns = validate_columns(raw_columns)
    col_names = {c["name"] for c in columns}

    prompt = str(spec.get("prompt") or "").strip()
    worker = str(spec.get("worker") or ("librarian" if output == "pages" else "llm")).strip().lower()
    if worker not in WORKERS:
        raise LensSpecError(f"worker must be one of {', '.join(WORKERS)}")
    if (worker == "librarian") != (output == "pages"):
        raise LensSpecError("the librarian worker writes pages: use worker 'librarian' exactly with output 'pages'")
    if worker in ("llm", "agent", "librarian") and not prompt:
        raise LensSpecError(f"worker '{worker}' requires a non-empty prompt")

    grain = str(spec.get("grain") or "message").strip().lower()
    if grain not in GRAINS:
        raise LensSpecError(f"grain must be one of {', '.join(GRAINS)}")

    channels = spec.get("channels") or []
    if isinstance(channels, str):
        channels = [channels]
    if not isinstance(channels, list) or not all(isinstance(c, str) and c.strip() for c in channels):
        raise LensSpecError("channels must be a list of channel names")
    channels = [c.strip() for c in channels]

    context_query = spec.get("context_query")
    if context_query is None:
        context_query = spec.get("query")
    if context_query is not None:
        context_query = str(context_query).strip() or None

    schedule = spec.get("schedule")
    if schedule is not None:
        schedule = str(schedule).strip() or None
    if schedule is not None:
        from .scheduler import parse_schedule  # late: scheduler imports this module

        parse_schedule(schedule)  # LensSpecError when it is not a cron expression

    depends_on = spec.get("depends_on") or []
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if not isinstance(depends_on, list) or not all(isinstance(d, str) and d.strip() for d in depends_on):
        raise LensSpecError("depends_on must be a list of lens names")
    depends_on = [validate_lens_name(d) for d in depends_on]
    if name in depends_on:
        raise LensSpecError("a lens cannot depend on itself")

    natural_key = spec.get("natural_key") or []
    if isinstance(natural_key, str):
        natural_key = [natural_key]
    if not isinstance(natural_key, list) or not all(isinstance(k, str) for k in natural_key):
        raise LensSpecError("natural_key must be a list of column names")
    bad_keys = [k for k in natural_key if k not in col_names]
    if bad_keys:
        raise LensSpecError(f"natural_key references unknown columns: {', '.join(bad_keys)}")

    worker_config = spec.get("worker_config") or {}
    if not isinstance(worker_config, dict):
        raise LensSpecError("worker_config must be an object")
    if "item_context" in worker_config:
        validate_item_context(worker_config["item_context"])
    if output == "pages":
        from ..pages.librarian import validate_config

        validate_config(worker_config)
        if natural_key:
            raise LensSpecError("a lens with output 'pages' has no natural_key")

    max_items = int(spec.get("max_items_per_run", 200))
    if max_items < 1:
        raise LensSpecError("max_items_per_run must be >= 1")

    max_cost = float(spec.get("max_cost_per_run", 0.50))
    if not math.isfinite(max_cost) or max_cost < 0:
        raise LensSpecError("max_cost_per_run must be a finite non-negative number")

    return {
        "name": name,
        "description": str(spec.get("description") or "").strip(),
        "output": output,
        "columns": columns,
        "prompt": prompt,
        "context_query": context_query,
        "channels": channels,
        "grain": grain,
        "worker": worker,
        "worker_config": worker_config,
        "schedule": schedule,
        "depends_on": depends_on,
        "natural_key": natural_key,
        "enabled": bool(spec.get("enabled", True)),
        "lazy_backfill": bool(spec.get("lazy_backfill", False)),
        "max_items_per_run": max_items,
        "max_cost_per_run": max_cost,
    }


def load_spec_file(path: str | Path) -> dict[str, Any]:
    """Load a single lens definition file (.toml or .json)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".toml":
        data = tomllib.loads(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise LensSpecError("definition file must contain a JSON object or TOML table")
    return validate_lens_spec(data)


def _coerce_value(col: dict[str, Any], value: Any) -> Any:
    name = col["name"]
    typ = col["type"]
    if value is None:
        if not col.get("nullable", True):
            raise LensSpecError(f"column '{name}' is not nullable")
        return None

    if typ == "text":
        if not isinstance(value, str):
            raise LensSpecError(f"column '{name}' expects text, got {type(value).__name__}")
        out: Any = value
    elif typ == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise LensSpecError(f"column '{name}' expects integer, got {type(value).__name__}")
        out = int(value)
    elif typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise LensSpecError(f"column '{name}' expects finite number, got {value!r}")
        out = float(value)
    elif typ == "boolean":
        if not isinstance(value, bool):
            raise LensSpecError(f"column '{name}' expects boolean, got {type(value).__name__}")
        out = value
    elif typ == "timestamp":
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            out = dt.isoformat()
        elif isinstance(value, date):
            out = datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()
        elif isinstance(value, str):
            s = value.strip()
            try:
                if len(s) == 10 and s[4] == "-" and s[7] == "-":
                    d = date.fromisoformat(s)
                    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                else:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise LensSpecError(f"column '{name}' expects ISO timestamp, got {value!r}") from exc
            out = dt.isoformat()
        else:
            raise LensSpecError(f"column '{name}' expects ISO timestamp, got {type(value).__name__}")
    elif typ == "json":
        try:
            json.dumps(value)
        except TypeError as exc:
            raise LensSpecError(f"column '{name}' is not JSON-serializable") from exc
        out = value
    else:
        raise LensSpecError(f"unsupported type {typ!r}")

    if "enum" in col and out not in col["enum"]:
        raise LensSpecError(f"column '{name}' value {out!r} not in allowed enum {col['enum']!r}")
    return out


def validate_row_data(columns: list[dict[str, Any]], raw: Any) -> dict[str, Any]:
    """Validate an extracted row's column payload strictly against the table schema (TBL-2)."""
    if not isinstance(raw, dict):
        raise LensSpecError("row data must be a JSON object")
    declared = {c["name"]: c for c in columns}
    unknown = set(raw) - set(declared)
    if unknown:
        raise LensSpecError(f"unknown columns in row: {', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    for col in columns:
        name = col["name"]
        value = raw.get(name)
        cleaned[name] = _coerce_value(col, value)
    return cleaned


def compute_natural_key(
    natural_key_cols: list[str],
    data: dict[str, Any],
    fallback_seed: str,
) -> str:
    """Stable natural key for upsert/dedup/supersession."""
    if natural_key_cols:
        parts = [data.get(col) for col in natural_key_cols]
        if any(p is not None and str(p).strip() != "" for p in parts):
            canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str).lower()
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return hashlib.sha256(fallback_seed.encode("utf-8")).hexdigest()[:32]


def json_schema_for_lens(table_def: dict[str, Any]) -> dict[str, Any]:
    """Generate a strict JSON Schema for structured LLM output (TBL-2)."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for col in table_def["columns"]:
        t = col["type"]
        if t == "text":
            base: dict[str, Any] = {"type": "string"}
        elif t == "integer":
            base = {"type": "integer"}
        elif t == "number":
            base = {"type": "number"}
        elif t == "boolean":
            base = {"type": "boolean"}
        elif t == "timestamp":
            base = {"type": "string", "description": "ISO-8601 timestamp with timezone"}
        else:
            base = {}
        if col.get("description"):
            base["description"] = col["description"]
        if col.get("enum"):
            base["enum"] = col["enum"]
        if col.get("nullable", True) and "type" in base:
            base = {**base, "type": [base["type"], "null"]}
        props[col["name"]] = base
        required.append(col["name"])

    row_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "data": {
                "type": "object",
                "additionalProperties": False,
                "properties": props,
                "required": required,
            },
            "source_message_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "IDs of the source messages supporting this row",
            },
            "no_source_reason": {
                "type": ["string", "null"],
                "description": "Required only when source_message_ids is empty",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "evidence": {
                "type": "object",
                "description": "Brief rationale or quote supporting this row",
            },
            "same_as_row_id": {
                "type": ["integer", "null"],
                "description": "row_id of an existing row of this lens (from context_query_results) that is the same real-world item; that row is updated instead of a new one being added",
            },
        },
        "required": ["data", "source_message_ids", "confidence"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rows": {
                "type": "array",
                "items": row_schema,
            }
        },
        "required": ["rows"],
    }
