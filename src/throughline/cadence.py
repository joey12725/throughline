"""Arrival-cadence anomaly detection: is a channel's silence unusual?

A source can look healthy (its runs succeed) while nothing new arrives: a
feed that serves only its newest items, an expired session that returns an
empty page, an upstream job that stopped. This module learns, per channel,
when new items usually appear and scores how unlikely the current silence is.

Model. Over the HISTORY_WEEKS (12) weeks before a channel's newest item,
every hour of the week (7 days x 24 hours) gets p = the share of its occurrences that had at least one
item. Counting active hours rather than items keeps bursts (a conversation,
a batch upload) from dominating. Sparse slots borrow strength: p(slot) is
shrunk toward p(hour of day), which is shrunk toward the channel's overall
rate (PRIOR_WEIGHT pseudo-occurrences each), and capped at P_CAP so one
missed hour is never proof. The grid is fitted twice, in the owner's zone
(THROUGHLINE_TIMEZONE or the system zone) and in UTC, and the one that
explains the history better is kept: people follow local time, while
scheduled jobs often run on UTC, whose local hour moves with daylight saving.

A second, model-free test compares the silence with the longest one in the
history: longer than any is quiet, GAP_OVERDUE (2) times the longest is
overdue (with at least 4 weeks and 20 gaps of history). It catches regular
sources whose items jitter across neighbouring hours.

The silence runs from the newest item to now minus the expected delivery
delay (the plugin's interval plus the channel's delivery lag: per recent import
run, how old the newest item it brought was; the lower quartile over the
last ten runs, so a bulk import of old history does not inflate it; with
fewer than three runs the lag cannot be told from the silence and is zero). Treating
hours as independent, P(silence) = product of (1 - p(slot)) over those hours.
That is small when the silence covers many hours in which the channel is
normally active: three missed weekday mornings of a daily digest, or an
evening without a message on a chat that is busy every evening. A quiet
weekend on a work channel stays likely.

Hours are not really independent for people (a whole quiet day or a
holiday), so the product alone overstates how rare a quiet stretch is. The
score is therefore calibrated against the channel's own history: every past
silence is scored the same way, and the current one must be rarer than all
of them before it counts.

Verdicts: learning (under MIN_SPAN_DAYS of history or MIN_ACTIVE_HOURS
active hours), normal, quiet (rarer than every past silence and P below
THROUGHLINE_CADENCE_QUIET_P, default 0.01, or longer than the longest past
silence: shown, never alerted) and overdue (rarer than every past silence
and P below THROUGHLINE_CADENCE_OVERDUE_P, default 0.001, or GAP_OVERDUE
times the longest past silence: the source is likely down). Each answer says when the
channel would become overdue if nothing arrives.
"""

import math
import os
from datetime import datetime, timedelta, timezone

HISTORY_WEEKS = 12
MIN_SPAN_DAYS = 14
MIN_ACTIVE_HOURS = 12
PRIOR_WEIGHT = 2.0
P_CAP = 0.95
LAG_CAP_S = 3 * 86400
LOOKAHEAD_HOURS = 28 * 24
# A silence this many times the longest one in the history is overdue,
# given at least GAP_MIN_DAYS of history with GAP_MIN_COUNT gaps.
GAP_OVERDUE = 2.0
GAP_MIN_DAYS = 28
GAP_MIN_COUNT = 20
HOUR = timedelta(hours=1)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _env_p(name, default):
    try:
        value = float(os.getenv(name) or default)
    except ValueError:
        return default
    return value if 0 < value < 1 else default


def thresholds():
    return _env_p("THROUGHLINE_CADENCE_QUIET_P", 0.01), _env_p(
        "THROUGHLINE_CADENCE_OVERDUE_P", 0.001
    )


def zone():
    """The owner's zone for day-of-week and hour: THROUGHLINE_TIMEZONE (or
    its deprecated alias THROUGHLINE_OWNER_TZ), else TZ, else the system zone
    (/etc/localtime), as a real zone with its daylight-saving rules. A fixed
    UTC offset is only the last resort."""
    from zoneinfo import ZoneInfo

    from . import tz

    for name in (tz.name(), os.getenv("TZ")):
        if name:
            try:
                return ZoneInfo(name.lstrip(":"))
            except Exception:
                pass
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in target:
            return ZoneInfo(target.split(marker, 1)[1])
    except Exception:
        pass
    return datetime.now().astimezone().tzinfo


def _slot(at, tz):
    local = at.astimezone(tz)
    return local.weekday() * 24 + local.hour, local.hour


def _hour(at):
    return at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


class Profile:
    """Weekly activity profile of one channel, learned from active hours."""

    def __init__(self, active_hours, start, end, tz):
        self.tz = tz
        self.start = _hour(start)
        self.end = _hour(end)
        active = {_hour(h) for h in active_hours}
        occ = [0] * 168
        hits = [0] * 168
        occ_hod = [0] * 24
        hits_hod = [0] * 24
        at = self.start
        while at <= self.end:
            slot, hod = _slot(at, tz)
            occ[slot] += 1
            occ_hod[hod] += 1
            if at in active:
                hits[slot] += 1
                hits_hod[hod] += 1
            at += HOUR
        total = sum(occ)
        self.active_hours = sum(hits)
        self.total_hours = total
        overall = self.active_hours / total if total else 0.0
        w = PRIOR_WEIGHT
        p_hod = [(hits_hod[h] + w * overall) / (occ_hod[h] + w) for h in range(24)]
        self.p = [
            min(P_CAP, (hits[s] + w * p_hod[s % 24]) / (occ[s] + w)) for s in range(168)
        ]
        self.overall = overall
        # In-sample Bernoulli log-likelihood per hour: how well this zone's
        # weekly grid explains the history (used to pick UTC or local).
        self.fit = (
            sum(
                hits[x] * math.log(max(self.p[x], 1e-9))
                + (occ[x] - hits[x]) * math.log(max(1 - self.p[x], 1e-9))
                for x in range(168)
            )
            / total
            if total
            else 0.0
        )
        ordered = sorted(active)
        gaps = [(b - a).total_seconds() for a, b in zip(ordered, ordered[1:])]
        self.gaps = len(gaps)
        self.longest_gap_s = max(gaps) if gaps else None
        # The most unlikely silence the channel actually had in its history,
        # scored by the same grid: a current silence must be rarer than all
        # of them before it is overdue (hours are not independent for human
        # sources, so the raw product alone overstates how rare a quiet
        # stretch is).
        self.worst_gap_log_p = min(
            (self.log_p_silence(a, b) for a, b in zip(ordered, ordered[1:])),
            default=0.0,
        )

    @property
    def span_days(self):
        return (self.end - self.start).total_seconds() / 86400

    def learning(self):
        return self.span_days < MIN_SPAN_DAYS or self.active_hours < MIN_ACTIVE_HOURS

    def log_p_silence(self, after, until):
        """log P(no item in any whole hour after `after`'s hour up to until),
        with the last partial hour weighted by its fraction."""
        total = 0.0
        at = _hour(after) + HOUR
        while at + HOUR <= until:
            total += math.log1p(-self.p[_slot(at, self.tz)[0]])
            at += HOUR
        if at < until:
            frac = (until - at).total_seconds() / 3600
            total += frac * math.log1p(-self.p[_slot(at, self.tz)[0]])
        return total

    def crossing(self, after, start, log_p, threshold):
        """First time from start at which the silence since `after` drops
        below threshold, if nothing arrives (None beyond the lookahead)."""
        target = math.log(threshold)
        if log_p < target:
            return start
        at = max(_hour(start), _hour(after) + HOUR)
        if at < start:
            # finish the current partial hour first
            step = math.log1p(-self.p[_slot(at, self.tz)[0]])
            remaining = 1 - (start - at).total_seconds() / 3600
            if log_p + remaining * step < target:
                return start + timedelta(hours=remaining * (target - log_p) / step)
            log_p += remaining * step
            at += HOUR
        for _ in range(LOOKAHEAD_HOURS):
            step = math.log1p(-self.p[_slot(at, self.tz)[0]])
            if log_p + step < target:
                return at + timedelta(hours=(target - log_p) / step if step else 1)
            log_p += step
            at += HOUR
        return None

    def busiest(self, n=3):
        """The n most active slots in the owner's clock, e.g. ['Mon 09h']."""
        ranked = sorted(range(168), key=lambda s: -self.p[s])[:n]
        if self.tz is timezone.utc:
            # Name UTC slots by today's local hour.
            base = datetime(2024, 1, 1, tzinfo=timezone.utc)  # a Monday
            local = zone()
            return [
                (base + timedelta(hours=s)).astimezone(local).strftime("%a %Hh")
                for s in ranked
            ]
        return [f"{DAYS[s // 24]} {s % 24:02d}h" for s in ranked]


_CHANNELS_TEMPLATE = """
SELECT c.id,c.name,c.plugin,coalesce(p.interval_s,3600) AS interval_s,l.at AS last_at,
       (SELECT CASE WHEN count(*)>=MIN_RUNS THEN percentile_disc(0.25) WITHIN GROUP (ORDER BY r.lag) END
          FROM (SELECT min(extract(epoch FROM m.ingested_at-m.happened_at)) AS lag
                  FROM (SELECT m.ingested_at,m.happened_at FROM tl.messages m
                         WHERE m.channel_id=c.id AND m.superseded_by IS NULL AND m.happened_at<=now()
                         ORDER BY m.happened_at DESC LIMIT 200) m
                 GROUP BY date_trunc('hour',m.ingested_at)
                 ORDER BY date_trunc('hour',m.ingested_at) DESC LIMIT 10) r) AS lag_s,
       cp.reason AS paused_reason,cp.channel_id IS NOT NULL AS paused
FROM tl.channels c
LEFT JOIN tl.plugins p ON p.name=c.plugin
LEFT JOIN tl.channel_pauses cp ON cp.channel_id=c.id
CROSS JOIN LATERAL (SELECT max(m.happened_at) AS at FROM tl.messages m
                     WHERE m.channel_id=c.id AND m.superseded_by IS NULL AND m.happened_at<=now()) l
WHERE NOT c.is_reference
"""
# Fewer import runs than this cannot tell delivery lag from silence (one
# bulk import of a source that has since died): the lag is taken as zero.
MIN_RUNS = 3
_CHANNELS = _CHANNELS_TEMPLATE.replace("MIN_RUNS", str(MIN_RUNS))

_HOURS = """
SELECT m.channel_id,date_trunc('hour',m.happened_at) AS h
FROM tl.messages m
JOIN unnest(%s::bigint[],%s::timestamptz[]) AS w(id,last_at) ON m.channel_id=w.id
WHERE m.superseded_by IS NULL
  AND m.happened_at>w.last_at-make_interval(days=>%s) AND m.happened_at<=w.last_at
GROUP BY 1,2
"""


def channels(conn, plugins=None, *, now=None):
    """Cadence verdict of every non-reference channel (of these plugins)."""
    quiet_p, overdue_p = thresholds()
    tz = zone()
    statement = (
        _CHANNELS
        + (" AND c.plugin=ANY(%s)" if plugins else "")
        + " ORDER BY c.plugin,c.name"
    )
    rows = conn.execute(statement, (list(plugins),) if plugins else ()).fetchall()
    now = now or conn.execute("SELECT now() AS at").fetchone()["at"]
    with_data = [r for r in rows if r["last_at"] is not None]
    hours = {}
    if with_data:
        for h in conn.execute(
            _HOURS,
            (
                [r["id"] for r in with_data],
                [r["last_at"] for r in with_data],
                HISTORY_WEEKS * 7,
            ),
        ):
            hours.setdefault(h["channel_id"], []).append(h["h"])
    out = []
    for row in rows:
        entry = dict(
            channel=row["name"],
            plugin=row["plugin"],
            last_item_at=row["last_at"],
            silence_s=None
            if row["last_at"] is None
            else round((now - row["last_at"]).total_seconds(), 1),
            verdict="no_data",
            p_silence=None,
            overdue_at=None,
            expected_delay_s=None,
            history_days=0.0,
            usual=[],
            clock=None,
            longest_gap_s=None,
            worst_past_p=None,
            gap_ratio=None,
            paused=bool(row["paused"]),
            paused_reason=row["paused_reason"],
        )
        out.append(entry)
        if row["last_at"] is None:
            continue
        active = hours.get(row["id"], [])
        start = min(active) if active else row["last_at"]
        # Human sources follow the owner's clock, scheduled jobs often UTC
        # (whose local hour moves with daylight saving): keep the better fit.
        profile = max(
            (Profile(active, start, row["last_at"], z) for z in (tz, timezone.utc)),
            key=lambda candidate: candidate.fit,
        )
        entry["clock"] = "utc" if profile.tz is timezone.utc else "local"
        entry["history_days"] = round(profile.span_days, 1)
        if profile.learning():
            entry["verdict"] = "learning"
            continue
        lag = min(max(float(row["lag_s"] or 0), 0.0), LAG_CAP_S)
        delay = timedelta(seconds=int(row["interval_s"]) + lag)
        until = now - delay
        log_p = (
            profile.log_p_silence(row["last_at"], until)
            if until > row["last_at"]
            else 0.0
        )
        p = math.exp(log_p)
        silence = max(0.0, (until - row["last_at"]).total_seconds())
        longest = profile.longest_gap_s
        ratio = (
            silence / longest
            if longest
            and profile.span_days >= GAP_MIN_DAYS
            and profile.gaps >= GAP_MIN_COUNT
            else None
        )
        entry["p_silence"] = p
        entry["expected_delay_s"] = round(delay.total_seconds(), 1)
        entry["longest_gap_s"] = None if longest is None else round(longest, 1)
        entry["gap_ratio"] = None if ratio is None else round(ratio, 2)
        entry["usual"] = profile.busiest()
        worst = profile.worst_gap_log_p
        entry["worst_past_p"] = math.exp(worst)
        rarer = log_p < worst
        overdue = (rarer and p < overdue_p) or (
            ratio is not None and ratio >= GAP_OVERDUE
        )
        quiet = (rarer and p < quiet_p) or (ratio is not None and ratio > 1)
        entry["verdict"] = "overdue" if overdue else "quiet" if quiet else "normal"
        # When the silence (began, or will) become overdue if nothing arrives.
        threshold = min(overdue_p, math.exp(worst) * 0.999)
        crossing = profile.crossing(row["last_at"], row["last_at"], 0.0, threshold)
        if longest and ratio is not None:
            by_gap = row["last_at"] + timedelta(seconds=GAP_OVERDUE * longest)
            crossing = by_gap if crossing is None else min(crossing, by_gap)
        entry["overdue_at"] = None if crossing is None else crossing + delay
    return out


def odds(p):
    """'1 in 4,200' for a small probability."""
    if p is None:
        return "-"
    if p <= 0:
        return "under 1 in a billion"
    n = 1 / p
    if n >= 1e9:
        return "under 1 in a billion"
    if n >= 100:
        n = round(n, -int(math.floor(math.log10(n))) + 1)
    return f"1 in {int(round(n)):,}"
