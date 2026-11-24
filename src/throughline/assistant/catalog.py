"""What the helper knows about the SQL surface, read under the request's scope.

Everything here is catalogue: channel names and declared fields, view and
column names with their types, api function names and the builtin allow-list.
No row value is read. A restricted asker sees the channels (and channel
views) its grants show, nothing more.
"""

import json
import threading

from .. import query
from ..query import query_ex, reader_transaction

# Columns every message-shaped view has (api.messages, api.ch_<name>).
MESSAGE_COLUMNS = (
    ("id", "int8"),
    ("channel_id", "int8"),
    ("external_id", "text"),
    ("version", "int4"),
    ("superseded_by", "int8"),
    ("thread_id", "int8"),
    ("thread_no", "int4"),
    ("happened_at", "timestamptz"),
    ("happened_offset_min", "int4"),
    ("ended_at", "timestamptz"),
    ("ingested_at", "timestamptz"),
    ("author_handle", "text"),
    ("author_identifier_id", "int8"),
    ("person_id", "int8"),
    ("content", "text"),
    ("images", "jsonb"),
    ("fields", "jsonb"),
    ("lat", "float8"),
    ("lon", "float8"),
    ("loc_accuracy_m", "float4"),
    ("loc_method", "text"),
    ("loc_reason", "text"),
    ("access_label", "text"),
    ("payload_hash", "text"),
    ("tsv", "tsvector"),
    ("channel", "text"),
    ("is_reference", "bool"),
    ("is_outside", "bool"),
    ("thread_external_id", "text"),
    ("thread_title", "text"),
    ("person_handle", "text"),
    ("display_name", "text"),
)
# The api functions that run models or embeddings (semantic SQL): never a
# good first choice for a helper, and they may need an approval.
SEMANTIC = ("recall", "expand", "match", "librarian")
FIELD_TYPES = {
    "text": "text",
    "integer": "int8",
    "number": "float8",
    "boolean": "bool",
    "timestamp": "timestamptz",
    "json": "jsonb",
}


def view_name(channel):
    return "ch_" + channel.replace(".", "_")


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


class Catalog:
    """Lazily read, cached for one run."""

    def __init__(self, scope):
        self.scope = scope
        self._lock = threading.Lock()
        self._index = None
        self._channels = None
        self._relations = None

    def describe(self, subject):
        from psycopg import sql

        statement = (
            sql.SQL("SELECT api.describe({}) AS d")
            .format(sql.Literal(subject))
            .as_string()
        )
        return _json(
            query_ex(statement, 1, scope=self.scope, materialize=False)["rows"][0]["d"]
        )

    def index(self):
        with self._lock:
            if self._index is None:
                try:
                    self._index = self.describe(None) or {}
                except Exception:
                    self._index = {}
            return self._index

    def functions(self):
        return [str(f) for f in self.index().get("functions") or []]

    def channels(self):
        """[{name, description, record_kind, fields: [{name, type, description}],
        view}] of the channels the scope sees."""
        with self._lock:
            if self._channels is not None:
                return self._channels
        rows = query_ex(
            "SELECT name,description,record_kind,fields FROM api.channels ORDER BY name",
            10000,
            scope=self.scope,
            materialize=False,
        )["rows"]
        views = set(self.relations())
        found = []
        for row in rows:
            fields = [
                dict(
                    name=str(f.get("name")),
                    type=str(f.get("type") or "text"),
                    description=f.get("description"),
                )
                for f in (_json(row["fields"]) or [])
                if isinstance(f, dict) and f.get("name")
            ]
            view = view_name(row["name"])
            found.append(
                dict(
                    name=row["name"],
                    description=row["description"],
                    record_kind=row["record_kind"],
                    fields=fields,
                    view="api." + view if view in views else None,
                )
            )
        with self._lock:
            self._channels = found
        return found

    def channel(self, name):
        for channel in self.channels():
            if channel["name"] == name:
                return channel
        return None

    def relations(self):
        """{relation name: [(column, type)]} of schema api. Channel views of
        channels the scope does not see are left out."""
        with self._lock:
            if self._relations is not None:
                return self._relations
        relations = {}
        with reader_transaction(self.scope, 5000) as conn:
            for row in conn.execute(
                """SELECT c.relname::text AS rel, a.attname::text AS col, t.typname::text AS typ
                   FROM pg_catalog.pg_class c
                   JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                   JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
                   JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
                   WHERE n.nspname='api' AND c.relkind IN ('v','r','m')
                   ORDER BY c.relname, a.attnum"""
            ):
                relations.setdefault(row["rel"], []).append((row["col"], row["typ"]))
        try:
            visible = {
                view_name(r["name"])
                for r in query_ex(
                    "SELECT name FROM api.channels",
                    10000,
                    scope=self.scope,
                    materialize=False,
                )["rows"]
            }
        except Exception:
            visible = set()
        relations = {
            name: cols
            for name, cols in relations.items()
            if not name.startswith("ch_") or name in visible
        }
        with self._lock:
            self._relations = relations
        return relations

    def columns(self, relation):
        """[(column, type)] of api.<relation> (bare or qualified name)."""
        name = relation.split(".", 1)[1] if relation.startswith("api.") else relation
        return self.relations().get(name) or []

    @staticmethod
    def builtins():
        return sorted(query.BUILTINS)

    def api_functions(self):
        """Allow-listed api functions the scope's catalogue lists."""
        listed = set(self.functions())
        allowed = set(query.FUNCTIONS)
        return sorted((listed & allowed) | (listed & set(SEMANTIC)))
