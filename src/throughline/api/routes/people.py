"""Person-centric reads over HTTP (QRY-6, ACL-8, INT-10).

Every GET runs one person function (migration 0047) on the audited reader
path, so the database gate decides what a restricted asker counts, ranks and
lists; identity data comes back to unrestricted askers only. Before the
function runs the exchange index is refreshed within
THROUGHLINE_PEOPLE_REFRESH_MS (default 2000 ms); threads still stale are
disclosed as execution.incomplete 'exchange_index_stale' with the count.

POST /v1/identity/identifiers/{id}/status (owner tokens only) confirms,
suspects or ignores an account identifier. Only a confirmed identifier
attributes messages, so this is how a suspected account joins (or leaves)
every person function. It writes an identity event with the actor and the
reason, and uses the write contract (idempotency key, receipt) when present.
"""

from contextlib import nullcontext
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Request

from ... import identity, people
from ...execution import Execution
from ..deps import (
    Token,
    audit,
    audited,
    authenticated_connection,
    require,
    require_unrestricted,
    scope_of,
)

try:  # the write contract module is optional here.
    from ... import writes
except ImportError:  # pragma: no cover - exercised only without the package
    writes = None

router = APIRouter()
STATUSES = ("confirmed", "suspected", "ignored")


def _when(value, label):
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"{label} must be an ISO timestamp") from None
    if parsed.tzinfo is None:
        raise HTTPException(422, f"{label} needs a UTC offset")
    return parsed


def _date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD") from None


def _ids(value, label, minimum=1):
    if value is None or value == "":
        items = []
    else:
        try:
            items = [int(v) for v in str(value).split(",") if v.strip()]
        except ValueError:
            raise HTTPException(422, f"{label} must be comma-separated ids") from None
    if len(items) < minimum:
        raise HTTPException(
            400,
            f"{label} needs at least {minimum} person id"
            + ("s" if minimum > 1 else ""),
        )
    return items


def _limit(value):
    if value is not None and not 0 <= value <= 10000:
        raise HTTPException(422, "max_results must be between 0 and 10000")
    return value


def _run(token, name, **arguments):
    """Refresh the index, run api.<name> on the audited reader path,
    disclose staleness. Returns {rows, row_count, truncated, execution}."""
    require(token, "owner", "agent")
    execution = Execution()
    refreshed = None
    try:
        refreshed = people.refresh()
    except Exception:
        execution.warn("exchange index refresh failed")
    _order, cap = people.FUNCTIONS[name]
    result = audited(
        people.statement(name, **arguments), token, cap, execution=execution
    )
    people.disclose(scope_of(token), execution, refreshed)
    audit(token, result["row_count"], operation=name)
    return dict(
        rows=result["rows"],
        row_count=result["row_count"],
        truncated=result["truncated"],
        execution=execution.to_dict(),
    )


@router.get("/v1/people/ties")
def ties(
    token: Token,
    a: int,
    b: int,
    after: str | None = None,
    before: str | None = None,
):
    """Pair breakdown of a tie: exchanges by direction, shared conversations."""
    return _run(
        token,
        "ties",
        a=a,
        b=b,
        after_at=_when(after, "after"),
        before_at=_when(before, "before"),
    )


@router.get("/v1/people/index")
def index_status(token: Token):
    """Refresh the exchange index within the budget; its state over the
    threads you may read."""
    return _run(token, "exchange_index_status")


@router.get("/v1/people/{person_id}/brief")
def brief(person_id: int, token: Token):
    """The default first step: who the person is and the shape of contact."""
    result = _run(token, "person_brief", person_id=person_id)
    return dict(
        brief=result["rows"][0] if result["rows"] else None,
        execution=result["execution"],
    )


@router.get("/v1/people/{person_id}/timeline")
def timeline(
    person_id: int,
    token: Token,
    after: str | None = None,
    before: str | None = None,
    max_results: int | None = 200,
):
    return _run(
        token,
        "person_timeline",
        person_id=person_id,
        after_at=_when(after, "after"),
        before_at=_when(before, "before"),
        max_results=_limit(max_results),
    )


@router.get("/v1/people/{person_id}/arc")
def arc(person_id: int, token: Token, grain: str = "month"):
    if grain not in ("week", "month", "quarter", "year"):
        raise HTTPException(422, "grain must be week, month, quarter or year")
    return _run(token, "person_arc", person_id=person_id, grain=grain)


@router.get("/v1/people/{person_id}/dms")
def dms(
    person_id: int,
    token: Token,
    after: str | None = None,
    before: str | None = None,
    max_results: int | None = 200,
):
    return _run(
        token,
        "dms_with",
        person_id=person_id,
        after_at=_when(after, "after"),
        before_at=_when(before, "before"),
        max_results=_limit(max_results),
    )


@router.get("/v1/people/{person_id}/last-contact")
def last_contact(person_id: int, token: Token):
    result = _run(token, "last_contact", person_id=person_id)
    return dict(
        last_contact=result["rows"][0] if result["rows"] else None,
        execution=result["execution"],
    )


def _loops(token, person_id, older_than, horizon_days, max_results):
    return _run(
        token,
        "open_loops",
        person_id=person_id,
        older_than=older_than,
        horizon_days=horizon_days,
        max_results=_limit(max_results),
        ask_patterns=people.ask_patterns(),
        closing_patterns=people.closing_patterns(),
    )


@router.get("/v1/people/{person_id}/open-loops")
def person_open_loops(
    person_id: int,
    token: Token,
    older_than: str = "1 day",
    horizon_days: int = 30,
    max_results: int | None = 100,
):
    return _loops(token, person_id, older_than, horizon_days, max_results)


@router.get("/v1/open-loops")
def open_loops(
    token: Token,
    person: int | None = None,
    older_than: str = "1 day",
    horizon_days: int = 30,
    max_results: int | None = 100,
):
    """Open loops in either direction (owner_owes | they_owe)."""
    return _loops(token, person, older_than, horizon_days, max_results)


@router.get("/v1/people/{person_id}/interactions")
def interactions(
    person_id: int,
    token: Token,
    after: str | None = None,
    before: str | None = None,
    grain: str = "week",
):
    if grain not in ("day", "week", "month"):
        raise HTTPException(422, "grain must be day, week or month")
    return _run(
        token,
        "interactions",
        person_id=person_id,
        after_at=_when(after, "after"),
        before_at=_when(before, "before"),
        grain=grain,
    )


@router.get("/v1/people/{person_id}/circle")
def circle(
    person_id: int,
    token: Token,
    mode: str = "absolute",
    after: str | None = None,
    before: str | None = None,
    max_results: int | None = 50,
):
    if mode not in ("absolute", "relative"):
        raise HTTPException(422, "mode must be absolute or relative")
    return _run(
        token,
        "circle",
        person_id=person_id,
        mode=mode,
        after_at=_when(after, "after"),
        before_at=_when(before, "before"),
        max_results=_limit(max_results),
    )


@router.get("/v1/people/{person_id}/rhythm")
def rhythm(
    person_id: int,
    token: Token,
    after: str | None = None,
    before: str | None = None,
    tz: str | None = None,
):
    return _run(
        token,
        "rhythm",
        person_id=person_id,
        after_at=_when(after, "after"),
        before_at=_when(before, "before"),
        tz=tz or people.timezone(),
    )


@router.get("/v1/people/{person_id}/mentions")
def mentions(
    person_id: int,
    token: Token,
    after: str | None = None,
    before: str | None = None,
    max_results: int | None = 100,
):
    return _run(
        token,
        "mentions",
        person_id=person_id,
        after_at=_when(after, "after"),
        before_at=_when(before, "before"),
        max_results=_limit(max_results),
    )


@router.get("/v1/around")
def around(
    token: Token,
    at: str,
    window_s: int = 1800,
    persons: str | None = None,
    max_results: int | None = 100,
    include_ignored: bool = False,
):
    """Records across channels around a time, by effective time."""
    if not 0 <= window_s <= 31536000:
        raise HTTPException(422, "window_s must be between 0 and 31536000")
    return _run(
        token,
        "around",
        at=_when(at, "at"),
        window_s=window_s,
        persons=_ids(persons, "persons") if persons else None,
        max_results=_limit(max_results),
        include_ignored=include_ignored,
    )


@router.get("/v1/day")
def day(
    token: Token,
    date: str,
    tz: str | None = None,
    persons: str | None = None,
    max_results: int | None = 500,
    include_ignored: bool = False,
):
    """Records of one local day by effective time."""
    return _run(
        token,
        "day",
        on_date=_date(date),
        tz=tz or people.timezone(),
        persons=_ids(persons, "persons") if persons else None,
        max_results=_limit(max_results),
        include_ignored=include_ignored,
    )


@router.get("/v1/together")
def together(
    token: Token,
    persons: str,
    after: str | None = None,
    before: str | None = None,
    max_results: int | None = 100,
):
    """Episodes where every listed person has a message."""
    return _run(
        token,
        "together",
        persons=_ids(persons, "persons", 2),
        after_at=_when(after, "after"),
        before_at=_when(before, "before"),
        max_results=_limit(max_results),
    )


@router.get("/v1/between")
def between(
    token: Token,
    persons: str,
    after: str | None = None,
    before: str | None = None,
    max_results: int | None = 200,
):
    """Messages by any listed person in the threads they all share."""
    return _run(
        token,
        "between",
        persons=_ids(persons, "persons", 2),
        after_at=_when(after, "after"),
        before_at=_when(before, "before"),
        max_results=_limit(max_results),
    )


def _set_status(conn, identifier_id, status, reason, token=None):
    row = conn.execute(
        "SELECT id,status,person_id FROM tl.identifiers WHERE id=%s FOR UPDATE",
        (identifier_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "identifier not found")
    conn.execute(
        "UPDATE tl.identifiers SET status=%s,asserted_by='owner',updated_at=now() WHERE id=%s",
        (status, identifier_id),
    )
    attributed = conn.execute(
        "SELECT count(*) AS n FROM tl.messages WHERE author_identifier_id=%s AND person_id IS NOT NULL",
        (identifier_id,),
    ).fetchone()["n"]
    event = identity.event(
        conn,
        "identifier_status",
        "owner",
        dict(
            identifier_id=identifier_id,
            person_id=row["person_id"],
            previous_status=row["status"],
            status=status,
            reason=reason,
            attributed_messages=attributed,
            token_id=(token or {}).get("id"),
            token_name=(token or {}).get("name"),
        ),
    )
    return dict(
        identifier_id=identifier_id,
        person_id=row["person_id"],
        previous_status=row["status"],
        status=status,
        attributed_messages=attributed,
        event_id=event["id"],
    )


@router.post("/v1/identity/identifiers/{identifier_id}/status")
def identifier_status(identifier_id: int, data: dict, request: Request, token: Token):
    """Owner only: {status: confirmed | suspected | ignored, reason?}. Only a
    confirmed identifier attributes its messages to its person."""
    require_unrestricted(token)
    require(token, "owner")
    status = data.get("status") if isinstance(data, dict) else None
    if status not in STATUSES:
        raise HTTPException(422, "status must be confirmed, suspected or ignored")
    reason = data.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 2000):
        raise HTTPException(422, "reason must be text of at most 2000 characters")
    with authenticated_connection(token) as conn:
        guard = (
            writes.idempotent(conn, token, request, data)
            if writes is not None
            else nullcontext(None)
        )
        with guard as slot:
            if slot is not None and slot.replayed:
                audit(token, 0, operation="identifier_status", replayed=True)
                return slot.response
            result = _set_status(conn, identifier_id, status, reason, token)
            if writes is not None:
                try:
                    result["receipt"] = writes.receipt(
                        conn, "identifier", identifier_id
                    )
                except (LookupError, ValueError):
                    result["receipt"] = None
            if slot is not None:
                slot.store(200, result)
    audit(
        token,
        1,
        operation="identifier_status",
        identifier_id=identifier_id,
        status=status,
    )
    return result
