"""Shared model calls (lenses.coalesce): lenses that read the same items with
the same model ask in one call during a scheduler tick, a lens that is not due
rides along on the answers it was given, and anything that is not the same
input still calls alone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import ingest, message

from throughline import lenses
from throughline.db import connect
from throughline.providers.base import ProviderUnavailable

YEARLY = "0 0 1 1 *"


@pytest.fixture(autouse=True)
def cleanup_views(database):
    yield
    with connect() as c:
        for r in c.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema='api' AND table_name LIKE 'lens_%'"
        ).fetchall():
            base = r["table_name"][len("lens_") :]
            base = base[: -len("_versions")] if base.endswith("_versions") else base
            try:
                c.execute("SELECT tl.drop_lens_views(%s)", (base,))
            except Exception:
                pass


def _channel(client, name="synthetic.chat"):
    r = client.post("/v1/channels", json=dict(name=name, plugin="synthetic", fields=[dict(name="rating", type="integer")]))
    assert r.status_code == 200, r.text


def _lens(name, column, **extra):
    spec = {
        "name": name,
        "channels": ["synthetic.chat"],
        "grain": "message",
        "worker": "llm",
        "worker_config": {"base_url": "http://127.0.0.1:9/v1", "model": "m1", "price_per_million": 0},
        "prompt": f"Extract the {column}.",
        "columns": [{"name": column, "type": "text", "nullable": False}],
    }
    cfg = extra.pop("worker_config", {})
    spec.update(extra)
    spec["worker_config"] = {**spec["worker_config"], **cfg}
    return lenses.define_lens(spec)


def _rows_for(tdef, payload):
    col = tdef["columns"][0]["name"]
    return [
        {"data": {col: f"{col}-{m['id']}"}, "source_message_ids": [m["id"]], "confidence": 0.9}
        for m in payload["items"]
    ]


class Recorder:
    """llm override: answers solo and shared calls, records who was asked."""

    def __init__(self, bad_once=()):
        self.calls = []
        self.bad_once = set(bad_once)

    def __call__(self, tdef, payload, attempt):
        if isinstance(tdef, list):
            names = [t["name"] for t in tdef]
            self.calls.append(names)
            tables = {}
            for t in tdef:
                if t["name"] in self.bad_once:
                    self.bad_once.discard(t["name"])
                    tables[t["name"]] = {"rows": [{"data": {}, "source_message_ids": []}]}
                else:
                    tables[t["name"]] = {"rows": _rows_for(t, payload)}
            return {"tables": tables}
        self.calls.append(tdef["name"])
        return {"rows": _rows_for(tdef, payload)}


def _count(name):
    with connect() as c:
        return c.execute(
            "SELECT count(*) AS n FROM tl.table_rows r JOIN tl.table_defs t ON t.id=r.table_id WHERE t.name=%s AND r.status='active'",
            (name,),
        ).fetchone()["n"]


def _settle(rec):
    """First tick: every scheduled lens is due once (nothing to read yet)."""
    with lenses.override_worker("llm", rec):
        lenses.tick()
    rec.calls.clear()


def test_due_lenses_share_one_call_per_item(client):
    _channel(client)
    _lens("mood", "mood", schedule="@hourly")
    _lens("topic", "topic", schedule="@hourly")
    ingest(client, [message(i, external_id=f"m{i}", content=f"note {i}") for i in range(3)])
    rec = Recorder()
    with lenses.override_worker("llm", rec):
        out = lenses.tick()
    assert rec.calls == [["mood", "topic"]] * 3
    assert _count("mood") == 3 and _count("topic") == 3
    assert out["shared"]["shared_calls"] == 3 and out["shared"]["answers_used"] == 3
    by = {e["lens"]: e for e in out["lenses"]}
    assert by["mood"]["result"]["model_calls"] == 3
    assert by["topic"]["result"]["model_calls"] == 0  # its answers came with mood's calls
    assert by["mood"]["result"]["shared"]["shared_with"] == ["topic"]
    with connect() as c:
        led = {
            r["purpose"]: r
            for r in c.execute(
                "SELECT purpose, sum(calls) AS calls, sum(tokens) AS tokens FROM tl.model_ledger GROUP BY 1"
            ).fetchall()
        }
    assert led["table.mood"]["calls"] == 3 and led["table.topic"]["calls"] == 0
    assert led["table.topic"]["tokens"] > 0  # each lens is charged its share


def test_lens_not_due_rides_along_and_is_not_called_at_its_fire(client):
    _channel(client)
    _lens("often", "mood", schedule="@hourly")
    _lens("rarely", "topic", schedule=YEARLY)
    rec = Recorder()
    _settle(rec)
    ingest(client, [message(i, external_id=f"r{i}", content=f"note {i}") for i in range(2)])
    later = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
    with lenses.override_worker("llm", rec):
        out = lenses.tick(now=later)
    by = {e["lens"]: e for e in out["lenses"]}
    assert by["rarely"]["due"] is False and by["rarely"]["rode_along"]["processed"] == 2
    assert by["rarely"]["rode_along"]["model_calls"] == 0
    assert rec.calls == [["often", "rarely"]] * 2
    assert _count("rarely") == 2
    # Its own schedule fires later: nothing left to read, no call.
    rec.calls.clear()
    with lenses.override_worker("llm", rec):
        out = lenses.tick(now=later + timedelta(days=400))
    assert rec.calls == []


def test_coalesce_due_and_off(client):
    _channel(client)
    _lens("lead", "mood", schedule="@hourly")
    _lens("waits", "topic", schedule=YEARLY, worker_config={"coalesce": "due"})
    _lens("alone", "kind", schedule="@hourly", worker_config={"coalesce": False})
    rec = Recorder()
    _settle(rec)
    ingest(client, [message(0, external_id="c0", content="x")])
    later = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
    with lenses.override_worker("llm", rec):
        out = lenses.tick(now=later)
    assert sorted(map(str, rec.calls)) == ["alone", "lead"]  # 'waits' is not due and does not ride
    assert "rode_along" not in {e["lens"]: e for e in out["lenses"]}["waits"]


def test_different_input_or_model_calls_alone(client):
    _channel(client)
    _lens("a", "mood", schedule="@hourly")
    _lens("b", "topic", schedule="@hourly", worker_config={"include_fields": False})  # renders items differently
    _lens("c", "kind", schedule="@hourly", worker_config={"model": "m2"})
    ingest(client, [message(0, external_id="d0", content="x")])
    rec = Recorder()
    with lenses.override_worker("llm", rec):
        lenses.tick()
    assert sorted(rec.calls) == ["a", "b", "c"]


def test_rider_with_an_older_unanswered_item_does_not_join(client):
    _channel(client)
    _lens("recent_only", "mood", schedule="@hourly", worker_config={"not_before": "2026-01-01"})
    _lens("everything", "topic", schedule=YEARLY)
    rec = Recorder()
    _settle(rec)
    ingest(
        client,
        [
            message(0, external_id="old", content="old", happened_at="2025-06-01T12:00:00+00:00"),
            message(1, external_id="new", content="new", happened_at="2026-06-01T12:00:00+00:00"),
        ],
    )
    later = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
    with lenses.override_worker("llm", rec):
        out = lenses.tick(now=later)
    # 'everything' would read 'old' first, which nobody answers for it: it
    # cannot use an answer for 'new' yet, so it is not asked.
    assert rec.calls == ["recent_only"]
    assert "rode_along" not in {e["lens"]: e for e in out["lenses"]}["everything"]


def test_invalid_part_is_retried_for_that_lens_only(client):
    _channel(client)
    _lens("good", "mood", schedule="@hourly")
    _lens("flaky", "topic", schedule="@hourly")
    ingest(client, [message(0, external_id="v0", content="x")])
    rec = Recorder(bad_once=["flaky"])
    with lenses.override_worker("llm", rec):
        lenses.tick()
    assert sorted(rec.calls[0]) == ["flaky", "good"] and rec.calls[1:] == [["flaky"]]
    assert _count("good") == 1 and _count("flaky") == 1


def test_outage_in_a_shared_call_keeps_both_cursors(client):
    _channel(client)
    _lens("x1", "mood", schedule="@hourly")
    _lens("x2", "topic", schedule="@hourly")
    ingest(client, [message(0, external_id="o0", content="x")])
    calls = []

    def down(tdef, payload, attempt):
        calls.append(tdef)
        raise ProviderUnavailable("table_llm endpoint unavailable")

    with lenses.override_worker("llm", down):
        out = lenses.tick()
    assert len(calls) == 1  # one shared call; the other lens is skipped as endpoint_down
    by = {e["lens"]: e for e in out["lenses"]}
    assert by["x1"]["result"]["status"] == "provider_unavailable"
    assert by["x2"]["why"] == "endpoint_down"
    with connect() as c:
        for name in ("x1", "x2"):
            cur = lenses.get_lens(name, conn=c).get("cursor") or {}
            assert int(cur.get("last_message_id") or 0) == 0


def test_run_lens_outside_a_tick_calls_alone(client):
    _channel(client)
    _lens("solo1", "mood")
    _lens("solo2", "topic")
    ingest(client, [message(0, external_id="s0", content="x")])
    rec = Recorder()
    with lenses.override_worker("llm", rec):
        lenses.run_lens("solo1")
        lenses.run_lens("solo2")
    assert rec.calls == ["solo1", "solo2"]


def _jev_lens(name, question):
    return lenses.define_lens(
        {
            "name": name,
            "channels": ["youtube.uploads"],
            "grain": "message",
            "worker": "jev",
            "schedule": "@hourly",
            "prompt": f"Policy for {name}.",
            "worker_config": {
                "model": "typesafe/jev-1.14",
                "questions": {question: {"type": "noul", "instructions": f"{question}?"}},
            },
            "columns": [
                {"name": "video_id", "type": "text", "nullable": True},
                {"name": f"{question}_probability", "type": "number", "nullable": True},
            ],
        }
    )


def test_jev_lenses_share_one_call(client):
    _channel(client, "youtube.uploads")
    _jev_lens("j_watch", "watch")
    _jev_lens("j_junk", "junk")
    ingest(
        client,
        [message(0, channel="youtube.uploads", external_id="v1", content="video")],
    )
    calls = []

    def fake(tdef, payload, item):
        names = [t["name"] for t in tdef] if isinstance(tdef, list) else [tdef["name"]]
        calls.append((names, sorted(payload["questions"])))
        return {"model": "jev", "answers": {k: {"type": "noul", "noul": 0.7} for k in payload["questions"]}}

    with lenses.override_worker("jev", fake):
        lenses.tick()
    assert calls == [(["j_watch", "j_junk"], ["j_junk__junk", "j_watch__watch"])] or calls == [
        (["j_junk", "j_watch"], ["j_junk__junk", "j_watch__watch"])
    ]
    assert _count("j_watch") == 1 and _count("j_junk") == 1


def test_day_lenses_with_different_schedules_share_each_day(client, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_TIMEZONE", "America/Winnipeg")
    _channel(client)
    _lens("plans", "title", grain="day", schedule="@hourly")
    _lens("recs", "media", grain="day", schedule=YEARLY)
    rec = Recorder()
    _settle(rec)
    ingest(
        client,
        [
            message(0, external_id="d1", content="dinner friday?", happened_at="2026-11-18T15:00:00+00:00"),
            message(1, external_id="d2", content="watch Severance", happened_at="2026-11-19T15:00:00+00:00"),
        ],
    )
    t = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)  # 'plans' fires, 'recs' does not
    with lenses.override_worker("llm", rec):
        out = lenses.tick(now=t)
    assert rec.calls == [["plans", "recs"]] * 2
    by = {e["lens"]: e for e in out["lenses"]}
    assert by["recs"]["rode_along"]["processed"] == 2
