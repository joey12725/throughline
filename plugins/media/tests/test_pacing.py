"""Rolling daily limits and new-inbound-only enable times (throughline_media.pacing).

A fake clock stands in for time; two connections to one SQLite file stand in
for two processes sharing a state directory.
"""

import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime

import httpx
import pytest

from throughline_media import transcribe as stt
from throughline_media.assets import Cache
from throughline_media.errors import BudgetExhausted
from throughline_media.pacing import (
    WINDOW_S,
    Allowance,
    Pacing,
    endpoint_allowance,
    instant,
    parse_limit,
    parse_since,
)

needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg not on PATH"
)
DAY = WINDOW_S


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_limits_parse_zero_numbers_and_unlimited():
    assert parse_limit("20", "X") == 20
    assert parse_limit(0, "X") == 0
    assert parse_limit(" Unlimited ", "X") is None
    for bad in ("-1", "1.5", "lots", ""):
        with pytest.raises(ValueError, match="X must be"):
            parse_limit(bad, "X")


def test_since_parses_dates_and_times():
    assert parse_since(None, "S") is None and parse_since(" ", "S") is None
    assert parse_since("2026-01-31", "S") == datetime(2026, 1, 31, tzinfo=UTC)
    assert parse_since("2026-01-31T12:00:00+02:00", "S") == datetime(
        2026, 1, 31, 10, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="S must be an ISO date"):
        parse_since("last tuesday", "S")
    assert instant("2026-01-31T00:00:00Z") == datetime(2026, 1, 31, tzinfo=UTC)
    assert instant("not a time") is None and instant(None) is None


def test_rolling_window_counts_attempts_and_frees_up(tmp_path):
    clock = Clock()
    cache = Cache(tmp_path / "c.sqlite3")
    pacing = Pacing(cache.conn, clock=clock)
    assert pacing.take("instagram", 2) and pacing.used("instagram") == 1
    clock.now += 3600
    assert pacing.take("instagram", 2)
    assert not pacing.take("instagram", 2)  # The third in 24 hours waits.
    assert pacing.remaining("instagram", 2) == 0
    # Frees up when the oldest attempt leaves the window, not at midnight.
    assert pacing.frees_at("instagram", 2) == datetime.fromtimestamp(
        clock.now - 3600 + DAY, UTC
    ).isoformat(timespec="seconds")
    assert pacing.frees_at("youtube", 2) is None  # Other kinds are separate.
    clock.now += DAY - 3600 + 1
    assert pacing.remaining("instagram", 2) == 1
    assert pacing.take("instagram", 2)
    # 0 disables; unlimited (None) always fits and still counts.
    assert not pacing.take("attachments", 0)
    assert pacing.take("attachments", None) and pacing.used("attachments") == 1
    assert pacing.remaining("attachments", None) is None
    # Amounts: one item larger than the whole limit never fits.
    assert (
        not pacing.take("minutes", 60, 61)
        and pacing.frees_at("minutes", 60, 61) is None
    )
    assert pacing.take("minutes", 60, 45) and not pacing.take("minutes", 60, 30)


def test_limits_hold_across_processes_and_restarts(tmp_path):
    clock = Clock()
    path = tmp_path / "c.sqlite3"
    first, second = Cache(path), Cache(path)
    one, two = Pacing(first.conn, clock=clock), Pacing(second.conn, clock=clock)
    assert one.take("youtube", 3) and two.take("youtube", 3) and one.take("youtube", 3)
    assert not two.take("youtube", 3)  # Never both take the last unit.
    first.close(), second.close()
    again = Pacing(Cache(path).conn, clock=clock)
    assert again.used("youtube") == 3 and not again.take("youtube", 3)
    # Old usage rows are pruned, recent ones are kept.
    clock.now += 3 * DAY
    later = Pacing(Cache(path).conn, clock=clock)
    assert later.conn.execute("SELECT COUNT(*) FROM usage").fetchone() == (0,)


def test_enable_time_is_stored_on_first_use_and_can_be_set(tmp_path):
    clock = Clock()
    path = tmp_path / "c.sqlite3"
    pacing = Pacing(Cache(path).conn, clock=clock)
    first = pacing.since("instagram")
    assert first == datetime.fromtimestamp(clock.now, UTC)
    clock.now += DAY
    assert Pacing(Cache(path).conn, clock=clock).since("instagram") == first
    explicit = datetime(2026, 1, 1, tzinfo=UTC)
    assert pacing.since("instagram", explicit) == explicit
    assert pacing.since("instagram") == explicit  # Stored for later runs.
    assert pacing.since("youtube") == datetime.fromtimestamp(clock.now, UTC)


def test_read_only_runs_store_nothing(tmp_path):
    clock = Clock()
    path = tmp_path / "c.sqlite3"
    assert Pacing(None).since("instagram") is None
    assert not Pacing(None).take("instagram", 5)
    # A cache written before pacing existed has no pacing tables.
    Cache(path).close()
    readonly = Cache(path, readonly=True)
    pacing = Pacing(readonly.conn, writable=False, clock=clock)
    assert pacing.since("instagram") is None and pacing.used("instagram") == 0
    assert not pacing.take("instagram", 5)
    writer = Pacing(Cache(path).conn, clock=clock)
    stored = writer.since("instagram")
    writer.take("instagram", 5)
    reader = Pacing(Cache(path, readonly=True).conn, writable=False, clock=clock)
    assert reader.since("instagram") == stored and reader.used("instagram") == 1
    with pytest.raises(sqlite3.OperationalError):
        readonly.conn.execute("INSERT INTO results VALUES ('k', 'v')")


def test_allowance_in_minutes(tmp_path):
    pacing = Pacing(Cache(tmp_path / "c.sqlite3").conn, clock=Clock())
    allowance = Allowance(pacing, 2)
    assert allowance.capacity_s() == 120 and allowance.remaining_s() == 120
    assert allowance.take(90) and allowance.remaining_s() == 30
    assert not allowance.take(31) and allowance.frees_at(31)
    assert Allowance(pacing, None).remaining_s() is None


def test_daily_minutes_setting():
    assert stt.daily_minutes({}) == 120
    assert stt.daily_minutes({"transcribe_minutes_per_day": "unlimited"}) is None
    with pytest.raises(ValueError, match="THROUGHLINE_TRANSCRIBE_MINUTES_PER_DAY"):
        stt.validate(
            dict(
                {name: default for name, _, default, _ in stt.SETTINGS},
                transcribe_minutes_per_day="many",
            )
        )
    # 0 switches the endpoint off entirely.
    url = {"transcribe_url": "https://stt.example.invalid/v1"}
    assert stt.endpoint_from(url, {}) is not None
    assert stt.endpoint_from(dict(url, transcribe_minutes_per_day="0"), {}) is None


@needs_ffmpeg
def test_endpoint_counts_daily_minutes_before_sending(tmp_path):
    audio = tmp_path / "a.ogg"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=50",
            "-c:a",
            "libopus",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"text": "invented"})

    clock = Clock()
    path = tmp_path / "c.sqlite3"

    def client():
        endpoint = stt.OpenAICompatible(
            "https://stt.example.invalid/v1/",
            None,
            "fake-model",
            transport=httpx.MockTransport(handler),
            sleep=lambda s: None,
        )
        endpoint.daily = Allowance(Pacing(Cache(path).conn, clock=clock), 1.5)
        return endpoint

    first = client()
    assert first.capacity_s() == 90
    first.transcript(audio)
    assert first.remaining_s() == pytest.approx(40, abs=0.5)
    # A later run in the same 24 hours sees the minutes already used.
    second = client()
    with pytest.raises(BudgetExhausted, match="daily"):
        second.transcript(audio)
    assert len(sent) == 1  # Nothing was sent for the refused audio.
    clock.now += DAY + 1
    client().transcript(audio)
    assert len(sent) == 2


def test_first_enable_time_race_keeps_the_first_stored(tmp_path):
    """Two runs enabling a kind at once: the second neither fails nor overwrites."""
    path = tmp_path / "c.sqlite3"
    early, late = Clock(), Clock(1_800_000_500.0)
    first = Pacing(Cache(path).conn, clock=early)
    stored = []

    def racing_clock():
        # The other run stores its time between this run's read and write.
        if not stored:
            stored.append(first.since("instagram"))
        return late.now

    second = Pacing(Cache(path).conn, clock=racing_clock)
    assert second.since("instagram") == stored[0]
    assert stored[0] == datetime.fromtimestamp(early.now, UTC)
    assert Pacing(Cache(path).conn, clock=late).since("instagram") == stored[0]


def test_endpoint_minutes_are_one_pool_for_every_plugin(tmp_path, monkeypatch):
    clock = Clock()
    shared = tmp_path / "shared"
    settings = {"transcribe_state_dir": str(shared)}
    beeper = endpoint_allowance(settings, 2, clock=clock)
    youtube = endpoint_allowance(settings, 2, clock=clock)
    assert beeper.take(90)
    assert youtube.remaining_s() == 30 and not youtube.take(60)
    assert youtube.frees_at(60) == datetime.fromtimestamp(
        clock.now + DAY, UTC
    ).isoformat(timespec="seconds")
    beeper.close(), youtube.close()
    # The environment variable names the same place; read-only runs write nothing.
    monkeypatch.setenv("THROUGHLINE_TRANSCRIBE_STATE_DIR", str(shared))
    reader = endpoint_allowance({}, 2, writable=False, clock=clock)
    assert reader.remaining_s() == 30 and not reader.take(1)
    reader.close()
    elsewhere = tmp_path / "unused"
    nothing = endpoint_allowance(
        {"transcribe_state_dir": str(elsewhere)}, 2, writable=False
    )
    assert not nothing.take(1) and not elsewhere.exists()
