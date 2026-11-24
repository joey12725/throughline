"""Lenses survive a model endpoint outage: the cursor stays put, interrupted
runs are resumed by lenses.scheduler.tick, rule-triggered runs are queued for
retry, and a 200 without an answer counts as an outage (not as 'no rows')."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import ingest, message

from throughline import lenses
from throughline.db import connect
from throughline.lenses import scheduler, workers
from throughline.lenses.runner import _lens_lock
from throughline.providers.base import ProviderUnavailable


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


def _lens(name, **extra):
    spec = {
        "name": name,
        "channels": ["synthetic.chat"],
        "grain": "message",
        "worker": "llm",
        "worker_config": {"base_url": "http://127.0.0.1:9/v1", "price_per_million": 0},
        "prompt": "Extract the topic.",
        "columns": [{"name": "topic", "type": "text", "nullable": False}],
    }
    spec.update(extra)
    return lenses.define_lens(spec)


def _answer(tdef, payload, attempt):
    return {
        "rows": [
            {"data": {"topic": f"t{m['id']}"}, "source_message_ids": [m["id"]], "confidence": 0.9}
            for m in payload["items"]
        ]
    }


def _down(calls):
    def fn(tdef, payload, attempt):
        calls.append(tdef["name"] if isinstance(tdef, dict) else "merged")
        raise ProviderUnavailable("table_llm endpoint unavailable or returned an invalid response")

    return fn


def _cursor(name):
    return int((lenses.get_lens(name).get("cursor") or {}).get("last_message_id") or 0)


def _rows(client, name):
    return client.get(f"/v1/lenses/{name}/rows").json()["rows"]


def test_outage_keeps_cursor_and_tick_backfills(client):
    _channel(client)
    ingest(client, [message(i, external_id=f"o{i}", content=f"note {i}") for i in range(3)])
    _lens("topics")

    calls = []
    with lenses.override_worker("llm", _down(calls)):
        res = lenses.run_lens("topics")
    assert res["status"] == "provider_unavailable"
    assert res["written"] == 0 and _cursor("topics") == 0

    # Right after the failure the scheduler backs off instead of hammering.
    with lenses.override_worker("llm", _down(calls)):
        soon = lenses.tick()
    entry = soon["lenses"][0]
    assert entry["due"] is False and entry["why"].startswith("backoff")

    # Endpoint back: the next tick past the backoff resumes from the cursor.
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    with lenses.override_worker("llm", _answer):
        out = lenses.tick(now=later)
    entry = out["lenses"][0]
    assert entry["why"] == "resume_provider_unavailable"
    assert entry["result"]["status"] == "done" and entry["result"]["written"] == 3
    assert len(_rows(client, "topics")) == 3

    # Caught up and unscheduled: nothing further is due.
    with lenses.override_worker("llm", _down(calls)):
        again = lenses.tick(now=later + timedelta(minutes=30))
    assert again["lenses"][0]["why"] == "unscheduled" and again["ran"] == 0


def test_backoff_doubles_and_caps():
    runs = [{"status": "provider_unavailable"}] * 3 + [{"status": "done"}]
    assert scheduler.backoff_s(runs) == scheduler.RETRY_BASE_S * 4
    assert scheduler.backoff_s([{"status": "failed"}] * 20) == scheduler.RETRY_MAX_S
    assert scheduler.backoff_s([{"status": "done"}]) == 0


@pytest.mark.parametrize(
    "reply",
    [
        {"error": {"message": "upstream unavailable"}},
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"finish_reason": "error", "message": {"content": "x"}}]},
        {"object": "chat.completion"},
    ],
)
def test_http_200_without_an_answer_is_an_outage(client, monkeypatch, reply):
    _channel(client)
    ingest(client, [message(0, external_id="e0", content="hello")])
    _lens("proxy_topics")
    monkeypatch.setattr(workers.http, "request", lambda *a, **k: dict(reply))
    res = lenses.run_lens("proxy_topics")
    assert res["status"] == "provider_unavailable", res
    assert _cursor("proxy_topics") == 0 and res["processed"] == 0


def test_model_answer_that_fails_validation_is_still_an_answer():
    workers._raise_if_no_answer({"choices": [{"message": {"content": "not json"}}]})
    workers._raise_if_no_answer({"choices": [{"message": {"content": None, "refusal": "no"}}]})


def test_one_probe_per_endpoint_while_it_is_down(client):
    _channel(client)
    ingest(client, [message(0, external_id="p0", content="hello")])
    _lens("first_lens", schedule="* * * * *")
    _lens("second_lens", schedule="* * * * *")
    calls = []
    with lenses.override_worker("llm", _down(calls)):
        out = lenses.tick()
    by = {e["lens"]: e for e in out["lenses"]}
    statuses = sorted((e.get("result") or {}).get("status") or e["why"] for e in by.values())
    assert statuses == ["endpoint_down", "provider_unavailable"]
    assert len(calls) == 1  # the second lens on the same endpoint is not called
    assert out["endpoints_down"] == ["http://127.0.0.1:9/v1"]


def test_backlog_cut_at_max_items_is_resumed(client):
    _channel(client)
    ingest(client, [message(i, external_id=f"b{i}", content=f"item {i}") for i in range(5)])
    _lens("backlog", max_items_per_run=2)
    with lenses.override_worker("llm", _answer):
        first = lenses.run_lens("backlog")
        assert first["status"] == "max_items" and first["written"] == 2
        statuses = []
        for _ in range(4):
            out = lenses.tick()
            entry = out["lenses"][0]
            if "result" not in entry:
                break
            statuses.append(entry["result"]["status"])
    assert statuses == ["max_items", "done"]
    assert len(_rows(client, "backlog")) == 5


def test_rule_triggered_run_is_queued_and_retried(client):
    _channel(client)
    _lens("rule_lens")
    client.post(
        "/v1/rules",
        json={
            "name": "urgent",
            "channels": ["synthetic.chat"],
            "filter": {"keywords": ["urgent"]},
            "action_kind": "run_lens",
            "action_config": {"lens": "rule_lens"},
        },
    )
    ingest(client, [message(0, external_id="r0", content="urgent: call back")])
    calls = []
    with lenses.override_worker("llm", _down(calls)):
        ev = lenses.evaluate_rules()
        assert ev["triggered_lenses"][0]["queued_for_retry"] is True
        # Still down: one probe for the lens, the row stays queued.
        assert lenses.deliver_pending() == {"delivered": 0, "failed": 1}
    with lenses.override_worker("llm", _answer):
        assert lenses.deliver_pending() == {"delivered": 1, "failed": 0}
        assert lenses.deliver_pending() == {"delivered": 0, "failed": 0}
    assert len(_rows(client, "rule_lens")) == 1


def test_abandoned_running_row_is_resumed_and_concurrent_run_is_busy(client):
    _channel(client)
    ingest(client, [message(0, external_id="a0", content="hello")])
    tdef = _lens("crashy")
    with connect("app") as c:
        c.execute(
            "INSERT INTO tl.table_runs(table_id, trigger_kind, status, considered) VALUES (%s,'schedule','running',1)",
            (tdef["id"],),
        )
    with _lens_lock(int(tdef["id"])) as got:
        assert got
        assert lenses.run_lens("crashy")["status"] == "busy"
    with lenses.override_worker("llm", _answer):
        out = lenses.tick()
    entry = out["lenses"][0]
    assert entry["why"] == "resume_running" and entry["result"]["status"] == "done"
    with connect("app") as c:
        statuses = [r["status"] for r in c.execute("SELECT status FROM tl.table_runs WHERE table_id=%s ORDER BY id", (tdef["id"],))]
    assert statuses == ["failed", "done"]


def test_scheduled_lens_runs_once_per_fire(client):
    _channel(client)
    ingest(client, [message(0, external_id="s0", content="hello")])
    _lens("hourly", schedule="@hourly")
    with lenses.override_worker("llm", _answer):
        assert lenses.tick()["lenses"][0]["why"] == "scheduled"
        assert lenses.tick()["lenses"][0]["why"] == "up_to_date"
        nxt = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
        assert lenses.tick(now=nxt, dry_run=True)["lenses"][0]["why"] == "scheduled"


def test_cron_parsing_and_last_fire():
    z = ZoneInfo("America/Winnipeg")
    now = datetime(2026, 11, 22, 9, 7, tzinfo=z)
    assert scheduler.last_fire("0 */6 * * *", now, z) == datetime(2026, 11, 22, 6, 0, tzinfo=z)
    assert scheduler.last_fire("*/30 * * * *", now, z) == datetime(2026, 11, 22, 9, 0, tzinfo=z)
    assert scheduler.last_fire("@weekly", now, z) == datetime(2026, 11, 22, 0, 0, tzinfo=z)  # a Sunday
    assert scheduler.last_fire("30 10 * * 1-5", now, z) == datetime(2026, 11, 20, 10, 30, tzinfo=z)
    assert scheduler.last_fire("0 0 1 * *", now, z) == datetime(2026, 11, 1, 0, 0, tzinfo=z)
    for bad in ("every hour", "61 * * * *", "* * * *", "*/0 * * * *"):
        with pytest.raises(lenses.LensSpecError):
            scheduler.parse_schedule(bad)


def test_define_rejects_a_bad_schedule(client):
    _channel(client)
    with pytest.raises(lenses.LensSpecError):
        _lens("bad_sched", schedule="every 5 minutes")


def test_merged_batch_outage_reports_per_lens_and_keeps_cursors(client):
    _channel(client)
    ingest(client, [message(i, external_id=f"m{i}", content=f"note {i}") for i in range(2)])
    for name in ("merge_a", "merge_b"):
        lenses.define_lens(
            {
                "name": name,
                "channels": ["synthetic.chat"],
                "worker": "llm",
                "schedule": "0 * * * *",
                "prompt": "Classify.",
                "columns": [{"name": "topic", "type": "text", "nullable": False}],
            }
        )
    calls = []
    with lenses.override_worker("llm", _down(calls)):
        out = lenses.run_batch(["merge_a", "merge_b"])
    assert len(calls) == 1
    assert {r["status"] for r in out["lenses"]} == {"provider_unavailable"}
    assert _cursor("merge_a") == 0 and _cursor("merge_b") == 0
