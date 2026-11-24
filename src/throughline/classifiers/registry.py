"""The annotation attribute registry (migration 0068).

Every annotation attribute is a row of tl.annotation_attributes. The six core
attributes (effective_time, effective_place, location, ignore, media_noise,
speaker) keep their database value checks; any other attribute is checked
here against its value_schema before a write.

A value_schema maps field names to field specs:

    {"is_automated": {"type": "boolean", "required": true},
     "kind": {"type": "enum", "values": ["newsletter", "receipt", "alert"]},
     "score": {"type": "number", "min": 0, "max": 1}}

Field types: boolean, number, integer, string, enum (with values). Extra
fields not named in the schema are refused for registered attributes, so a
typo never silently stores an unread field.

recall_effect 'exclude' names a boolean `flag` field: a message whose
effective value has flag=true is kept out of default recall.
"""

import re

CORE = ("effective_time", "effective_place", "location", "ignore", "media_noise", "speaker")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
TYPES = ("boolean", "number", "integer", "string", "enum")
RECALL_EFFECTS = ("none", "exclude")


class AttributeError_(ValueError):
    """An attribute definition or value that does not fit the registry."""


def _owner_or_agent(token):
    kind = (token or {}).get("kind") if isinstance(token, dict) else None
    if kind not in ("owner", "agent"):
        raise PermissionError("only the owner or an agent registers attributes")
    return kind


def validate_schema(schema):
    if not isinstance(schema, dict) or not schema:
        raise AttributeError_("value_schema must be an object naming at least one field")
    clean = {}
    for field, spec in schema.items():
        if not isinstance(field, str) or not NAME_RE.fullmatch(field):
            raise AttributeError_(f"field name {field!r} must match ^[a-z][a-z0-9_]{{0,62}}$")
        if isinstance(spec, str):
            spec = {"type": spec}
        if not isinstance(spec, dict):
            raise AttributeError_(f"field {field}: spec must be an object or a type name")
        kind = spec.get("type")
        if kind not in TYPES:
            raise AttributeError_(f"field {field}: type must be one of {', '.join(TYPES)}")
        out = {"type": kind}
        if spec.get("required"):
            out["required"] = True
        if kind == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
                raise AttributeError_(f"field {field}: enum needs a non-empty list of string values")
            out["values"] = list(dict.fromkeys(values))
        for bound in ("min", "max"):
            if bound in spec and spec[bound] is not None:
                if kind not in ("number", "integer") or isinstance(spec[bound], bool) or not isinstance(spec[bound], (int, float)):
                    raise AttributeError_(f"field {field}: {bound} applies to number and integer fields")
                out[bound] = spec[bound]
        if isinstance(spec.get("description"), str) and spec["description"].strip():
            out["description"] = spec["description"].strip()[:300]
        clean[field] = out
    return clean


def check_value(schema, value, *, attribute="attribute"):
    """Raise AttributeError_ when value does not fit schema (a registered
    attribute); returns the value."""
    if not isinstance(value, dict):
        raise AttributeError_(f"{attribute} value must be an object")
    for field in value:
        if field not in schema:
            raise AttributeError_(f"{attribute} has no field {field!r} (fields: {', '.join(schema)})")
    for field, spec in schema.items():
        if field not in value or value[field] is None:
            if spec.get("required"):
                raise AttributeError_(f"{attribute}.{field} is required")
            continue
        v = value[field]
        kind = spec["type"]
        if kind == "boolean" and not isinstance(v, bool):
            raise AttributeError_(f"{attribute}.{field} must be true or false")
        if kind in ("number", "integer"):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise AttributeError_(f"{attribute}.{field} must be a number")
            if kind == "integer" and not float(v).is_integer():
                raise AttributeError_(f"{attribute}.{field} must be an integer")
            if "min" in spec and v < spec["min"]:
                raise AttributeError_(f"{attribute}.{field} must be at least {spec['min']}")
            if "max" in spec and v > spec["max"]:
                raise AttributeError_(f"{attribute}.{field} must be at most {spec['max']}")
        if kind == "string" and (not isinstance(v, str) or len(v) > 2000):
            raise AttributeError_(f"{attribute}.{field} must be text of at most 2000 characters")
        if kind == "enum" and v not in spec["values"]:
            raise AttributeError_(f"{attribute}.{field} must be one of {', '.join(spec['values'])}")
    return value


def get(conn, name):
    row = conn.execute("SELECT * FROM tl.annotation_attributes WHERE name=%s", (name,)).fetchone()
    return dict(row) if row else None


def require(conn, name):
    """The active registry row of `name`, or ValueError naming the choices."""
    row = get(conn, name) if isinstance(name, str) else None
    if not row or row["status"] != "active":
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM tl.annotation_attributes WHERE status='active' ORDER BY name")]
        raise ValueError("attribute must be one of " + ", ".join(names))
    return row


def check(conn, name, value):
    """Check a value for a write: registered attributes against their schema;
    core attributes are left to the database check (and its error text)."""
    row = require(conn, name)
    if row["origin"] != "core":
        check_value(row["value_schema"], value, attribute=name)
    return row


def list_attributes(conn, include_retired=False):
    return [
        dict(r)
        for r in conn.execute(
            """SELECT a.*,
             (SELECT count(*) FROM tl.annotations n WHERE n.attribute=a.name AND n.status='active') AS active_annotations,
             (SELECT count(*) FROM tl.classifiers c WHERE c.attribute=a.name) AS classifiers
            FROM tl.annotation_attributes a WHERE %s OR a.status='active'
            ORDER BY a.origin='core' DESC, a.name""",
            (include_retired,),
        )
    ]


def clean(spec):
    """A checked attribute definition (not stored): {name, description,
    value_schema, recall_effect, flag}."""
    if not isinstance(spec, dict):
        raise AttributeError_("an attribute definition is an object")
    name = spec.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name.strip()):
        raise AttributeError_("attribute name must match ^[a-z][a-z0-9_]{0,62}$")
    name = name.strip()
    if name in CORE:
        raise AttributeError_(f"{name} is a core attribute and cannot be redefined")
    schema = validate_schema(spec.get("value_schema") or spec.get("value"))
    effect = spec.get("recall_effect") or "none"
    if effect not in RECALL_EFFECTS:
        raise AttributeError_("recall_effect must be none or exclude")
    flag = spec.get("flag")
    if effect == "exclude":
        if flag is None:
            booleans = [f for f, s in schema.items() if s["type"] == "boolean"]
            if len(booleans) != 1:
                raise AttributeError_("recall_effect exclude needs flag: the boolean field that hides a message when true")
            flag = booleans[0]
        if schema.get(flag, {}).get("type") != "boolean":
            raise AttributeError_("flag must name a boolean field of value_schema")
    else:
        flag = None
    return {"name": name, "description": str(spec.get("description") or "").strip()[:500],
            "value_schema": schema, "recall_effect": effect, "flag": flag}


def draft(conn, spec):
    """The registry row a definition would have: the stored row when `spec`
    names a registered attribute, else a checked, unstored one."""
    if isinstance(spec, str):
        return require(conn, spec)
    if isinstance(spec, dict) and isinstance(spec.get("name"), str):
        stored = get(conn, spec["name"].strip())
        if stored and stored["status"] == "active" and (stored["origin"] == "core" or not spec.get("value_schema")):
            return stored
    row = clean(spec)
    return {**row, "origin": "draft", "status": "active"}


def define(conn, token, spec):
    """Register (or update) an attribute. Core attributes cannot be changed.
    Returns the row."""
    kind = _owner_or_agent(token)
    row = clean(spec)
    name = row["name"]
    existing = get(conn, name)
    if existing and existing["origin"] == "core":
        raise AttributeError_(f"{name} is a core attribute")
    if existing and kind == "agent" and existing["origin"] == "owner":
        if existing["value_schema"] == row["value_schema"] and existing["recall_effect"] == row["recall_effect"]:
            return existing
        raise PermissionError("an agent cannot change an attribute the owner defined")
    stored = conn.execute(
        """INSERT INTO tl.annotation_attributes(name,description,value_schema,recall_effect,flag,origin)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (name) DO UPDATE SET description=EXCLUDED.description,value_schema=EXCLUDED.value_schema,
         recall_effect=EXCLUDED.recall_effect,flag=EXCLUDED.flag,status='active',updated_at=clock_timestamp()
        RETURNING *""",
        (name, row["description"], _jsonb(row["value_schema"]), row["recall_effect"], row["flag"],
         "owner" if kind == "owner" else "agent"),
    ).fetchone()
    return dict(stored)


def value_ok(conn, attr_row, value):
    """Raise ValueError when value is not valid for this attribute row (a
    stored or a draft one)."""
    if attr_row.get("origin") == "core":
        from .. import annotations

        with conn.transaction():
            annotations._check_value(conn, attr_row["name"], value)
    else:
        check_value(attr_row["value_schema"], value, attribute=attr_row["name"])


def retire(conn, token, name):
    if (token or {}).get("kind") != "owner":
        raise PermissionError("only the owner retires attributes")
    row = get(conn, name)
    if not row:
        raise LookupError("attribute not found")
    if row["origin"] == "core":
        raise AttributeError_(f"{name} is a core attribute")
    return dict(
        conn.execute(
            "UPDATE tl.annotation_attributes SET status='retired',updated_at=clock_timestamp() WHERE name=%s RETURNING *",
            (name,),
        ).fetchone()
    )


def excludes(row, value):
    """True when this value hides its message from default recall."""
    return bool(row and row["recall_effect"] == "exclude" and row.get("flag") and isinstance(value, dict) and value.get(row["flag"]) is True)


def _jsonb(value):
    from psycopg.types.json import Jsonb

    return Jsonb(value)
