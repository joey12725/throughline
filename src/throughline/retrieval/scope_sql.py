"""The hard scope as SQL arguments, and its resolution before any leg runs.

args() renders the scope arguments S of migration 0038 (in their order) as
SQL literals, so every leg passes the identical predicate to its function.
resolve() turns names into ids (a place through api.places, persons through
api.people, result-set handles into message ids). Anything that cannot be
resolved makes the whole request unresolvable: recall then returns nothing
with a reason, never an unscoped search.
"""

import os
import re
from dataclasses import dataclass, field

from psycopg import sql

from .types import MESSAGE_KINDS, HardScope

# Order of the scope arguments S in every 0038 function.
S_ORDER = (
    "kinds",
    "channels",
    "persons",
    "person_role",
    "after_at",
    "before_at",
    "time_basis",
    "lat",
    "lon",
    "meters",
    "thread_ids",
    "include_outside",
    "as_of",
    "known_by",
    "must_terms",
    "exclude_message_ids",
)
DEFAULT_KINDS = ["life_event", "derived"]
# Pseudo-kind of the SQL kinds list (migration 0042): records whose effective
# overlay state is ignored are eligible only when it is listed. Never a public
# kind of HardScope.kinds; rendered only for scope.include_ignored.
IGNORED = "ignored"
HANDLE = re.compile(r"^rs_[0-9a-f]{32}$")
# person_role 'between' (migration 0047) is resolved here; the SQL scope
# functions know author and involved only.
SQL_ROLES = ("author", "involved")
DEFAULT_SOURCE_CAP = 5000
RECORD_ID = re.compile(r"^([a-z]{1,8}):([0-9]{1,18})$")


# SQL type of each array argument of S: an empty list must keep its type, or
# the function call no longer resolves.
ARRAY_TYPES = dict(
    kinds="text[]",
    channels="text[]",
    persons="bigint[]",
    thread_ids="bigint[]",
    must_terms="text[]",
    exclude_message_ids="bigint[]",
)


def lit(value, array_type=None):
    """One SQL literal; lists become typed arrays (bigint[] or text[]).
    array_type fixes the type of an array (an empty list has none of its own)."""
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        integers = all(isinstance(v, int) and not isinstance(v, bool) for v in items)
        if array_type is None:
            array_type = "bigint[]" if integers else "text[]"
        if array_type == "bigint[]":
            if not integers:
                raise TypeError("a bigint[] literal needs integers")
            if not items:
                return "'{}'::bigint[]"
            return "ARRAY[" + ",".join(str(int(v)) for v in items) + "]::bigint[]"
        if not items:
            return "'{}'::text[]"
        return (
            "ARRAY["
            + ",".join(sql.Literal(str(v)).as_string() for v in items)
            + "]::text[]"
        )
    if isinstance(value, float):
        return sql.Literal(value).as_string() + "::double precision"
    return sql.Literal(value).as_string()


def message_kinds(scope):
    """Message record kinds the scope admits: None = the default set, [] =
    none (the scope names only non-message kinds)."""
    if scope.kinds is None:
        return DEFAULT_KINDS + ["reference"] if scope.include_reference else None
    kinds = [k for k in scope.kinds if k in MESSAGE_KINDS]
    if scope.include_reference and "reference" not in kinds:
        kinds.append("reference")
    return kinds


def admits(scope, kind):
    """Whether a non-message record kind (link, entity, passage, claim) may be
    returned under the scope."""
    return scope.kinds is None or kind in scope.kinds


@dataclass
class Resolved:
    """The scope after resolution: what args() renders."""

    scope: HardScope
    kinds: list | None = None
    lat: float | None = None
    lon: float | None = None
    meters: float | None = None
    message_ids: list | None = None
    exclude_message_ids: list = field(default_factory=list)
    # Non-message records (l:, e:, p:, c: ...) named by exclude_seen; removed
    # from the candidates before any selection.
    exclude_records: set = field(default_factory=set)
    unresolvable: str | None = None

    @property
    def cutoff(self):
        """How new derived state (links) may be: known_by (what had been
        stored then), else as_of. Validity intervals are tested at as_of."""
        if self.scope.known_by is not None:
            return self.scope.known_by
        return self.scope.as_of

    @property
    def historical(self):
        return self.scope.as_of is not None or self.scope.known_by is not None


def sql_kinds(scope, kinds):
    """The kinds list the SQL functions receive. kinds is message_kinds(scope)
    (None = the default set, [] = no message kinds). With include_ignored the
    pseudo-kind 'ignored' is added: to the default set (spelled out), to a
    non-empty list, never to [] (the scope names only non-message kinds)."""
    if not getattr(scope, "include_ignored", False):
        return kinds
    if kinds is None:
        base = list(DEFAULT_KINDS)
        if scope.include_reference:
            base.append("reference")
        return base + [IGNORED]
    if not kinds:
        return kinds
    return list(kinds) + ([IGNORED] if IGNORED not in kinds else [])


def values(resolved, reduced=False):
    """S as a {name: value} mapping. reduced keeps only what dependency and
    context rows must satisfy: kinds, as_of, known_by, include_outside.
    time_basis is passed as given ('effective' is understood by the SQL scope
    from migration 0042 on)."""
    scope = resolved.scope
    result = dict.fromkeys(S_ORDER)
    result.update(
        kinds=sql_kinds(scope, resolved.kinds),
        person_role="author",
        time_basis="record",
        include_outside=scope.include_outside,
        as_of=scope.as_of,
        known_by=scope.known_by,
    )
    if reduced:
        return result
    result.update(
        channels=scope.channels,
        persons=scope.persons,
        # An unresolved 'between' (a caller that did not run resolve()) is
        # narrowed to author, never widened.
        person_role=scope.person_role if scope.person_role in SQL_ROLES else "author",
        after_at=scope.after,
        before_at=scope.before,
        time_basis=scope.time_basis,
        lat=resolved.lat,
        lon=resolved.lon,
        meters=resolved.meters,
        thread_ids=scope.thread_ids,
        must_terms=scope.must_terms,
        exclude_message_ids=resolved.exclude_message_ids or None,
    )
    return result


def args(resolved, reduced=False):
    """S rendered as comma-separated SQL literals, in function order."""
    return render(values(resolved, reduced))


def render(current):
    """A {name: value} mapping of S as comma-separated SQL literals."""
    return ",".join(lit(current[name], ARRAY_TYPES.get(name)) for name in S_ORDER)


def source_values(resolved):
    """S for enumerating the in-scope SOURCE messages of derived records
    (passages): only the scope parts a derived record satisfies through its
    sources (persons with role, place, include_outside, kinds) and the
    as_of/known_by cutoffs. Channels, threads, time, terms and exclusions
    belong to the derived record itself and are applied by its own function.
    Every non-page message kind may be a source, and a source's own overlay
    state (ignored) does not decide whether the record built from it is in
    scope, so kinds is the full list of source kinds plus 'ignored'."""
    scope = resolved.scope
    result = values(resolved, reduced=True)
    result.update(
        kinds=["life_event", "reference", "derived", IGNORED],
        persons=scope.persons,
        person_role=scope.person_role if scope.person_role in SQL_ROLES else "author",
        lat=resolved.lat,
        lon=resolved.lon,
        meters=resolved.meters,
    )
    return result


def passage_time_basis(scope):
    """Passage functions know record, subject and either. 'effective' is an
    overlay interval of messages; a page revision has none, so it is read as
    record (documented in docs/v11/ranking.md)."""
    basis = scope.time_basis or "record"
    return "record" if basis == "effective" else basis


def needs_sources(resolved):
    """Whether the scope names something a passage can satisfy only through
    its provenance sources (persons, place or near)."""
    return bool(resolved.scope.persons) or (
        resolved.lat is not None and resolved.lon is not None
    )


def _handle_ids(ctx, handle, field_name, others=None):
    try:
        from .. import resultsets
    except ImportError:
        return None
    try:
        members = resultsets.members(ctx.scope, handle)
    except Exception:
        return None
    ids = []
    for member in members:
        record_id = member.get("record_id") or ""
        if record_id.startswith("m:") or member.get("prefix") == "m":
            ids.append(int(member.get("message_id") or record_id.split(":", 1)[1]))
        elif RECORD_ID.fullmatch(record_id) and others is not None:
            others.add(record_id)
    return ids


def _ids(ctx, items, field_name, others=None):
    """Message ids named by handles, 'm:<id>' record ids or plain ids; None
    when one of them cannot be resolved. Other record ids ('l:5') are added
    to others when it is given."""
    ids = []
    for item in items or []:
        if isinstance(item, int) and not isinstance(item, bool):
            ids.append(item)
            continue
        text = str(item).strip()
        if text.isdigit():
            ids.append(int(text))
            continue
        match = RECORD_ID.fullmatch(text)
        if match:
            if match.group(1) == "m":
                ids.append(int(match.group(2)))
            elif others is not None:
                others.add(text)
            continue
        if HANDLE.fullmatch(text):
            found = _handle_ids(ctx, text, field_name, others)
            if found is None:
                return None
            ids.extend(found)
            continue
        return None
    return ids


def source_cap():
    """THROUGHLINE_SCOPE_SOURCE_CAP: how many message ids a resolved scope
    may pass (default 5,000)."""
    try:
        return max(
            1, int(os.getenv("THROUGHLINE_SCOPE_SOURCE_CAP") or DEFAULT_SOURCE_CAP)
        )
    except ValueError:
        return DEFAULT_SOURCE_CAP


def _warn(ctx, message):
    execution = getattr(ctx, "execution", None)
    if execution is not None and hasattr(execution, "warn"):
        execution.warn(message)


def _between(ctx, scope, resolved):
    """person_role 'between': the threads where every listed person has a
    message (api.between_threads), intersected with scope.thread_ids; the
    role passed to SQL is 'involved' restricted to messages whose party_id is
    a listed person (message ids from one api.appearances statement), or
    'author' with the warning between_author_fallback when that set is over
    the source cap or the scope is historical (as_of / known_by). No shared
    thread: an empty result with the warning no_shared_threads."""
    persons = sorted(set(scope.persons or []))
    rows = ctx.sql(
        "SELECT b.thread_id FROM api.between_threads(" + lit(persons) + ") b",
        cap=10000,
    )
    threads = sorted({r["thread_id"] for r in rows})
    if scope.thread_ids is not None:
        wanted = set(scope.thread_ids)
        threads = [t for t in threads if t in wanted]
    role = "involved"
    message_ids = resolved.message_ids
    if not threads:
        _warn(ctx, "no_shared_threads")
        message_ids = []
    else:
        cap = source_cap()
        found = None
        if not resolved.historical and len(threads) <= cap:
            found = ctx.sql(
                "SELECT a.message_id FROM api.appearances a WHERE a.thread_id = ANY("
                + lit(threads)
                + ") AND a.party_id = ANY("
                + lit(persons)
                + ")",
                cap=cap + 1,
            )
        if found is None or len(found) > cap:
            role = "author"
            _warn(ctx, "between_author_fallback")
        else:
            ids = {r["message_id"] for r in found}
            message_ids = sorted(ids if message_ids is None else ids & set(message_ids))
    resolved.scope = scope.model_copy(
        update=dict(person_role=role, thread_ids=threads, persons=persons)
    )
    resolved.message_ids = message_ids
    return resolved


def resolve(ctx, scope):
    """Resolve names in the scope; Resolved.unresolvable names the field
    that could not be resolved (place, persons, starting_sets, exclude_seen).
    person_role 'between' needs at least two persons (ValueError, a 400)."""
    if scope.person_role == "between" and len(set(scope.persons or [])) < 2:
        raise ValueError("person_role between needs at least two persons")
    request = ctx.request
    resolved = Resolved(scope=scope, kinds=message_kinds(scope))
    if scope.near is not None:
        resolved.lat, resolved.lon, resolved.meters = (
            scope.near.lat,
            scope.near.lon,
            scope.near.meters,
        )
    if scope.place is not None:
        rows = ctx.sql(
            "SELECT p.id, p.metadata FROM api.places p WHERE p.name="
            + lit(scope.place.name)
            + " ORDER BY p.id",
            cap=50,
        )
        points = set()
        for row in rows:
            meta = row["metadata"] or {}
            lat, lon = meta.get("lat"), meta.get("lon")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                points.add((float(lat), float(lon)))
        if len(points) != 1:
            resolved.unresolvable = "place"
            return resolved
        (resolved.lat, resolved.lon), resolved.meters = (
            points.pop(),
            scope.place.meters,
        )
    if scope.persons:
        wanted = sorted(set(scope.persons))
        rows = ctx.sql(
            "SELECT p.id FROM api.people p WHERE p.id = ANY(" + lit(wanted) + ")",
            cap=len(wanted) + 1,
        )
        if {r["id"] for r in rows} != set(wanted):
            resolved.unresolvable = "persons"
            return resolved
    message_ids = list(scope.message_ids) if scope.message_ids is not None else None
    if request is not None and request.starting_sets:
        started = _ids(ctx, request.starting_sets, "starting_sets")
        if started is None:
            resolved.unresolvable = "starting_sets"
            return resolved
        message_ids = (
            started
            if message_ids is None
            else [i for i in message_ids if i in set(started)]
        )
    resolved.message_ids = sorted(set(message_ids)) if message_ids is not None else None
    if request is not None and request.exclude_seen:
        excluded = _ids(
            ctx, request.exclude_seen, "exclude_seen", resolved.exclude_records
        )
        if excluded is None:
            resolved.unresolvable = "exclude_seen"
            return resolved
        resolved.exclude_message_ids = sorted(set(excluded))
    if scope.person_role == "between":
        return _between(ctx, scope, resolved)
    return resolved
