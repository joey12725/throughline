"""Person-centric read model (migration 0047).

The person functions are SQL functions in schema api (person_brief first,
then timeline, arc, dms_with, between, last_contact, open_loops,
interactions, around, day, together, mentions, circle, ties, rhythm). They
read the gated api.appearances view and the exchange index, so a restricted
asker's counts, ranks and lists cover granted rows only.

The exchange index (tl.exchanges) resolves replies once: refresh() rebuilds
the threads marked stale, set-based, one thread per transaction, within a
time budget. Routes call it before running a person function and disclose
threads that are still stale as execution.incomplete 'exchange_index_stale';
SQL callers read api.exchange_index_status().

Every call goes through query.query_ex with the caller's scope (statement()
renders the call; run() executes it), so the asker gate always applies.
"""

import json
import os
import time
from datetime import date, datetime

from .db import connect
from .execution import Execution
from .query import query_ex
from .retrieval.scope_sql import lit

DEFAULT_WINDOW_S = 1800
DEFAULT_REFRESH_MS = 2000
MAX_STALE_LISTED = 10000

# name -> (argument names in SQL order, result cap); every argument is
# optional after the first unless it is listed in REQUIRED.
FUNCTIONS = {
    "person_brief": (("person_id",), 1),
    "person_timeline": (("person_id", "after_at", "before_at", "max_results"), 10000),
    "person_arc": (("person_id", "grain"), 10000),
    "dms_with": (("person_id", "after_at", "before_at", "max_results"), 10000),
    "between_threads": (("persons",), 10000),
    "between": (("persons", "after_at", "before_at", "max_results"), 10000),
    "last_contact": (("person_id",), 1),
    "open_loops": (
        (
            "person_id",
            "older_than",
            "horizon_days",
            "max_results",
            "ask_patterns",
            "closing_patterns",
        ),
        10000,
    ),
    "interactions": (("person_id", "after_at", "before_at", "grain"), 10000),
    "around": (
        ("at", "window_s", "persons", "max_results", "include_ignored"),
        10000,
    ),
    "day": (("on_date", "tz", "persons", "max_results", "include_ignored"), 10000),
    "together": (("persons", "after_at", "before_at", "max_results"), 10000),
    "mentions": (("person_id", "after_at", "before_at", "max_results"), 10000),
    "circle": (
        ("person_id", "mode", "after_at", "before_at", "max_results"),
        10000,
    ),
    "ties": (("a", "b", "after_at", "before_at"), 1),
    "rhythm": (("person_id", "after_at", "before_at", "tz"), 1000),
    "exchange_index_status": ((), 1),
}
# SQL types of arguments whose literal needs one.
TYPES = {
    "persons": "bigint[]",
    "ask_patterns": "text[]",
    "closing_patterns": "text[]",
    "older_than": "interval",
    "after_at": "timestamptz",
    "before_at": "timestamptz",
    "at": "timestamptz",
    "on_date": "date",
    "person_id": "bigint",
    "a": "bigint",
    "b": "bigint",
    "max_results": "int",
    "window_s": "int",
    "horizon_days": "int",
    "grain": "text",
    "mode": "text",
    "tz": "text",
    "include_ignored": "boolean",
}


def window_s():
    """THROUGHLINE_EXCHANGE_WINDOW_S: the next-turn window (default 1800 s)."""
    try:
        value = int(os.getenv("THROUGHLINE_EXCHANGE_WINDOW_S") or DEFAULT_WINDOW_S)
    except ValueError:
        return DEFAULT_WINDOW_S
    return max(0, value)


def refresh_ms():
    """THROUGHLINE_PEOPLE_REFRESH_MS: the refresh budget routes use."""
    try:
        value = int(os.getenv("THROUGHLINE_PEOPLE_REFRESH_MS") or DEFAULT_REFRESH_MS)
    except ValueError:
        return DEFAULT_REFRESH_MS
    return max(0, value)


def timezone():
    """THROUGHLINE_TIMEZONE (deprecated alias THROUGHLINE_OWNER_TZ), else UTC."""
    from . import tz

    return tz.name() or "UTC"


def _patterns(name):
    """A configured lexicon: a JSON list, else entries separated by ';;'.
    None when unset (the SQL default lexicon applies)."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = [p.strip() for p in raw.split(";;")]
    if not isinstance(value, list):
        return None
    value = [p for p in value if isinstance(p, str) and p.strip()]
    return value or None


def ask_patterns():
    return _patterns("THROUGHLINE_ASK_PATTERNS")


def closing_patterns():
    return _patterns("THROUGHLINE_CLOSING_PATTERNS")


# --- exchange index ------------------------------------------------------------


def _stale(conn, window, limit):
    conn.execute(
        "UPDATE tl.exchange_state SET dirty=true WHERE NOT dirty AND window_s IS DISTINCT FROM %s",
        (window,),
    )
    rows = conn.execute(
        "SELECT thread_id FROM tl.exchange_state WHERE dirty OR indexed_to IS NULL ORDER BY thread_id LIMIT %s",
        (limit,),
    ).fetchall()
    conn.commit()
    return [r["thread_id"] for r in rows]


def refresh(conn=None, budget_ms=None, window=None):
    """Rebuild stale threads of the exchange index within budget_ms (None: no
    limit), one thread per transaction. Returns {rebuilt, exchanges,
    remaining, ms}; remaining counts every stale thread (not the asker's)."""
    budget = refresh_ms() if budget_ms is None else budget_ms
    window = window_s() if window is None else max(0, int(window))
    started = time.perf_counter()
    if conn is None:
        with connect("app") as own:
            return _refresh(own, budget, window, started)
    return _refresh(conn, budget, window, started)


def _refresh(conn, budget, window, started):
    def spent():
        return (time.perf_counter() - started) * 1000

    rebuilt = written = 0
    stale = _stale(conn, window, MAX_STALE_LISTED)
    left = list(stale)
    while left and (budget is None or spent() < budget):
        thread = left.pop(0)
        try:
            conn.execute("SET LOCAL lock_timeout='500ms'")
            row = conn.execute(
                "SELECT tl.rebuild_exchanges(%s,%s) AS n", (thread, window)
            ).fetchone()
            conn.commit()
            rebuilt += 1
            written += row["n"] or 0
        except Exception:
            conn.rollback()
    remaining = conn.execute(
        "SELECT count(*) AS n FROM tl.exchange_state WHERE dirty OR indexed_to IS NULL"
    ).fetchone()["n"]
    conn.commit()
    return dict(
        rebuilt=rebuilt, exchanges=written, remaining=remaining, ms=round(spent(), 3)
    )


def rebuild_all(window=None):
    """Rebuild every stale thread (no budget); for maintenance and tests."""
    return refresh(budget_ms=None, window=window)


# --- statements ----------------------------------------------------------------


def literal(name, value):
    """One argument as a typed SQL literal (NULL when None)."""
    kind = TYPES.get(name, "text")
    if value is None:
        return "NULL::" + kind
    if kind.endswith("[]"):
        return lit(list(value), kind)
    if kind == "boolean":
        return "true" if value else "false"
    if kind in ("bigint", "int"):
        if isinstance(value, bool) or not isinstance(value, int):
            value = int(str(value).strip())
        return str(int(value)) + "::" + kind
    if isinstance(value, (datetime, date)):
        value = value.isoformat()
    return lit(str(value)) + "::" + kind


def statement(name, **arguments):
    """SELECT * FROM api.<name>(arg => value, ...) in named notation, so an
    argument that is not given keeps its SQL default. Unknown names raise
    ValueError; None means 'not given'."""
    if name not in FUNCTIONS:
        raise ValueError("unknown person function " + name)
    order, _cap = FUNCTIONS[name]
    unknown = sorted(set(arguments) - set(order))
    if unknown:
        raise ValueError(f"{name}: unknown argument " + ", ".join(unknown))
    parts = [
        f"{n} => {literal(n, arguments[n])}"
        for n in order
        if arguments.get(n) is not None
    ]
    return f"SELECT * FROM api.{name}(" + ",".join(parts) + ")"


def status(scope):
    """api.exchange_index_status() under the scope (threads the asker sees)."""
    rows = query_ex(statement("exchange_index_status"), 1, scope=scope)["rows"]
    return rows[0] if rows else {}


def disclose(scope, execution, refreshed=None):
    """Put the index state into an Execution: stale threads the asker can see
    are execution.incomplete 'exchange_index_stale' with the count."""
    try:
        state = status(scope)
    except Exception:
        execution.warn("exchange index status unavailable")
        return None
    stale = int(state.get("stale_threads") or 0)
    index = dict(
        stale_threads=stale,
        indexed_to=state.get("indexed_to"),
        window_s=window_s(),
    )
    if refreshed is not None:
        index["rebuilt_threads"] = refreshed.get("rebuilt", 0)
    execution.set("exchange_index", index)
    if stale:
        execution.degrade("exchange_index", "exchange_index_stale")
        execution.warn(f"exchange_index_stale:{stale}")
    return index


def run(
    scope, name, *, refresh_first=True, budget_ms=None, execution=None, **arguments
):
    """Refresh the index within the budget, then run one person function
    through query_ex under the scope. Returns {rows, execution}."""
    execution = execution if execution is not None else Execution()
    refreshed = None
    if refresh_first:
        try:
            refreshed = refresh(budget_ms=budget_ms)
        except Exception:
            execution.warn("exchange index refresh failed")
    order, cap = FUNCTIONS[name]
    result = query_ex(
        statement(name, **arguments), cap, scope=scope, execution=execution
    )
    disclose(scope, execution, refreshed)
    result["execution"] = execution.to_dict()
    return result
