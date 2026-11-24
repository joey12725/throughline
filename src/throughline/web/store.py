"""Storage behind the web interface's own routes: its saved documents
(tl.web_state) and what this database can offer it (feature detection).

The documents are the interface's working state: preferences, colours, lens
overrides, saved queries, boards, import mappings. The server stores what the
client sends and never copies record content into a document.
"""

import json
import re

from psycopg.types.json import Jsonb

from ..query import reader_transaction

KIND = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# The kinds the interface uses (web/src/api/types.ts UiStateKind).
KINDS = frozenset(
    {"prefs", "colors", "overrides", "saved_query", "board", "import_mapping"}
)
MAX_KEY = 200
MAX_VALUE_BYTES = 512 * 1024
MAX_DOCUMENTS = 2000  # per namespace and kind
MAX_NAMESPACE_BYTES = 8 * 1024 * 1024  # all documents of one namespace


class TooLarge(ValueError):
    """A document or a namespace would exceed its byte limit (413)."""

HEALTH_COLUMNS = (
    "last_attempt_at",
    "last_error_at",
    "consecutive_failures",
    "paused_at",
    "paused_reason",
)


def features(scope):
    """{web_state, source_health, channel_views} from the catalogue alone.

    Read with the reader role under the caller's scope: pg_catalog is readable
    by every role and holds no record data, so this works for a restricted
    asker and on a database that is behind on migrations.
    """
    with reader_transaction(scope, 5000) as conn:
        row = conn.execute(
            """SELECT
                 EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                         WHERE n.nspname='tl' AND c.relname='web_state' AND c.relkind='r') AS web_state,
                 EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
                         JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                         WHERE n.nspname='tl' AND c.relname='plugins' AND a.attname='consecutive_failures'
                           AND NOT a.attisdropped) AS source_health,
                 ARRAY(SELECT c.relname::text FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                       WHERE n.nspname='api' AND c.relkind='v' AND c.relname LIKE 'ch\\_%') AS channel_views"""
        ).fetchone()
    return dict(
        web_state=bool(row["web_state"]),
        source_health=bool(row["source_health"]),
        channel_views=set(row["channel_views"] or []),
    )


def check_kind(kind):
    if not isinstance(kind, str) or not KIND.match(kind) or kind not in KINDS:
        raise ValueError("kind must be one of " + ", ".join(sorted(KINDS)))
    return kind


def check_key(key):
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= MAX_KEY
        or any(ord(c) < 32 or c == "/" for c in key)
    ):
        raise ValueError(f"key must be 1..{MAX_KEY} characters without / or controls")
    return key


def _document(row):
    return dict(key=row["key"], value=row["value"], updated_at=row["updated_at"])


def list_documents(conn, namespace, kind):
    return [
        _document(row)
        for row in conn.execute(
            "SELECT key,value,updated_at FROM tl.web_state WHERE namespace=%s AND kind=%s ORDER BY key",
            (namespace, check_kind(kind)),
        )
    ]


def get_document(conn, namespace, kind, key):
    row = conn.execute(
        "SELECT key,value,updated_at FROM tl.web_state WHERE namespace=%s AND kind=%s AND key=%s",
        (namespace, check_kind(kind), check_key(key)),
    ).fetchone()
    return _document(row) if row else None


def put_document(conn, namespace, kind, key, value, token_id=None):
    check_kind(kind)
    check_key(key)
    if value is None:
        raise ValueError("value must not be null (delete the document instead)")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    size = len(encoded.encode("utf-8"))
    if size > MAX_VALUE_BYTES:
        raise TooLarge(f"value is larger than {MAX_VALUE_BYTES} bytes")
    # One writer per namespace at a time, so the document and byte caps hold.
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,71824060))",
        (namespace,),
    )
    others = conn.execute(
        "SELECT coalesce(sum(octet_length(value::text)),0)::bigint AS n FROM tl.web_state"
        " WHERE namespace=%s AND NOT (kind=%s AND key=%s)",
        (namespace, kind, key),
    ).fetchone()["n"]
    if others + size > MAX_NAMESPACE_BYTES:
        raise TooLarge(
            f"the interface's documents would exceed {MAX_NAMESPACE_BYTES} bytes; delete some first"
        )
    exists = conn.execute(
        "SELECT 1 FROM tl.web_state WHERE namespace=%s AND kind=%s AND key=%s",
        (namespace, kind, key),
    ).fetchone()
    if not exists:
        count = conn.execute(
            "SELECT count(*) AS n FROM tl.web_state WHERE namespace=%s AND kind=%s",
            (namespace, kind),
        ).fetchone()["n"]
        if count >= MAX_DOCUMENTS:
            raise ValueError(f"at most {MAX_DOCUMENTS} documents per kind")
    row = conn.execute(
        """INSERT INTO tl.web_state(namespace,kind,key,value,token_id) VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT(namespace,kind,key) DO UPDATE
             SET value=EXCLUDED.value,updated_at=clock_timestamp(),token_id=EXCLUDED.token_id
           RETURNING key,value,updated_at""",
        (namespace, kind, key, Jsonb(value), token_id),
    ).fetchone()
    return _document(row)


def delete_document(conn, namespace, kind, key):
    return (
        conn.execute(
            "DELETE FROM tl.web_state WHERE namespace=%s AND kind=%s AND key=%s",
            (namespace, check_kind(kind), check_key(key)),
        ).rowcount
        > 0
    )
