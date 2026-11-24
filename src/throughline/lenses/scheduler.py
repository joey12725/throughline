"""The lens scheduler: one `tick` runs every lens that is due and resumes every
lens an outage interrupted, so a model endpoint that goes away (a local proxy
such as CLIProxyAPI stopping, a 429/5xx burst, an exhausted quota) delays
lens rows instead of losing them.

Nothing here keeps its own state. Whether a lens is due is read from
tl.table_runs and the lens cursor: the runner already leaves the cursor before
any item it could not finish (verify-then-advance, JOB-3), so resuming is just
another cursor run. Run `tick` from a timer (systemd, cron); it is cheap when
nothing is due and stops at one failed request per endpoint while it is down.

A lens is due when
- its last cursor run was interrupted: provider_unavailable, failed, a
  backlog cut at max_items, the monthly cap, or a 'running' row whose process
  died. Retries back off (RETRY_BASE_S doubling, at most RETRY_MAX_S) while
  the same endpoint keeps failing; or
- it has a cron `schedule` that fired since its last cursor run.

A lens with no schedule is only resumed, never started: it runs when the
owner (or a rule) runs it. Lazy lenses fill on read and are left alone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..agent_view import owner_zone
from ..db import connect
from . import coalesce
from .rules import deliver_pending, evaluate_rules
from .runner import _topo_layers, get_lens, list_lenses, run_lens
from .schema import LensSpecError
from .workers import JEV_ENDPOINT, _resolve_llm_endpoint, check_monthly_cap

RETRY_BASE_S = 5 * 60
RETRY_MAX_S = 60 * 60
# Statuses after which the cursor has unfinished work behind it.
OUTAGE = ("provider_unavailable", "failed")
RESUME = OUTAGE + ("max_items", "running")
# A 'running' row this old is resumed even if its lock is somehow held.
STALE_RUNNING_S = 2 * 60 * 60

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))


# --- cron -------------------------------------------------------------------------


def _field(text: str, name: str, lo: int, hi: int) -> tuple[set[int], bool]:
    """The values of one cron field and whether it was '*' (unrestricted)."""
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise LensSpecError(f"schedule: bad step in {name} field")
            step = int(step_text)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise LensSpecError(f"schedule: bad range in {name} field")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = int(part)
            end = hi if step > 1 else start
        else:
            raise LensSpecError(f"schedule: bad {name} field {part!r}")
        if start < lo or end > hi or start > end:
            raise LensSpecError(f"schedule: {name} must be within {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values, text == "*"


def parse_schedule(expr: str) -> tuple:
    """A five-field cron expression (or @hourly/@daily/@weekly/@monthly/@yearly)
    as (minutes, hours, days, months, weekdays, dom_any, dow_any); weekday 7 is
    Sunday like 0. LensSpecError when it is not one."""
    text = MACROS.get(str(expr).strip().lower(), str(expr).strip())
    parts = text.split()
    if len(parts) != 5:
        raise LensSpecError("schedule must be a 5-field cron expression (minute hour day month weekday) or @hourly/@daily/...")
    parsed = [_field(p, name, lo, hi) for p, (name, lo, hi) in zip(parts, _FIELDS)]
    weekdays = {0 if d == 7 else d for d in parsed[4][0]}
    return (parsed[0][0], parsed[1][0], parsed[2][0], parsed[3][0], weekdays, parsed[2][1], parsed[4][1])


def _day_matches(day: date, spec: tuple) -> bool:
    _, _, days, months, weekdays, dom_any, dow_any = spec
    if day.month not in months:
        return False
    dom = day.day in days
    dow = (day.isoweekday() % 7) in weekdays
    if dom_any and dow_any:
        return True
    if dom_any:
        return dow
    if dow_any:
        return dom
    return dom or dow  # cron: both restricted means either


def last_fire(expr: str, now: datetime, zone=None) -> datetime | None:
    """The latest time at or before `now` the schedule fired, in the owner's
    zone (THROUGHLINE_TIMEZONE); None when it has not fired in ~13 months."""
    spec = parse_schedule(expr)
    zone = zone or owner_zone()
    local = now.astimezone(zone)
    minutes, hours = sorted(spec[0], reverse=True), sorted(spec[1], reverse=True)
    for back in range(0, 400):
        day = local.date() - timedelta(days=back)
        if not _day_matches(day, spec):
            continue
        for h in hours:
            if back == 0 and h > local.hour:
                continue
            for m in minutes:
                if back == 0 and h == local.hour and m > local.minute:
                    continue
                return datetime(day.year, day.month, day.day, h, m, tzinfo=zone)
    return None


# --- due --------------------------------------------------------------------------

CURSOR_RUNS = """coalesce(detail->>'mode', CASE WHEN trigger_kind IN ('rule','backfill') THEN 'other' ELSE 'cursor' END)='cursor'"""


def _recent_cursor_runs(conn, table_id: int, limit: int = 12) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            f"""SELECT id, status, trigger_kind, detail, started_at, finished_at
            FROM tl.table_runs WHERE table_id=%s AND {CURSOR_RUNS}
            ORDER BY id DESC LIMIT %s""",
            (table_id, limit),
        ).fetchall()
    ]


def _interrupted(run: dict[str, Any]) -> bool:
    status = run["status"]
    if status == "budget_exhausted":
        # The monthly cap lifts by itself (next month, or a raised cap); a
        # lens's own max_cost_per_run waits for its next scheduled fire.
        return (run.get("detail") or {}).get("reason") == "monthly_cap_reached"
    # 'running': either still going (run_lens answers 'busy' under the lens
    # lock and nothing happens) or left by a process that died.
    return status in RESUME


def backoff_s(runs: list[dict[str, Any]]) -> int:
    """Seconds to wait after the latest run: doubles per consecutive outage."""
    streak = 0
    for run in runs:
        if run["status"] in OUTAGE:
            streak += 1
        else:
            break
    if streak == 0:
        return 0
    return min(RETRY_MAX_S, RETRY_BASE_S * 2 ** (streak - 1))


def due(tdef: dict[str, Any], runs: list[dict[str, Any]], now: datetime) -> tuple[bool, str]:
    """(due, why) for one lens given its recent cursor runs (newest first)."""
    if not tdef.get("enabled"):
        return False, "disabled"
    if tdef.get("lazy_backfill"):
        return False, "lazy"
    latest = runs[0] if runs else None
    if latest is not None and _interrupted(latest):
        if latest["status"] == "running":
            age = (now - latest["started_at"]).total_seconds()
            return True, "resume_running" if age < STALE_RUNNING_S else "resume_stale"
        wait = backoff_s(runs)
        ended = latest.get("finished_at") or latest["started_at"]
        if wait and (now - ended).total_seconds() < wait:
            return False, f"backoff:{int(wait - (now - ended).total_seconds())}s"
        return True, f"resume_{latest['status']}"
    schedule = tdef.get("schedule")
    if not schedule:
        return False, "unscheduled"
    try:
        fired = last_fire(schedule, now)
    except LensSpecError as exc:
        return False, f"invalid_schedule: {exc}"
    if fired is None:
        return False, "not_fired"
    if latest is None or latest["started_at"] < fired:
        return True, "scheduled"
    return False, "up_to_date"


def endpoint_of(tdef: dict[str, Any]) -> str | None:
    """The model endpoint a lens calls (None for code workers and local)."""
    cfg = tdef.get("worker_config") or {}
    worker = tdef.get("worker")
    if worker == "jev":
        return str(cfg.get("endpoint") or JEV_ENDPOINT)
    if worker in ("llm", "agent", "librarian") and cfg.get("provider") != "local":
        return _resolve_llm_endpoint(cfg)[0]
    return None


# --- tick -------------------------------------------------------------------------


def _result_summary(res: dict[str, Any]) -> dict[str, Any]:
    out = {
        k: res.get(k)
        for k in ("run_id", "status", "reason", "considered", "processed", "written", "updated", "model_calls", "cost", "shared")
        if res.get(k) is not None
    }
    if res.get("errors"):
        out["errors"] = res["errors"][:3]
    return out


def tick(
    *,
    names: list[str] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    rules: bool = True,
) -> dict[str, Any]:
    """Run due lenses (dependency order), resume interrupted ones, retry queued
    rule work. One endpoint that answers provider_unavailable is not called
    again in the same tick: its other lenses are skipped as 'endpoint_down'.
    A lens whose dependency did not finish this tick waits for the next."""
    now = now or datetime.now(timezone.utc)
    with connect("app") as conn:
        if names:
            defs = {n: get_lens(n, conn=conn) for n in names}
        else:
            defs = {d["name"]: get_lens(d["name"], conn=conn) for d in list_lenses(conn=conn)}
        runs = {n: _recent_cursor_runs(conn, int(d["id"])) for n, d in defs.items()}
        cap_ok, spent, cap = check_monthly_cap(conn)

    out: dict[str, Any] = {"at": now.isoformat(), "lenses": [], "dry_run": dry_run}
    if rules and not dry_run:
        # Rules first: they may queue lens work this tick retries right away.
        out["rules"] = {k: v for k, v in evaluate_rules().items() if k in ("evaluated_rules", "matched", "notified")}
        out["retries"] = deliver_pending()

    down: set[str] = set()
    unfinished: set[str] = set()
    shared_totals = {"shared_calls": 0, "answers_given": 0, "answers_used": 0, "saved_tokens": 0, "rides": 0}
    for layer in _topo_layers(defs) if defs else []:
        # First decide who runs, so lenses of this layer that read the same
        # items with the same model can share calls (lenses.coalesce).
        plans: list[tuple[dict[str, Any], dict[str, Any], str | None]] = []
        members: dict[str, coalesce.Member] = {}
        for tdef in layer:
            name = tdef["name"]
            is_due, why = due(tdef, runs[name], now)
            entry: dict[str, Any] = {"lens": name, "due": is_due, "why": why}
            blocked = [d for d in tdef.get("depends_on") or [] if d in unfinished]
            endpoint = endpoint_of(tdef)
            if not is_due:
                pass
            elif blocked:
                entry.update(due=False, why="waiting_on:" + ",".join(blocked))
            elif endpoint and endpoint in down:
                entry.update(due=False, why="endpoint_down")
            elif endpoint and not cap_ok:
                entry.update(due=False, why=f"monthly_cap_reached ({spent:.2f}/{cap:.2f} USD)")
            plans.append((tdef, entry, endpoint))
            if coalesce.group_key(tdef) is None or dry_run:
                continue
            runs_now = bool(entry["due"])
            # Ride along only when the schedule simply has not fired yet: never
            # for a lens held back (backoff, outage, cap, dependencies) or one
            # that runs only by hand.
            may_ride = (
                not runs_now
                and why in ("up_to_date", "not_fired")
                and not blocked
                and cap_ok
                and coalesce.mode_of(tdef) == "ride"
            )
            if runs_now or may_ride:
                members[name] = coalesce.Member(tdef=tdef, due=runs_now, ride=may_ride)
        with coalesce.sharing(members) as share:
            for tdef, entry, endpoint in plans:
                name = tdef["name"]
                if entry["due"] and endpoint and endpoint in down:
                    entry.update(due=False, why="endpoint_down")
                elif entry["due"] and not dry_run:
                    res = run_lens(name, trigger_kind="schedule")
                    share.finished(name)
                    entry["result"] = _result_summary(res)
                    if res.get("status") == "provider_unavailable" and endpoint:
                        down.add(endpoint)
            # Riders run on the answers they were given (no model calls).
            for tdef, entry, endpoint in plans:
                name = tdef["name"]
                if entry["due"] or dry_run or not share.pending_answers(name):
                    continue
                res = run_lens(name, trigger_kind="schedule")
                share.finished(name)
                entry["rode_along"] = _result_summary(res)
                shared_totals["rides"] += 1
            for k in ("shared_calls", "answers_given", "answers_used", "saved_tokens"):
                shared_totals[k] += share.stats[k]
        for tdef, entry, endpoint in plans:
            name = tdef["name"]
            status = (entry.get("result") or {}).get("status")
            held_back = entry["why"].startswith(("backoff", "endpoint_down", "waiting_on", "monthly_cap"))
            if not dry_run and (held_back or (entry["due"] and status != "done")):
                unfinished.add(name)  # its dependents read a table that is behind
            out["lenses"].append(entry)
    if shared_totals["shared_calls"] or shared_totals["rides"]:
        out["shared"] = shared_totals
    out["ran"] = sum(1 for e in out["lenses"] if "result" in e)
    out["endpoints_down"] = sorted(down)
    return out
