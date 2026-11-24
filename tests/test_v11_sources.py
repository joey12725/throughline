"""Source health (migration 0057, throughline.health, throughline.cadence):
outages from heartbeats and the clock, stalls from arrival cadence, catch-up
accounting, pause, the alarm, routes and the CLI. Synthetic data only."""

import json
import sys
from datetime import datetime, timedelta, timezone

from conftest import ingest, message
from typer.testing import CliRunner

from throughline import cadence, health
from throughline.db import connect, create_token

T0 = datetime(2020, 1, 6, tzinfo=timezone.utc)  # a Monday


def plugin_headers(name="synthetic"):
    token = create_token("synthetic-plugin", "plugin", name)["token"]
    return {"Authorization": "Bearer " + token}


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def beat(client, h, **data):
    return ok(client.post("/v1/plugins/synthetic/heartbeat", json=data, headers=h))


def sql(statement, params=()):
    with connect() as c:
        cursor = c.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def state(name="synthetic", record=True):
    with connect() as c:
        entries = health.sources(c, [name], record=record)
    return entries[0] if entries else None


def age(name="synthetic", success=None, error=None):
    """Move a plugin's last success (and error) into the past."""
    if success is not None:
        sql(
            "UPDATE tl.plugins SET last_success_at=now()-%s::interval WHERE name=%s",
            (success, name),
        )
    if error is not None:
        sql(
            "UPDATE tl.plugins SET last_error_at=now()-%s::interval WHERE name=%s",
            (error, name),
        )


# --- runs ------------------------------------------------------------------------


def test_heartbeats_drive_ok_failing_down_and_restored(client, channel):
    h = plugin_headers()
    beat(client, h, interval_s=60, messages_ingested=2)
    assert state()["state"] == "ok"
    # A failure heartbeat carries no interval; the known one is kept.
    row = beat(client, h, error="source unreachable")
    assert row["interval_s"] == 60 and row["consecutive_failures"] == 1
    entry = state()
    assert entry["state"] == "failing" and entry["last_error"] == "source unreachable"
    assert sql("SELECT count(*) n FROM tl.source_outages")[0]["n"] == 0
    beat(client, h, error="source unreachable")
    beat(client, h, error="source unreachable again")
    entry = state()
    assert entry["state"] == "down" and entry["kind"] == "failing"
    outage = sql("SELECT * FROM tl.source_outages")[0]
    assert outage["kind"] == "failing" and outage["restored_at"] is None
    assert outage["first_error"] == "source unreachable again"
    assert outage["failures"] == 3 and outage["last_good_at"] is not None
    beat(client, h, error="still unreachable")
    outage = sql("SELECT * FROM tl.source_outages")[0]
    assert outage["first_error"] == "source unreachable again"
    assert outage["last_error"] == "still unreachable" and outage["failures"] == 4
    # The source is back: the catch-up run delivers what was missed.
    ok(ingest(client, [message(i) for i in range(5)]))
    sql(
        "UPDATE tl.source_outages SET last_good_at=%s",
        (T0 - timedelta(days=30),),
    )
    beat(client, h, interval_s=60, messages_ingested=5)
    entry = state()
    assert entry["state"] == "ok" and entry["outage"] is None
    outage = sql("SELECT * FROM tl.source_outages")[0]
    assert outage["restored_at"] is not None
    assert outage["caught_up_messages"] == 5 and outage["catch_up"] == "caught_up"
    assert "restored" in entry["note"] and "caught up 5 messages" in entry["note"]


def test_silent_outage_is_noticed_by_the_clock_and_closed_by_a_run(client, channel):
    h = plugin_headers()
    beat(client, h, interval_s=60)
    age(success="10 minutes")
    entry = state()
    assert entry["state"] == "down" and entry["kind"] == "silent"
    assert "is its timer or service running" in entry["note"]
    outages = sql("SELECT * FROM tl.source_outages")
    assert len(outages) == 1 and outages[0]["kind"] == "silent"
    # Reading again neither duplicates nor closes it.
    state()
    assert len(sql("SELECT * FROM tl.source_outages WHERE restored_at IS NULL")) == 1
    beat(client, h, interval_s=60, messages_ingested=0)
    assert state()["state"] == "ok"
    assert sql("SELECT restored_at FROM tl.source_outages")[0]["restored_at"]


def test_never_and_failing_from_the_start(client, channel):
    ok(client.post("/v1/plugins/synthetic/manifest", json={"name": "synthetic"}))
    assert state()["state"] == "never"
    with connect() as c:
        # registered (or a dry run) but never ran: shown, not alarmed
        assert health.summary(health.sources(c))["ok"] is True
    h = plugin_headers()
    beat(client, h, error="bad credentials")
    entry = state()
    assert entry["state"] == "down" and entry["last_success_at"] is None
    assert sql("SELECT last_good_at FROM tl.source_outages")[0]["last_good_at"] is None


def test_partial_catch_up_from_source_cap_coverage(client, channel):
    h = plugin_headers()
    beat(client, h, interval_s=60)
    age(success="1 hour")
    assert state()["state"] == "down"
    ok(
        client.post(
            "/v1/plugins/synthetic/coverage",
            json={
                "reports": [
                    {
                        "channel": channel,
                        "status": "partial",
                        "reasons": ["source_cap"],
                        "gaps": [
                            {
                                "from": "2020-01-01T00:00:00+00:00",
                                "to": "2020-01-02T00:00:00+00:00",
                                "reason": "source_cap",
                            }
                        ],
                    }
                ]
            },
            headers=h,
        )
    )
    beat(client, h, interval_s=60, messages_ingested=50)
    outage = sql("SELECT * FROM tl.source_outages")[0]
    assert outage["catch_up"] == "partial"
    assert "source_cap" in outage["catch_up_reasons"]
    assert "partial: " in state()["note"]


def test_pause_hides_a_source_from_the_alarm(client, channel):
    h = plugin_headers()
    beat(client, h, interval_s=60)
    age(success="1 day")
    ok(
        client.post(
            "/v1/source-health/synthetic/pause", json={"reason": "cookies expired"}
        )
    )
    entry = state()
    assert entry["state"] == "paused" and "cookies expired" in entry["note"]
    body = ok(client.get("/v1/source-health"))
    assert body["summary"]["ok"] is True and body["summary"]["counts"]["paused"] == 1
    ok(client.post("/v1/source-health/synthetic/resume"))
    assert state()["state"] == "down"
    assert client.post("/v1/source-health/nobody/pause").status_code == 404


# --- arrivals --------------------------------------------------------------------


def hourly(start, days, hours=(9, 10, 11, 14, 15), weekdays=range(5)):
    """Synthetic items on weekday working hours (UTC) for `days` days."""
    out = []
    i = 0
    for day in range(days):
        at = start + timedelta(days=day)
        if at.weekday() not in weekdays:
            continue
        for hour in hours:
            out.append(
                message(
                    i,
                    external_id=f"cad-{i}",
                    happened_at=(at + timedelta(hours=hour, minutes=7)).isoformat(),
                )
            )
            i += 1
    return out


def profile_from(items, tz=timezone.utc):
    times = [datetime.fromisoformat(m["happened_at"]) for m in items]
    hours = {t.replace(minute=0, second=0, microsecond=0) for t in times}
    return cadence.Profile(hours, min(times), max(times), tz)


def test_cadence_profile_learns_the_weekly_pattern():
    items = hourly(T0, 56)
    profile = profile_from(items)
    assert not profile.learning()
    monday_9 = 0 * 24 + 9
    sunday_9 = 6 * 24 + 9
    assert profile.p[monday_9] > 0.8
    assert profile.p[sunday_9] < 0.2 < profile.p[monday_9] / 4
    last = datetime.fromisoformat(items[-1]["happened_at"])  # a Friday 15:07
    weekend = last + timedelta(days=2, hours=12)  # Monday 03:07
    # A weekend without items is ordinary for a weekday source ...
    assert profile.log_p_silence(last, weekend) > -2
    # ... missing Monday's working hours is not.
    monday_noon = last + timedelta(days=2, hours=21)
    assert profile.log_p_silence(last, monday_noon) < -6
    assert profile.busiest(1)[0][:3] in {"Mon", "Tue", "Wed", "Thu", "Fri"}


def test_cadence_needs_history_before_judging():
    profile = profile_from(hourly(T0, 5))
    assert profile.learning()


def test_cadence_picks_utc_for_a_utc_scheduled_job():
    # A daily job at 07:00 UTC across a daylight-saving change: in local
    # time its hour moves, in UTC it does not.
    from zoneinfo import ZoneInfo

    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    items = [
        message(
            i,
            external_id=f"d-{i}",
            happened_at=(start + timedelta(days=i, hours=7)).isoformat(),
        )
        for i in range(70)
    ]
    local = profile_from(items, ZoneInfo("America/New_York"))
    utc = profile_from(items, timezone.utc)
    assert utc.fit > local.fit


def stalled_setup(client, channel, silence_days):
    """A weekday-hours channel whose newest item is silence_days old."""
    end = datetime.now(timezone.utc) - timedelta(days=silence_days)
    start = end - timedelta(days=60)
    items = hourly(start, 60, hours=range(24), weekdays=range(7))
    items = [m for m in items if datetime.fromisoformat(m["happened_at"]) <= end]
    for batch in range(0, len(items), 1000):
        ok(ingest(client, items[batch : batch + 1000]))
    h = plugin_headers()
    beat(client, h, interval_s=60)
    return h


def test_a_source_that_runs_but_brings_nothing_is_stalled(client, channel):
    h = stalled_setup(client, channel, silence_days=2)
    entry = state()
    assert entry["state"] == "stalled", entry["note"]
    assert entry["overdue"] == [channel]
    assert "runs succeed, but synthetic.chat" in entry["note"]
    outage = sql("SELECT * FROM tl.source_outages")[0]
    assert outage["kind"] == "stalled" and outage["last_good_at"] is not None
    # Another successful run that brings nothing keeps it stalled.
    beat(client, h, interval_s=60, messages_ingested=0)
    assert state()["state"] == "stalled"
    assert sql("SELECT restored_at FROM tl.source_outages")[0]["restored_at"] is None
    # New items arrive: the stall ends and the catch-up is counted.
    now = datetime.now(timezone.utc)
    ok(
        ingest(
            client,
            [
                message(
                    100000 + i,
                    external_id=f"new-{i}",
                    happened_at=(now - timedelta(hours=i + 1)).isoformat(),
                )
                for i in range(30)
            ],
        )
    )
    beat(client, h, interval_s=60, messages_ingested=30)
    entry = state()
    assert entry["state"] == "ok", entry["note"]
    outage = sql("SELECT * FROM tl.source_outages")[0]
    assert outage["restored_at"] is not None and outage["caught_up_messages"] >= 30
    assert "after" in entry["note"] and "stalled" in entry["note"]


def test_an_ordinary_silence_is_not_a_stall(client, channel):
    stalled_setup(client, channel, silence_days=0)
    entry = state()
    assert entry["state"] == "ok", entry["note"]
    assert entry["channels"][0]["verdict"] == "normal"
    assert entry["channels"][0]["overdue_at"] is not None


def test_paused_channel_does_not_stall_its_source(client, channel):
    stalled_setup(client, channel, silence_days=3)
    assert state()["state"] == "stalled"
    with connect() as c:
        assert health.pause(c, channel, "feed ended")["kind"] == "channel"
    entry = state()
    assert entry["state"] == "ok"
    # The stall outage closes once nothing keeps the source unhealthy.
    assert sql("SELECT restored_at FROM tl.source_outages")[0]["restored_at"]
    with connect() as c:
        health.resume(c, channel)
    assert state()["state"] == "stalled"


def test_reference_channels_are_not_judged(client):
    ok(
        client.post(
            "/v1/channels",
            json=dict(name="synthetic.registry", plugin="synthetic", is_reference=True),
        )
    )
    with connect() as c:
        assert [r["channel"] for r in cadence.channels(c, ["synthetic"])] == []


def test_odds_wording():
    assert cadence.odds(0.25) == "1 in 4"
    assert cadence.odds(1 / 4321) == "1 in 4,300"
    assert cadence.odds(0.0) == "under 1 in a billion"


# --- the alarm, routes and CLI ----------------------------------------------------


def alert_command(tmp_path):
    out = tmp_path / "alerts.txt"
    script = tmp_path / "alert.py"
    script.write_text(
        "import sys\nopen(sys.argv[1],'a').write(sys.argv[2]+'|'+sys.argv[3]+'\\n')\n"
    )
    return [sys.executable, str(script), str(out), "{title}", "{state}"], out


def test_check_alerts_once_per_transition(client, channel, tmp_path):
    command, out = alert_command(tmp_path)
    h = plugin_headers()
    beat(client, h, interval_s=60)
    with connect() as c:
        assert health.check(c, command=command)["alerts"] == []
    age(success="1 hour")
    with connect() as c:
        result = health.check(c, command=command)
    assert result["summary"]["ok"] is False
    assert [a["sent"] for a in result["alerts"]] == [True]
    with connect() as c:
        assert health.check(c, command=command)["alerts"] == []
    beat(client, h, interval_s=60)
    with connect() as c:
        health.check(c, command=command)
    lines = out.read_text().splitlines()
    assert lines == [
        "Throughline: synthetic is down|down",
        "Throughline: synthetic is back|ok",
    ]


def test_check_without_notifier_keeps_the_alert_pending(client, channel, tmp_path):
    h = plugin_headers()
    beat(client, h, interval_s=60)
    age(success="1 hour")
    with connect() as c:
        first = health.check(c, command="")
    assert first["alerts"] and first["alerts"][0]["sent"] is False
    command, out = alert_command(tmp_path)
    with connect() as c:
        health.check(c, command=command)
    assert out.read_text().startswith("Throughline: synthetic is down")


def test_failed_alert_is_retried(client, channel, tmp_path):
    h = plugin_headers()
    beat(client, h, interval_s=60)
    age(success="1 hour")
    with connect() as c:
        result = health.check(c, command=[sys.executable, "-c", "raise SystemExit(3)"])
    assert (
        result["alerts"][0]["sent"] is False
        and "exit code 3" in result["alerts"][0]["error"]
    )
    command, out = alert_command(tmp_path)
    with connect() as c:
        health.check(c, command=command)
    assert out.exists()


def test_routes_and_status(client, channel):
    h = plugin_headers()
    beat(client, h, interval_s=60)
    body = ok(client.get("/v1/source-health"))
    assert body["summary"]["ok"] is True
    assert [s["name"] for s in body["sources"]] == ["synthetic"]
    own = ok(client.get("/v1/plugins/synthetic/health", headers=h))
    assert own["state"] == "ok" and own["last_success_at"]
    assert client.get("/v1/plugins/other/health", headers=h).status_code == 403
    assert client.get("/v1/source-health", headers=h).status_code == 403
    assert (
        client.post("/v1/source-health/synthetic/pause", headers=h).status_code == 403
    )
    status = ok(client.get("/v1/status"))
    assert status["staleness"]["sources"]["ok"] is True
    age(success="1 hour")
    assert (
        ok(client.get("/v1/source-health/outages"))["outages"][0]["plugin"]
        == "synthetic"
    )


def test_cli_board_and_check_exit_code(client, channel):
    from throughline import cli

    h = plugin_headers()
    beat(client, h, interval_s=60)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["lib", "sources"], color=False)
    assert result.exit_code == 0, result.output
    assert "synthetic" in result.output and "1 ok" in result.output
    age(success="2 hours")
    result = runner.invoke(
        cli.app, ["lib", "sources", "check", "--no-notify"], color=False
    )
    assert result.exit_code == 2
    assert "DOWN" in result.output and "catch up on their own" in result.output
    result = runner.invoke(cli.app, ["lib", "sources", "outages"], color=False)
    assert "synthetic: silent" in result.output and "still down" in result.output
    result = runner.invoke(cli.app, ["lib", "sources", "--json"], color=False)
    assert json.loads(result.output)["summary"]["problems"] == ["synthetic"]
    result = runner.invoke(cli.app, ["lib", "sources", "cadence"], color=False)
    assert result.exit_code == 0 and "synthetic.chat" in result.output
    result = runner.invoke(
        cli.app, ["lib", "sources", "pause", "synthetic"], color=False
    )
    assert "source synthetic paused" in result.output


def test_a_quiet_stretch_on_a_human_channel_is_not_overdue():
    """A chat used on about half of the days, in the evening: two quiet
    evenings in a row are ordinary. Hours are not independent, so the score
    is calibrated against the channel's own past silences."""
    import random

    rng = random.Random(7)
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    hours = set()
    for day in range(84):
        if rng.random() < 0.5:
            for hour in rng.sample(range(18, 24), 4):
                hours.add(start + timedelta(days=day, hours=hour))
    last = max(hours)
    profile = cadence.Profile(hours, min(hours), last, timezone.utc)
    two_evenings = (last + timedelta(days=2)).replace(hour=23, minute=59)
    log_p = profile.log_p_silence(last, two_evenings)
    assert log_p > profile.worst_gap_log_p  # not rarer than its own past
    three_weeks = last + timedelta(days=21)
    assert profile.log_p_silence(last, three_weeks) < profile.worst_gap_log_p


def test_zone_is_a_real_zone_with_daylight_saving(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_TIMEZONE", "America/New_York")
    tz = cadence.zone()
    summer = datetime(2026, 7, 1, 12, tzinfo=timezone.utc).astimezone(tz)
    winter = datetime(2026, 12, 1, 12, tzinfo=timezone.utc).astimezone(tz)
    assert summer.utcoffset() != winter.utcoffset()


def test_restore_never_overwrites_a_closed_outage(client, channel):
    h = plugin_headers()
    beat(client, h, interval_s=60)
    age(success="1 hour")
    state()
    beat(client, h, interval_s=60)
    first = sql("SELECT restored_at FROM tl.source_outages")[0]["restored_at"]
    with connect() as c:
        outage = c.execute("SELECT * FROM tl.source_outages").fetchone()
        entry = health.evaluate(c, ["synthetic"])[0]
        assert health._restore(c, entry, outage, 0, None) is None
    assert sql("SELECT restored_at FROM tl.source_outages")[0]["restored_at"] == first


def test_open_stall_stands_when_arrivals_are_not_checked(client, channel):
    stalled_setup(client, channel, silence_days=3)
    assert state()["state"] == "stalled"
    with connect() as c:
        entry = health.evaluate(c, ["synthetic"], arrivals=False)[0]
    assert entry["state"] == "stalled" and "not re-checked" in entry["note"]
