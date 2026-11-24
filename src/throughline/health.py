"""Source health: is each data source up, since when is it down, and did it
catch back up (migration 0057, docs/v11/sources.md).

Two signals decide a source's state.

Runs. Every plugin run ends with a heartbeat (POST /v1/plugins/{name}/
heartbeat): success or a redacted error.

Arrivals. throughline.cadence learns when each channel usually gets new items
(hour of the week, in the owner's clock or UTC) and scores how unlikely the
current silence is. This catches sources whose runs succeed while nothing
arrives: a feed that only serves its newest items, an expired session that
answers with an empty page, an upstream job that stopped.

    ok       runs succeed and every channel's silence is ordinary
    failing  the last run failed, but data is still recent (fewer than
             THROUGHLINE_SOURCE_DOWN_AFTER failures in a row, default 3)
    stalled  runs succeed (or fail only briefly), but a channel is overdue:
             a silence this long is very unlikely given its usual pattern
    down     no success within twice the interval, or that many failures in
             a row; kind `silent` when no run reported at all (the scheduler
             or the importer stopped), `failing` when runs keep failing
    never    registered but never succeeded
    paused   switched off on purpose (lib sources pause); never alerts

Outages (tl.source_outages, one open row per source) open when a source
becomes down or stalled, from a failed heartbeat or from sweep(), which
notices silent outages and stalls by the clock. Each records when the data
was last known good. An outage closes when the source is healthy again (its
runs succeed and its channels are no longer overdue): restored_at, how many
messages now fill the window since the data was last good, the newest data,
and whether the catch-up was complete. Plugins resume from their stored
cursor, so the first good run normally ingests everything the source still
holds; a run that hits a source cap reports partial coverage or a gap, and
the outage is closed with catch_up 'partial' and those reasons.

check() is the alarm: it sweeps, and with an alert command configured
(THROUGHLINE_ALERT_CMD, no shell, placeholders {plugin} {state} {title}
{message}) runs it once when a source goes down or stalls and once when it
is back.
"""

import logging
import os
from datetime import datetime, timezone

from . import cadence

STATES = ("ok", "failing", "stalled", "down", "never", "paused")
# Worst first, for summaries and the board.
SEVERITY = ("down", "stalled", "never", "failing", "paused", "ok")
# A source that only registered (or ran --dry-run) and never ran is shown,
# not alarmed; one that ran and failed is down.
PROBLEMS = ("down", "stalled")
OUTAGE_STATES = ("down", "stalled")
# Coverage reasons that describe the outage itself, not the catch-up.
OUTAGE_REASONS = {"stale", "run_failed", "not_reported"}
ERROR_LIMIT = 500

log = logging.getLogger(__name__)


def down_after():
    try:
        return max(1, int(os.getenv("THROUGHLINE_SOURCE_DOWN_AFTER") or 3))
    except ValueError:
        return 3


def _now(conn):
    return conn.execute("SELECT now() AS at").fetchone()["at"]


# --- heartbeats ------------------------------------------------------------------


def beat(conn, name, data):
    """Record one run's outcome and move the outage state. Returns the
    tl.plugins row (as the heartbeat route always has)."""
    data = data if isinstance(data, dict) else {}
    error = data.get("error")
    error = None if error is None else str(error)[:ERROR_LIMIT]
    interval = data.get("interval_s")
    try:
        interval = None if interval is None else max(1, int(interval))
    except (TypeError, ValueError):
        interval = None
    try:
        delivered = max(0, int(data.get("messages_ingested") or 0))
    except (TypeError, ValueError):
        delivered = 0
    previous = conn.execute(
        "SELECT last_attempt_at FROM tl.plugins WHERE name=%s FOR UPDATE", (name,)
    ).fetchone()
    row = conn.execute(
        """INSERT INTO tl.plugins(name,interval_s,last_attempt_at,last_success_at,last_error,
                                  last_error_at,consecutive_failures,messages_ingested)
           VALUES (%(name)s,coalesce(%(interval)s,3600),now(),
                   CASE WHEN %(error)s::text IS NULL THEN now() END,%(error)s,
                   CASE WHEN %(error)s::text IS NOT NULL THEN now() END,
                   CASE WHEN %(error)s::text IS NULL THEN 0 ELSE 1 END,%(delivered)s)
           ON CONFLICT(name) DO UPDATE SET
             -- a failure heartbeat carries no interval: keep the known one
             interval_s=coalesce(%(interval)s,tl.plugins.interval_s),
             last_attempt_at=now(),
             last_success_at=coalesce(EXCLUDED.last_success_at,tl.plugins.last_success_at),
             last_error=EXCLUDED.last_error,
             last_error_at=coalesce(EXCLUDED.last_error_at,tl.plugins.last_error_at),
             consecutive_failures=CASE WHEN %(error)s::text IS NULL THEN 0
                                       ELSE tl.plugins.consecutive_failures+1 END,
             messages_ingested=tl.plugins.messages_ingested+EXCLUDED.messages_ingested,
             updated_at=now()
           RETURNING *""",
        dict(name=name, interval=interval, error=error, delivered=delivered),
    ).fetchone()
    # A failure only needs the run signal; a success may end an outage, which
    # needs the arrivals too (a run that succeeds with nothing new is no fix).
    sweep(
        conn,
        [name],
        arrivals=error is None,
        delivered=delivered,
        run_after=previous and previous["last_attempt_at"],
    )
    return row


# --- state -----------------------------------------------------------------------

_ROWS = """
SELECT p.name,p.interval_s,p.last_attempt_at,p.last_success_at,p.last_error,p.last_error_at,
       p.consecutive_failures,p.paused_at,p.paused_reason,p.alerted_state,p.messages_ingested,
       (SELECT max(c.high_water_at) FROM tl.coverage c WHERE c.plugin=p.name) AS newest_at,
       (SELECT count(*) FROM tl.coverage_gaps g WHERE g.plugin=p.name AND g.closed_at IS NULL) AS open_gaps
FROM tl.plugins p
"""


def classify(row, now, failures_down=None):
    """(state, kind) of one tl.plugins row from its runs alone. kind is
    'silent' or 'failing' for down sources, else None."""
    failures_down = failures_down or down_after()
    if row.get("paused_at") is not None:
        return "paused", None
    interval = max(int(row.get("interval_s") or 3600), 1)
    success = row.get("last_success_at")
    error_at = row.get("last_error_at")
    erroring = error_at is not None and (success is None or error_at > success)
    if success is None:
        return ("down", "failing") if erroring else ("never", None)
    if (now - success).total_seconds() > 2 * interval:
        recent_error = erroring and (now - error_at).total_seconds() <= 2 * interval
        return "down", "failing" if recent_error else "silent"
    if erroring and int(row.get("consecutive_failures") or 0) >= failures_down:
        return "down", "failing"
    if erroring:
        return "failing", None
    return "ok", None


def _arrivals(conn, names):
    """{plugin: [cadence entries]}; {} (and a warning) when the cadence
    statement fails, so a health read never fails on it."""
    try:
        with conn.transaction():
            rows = cadence.channels(conn, names)
    except Exception as error:  # degrade, never fail the health read
        log.warning("cadence unavailable: %s", type(error).__name__)
        return None
    out = {}
    for row in rows:
        out.setdefault(row["plugin"], []).append(row)
    return out


def _outages(conn, names):
    rows = conn.execute(
        """SELECT DISTINCT ON (plugin,restored_at IS NULL) *
           FROM tl.source_outages WHERE plugin=ANY(%s)
           ORDER BY plugin,restored_at IS NULL,restored_at DESC NULLS LAST,id DESC""",
        (list(names),),
    ).fetchall()
    current, last = {}, {}
    for row in rows:
        (current if row["restored_at"] is None else last)[row["plugin"]] = row
    return current, last


def evaluate(conn, names=None, *, arrivals=True):
    """Every source (of names) with its state; no writes."""
    now = _now(conn)
    statement = _ROWS + (" WHERE p.name=ANY(%s)" if names else "") + " ORDER BY p.name"
    rows = conn.execute(statement, (list(names),) if names else ()).fetchall()
    found = [r["name"] for r in rows]
    channels = _arrivals(conn, found) if arrivals and found else None
    current, last = _outages(conn, found) if found else ({}, {})
    out = []
    for row in rows:
        state, kind = classify(row, now)
        own = None if channels is None else channels.get(row["name"], [])
        overdue = [
            c for c in own or [] if c["verdict"] == "overdue" and not c["paused"]
        ]
        if overdue and state in ("ok", "failing"):
            state, kind = "stalled", "stalled"
        elif (
            own is None
            and state in ("ok", "failing")
            and (current.get(row["name"]) or {}).get("kind") == "stalled"
        ):
            # Arrivals not checked (a failure heartbeat, or the cadence
            # statement failed): an open stall stands until proven over.
            state, kind = "stalled", "stalled"
        success = row["last_success_at"]
        erroring = row["last_error_at"] is not None and (
            success is None or row["last_error_at"] > success
        )
        entry = dict(
            name=row["name"],
            state=state,
            kind=kind,
            interval_s=row["interval_s"],
            last_attempt_at=row["last_attempt_at"],
            last_success_at=success,
            age_s=None
            if success is None
            else round((now - success).total_seconds(), 1),
            newest_at=row["newest_at"],
            last_error=row["last_error"] if erroring else None,
            consecutive_failures=row["consecutive_failures"],
            open_gaps=row["open_gaps"],
            paused_at=row["paused_at"],
            paused_reason=row["paused_reason"],
            alerted_state=row["alerted_state"],
            arrivals_checked=own is not None,
            overdue=[c["channel"] for c in overdue],
            channels=own or [],
            outage=current.get(row["name"]),
            last_outage=last.get(row["name"]),
        )
        entry["note"] = note(entry, now)
        out.append(entry)
    return out


def sources(conn, names=None, *, record=True):
    """Every source with its state, ages, channels and outage. With record
    (the default) outages noticed now (silent, stalled) are written first."""
    if record:
        return sweep(conn, names)
    return evaluate(conn, names)


def summary(entries):
    counts = {state: 0 for state in STATES}
    for entry in entries:
        counts[entry["state"]] += 1
    worst = next((s for s in SEVERITY if counts[s]), "ok")
    problems = sorted(e["name"] for e in entries if e["state"] in PROBLEMS)
    return dict(counts=counts, worst=worst, problems=problems, ok=not problems)


def _last_good(entry):
    if entry["state"] == "stalled":
        times = [
            c["last_item_at"]
            for c in entry["channels"]
            if c["channel"] in entry["overdue"] and c["last_item_at"] is not None
        ]
        return min(times) if times else entry["last_success_at"]
    return entry["last_success_at"]


def sweep(conn, names=None, *, arrivals=True, delivered=None, run_after=None):
    """Bring tl.source_outages in line with the current states: open an
    outage for every down or stalled source without one, keep open ones
    current, and (when arrivals were checked) close the outage of every
    source that is healthy again. Returns the evaluated entries."""
    entries = evaluate(conn, names, arrivals=arrivals)
    for entry in entries:
        outage = entry["outage"]
        state = entry["state"]
        error = entry["last_error"]
        if state in OUTAGE_STATES:
            if outage is None:
                entry["outage"] = (
                    conn.execute(
                        """INSERT INTO tl.source_outages(plugin,kind,last_good_at,first_error,last_error,failures)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (plugin) WHERE restored_at IS NULL DO NOTHING RETURNING *""",
                        (
                            entry["name"],
                            entry["kind"] or "failing",
                            _last_good(entry),
                            error,
                            error,
                            entry["consecutive_failures"] if error else 0,
                        ),
                    ).fetchone()
                    # another sweep opened it first: show that one
                    or conn.execute(
                        "SELECT * FROM tl.source_outages WHERE plugin=%s AND restored_at IS NULL",
                        (entry["name"],),
                    ).fetchone()
                )
            else:
                kind = entry["kind"] or outage["kind"]
                # Failing runs are the worse news; a stall does not hide them.
                if outage["kind"] == "failing" and kind != "failing" and error:
                    kind = "failing"
                entry["outage"] = conn.execute(
                    """UPDATE tl.source_outages SET kind=%s,
                              last_error=coalesce(%s,last_error),
                              first_error=coalesce(first_error,%s),
                              failures=greatest(failures,%s)
                       WHERE id=%s RETURNING *""",
                    (kind, error, error, entry["consecutive_failures"], outage["id"]),
                ).fetchone()
        elif (
            outage is not None
            and entry["arrivals_checked"]
            and state in ("ok", "failing")
        ):
            entry["last_outage"] = _restore(conn, entry, outage, delivered, run_after)
            entry["outage"] = None
        entry["note"] = note(entry, _now(conn))
    return entries


def _restore(conn, entry, outage, delivered, run_after):
    """Close an outage of a source that is healthy again."""
    name = entry["name"]
    reasons = set()
    # Coverage reported since the outage was noticed (the catch-up runs).
    for report in conn.execute(
        """SELECT normalized FROM tl.coverage_reports
           WHERE plugin=%s AND at>=least(coalesce(%s::timestamptz,'infinity'),%s::timestamptz)""",
        (name, run_after, outage["detected_at"]),
    ):
        normalized = report["normalized"] or {}
        if normalized.get("status") == "partial":
            reasons.update(normalized.get("reasons") or ["partial"])
    reasons -= OUTAGE_REASONS
    gaps = conn.execute(
        """SELECT count(*) AS n FROM tl.coverage_gaps
           WHERE plugin=%s AND closed_at IS NULL AND gap_to>=coalesce(%s::timestamptz,'-infinity')""",
        (name, outage["last_good_at"]),
    ).fetchone()["n"]
    if gaps:
        reasons.add("open_gaps")
    if outage["last_good_at"] is not None:
        filled = conn.execute(
            """SELECT count(*) AS n FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id
               WHERE c.plugin=%s AND m.superseded_by IS NULL
                 AND m.happened_at>%s AND m.happened_at<=now()""",
            (name, outage["last_good_at"]),
        ).fetchone()["n"]
    else:
        filled = delivered or 0
    newest = [c["last_item_at"] for c in entry["channels"] if c["last_item_at"]]
    return conn.execute(
        """UPDATE tl.source_outages SET restored_at=greatest(now(),detected_at),caught_up_messages=%s,
                  caught_up_to=%s,catch_up=%s,catch_up_reasons=%s
           WHERE id=%s AND restored_at IS NULL RETURNING *""",
        (
            filled,
            max(newest) if newest else entry["newest_at"],
            "partial" if reasons else "caught_up",
            sorted(reasons),
            outage["id"],
        ),
    ).fetchone()


def outages(conn, name=None, limit=50):
    rows = conn.execute(
        "SELECT * FROM tl.source_outages"
        + (" WHERE plugin=%s" if name else "")
        + " ORDER BY detected_at DESC,id DESC LIMIT %s",
        ((name, limit) if name else (limit,)),
    ).fetchall()
    now = _now(conn)
    for row in rows:
        start = row["last_good_at"] or row["detected_at"]
        row["duration_s"] = round(
            ((row["restored_at"] or now) - start).total_seconds(), 1
        )
    return rows


def pause(conn, name, reason=None):
    """Pause a source (plugin name) or one of its channels (channel name)."""
    row = conn.execute(
        "UPDATE tl.plugins SET paused_at=coalesce(paused_at,now()),paused_reason=%s,updated_at=now() WHERE name=%s RETURNING name,paused_at,paused_reason",
        (reason, name),
    ).fetchone()
    if row is not None:
        return dict(row, kind="source")
    row = conn.execute(
        """INSERT INTO tl.channel_pauses(channel_id,reason)
           SELECT id,%s FROM tl.channels WHERE name=%s
           ON CONFLICT (channel_id) DO UPDATE SET reason=EXCLUDED.reason
           RETURNING channel_id,paused_at,reason""",
        (reason, name),
    ).fetchone()
    if row is None:
        raise LookupError(f"unknown source or channel {name}")
    return dict(
        name=name,
        paused_at=row["paused_at"],
        paused_reason=row["reason"],
        kind="channel",
    )


def resume(conn, name):
    row = conn.execute(
        "UPDATE tl.plugins SET paused_at=NULL,paused_reason=NULL,updated_at=now() WHERE name=%s RETURNING name",
        (name,),
    ).fetchone()
    if row is not None:
        return dict(row, kind="source")
    channel = conn.execute(
        "SELECT id FROM tl.channels WHERE name=%s", (name,)
    ).fetchone()
    if channel is None:
        raise LookupError(f"unknown source or channel {name}")
    conn.execute("DELETE FROM tl.channel_pauses WHERE channel_id=%s", (channel["id"],))
    return dict(name=name, kind="channel")


# --- words -----------------------------------------------------------------------


def duration(seconds):
    """'3d 4h', '2h 5m', '12m', '40s'."""
    if seconds is None:
        return "-"
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _local(at):
    return at.astimezone(cadence.zone()).strftime("%a %b %d %H:%M")


def channel_note(channel):
    """One sentence about a channel's arrivals."""
    verdict = channel["verdict"]
    if verdict == "no_data":
        return "no items yet"
    silence = duration(channel["silence_s"])
    if verdict == "learning":
        return f"last item {silence} ago; learning its pattern ({channel['history_days']} days of history so far)"
    usual = ", ".join(channel["usual"])
    parts = [f"no new item for {silence}"]
    if verdict in ("overdue", "quiet"):
        reasons = []
        if channel["p_silence"] is not None and channel["p_silence"] < 0.5:
            reasons.append(
                f"a silence this long is {cadence.odds(channel['p_silence'])} for its usual pattern"
            )
        if channel.get("gap_ratio") and channel["gap_ratio"] > 1:
            reasons.append(
                f"{channel['gap_ratio']:g}x its longest silence in 12 weeks ({duration(channel['longest_gap_s'])})"
            )
        parts.append("; ".join(reasons) or "unusual for its pattern")
        if verdict == "overdue" and channel["overdue_at"]:
            parts.append(f"overdue since {_local(channel['overdue_at'])}")
        elif channel["overdue_at"]:
            parts.append(
                f"overdue at {_local(channel['overdue_at'])} if nothing arrives"
            )
    elif channel["overdue_at"]:
        parts.append(f"would be overdue at {_local(channel['overdue_at'])}")
    if usual:
        parts.append(f"busiest {usual}")
    if channel.get("paused"):
        parts.append(
            "paused"
            + (f": {channel['paused_reason']}" if channel["paused_reason"] else "")
        )
    return "; ".join(parts)


def note(entry, now):
    """One plain sentence about a source."""
    every = duration(entry["interval_s"])
    state = entry["state"]
    if state == "paused":
        reason = f": {entry['paused_reason']}" if entry["paused_reason"] else ""
        return (
            f"paused {duration((now - entry['paused_at']).total_seconds())} ago{reason}"
        )
    if state == "never":
        return "registered but has never completed a run"
    if state == "down":
        outage = entry["outage"] or {}
        if entry["last_success_at"] is None:
            text = "has never completed a run; it keeps failing"
        elif entry["kind"] == "silent":
            text = (
                f"no run has reported for {duration(entry['age_s'])} "
                f"(expected every {every}): is its timer or service running?"
            )
        else:
            text = (
                f"failing for {duration(entry['age_s'])}, "
                f"{entry['consecutive_failures']} failed runs in a row"
            )
        error = entry["last_error"] or outage.get("last_error")
        if error and entry["kind"] != "silent":
            text += f"; last error: {error[:160]}"
        return text
    if state == "stalled":
        overdue = [c for c in entry["channels"] if c["channel"] in entry["overdue"]]
        if not overdue:
            since = (entry["outage"] or {}).get("last_good_at")
            return (
                "stalled"
                + (f": no new items since {_local(since)}" if since else "")
                + " (arrivals not re-checked just now)"
            )
        first = overdue[0]
        text = f"runs succeed, but {first['channel']}: {channel_note(first)}"
        if len(overdue) > 1:
            text += (
                f" (+{len(overdue) - 1} more overdue: "
                + ", ".join(c["channel"] for c in overdue[1:])
                + ")"
            )
        return text
    if state == "failing":
        return (
            f"last run failed ({entry['consecutive_failures']} in a row), "
            f"data still recent: {(entry['last_error'] or '')[:160]}"
        )
    last = entry["last_outage"]
    if (
        last
        and last.get("restored_at")
        and (now - last["restored_at"]).total_seconds() < 7 * 86400
    ):
        start = last["last_good_at"] or last["detected_at"]
        text = (
            f"restored {duration((now - last['restored_at']).total_seconds())} ago after "
            f"{duration((last['restored_at'] - start).total_seconds())} {'stalled' if last['kind'] == 'stalled' else 'down'}; "
            f"caught up {last.get('caught_up_messages') or 0} messages"
        )
        if last.get("catch_up") == "partial":
            text += " (partial: " + ", ".join(last.get("catch_up_reasons") or []) + ")"
        return text
    quiet = [
        c["channel"]
        for c in entry["channels"]
        if c["verdict"] == "quiet" and not c["paused"]
    ]
    text = f"last success {duration(entry['age_s'])} ago (every {every})"
    if quiet:
        text += "; quieter than usual: " + ", ".join(quiet)
    return text


# --- the alarm -------------------------------------------------------------------


def _alert_class(state):
    if state in PROBLEMS:
        return "down"
    if state == "paused":
        return "paused"
    return "ok"


def _alert(entry, previous):
    """(title, message) to send for this transition, or None."""
    current = _alert_class(entry["state"])
    if current == previous:
        return None
    if current == "down":
        verb = "has stalled" if entry["state"] == "stalled" else "is down"
        return (
            f"Throughline: {entry['name']} {verb}",
            f"{entry['name']}: {entry['note']}",
        )
    if current == "ok" and previous == "down":
        return (
            f"Throughline: {entry['name']} is back",
            f"{entry['name']}: {entry['note']}",
        )
    return None


def check(conn, *, notify=None, command=None):
    """Sweep, then alert on transitions. notify defaults to whether an alert
    command is configured. Returns {summary, sources, alerts, now}."""

    entries = sources(conn)
    # Commit the sweep before running any alert command: heartbeats wait on
    # these rows, and an alert may take up to a minute.
    conn.commit()
    command = command if command is not None else os.getenv("THROUGHLINE_ALERT_CMD")
    notify = bool(command) if notify is None else notify
    # One alarm at a time: a second check running meanwhile reports the
    # states but sends nothing, so no alert goes out twice.
    locked = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext('throughline.source_alarm')) AS ok"
    ).fetchone()["ok"]
    conn.commit()
    try:
        alerts = _send(conn, entries, notify and locked, command)
    finally:
        if locked:
            conn.execute(
                "SELECT pg_advisory_unlock(hashtext('throughline.source_alarm'))"
            )
            conn.commit()
    if not locked:
        alerts.append(dict(plugin=None, title="another check is alerting", sent=False))
    return dict(
        summary=summary(entries),
        sources=entries,
        alerts=alerts,
        now=datetime.now(timezone.utc),
    )


def _send(conn, entries, notify, command):
    """Run the alert command for each transition; remember what was sent."""
    from . import ops

    alerts = []
    for entry in entries:
        current = _alert_class(entry["state"])
        previous = entry["alerted_state"]
        message = _alert(entry, previous)
        if message is None:
            remember = current != previous
        elif notify and command:
            title, text = message
            try:
                ops.run_command(
                    ops.command_args(
                        command,
                        plugin=entry["name"],
                        state=entry["state"],
                        title=title,
                        message=text,
                    ),
                    step="alert",
                    timeout=60,
                )
            except ops.OpsError as error:
                # Not remembered: the next check sends it again.
                remember = False
                alerts.append(
                    dict(
                        plugin=entry["name"], title=title, sent=False, error=str(error)
                    )
                )
            else:
                remember = True
                alerts.append(
                    dict(plugin=entry["name"], title=title, message=text, sent=True)
                )
        else:
            # No notifier: report it, and keep it pending for when one is set.
            remember = False
            alerts.append(
                dict(
                    plugin=entry["name"],
                    title=message[0],
                    message=message[1],
                    sent=False,
                )
            )
        if remember:
            conn.execute(
                "UPDATE tl.plugins SET alerted_state=%s WHERE name=%s",
                (current, entry["name"]),
            )
            conn.commit()
            entry["alerted_state"] = current
    return alerts
