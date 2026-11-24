"""Per-token limits. Owner database connection only, like token creation."""

import math

import psycopg
from psycopg import sql

from .db import connect

_EXTRA_LIMITS = ("approve_limit", "allow_escalation")


def limit_columns(conn):
    """Limit columns that exist on tl.tokens now: {name: SQL type}.

    Discovered from the catalog, so limits added by any migration (max_*,
    approve_limit, allow_escalation) are settable without changing this module.
    """
    rows = conn.execute(
        "SELECT a.attname AS name,format_type(a.atttypid,a.atttypmod) AS type FROM pg_attribute a WHERE a.attrelid='tl.tokens'::regclass AND a.attnum>0 AND NOT a.attisdropped AND (a.attname LIKE 'max\\_%%' OR a.attname=ANY(%s)) ORDER BY a.attnum",
        (list(_EXTRA_LIMITS),),
    ).fetchall()
    return {r["name"]: r["type"] for r in rows}


def get_limits(token_id):
    with connect() as conn:
        return _row(conn, token_id, limit_columns(conn))


def _row(conn, token_id, columns):
    row = conn.execute(
        sql.SQL("SELECT id,name,kind,{} FROM tl.tokens WHERE id=%s").format(
            sql.SQL(",").join(map(sql.Identifier, columns))
        ),
        (token_id,),
    ).fetchone()
    if not row:
        raise ValueError("no token with that id")
    return row


def set_limits(token_id, **values):
    """Set limit columns of one token; returns the token's limits.

    Values may be strings (from the command line); the database casts them to
    the column type. Numeric limits cannot be negative.
    """
    with connect() as conn:
        columns = limit_columns(conn)
        unknown = sorted(set(values) - set(columns))
        if unknown:
            raise ValueError(
                "unknown limit: "
                + ", ".join(unknown)
                + " (available: "
                + ", ".join(columns)
                + ")"
            )
        for name, value in values.items():
            if value is None:
                raise ValueError(f"{name} needs a value")
            if columns[name] != "boolean":
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{name} needs a number") from None
                if not math.isfinite(number) or number < 0:
                    raise ValueError(f"{name} cannot be negative or infinite")
        if values:
            try:
                updated = conn.execute(
                    sql.SQL("UPDATE tl.tokens SET {} WHERE id=%s RETURNING id").format(
                        sql.SQL(",").join(
                            sql.SQL("{}=%s::{}").format(
                                sql.Identifier(name), sql.SQL(columns[name])
                            )
                            for name in values
                        )
                    ),
                    [str(v) for v in values.values()] + [token_id],
                ).fetchone()
            except psycopg.errors.DataError as error:
                # A value the column type cannot hold (for example 'maybe'
                # for a boolean, 1.5 for an integer, or out of range).
                raise ValueError(
                    "invalid limit value: " + (error.diag.message_primary or "")
                ) from None
            if not updated:
                raise ValueError("no token with that id")
        return _row(conn, token_id, columns)
