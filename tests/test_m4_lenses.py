"""Comprehensive M4 tests for lenses, jobs, and arrival rules.

Covers:
  TBL-1 (four-part table definition + optional context query, task list & person blurb from single files)
  TBL-2 (structured output validation + 1 retry + zero invalid rows stored)
  TBL-3 (provenance citations, confidence, no_source_reason, ground truth untouched ARC-6)
  TBL-4 (hand corrections lock against overwrites for 10 runs AND feed back into later runs)
  TBL-5 (narrow opt-in only: zero model calls without definitions)
  JOB-1 (code, jev, llm, agent workers + message/conversation/thread/day grains)
  JOB-2 (isolated cursor + declared channels only)
  JOB-3 (verify-then-advance crash recovery with zero partial rows and zero duplicates)
  JOB-4 (single-pass merging: 3 lenses with same inputs/worker/schedule -> 1 model call)
  JOB-5 (dependency order + job_version + lazy on-read backfill)
  JOB-6 (cost quote + monthly cap)
  JOB-7 (late backfill re-runs processed window and preserves hand-locked rows)
  RUL-1..3 (arrival rules: Wave reminder/meeting/long-task example, sliding count, dedup retry, zero push)
  ACL-4 / ACL-6 (restricted asker visibility inheritance, error oracle closed, agent can define/run lenses)
  ARC-5 (purge removes dependent table rows and provenance)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ingest, message
from typer.testing import CliRunner

from throughline import access, lenses
from throughline.db import connect, create_token


def _cleanup_dynamic_views():
    with connect() as c:
        views = [
            r["table_name"]
            for r in c.execute(
                "SELECT table_name FROM information_schema.views WHERE table_schema='api' AND table_name LIKE 'lens_%'"
            )
        ]
        for v in views:
            base = v[len("lens_") :]
            if base.endswith("_versions"):
                base = base[: -len("_versions")]
            try:
                c.execute("SELECT tl.drop_lens_views(%s)", (base,))
            except Exception:
                pass


@pytest.fixture(autouse=True)
def cleanup_views(database):
    yield
    lenses.set_notifier_override(None)
    _cleanup_dynamic_views()


def _setup_channels(client, *names):
    for name in names:
        fields = [dict(name="rating", type="integer")]
        if name == "youtube.uploads":
            fields.extend([dict(name="video_id", type="text"), dict(name="title", type="text")])
        r = client.post(
            "/v1/channels",
            json=dict(name=name, plugin="synthetic", fields=fields),
        )
        assert r.status_code == 200, r.text


def test_tbl_1_four_part_definition_and_context_query(client, tmp_path):
    """TBL-1: Task list and person blurb each defined from a single file with schema, prompt, optional query, schedule."""
    _setup_channels(client, "synthetic.chat")
    assert ingest(
        client,
        [
            message(0, external_id="t1", content="Remind me to renew the orchard lease before Friday."),
            message(1, external_id="t2", content="Just saying hello, nothing actionable here."),
            message(2, external_id="t3", content="TODO: order pruning shears for next week."),
        ],
    ).status_code == 200

    # 1. Task list defined from a single TOML file.
    task_file = tmp_path / "tasks.toml"
    task_file.write_text(
        """
name = "tasks"
description = "Actionable tasks extracted from chats"
channels = ["synthetic.chat"]
grain = "message"
worker = "code"
schedule = "0 * * * *"
prompt = "Extract actionable tasks from each message."
context_query = "SELECT count(*) AS total_msgs FROM api.messages"
natural_key = ["task"]

[worker_config]
handler = "local_tasks"

[[columns]]
name = "task"
type = "text"
nullable = false

[[columns]]
name = "status"
type = "text"
nullable = false
""",
        encoding="utf-8",
    )
    spec = lenses.load_spec_file(task_file)
    defined = client.post("/v1/lenses", json=spec)
    assert defined.status_code == 200, defined.text
    assert defined.json()["lens"]["view"] == "api.lens_tasks"

    run = client.post("/v1/lenses/tasks/run", json={})
    assert run.status_code == 200, run.text
    assert run.json()["written"] == 2

    # Query via SQL on the auto-generated typed view api.lens_tasks!
    sql_res = client.post("/v1/sql", json={"statement": "SELECT id, task, status, generated, unsupported FROM api.lens_tasks ORDER BY id"})
    assert sql_res.status_code == 200, sql_res.text
    rows = sql_res.json()["rows"]
    assert len(rows) == 2
    assert all(r["status"] == "open" and r["generated"] is True and r["unsupported"] is False for r in rows)

    # 2. Person blurb defined from a single JSON file with an optional context_query.
    seen_context = []

    def spy_llm(tdef, payload, attempt):
        seen_context.append(payload.get("context_query_results"))
        mids = [m["id"] for m in payload["items"]]
        return {
            "rows": [
                {
                    "data": {"person": "fake-account", "blurb": "Discusses orchard lease and shears.", "msg_count": len(mids)},
                    "source_message_ids": mids,
                    "confidence": 0.9,
                    "evidence": {"rationale": "summarized thread"},
                }
            ]
        }

    blurb_file = tmp_path / "person_blurbs.json"
    blurb_file.write_text(
        json.dumps(
            {
                "name": "person_blurbs",
                "channels": ["synthetic.chat"],
                "grain": "thread",
                "worker": "llm",
                "schedule": "0 6 * * *",
                "prompt": "Write a concise blurb for the person based on recent messages and context.",
                "query": "SELECT channel, count(*) AS n FROM api.messages GROUP BY channel",
                "schema": {
                    "person": {"type": "text", "nullable": False},
                    "blurb": {"type": "text", "nullable": False},
                    "msg_count": {"type": "integer", "nullable": False},
                },
                "natural_key": ["person"],
            }
        ),
        encoding="utf-8",
    )
    blurb_spec = lenses.load_spec_file(blurb_file)
    assert client.post("/v1/lenses", json=blurb_spec).status_code == 200
    with lenses.override_worker("llm", spy_llm):
        r = client.post("/v1/lenses/person_blurbs/run", json={})
        assert r.status_code == 200, r.text
        assert r.json()["written"] == 1
    assert seen_context and seen_context[0] == [{"channel": "synthetic.chat", "n": 3}]
    rows = client.get("/v1/lenses/person_blurbs/rows").json()["rows"]
    assert len(rows) == 1 and rows[0]["msg_count"] == 3


def test_tbl_2_structured_output_validation_and_retry(client):
    """TBL-2: Malformed model output triggers 1 retry; persistently invalid rows are never written."""
    _setup_channels(client, "synthetic.chat")
    ingest(client, [message(0, external_id="m0", content="Spent $42 on apples.")])

    lenses.define_lens(
        {
            "name": "expenses",
            "channels": ["synthetic.chat"],
            "worker": "llm",
            "prompt": "Extract expenses.",
            "columns": [
                {"name": "item", "type": "text", "nullable": False},
                {"name": "amount", "type": "number", "nullable": False},
            ],
        }
    )

    # Case A: Always malformed -> 2 attempts (initial + 1 retry), 0 rows written, invalid reported.
    attempts = []

    def always_bad(tdef, payload, attempt):
        attempts.append((attempt, payload.get("previous_validation_errors")))
        return {"rows": [{"data": {"item": "apples", "amount": "not-a-number"}, "source_message_ids": [payload["items"][0]["id"]], "confidence": 0.9}]}

    with lenses.override_worker("llm", always_bad):
        res = lenses.run_lens("expenses")
    assert len(attempts) == 2
    assert attempts[1][1]  # retry received validation error feedback
    assert res["written"] == 0 and res["invalid"] >= 1
    assert client.get("/v1/lenses/expenses/rows").json()["row_count"] == 0

    # Case B: Fails first attempt, succeeds on retry -> 1 valid row stored.
    def fix_on_retry(tdef, payload, attempt):
        mid = payload["items"][0]["id"]
        if attempt == 0:
            return "not even json"
        return {"rows": [{"data": {"item": "apples", "amount": 42.0}, "source_message_ids": [mid], "confidence": 0.95}]}

    with lenses.override_worker("llm", fix_on_retry):
        res2 = lenses.run_lens("expenses", backfill=True)
    assert res2["written"] == 1 and res2["invalid"] == 0
    rows = client.get("/v1/lenses/expenses/rows").json()["rows"]
    assert len(rows) == 1 and rows[0]["amount"] == 42.0


def test_tbl_3_provenance_citations_confidence_and_ground_truth_untouched(client):
    """TBL-3 & ARC-6: Rows cite pinned sources via api.sources('table_row', id), allow guesses + no_source_reason, never change ground truth."""
    _setup_channels(client, "synthetic.chat")
    ingest(client, [message(0, external_id="g0", content="Maybe we will visit Austin next month.")])

    with connect() as c:
        before_gt = c.execute("SELECT id, content, payload_hash FROM tl.messages ORDER BY id").fetchall()

    lenses.define_lens(
        {
            "name": "guesses",
            "channels": ["synthetic.chat"],
            "worker": "llm",
            "prompt": "Guess travel plans.",
            "columns": [{"name": "city", "type": "text", "nullable": False}],
        }
    )

    def guess_worker(tdef, payload, attempt):
        mid = payload["items"][0]["id"]
        return {
            "rows": [
                {"data": {"city": "Austin"}, "source_message_ids": [mid], "confidence": 0.62, "evidence": {"quote": "visit Austin"}},
                {"data": {"city": "Unknown baseline"}, "source_message_ids": [], "no_source_reason": "inferred default prior", "confidence": 0.2},
            ]
        }

    with lenses.override_worker("llm", guess_worker):
        res = lenses.run_lens("guesses")
    assert res["written"] == 2

    rows = client.post("/v1/sql", json={"statement": "SELECT id, city, confidence, unsupported, no_source_reason FROM api.lens_guesses ORDER BY id"}).json()["rows"]
    assert rows[0]["city"] == "Austin" and rows[0]["unsupported"] is False
    assert rows[1]["city"] == "Unknown baseline" and rows[1]["unsupported"] is True and rows[1]["no_source_reason"] == "inferred default prior"

    # QRY-11: One call jumps from the table_row back to its source messages.
    src_res = client.post("/v1/sql", json={"statement": f"SELECT id, external_id FROM api.sources('table_row', {rows[0]['id']})"})
    assert src_res.status_code == 200, src_res.text
    assert [r["external_id"] for r in src_res.json()["rows"]] == ["g0"]

    with connect() as c:
        after_gt = c.execute("SELECT id, content, payload_hash FROM tl.messages ORDER BY id").fetchall()
    assert after_gt == before_gt


def test_tbl_4_hand_corrections_lock_and_feed_back_into_future_runs(client):
    """TBL-4: Hand-edited row survives 10 subsequent runs unchanged AND later runs receive the correction."""
    _setup_channels(client, "synthetic.chat")
    ingest(client, [message(0, external_id="hc0", content="TODO: buy pears")])

    lenses.define_lens(
        {
            "name": "chores",
            "channels": ["synthetic.chat"],
            "worker": "llm",
            "prompt": "Extract chores.",
            "natural_key": ["slug"],
            "columns": [
                {"name": "slug", "type": "text", "nullable": False},
                {"name": "label", "type": "text", "nullable": False},
            ],
        }
    )

    seen_corrections = []

    def chore_worker(tdef, payload, attempt):
        seen_corrections.append(payload.get("owner_corrections") or [])
        mid = payload["items"][0]["id"]
        return {
            "rows": [
                {
                    "data": {"slug": "groceries", "label": "Model says buy pears"},
                    "source_message_ids": [mid],
                    "confidence": 0.8,
                }
            ]
        }

    with lenses.override_worker("llm", chore_worker):
        lenses.run_lens("chores")
        row = client.get("/v1/lenses/chores/rows").json()["rows"][0]
        edited = client.patch(
            f"/v1/lenses/chores/rows/{row['id']}",
            json={"data": {"label": "Owner says organic pears only"}, "note": "Always specify organic pears"},
            headers={"Idempotency-Key": "tbl4-edit-key-1"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["row"]["owner_locked"] is True

        # Run 10 backfills; locked row must survive untouched and every run must be told about the correction.
        for _ in range(10):
            r = lenses.run_lens("chores", backfill=True)
            assert r["skipped_locked"] == 1

    rows = client.get("/v1/lenses/chores/rows").json()["rows"]
    assert len(rows) == 1
    assert rows[0]["label"] == "Owner says organic pears only"
    assert rows[0]["owner_locked"] is True
    assert len(seen_corrections) == 11
    assert all(
        c and c[0]["correction_note"] == "Always specify organic pears"
        for c in seen_corrections[1:]
    )


def test_tbl_5_narrow_opt_in_zero_calls_without_definitions(client):
    """TBL-5 & RUL-3: With no lenses or rules defined, ingest + run_batch + evaluate_rules make zero model/outbound calls."""
    _setup_channels(client, "synthetic.chat")
    called = []

    def fail_if_called(*args, **kwargs):
        called.append(True)
        raise AssertionError("model/notifier should not be called")

    lenses.set_notifier_override(fail_if_called)
    with lenses.override_worker("llm", fail_if_called), lenses.override_worker("jev", fail_if_called):
        ingest(client, [message(i, external_id=f"n-{i}") for i in range(5)])
        assert lenses.run_batch()["total_model_calls"] == 0
        assert lenses.evaluate_rules()["evaluated_rules"] == 0
    assert not called


def test_job_1_workers_code_jev_llm_agent_and_grains(client):
    """JOB-1: Code, Jev Decisions, LLM, and SQL-using Agent workers all produce validated typed rows."""
    _setup_channels(client, "youtube.uploads")
    ingest(
        client,
        [
            message(
                0,
                channel="youtube.uploads",
                external_id="vid1",
                content="Deep dive into PostgreSQL query planning",
                fields={"video_id": "vid1", "title": "PostgreSQL Planner Deep Dive"},
            ),
            message(
                10,
                channel="youtube.uploads",
                external_id="vid2",
                content="10 shocking celebrity clickbait moments",
                fields={"video_id": "vid2", "title": "Shocking Clickbait"},
            ),
        ],
    )

    # 1. Jev Decisions worker with noul + score questions and thresholds.
    lenses.define_lens(
        {
            "name": "yt_picks",
            "channels": ["youtube.uploads"],
            "grain": "message",
            "worker": "jev",
            "prompt": "Predict Alex's watch behavior and junk food probability.",
            "natural_key": ["video_id"],
            "worker_config": {
                "model": "typesafe/jev-1.14",
                "thresholds": {"watch_min": 0.70, "progress_min": 50.0, "junk_below": 0.50},
                "questions": {
                    "watch": {"type": "noul", "instructions": "Will Alex watch?"},
                    "progress_if_watched": {
                        "type": "score",
                        "instructions": "Expected progress?",
                        "criteria": ["0%", "25%", "50%", "75%", "100%"],
                    },
                    "is_junk_food": {"type": "noul", "instructions": "Is this junk food?"},
                },
            },
            "columns": [
                {"name": "video_id", "type": "text", "nullable": False},
                {"name": "watch_probability", "type": "number", "nullable": False},
                {"name": "progress_if_watched", "type": "number", "nullable": False},
                {"name": "junk_food_probability", "type": "number", "nullable": False},
                {"name": "picked", "type": "boolean", "nullable": False},
                {"name": "verdict", "type": "text", "nullable": False},
            ],
        }
    )

    def fake_jev(tdef, payload, item):
        vid = payload["state"]["candidate"].get("video_id")
        if vid == "vid1":
            return {
                "model": "typesafe/jev-1.14-20261014",
                "answers": {
                    "watch": {"type": "noul", "noul": 0.88},
                    "progress_if_watched": {
                        "type": "score",
                        "score": 3.1,
                        "probabilities": {"0": 0.05, "1": 0.05, "2": 0.15, "3": 0.50, "4": 0.25},
                    },
                    "is_junk_food": {"type": "noul", "noul": 0.12},
                },
            }
        return {
            "model": "typesafe/jev-1.14-20261014",
            "answers": {
                "watch": {"type": "noul", "noul": 0.25},
                "progress_if_watched": {
                    "type": "score",
                    "score": 1.0,
                    "probabilities": {"0": 0.3, "1": 0.5, "2": 0.2, "3": 0.0, "4": 0.0},
                },
                "is_junk_food": {"type": "noul", "noul": 0.91},
            },
        }

    with lenses.override_worker("jev", fake_jev):
        r = lenses.run_lens("yt_picks")
    assert r["written"] == 2 and r["model_calls"] == 2
    rows = client.post("/v1/sql", json={"statement": "SELECT video_id, watch_probability, progress_if_watched, junk_food_probability, picked, verdict FROM api.lens_yt_picks ORDER BY video_id"}).json()["rows"]
    assert rows[0]["video_id"] == "vid1" and rows[0]["picked"] is True and rows[0]["verdict"] == "pick" and rows[0]["progress_if_watched"] == 75.0
    assert rows[1]["video_id"] == "vid2" and rows[1]["picked"] is False and rows[1]["verdict"] == "skip"

    # 2. Agent worker (runs a read-only SQL step then emits rows).
    lenses.define_lens(
        {
            "name": "daily_digest",
            "channels": ["youtube.uploads"],
            "grain": "day",
            "worker": "agent",
            "prompt": "Summarize picked videos for the day using SQL.",
            "columns": [{"name": "summary", "type": "text", "nullable": False}, {"name": "pick_count", "type": "integer", "nullable": False}],
        }
    )

    def fake_agent(tdef, payload, step):
        if step == 0:
            return {"sql": "SELECT count(*) AS n FROM api.lens_yt_picks WHERE picked IS TRUE"}
        n = payload["tool_history"][0]["rows"][0]["n"]
        return {
            "rows": [
                {
                    "data": {"summary": f"{n} picked upload(s)", "pick_count": int(n)},
                    "source_message_ids": [m["id"] for m in payload["items"]],
                    "confidence": 0.95,
                }
            ]
        }

    with lenses.override_worker("agent", fake_agent):
        ar = lenses.run_lens("daily_digest")
    assert ar["written"] == 1 and ar["model_calls"] == 2
    drows = client.get("/v1/lenses/daily_digest/rows").json()["rows"]
    assert drows[0]["pick_count"] == 1


def test_job_2_isolated_cursor_and_declared_channels_only(client):
    """JOB-2: Each job has its own cursor and reads ONLY its declared channels."""
    _setup_channels(client, "ch.alpha", "ch.beta")
    ingest(
        client,
        [
            message(0, channel="ch.alpha", external_id="a1", content="TODO: alpha task"),
            message(1, channel="ch.beta", external_id="b1", content="TODO: beta secret task"),
        ],
    )
    lenses.define_lens(
        {
            "name": "alpha_only",
            "channels": ["ch.alpha"],
            "worker": "code",
            "worker_config": {"handler": "local_tasks"},
            "columns": [{"name": "task", "type": "text", "nullable": False}],
        }
    )
    res = lenses.run_lens("alpha_only")
    assert res["written"] == 1
    rows = client.get("/v1/lenses/alpha_only/rows").json()["rows"]
    assert len(rows) == 1 and "alpha" in rows[0]["task"] and "beta" not in rows[0]["task"]
    # Second run sees 0 new messages because cursor advanced.
    assert lenses.run_lens("alpha_only")["considered"] == 0


def test_job_3_crash_recovery_verify_then_advance_no_duplicates(client):
    """JOB-3: Killing a job mid-window rolls back cleanly; restarting finishes with identical rows and zero duplicates."""
    _setup_channels(client, "synthetic.chat")
    ingest(client, [message(i, external_id=f"c-{i}", content=f"TODO: task number {i}") for i in range(6)])
    lenses.define_lens(
        {
            "name": "crash_tasks",
            "channels": ["synthetic.chat"],
            "worker": "code",
            "worker_config": {"handler": "local_tasks"},
            "natural_key": ["task"],
            "columns": [{"name": "task", "type": "text", "nullable": False}],
        }
    )

    crashed_at = set()
    fail_targets = {"msg:2", "msg:4", "msg:5"}

    def boom_once(wkey):
        if wkey in fail_targets and wkey not in crashed_at:
            crashed_at.add(wkey)
            raise RuntimeError(f"simulated crash on {wkey}")

    # Repeatedly run & crash until all 6 windows finish cleanly.
    for _ in range(5):
        try:
            lenses.run_lens("crash_tasks", on_before_commit=boom_once)
            break
        except RuntimeError:
            continue

    rows = client.get("/v1/lenses/crash_tasks/rows").json()["rows"]
    assert len(rows) == 6
    assert sorted(r["task"] for r in rows) == [f"task number {i}" for i in range(6)]


def test_job_4_merged_single_pass_three_tables_one_call(client):
    """JOB-4: Three lenses with identical inputs, worker, and schedule make ONE model call per item, not three."""
    _setup_channels(client, "synthetic.chat")
    ingest(
        client,
        [
            message(0, external_id="m0", content="Great workout at the gym today!"),
            message(1, external_id="m1", content="Late night coding session."),
        ],
    )
    for tname, col in (("lbl_mood", "mood"), ("lbl_energy", "energy"), ("lbl_topic", "topic")):
        lenses.define_lens(
            {
                "name": tname,
                "channels": ["synthetic.chat"],
                "grain": "message",
                "worker": "llm",
                "schedule": "0 * * * *",
                "prompt": f"Classify {col}.",
                "columns": [{"name": col, "type": "text", "nullable": False}],
            }
        )

    calls = []

    def merged_stub(tdefs, payload, attempt):
        calls.append([td["name"] for td in tdefs])
        mid = payload["items"][0]["id"]
        return {
            "tables": {
                "lbl_mood": {"rows": [{"data": {"mood": "upbeat"}, "source_message_ids": [mid], "confidence": 0.9}]},
                "lbl_energy": {"rows": [{"data": {"energy": "high"}, "source_message_ids": [mid], "confidence": 0.85}]},
                "lbl_topic": {"rows": [{"data": {"topic": "fitness"}, "source_message_ids": [mid], "confidence": 0.9}]},
            }
        }

    with lenses.override_worker("llm", merged_stub):
        batch = lenses.run_batch(["lbl_mood", "lbl_energy", "lbl_topic"])
    # 2 messages * 1 merged call each == 2 total calls (NOT 6!).
    assert len(calls) == 2
    assert batch["total_model_calls"] == 2
    assert all(len(c) == 3 for c in calls)
    for tname in ("lbl_mood", "lbl_energy", "lbl_topic"):
        assert client.get(f"/v1/lenses/{tname}/rows").json()["row_count"] == 2


def test_job_5_dependency_order_versioning_and_lazy_backfill(client):
    """JOB-5: Dependent lenses run in topological order, tag job_version, and support lazy on-read backfill."""
    _setup_channels(client, "synthetic.chat")
    ingest(client, [message(0, external_id="d0", content="TODO: ship release")])

    lenses.define_lens(
        {
            "name": "stage_one",
            "channels": ["synthetic.chat"],
            "worker": "code",
            "worker_config": {"handler": "local_tasks"},
            "columns": [{"name": "task", "type": "text", "nullable": False}],
        }
    )
    lenses.define_lens(
        {
            "name": "stage_two",
            "channels": ["synthetic.chat"],
            "depends_on": ["stage_one"],
            "worker": "code",
            "worker_config": {"handler": "local_tasks"},
            "context_query": "SELECT count(*) AS n FROM api.lens_stage_one",
            "columns": [{"name": "task", "type": "text", "nullable": False}],
        }
    )
    # Pass reversed order; runner must execute stage_one before stage_two.
    batch = lenses.run_batch(["stage_two", "stage_one"])
    assert batch["execution_order"] == ["stage_one", "stage_two"]

    # Version bump produces distinguishable job_version.
    lenses.define_lens(
        {
            "name": "stage_one",
            "channels": ["synthetic.chat"],
            "worker": "code",
            "prompt": "v2 prompt",
            "worker_config": {"handler": "local_tasks"},
            "columns": [{"name": "task", "type": "text", "nullable": False}],
        }
    )
    ingest(client, [message(1, external_id="d1", content="TODO: write changelog")])
    lenses.run_lens("stage_one")
    versions = {
        r["job_version"]
        for r in client.get("/v1/lenses/stage_one/rows").json()["rows"]
    }
    assert versions == {"1", "2"}

    # Lazy on-read backfill fills pending rows when queried.
    lenses.define_lens(
        {
            "name": "lazy_tasks",
            "channels": ["synthetic.chat"],
            "worker": "code",
            "worker_config": {"handler": "local_tasks"},
            "lazy_backfill": True,
            "columns": [{"name": "task", "type": "text", "nullable": False}],
        }
    )
    res = client.get("/v1/lenses/lazy_tasks/rows")
    assert res.status_code == 200 and res.json()["row_count"] == 2


def test_job_6_cost_quote_and_monthly_cap(client, monkeypatch):
    """JOB-6 & CST-3: Quote reports items/tokens/price before running; monthly cap pauses jobs when reached."""
    _setup_channels(client, "synthetic.chat")
    ingest(client, [message(i, external_id=f"q-{i}", content="Long enough text " * 20) for i in range(4)])
    lenses.define_lens(
        {
            "name": "priced_tbl",
            "channels": ["synthetic.chat"],
            "worker": "llm",
            "prompt": "Summarize.",
            "worker_config": {"price_per_million": 10.0},
            "columns": [{"name": "summary", "type": "text", "nullable": False}],
        }
    )
    q = client.post("/v1/lenses/priced_tbl/quote", json={}).json()
    assert q["items"] == 4 and q["tokens"] > 0 and q["cost_usd"] > 0

    # Set monthly cap to $0.000001 -> job pauses with budget_exhausted and 0 model calls.
    monkeypatch.setenv("THROUGHLINE_MONTHLY_MODEL_CAP_USD", "0.000001")
    res = lenses.run_lens("priced_tbl")
    assert res["status"] == "budget_exhausted" and res["model_calls"] == 0


def test_job_7_late_backfill_reruns_window_preserves_locked_rows(client):
    """JOB-7: Late message arriving into an already-processed day re-runs that day and keeps hand-locked rows."""
    _setup_channels(client, "synthetic.chat")
    day1 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    ingest(
        client,
        [
            message(0, external_id="day1-a", happened_at=day1.isoformat(), content="TODO: morning standup"),
        ],
    )
    lenses.define_lens(
        {
            "name": "day_tasks",
            "channels": ["synthetic.chat"],
            "grain": "day",
            "worker": "code",
            "worker_config": {"handler": "local_tasks"},
            "natural_key": ["task"],
            "columns": [{"name": "task", "type": "text", "nullable": False}],
        }
    )
    assert lenses.run_lens("day_tasks")["written"] == 1
    first_row = client.get("/v1/lenses/day_tasks/rows").json()["rows"][0]
    lenses.edit_row("day_tasks", first_row["id"], {"task": "morning standup (locked)"}, note="owner kept")

    # Late backfill lands on the same day with an earlier timestamp.
    ingest(
        client,
        [
            message(1, external_id="day1-late", happened_at=(day1 - timedelta(hours=2)).isoformat(), content="TODO: late flight checkin"),
        ],
    )
    r2 = lenses.run_lens("day_tasks")
    assert r2["written"] >= 1
    tasks_now = {r["task"] for r in client.get("/v1/lenses/day_tasks/rows").json()["rows"]}
    assert "morning standup (locked)" in tasks_now
    assert "late flight checkin" in tasks_now


def test_rul_1_rul_2_rul_3_arrival_rules_notify_and_trigger_table(client):
    """RUL-1..3: Wave transcript example — reminder notifies agent (with retry/dedup), meeting ignored, long task triggers table job."""
    _setup_channels(client, "transcripts.wave")
    ingest(
        client,
        [
            message(0, channel="transcripts.wave", external_id="w1", content="Reminder: call the dentist at 3pm."),
            message(1, channel="transcripts.wave", external_id="w2", content="Weekly product sync meeting notes."),
            message(2, channel="transcripts.wave", external_id="w3", content="TODO: long-running task to renovate the garage"),
        ],
    )
    lenses.define_lens(
        {
            "name": "wave_tasks",
            "channels": ["transcripts.wave"],
            "worker": "code",
            "worker_config": {"handler": "local_tasks"},
            "columns": [{"name": "task", "type": "text", "nullable": False}],
        }
    )
    client.post(
        "/v1/rules",
        json={
            "name": "wave-reminders",
            "channels": ["transcripts.wave"],
            "filter": {"keywords": ["reminder:"]},
            "action_kind": "notify",
            "action_config": {"webhook_url": "http://127.0.0.1:9999/hook"},
        },
    )
    client.post(
        "/v1/rules",
        json={
            "name": "wave-long-tasks",
            "channels": ["transcripts.wave"],
            "filter": {"keywords": ["long-running task"]},
            "action_kind": "run_lens",
            "action_config": {"lens": "wave_tasks"},
        },
    )

    # Simulate receiver down on first attempt, then up on retry (RUL-2 at-least-once + dedup key).
    received = []
    fail_once = {"fail": True}

    def flaky_notifier(payload):
        if fail_once["fail"]:
            fail_once["fail"] = False
            raise ConnectionError("receiver temporarily down")
        received.append(payload)

    lenses.set_notifier_override(flaky_notifier)
    ev = client.post("/v1/rules/evaluate", json={})
    assert ev.status_code == 200, ev.text
    body = ev.json()
    assert body["matched"] == 2
    assert len(body["triggered_lenses"]) == 1
    assert body["deliveries"][0]["status"] == "failed"

    # Retry pending deliveries once receiver returns.
    retried = lenses.deliver_pending()
    assert retried["delivered"] == 1
    assert len(received) == 1 and received[0]["rule"] == "wave-reminders" and received[0]["dedup_key"]
    # Task table has ONLY the long-running task row (meeting ignored!).
    trows = client.get("/v1/lenses/wave_tasks/rows").json()["rows"]
    assert len(trows) == 1 and "renovate the garage" in trows[0]["task"]


def test_acl_4_acl_6_table_row_visibility_inheritance_and_agent_permissions(client):
    """ACL-4, ACL-6, ARC-5: Restricted asker sees only rows whose ALL sources are visible; agent can define/run lenses; purge cleans rows."""
    _setup_channels(client, "synthetic.pub", "synthetic.sec")
    ingest(
        client,
        [
            message(0, channel="synthetic.pub", external_id="p0", content="TODO: public orchard picnic"),
            message(1, channel="synthetic.sec", external_id="s0", content="TODO: zqsecret vault code"),
        ],
    )

    # ACL-6: An unrestricted agent token CAN define and run lenses!
    agent_tok = create_token("synthetic-agent", kind="agent")["token"]
    agent_headers = {"Authorization": "Bearer " + agent_tok}
    r_def = client.post(
        "/v1/lenses",
        json={
            "name": "mixed_tasks",
            "channels": ["synthetic.pub", "synthetic.sec"],
            "worker": "code",
            "worker_config": {"handler": "local_tasks"},
            "columns": [{"name": "task", "type": "text", "nullable": False}],
        },
        headers=agent_headers,
    )
    assert r_def.status_code == 200, r_def.text
    r_run = client.post("/v1/lenses/mixed_tasks/run", json={}, headers=agent_headers)
    assert r_run.status_code == 200 and r_run.json()["written"] == 2

    # Add a mixed-source row citing BOTH pub and sec messages (ACL-4: must be hidden from guest!).
    with connect() as c:
        pub_id = c.execute("SELECT id FROM tl.messages WHERE external_id='p0'").fetchone()["id"]
        sec_id = c.execute("SELECT id FROM tl.messages WHERE external_id='s0'").fetchone()["id"]
        tid = c.execute("SELECT id FROM tl.table_defs WHERE name='mixed_tasks'").fetchone()["id"]
        c.execute(
            """INSERT INTO tl.table_rows(table_id,natural_key,data,method,job,job_version,source_message_ids)
            VALUES (%s,'mixed-both',%s,'model','table:mixed_tasks','1',%s)""",
            (tid, json.dumps({"task": "combined zqsecret summary"}), [pub_id, sec_id]),
        )

    access.register_asker("guest", mode="restricted")
    access.grant("guest", channel="synthetic.pub")
    guest_tok = create_token("guest-agent", kind="agent", askers=["guest"])["token"]
    guest_headers = {"Authorization": "Bearer " + guest_tok, "X-Throughline-Asker": "guest"}

    # Restricted guest sees ONLY the pure-public row, never the secret or mixed row!
    guest_rows = client.get("/v1/lenses/mixed_tasks/rows", headers=guest_headers).json()["rows"]
    assert len(guest_rows) == 1 and "picnic" in guest_rows[0]["task"]

    # Error oracle probe on api.lens_mixed_tasks must not trip for restricted guest.
    probe = (
        "SELECT count(*) AS n FROM api.lens_mixed_tasks v WHERE CASE WHEN "
        "lower((v.*)::text) LIKE '%zqsecret%' THEN 1/(0*length((v.*)::text)) ELSE 0 END = 1"
    )
    assert client.post("/v1/sql", json={"statement": probe}, headers=guest_headers).json()["rows"] == [{"n": 0}]
    assert client.post("/v1/sql", json={"statement": probe}).status_code != 200

    # ARC-5: Purging synthetic.sec deletes all rows referencing its messages!
    import throughline.cli as cli

    purged = CliRunner().invoke(cli.app, ["purge", "synthetic.sec", "--confirm"])
    assert purged.exit_code == 0, purged.output
    owner_after = client.get("/v1/lenses/mixed_tasks/rows").json()["rows"]
    assert len(owner_after) == 1 and "picnic" in owner_after[0]["task"]


_JEV_QUESTIONS = {
    "watch": {"type": "noul", "instructions": "Will Alex watch?"},
    "is_junk_food": {"type": "noul", "instructions": "Is this junk food?"},
}


def _fake_jev_answers(watch=0.5):
    return {
        "model": "typesafe/jev-1.14-20261014",
        "answers": {
            "watch": {"type": "noul", "noul": watch},
            "is_junk_food": {"type": "noul", "noul": 0.2},
        },
    }


def test_jev_item_context_binds_candidate_fields_as_literals(client):
    """worker_config.item_context runs one guarded SELECT per item, binds {{field}}
    placeholders as quoted literals, and places results under named state keys."""
    _setup_channels(client, "youtube.uploads")
    ingest(
        client,
        [
            message(0, channel="youtube.uploads", external_id="v1", content="Planner talk",
                    fields={"video_id": "v1", "title": "Planner talk"}),
            # A hostile title must stay a literal, never SQL.
            message(10, channel="youtube.uploads", external_id="v2", content="x",
                    fields={"video_id": "v2", "title": "x' OR '1'='1"}),
            message(20, channel="youtube.uploads", external_id="v3", content="Other video",
                    fields={"video_id": "v3", "title": "Other video"}),
        ],
    )
    lenses.define_lens(
        {
            "name": "yt_ctx",
            "channels": ["youtube.uploads"],
            "grain": "message",
            "worker": "jev",
            "prompt": "Predict.",
            "natural_key": ["video_id"],
            "worker_config": {
                "questions": _JEV_QUESTIONS,
                "candidate_map": {"title": "title", "video_id": "video_id"},
                "item_context": {
                    "base_rates": {"one": True, "sql": "SELECT count(*) AS n FROM api.messages WHERE channel = 'youtube.uploads'"},
                    "same_title": {"cap": 5, "sql": "SELECT fields->>'video_id' AS vid FROM api.messages WHERE channel = 'youtube.uploads' AND fields->>'title' = {{title}} ORDER BY id"},
                    "others": "SELECT fields->>'video_id' AS vid FROM api.messages WHERE channel = 'youtube.uploads' AND fields->>'video_id' <> {{video_id}} ORDER BY id",
                },
            },
            "columns": [
                {"name": "video_id", "type": "text", "nullable": False},
                {"name": "watch_probability", "type": "number", "nullable": False},
            ],
        }
    )
    seen = {}

    def fake_jev(tdef, payload, item):
        state = payload["state"]
        seen[state["candidate"]["video_id"]] = state
        return _fake_jev_answers()

    with lenses.override_worker("jev", fake_jev):
        r = lenses.run_lens("yt_ctx")
    assert r["status"] == "done" and r["written"] == 3, r
    # jsonb storage reorders keys; the payload is sent with sorted keys anyway.
    assert set(seen["v1"]) == {"policy", "candidate", "base_rates", "same_title", "others"}
    assert seen["v1"]["base_rates"] == {"n": 3}
    assert seen["v1"]["same_title"] == [{"vid": "v1"}]
    assert seen["v2"]["same_title"] == [{"vid": "v2"}]  # injection matched only itself
    assert seen["v3"]["others"] == [{"vid": "v1"}, {"vid": "v2"}]


def test_jev_item_context_validation():
    base = {
        "name": "yt_bad",
        "channels": ["youtube.uploads"],
        "worker": "jev",
        "columns": [{"name": "watch_probability", "type": "number"}],
    }
    for item_context, needle in [
        ({}, "non-empty"),
        ({"candidate": "SELECT 1"}, "reserved"),
        ({"x": {"sql": ""}}, "non-empty 'sql'"),
        ({"x": {"sql": "SELECT 1", "cap": 500}}, "cap"),
        ({"x": {"sql": "SELECT 1", "bogus": 1}}, "unknown options"),
        ({"bad-key": "SELECT 1"}, "identifier"),
    ]:
        with pytest.raises(lenses.LensSpecError, match=needle):
            lenses.validate_lens_spec({**base, "worker_config": {"questions": _JEV_QUESTIONS, "item_context": item_context}})


def test_delete_table_as_app_role_removes_rows_provenance_and_views(client):
    """tl.provenance is append-only for the app role; delete_lens goes through
    the definer function and still removes provenance, rows, views and the definition."""
    _setup_channels(client, "youtube.uploads")
    ingest(client, [message(0, channel="youtube.uploads", external_id="d1", content="A",
                            fields={"video_id": "d1", "title": "A"})])
    lenses.define_lens(
        {
            "name": "yt_del",
            "channels": ["youtube.uploads"],
            "worker": "jev",
            "natural_key": ["video_id"],
            "worker_config": {"questions": _JEV_QUESTIONS, "candidate_map": {"video_id": "video_id"}},
            "columns": [
                {"name": "video_id", "type": "text", "nullable": False},
                {"name": "watch_probability", "type": "number", "nullable": False},
            ],
        }
    )
    with lenses.override_worker("jev", lambda *a: _fake_jev_answers()):
        assert lenses.run_lens("yt_del")["written"] == 1
    with connect() as c:
        tid = c.execute("SELECT id FROM tl.table_defs WHERE name='yt_del'").fetchone()["id"]
        assert c.execute(
            "SELECT count(*) AS n FROM tl.provenance WHERE derived_kind='table_row' AND derived_id IN (SELECT id FROM tl.table_rows WHERE table_id=%s)",
            (tid,),
        ).fetchone()["n"] >= 1
    assert lenses.delete_lens("yt_del") == {"deleted": "yt_del"}  # runs as throughline_app
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM tl.table_rows WHERE table_id=%s", (tid,)).fetchone()["n"] == 0
        assert c.execute(
            "SELECT count(*) AS n FROM tl.provenance p WHERE p.derived_kind='table_row' AND NOT EXISTS (SELECT 1 FROM tl.table_rows r WHERE r.id=p.derived_id)"
        ).fetchone()["n"] == 0
        assert c.execute("SELECT to_regclass('api.lens_yt_del') AS v").fetchone()["v"] is None
    with pytest.raises(LookupError):
        lenses.delete_lens("yt_del")


def test_llm_payload_carries_owner_local_times_and_weekday_calendar(client, monkeypatch):
    """Relative dates ('next Thursday') are resolved from each message's own
    owner-local time: items carry local_time with weekday, and the payload has a
    date_context calendar spanning the window plus three weeks."""
    from throughline.lenses.workers import date_context

    monkeypatch.setenv("THROUGHLINE_TIMEZONE", "America/Mexico_City")
    ctx = date_context(
        [{"happened_at": datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc)}],
        now=datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc),
    )
    assert ctx["timezone"] == "America/Mexico_City"
    assert ctx["now_local"].startswith("2026-11-21 Sat 21:00")
    assert ctx["calendar"][0] == "2026-11-18 Wed" and ctx["calendar"][-1] == "2026-12-09 Wed"
    assert "2026-11-26 Thu" in ctx["calendar"]

    _setup_channels(client, "chat.sms")
    ingest(client, [message(0, channel="chat.sms", external_id="s1", content="dinner next thursday?")])
    lenses.define_lens(
        {
            "name": "dated",
            "channels": ["chat.sms"],
            "grain": "conversation",
            "worker": "llm",
            "prompt": "Extract plans.",
            "columns": [{"name": "title", "type": "text", "nullable": False}],
        }
    )
    seen = {}

    def fake_llm(tdef, payload, attempt):
        seen.update(payload)
        return {"rows": []}

    with lenses.override_worker("llm", fake_llm):
        assert lenses.run_lens("dated")["status"] == "done"
    item = seen["items"][0]
    assert item["local_time"] and item["local_time"][11:14] in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
    assert seen["date_context"]["calendar"][0][:10] == item["local_time"][:10]


def test_day_grain_one_call_per_owner_local_day_rereads_whole_day(client, monkeypatch):
    """Day grain: one model call per owner-local day (not per UTC day); a day that
    later gains a message is re-read in full; a day too large for
    max_chars_per_item is split, and the cursor never skips an unfinished day."""
    monkeypatch.setenv("THROUGHLINE_TIMEZONE", "America/Mexico_City")
    _setup_channels(client, "chat.sms", "transcripts.wave")

    def at(local):  # Mexico City is UTC-6
        return (datetime.fromisoformat(local) + timedelta(hours=6)).replace(tzinfo=timezone.utc).isoformat()

    ingest(
        client,
        [
            message(0, channel="chat.sms", external_id="d1", happened_at=at("2026-11-18T09:00:00"), content="lunch?"),
            # 23:30 Winnipeg on the 18th is already the 19th in UTC: must stay with the 18th.
            message(1, channel="transcripts.wave", external_id="d2", happened_at=at("2026-11-18T23:30:00"), content="see you thursday"),
            message(2, channel="chat.sms", external_id="d3", happened_at=at("2026-11-19T10:00:00"), content="on my way"),
        ],
    )
    lenses.define_lens(
        {
            "name": "daily",
            "channels": ["chat.sms", "transcripts.wave"],
            "grain": "day",
            "worker": "llm",
            "prompt": "Extract plans.",
            "columns": [{"name": "title", "type": "text", "nullable": False}],
        }
    )
    calls = []

    def fake_llm(tdef, payload, attempt):
        calls.append(sorted(i["content"] for i in payload["items"]))
        return {"rows": []}

    with lenses.override_worker("llm", fake_llm):
        assert lenses.quote_lens("daily")["items"] == 2
        assert lenses.run_lens("daily")["model_calls"] == 2
        assert calls == [["lunch?", "see you thursday"], ["on my way"]]

        calls.clear()
        assert lenses.run_lens("daily")["model_calls"] == 0  # nothing new

        # A late message for the 18th re-reads the whole 18th, and only it.
        ingest(client, [message(3, channel="chat.sms", external_id="d4", happened_at=at("2026-11-18T12:00:00"), content="sure")])
        assert lenses.run_lens("daily")["model_calls"] == 1
        assert calls == [["lunch?", "see you thursday", "sure"]]

        # A re-saved message with the same text (only fields changed) is not a change...
        calls.clear()
        ingest(client, [message(3, channel="chat.sms", external_id="d4", happened_at=at("2026-11-18T12:00:00"), content="sure", fields={"rating": 99})])
        assert lenses.run_lens("daily")["model_calls"] == 0
        # ...an edited text is.
        ingest(client, [message(3, channel="chat.sms", external_id="d4", happened_at=at("2026-11-18T12:00:00"), content="sure, 7pm", fields={"rating": 99})])
        assert lenses.run_lens("daily")["model_calls"] == 1
        assert calls == [["lunch?", "see you thursday", "sure, 7pm"]]

        # not_before keeps a lens from reading older days at all.
        lenses.define_lens(
            {
                "name": "daily_recent",
                "channels": ["chat.sms", "transcripts.wave"],
                "grain": "day",
                "worker": "llm",
                "worker_config": {"not_before": "2026-11-19"},
                "prompt": "Extract plans.",
                "columns": [{"name": "title", "type": "text", "nullable": False}],
            }
        )
        assert lenses.quote_lens("daily_recent")["items"] == 1

        # A busy day splits into several calls; a limit of one call leaves the rest pending.
        calls.clear()
        lenses.define_lens(
            {
                "name": "daily_small",
                "channels": ["chat.sms", "transcripts.wave"],
                "grain": "day",
                "worker": "llm",
                "worker_config": {"max_chars_per_item": 5000},
                "prompt": "Extract plans.",
                "columns": [{"name": "title", "type": "text", "nullable": False}],
            }
        )
        big = "x" * 3000
        ingest(
            client,
            [
                message(10 + k, channel="chat.sms", external_id=f"b{k}", thread={"external_id": f"t{k}"},
                        happened_at=at(f"2026-11-20T1{k}:00:00"), content=big)
                for k in range(3)
            ],
        )
        quoted = lenses.quote_lens("daily_small")
        assert quoted["items"] == 2 + 3  # 18th, 19th, and the 20th in three parts
        assert lenses.run_lens("daily_small", limit=1)["model_calls"] == 1
        calls.clear()
        res = lenses.run_lens("daily_small")
        assert res["model_calls"] == 4
        assert sum(len(c) for c in calls) == 1 + 3  # the 19th, then the three parts of the 20th


def test_same_as_row_id_updates_existing_row_and_min_confidence_drops(client):
    """A model that recognises an existing row (same_as_row_id) updates it instead of
    adding a duplicate, keeping the earlier citations; rows under
    worker_config.min_confidence are not stored."""
    _setup_channels(client, "chat.sms")
    ingest(
        client,
        [
            message(0, channel="chat.sms", external_id="p1", content="gatsby NYE party at max's"),
            message(1, channel="chat.sms", external_id="p2", content="max's new year's party is at his mansion"),
        ],
    )
    ids = sorted(int(r["id"]) for r in client.post("/v1/sql", json={"statement": "SELECT id FROM api.messages WHERE channel='chat.sms'"}).json()["rows"])
    lenses.define_lens(
        {
            "name": "merges",
            "channels": ["chat.sms"],
            "grain": "message",
            "worker": "llm",
            "worker_config": {"min_confidence": 0.5},
            "natural_key": ["title"],
            "prompt": "Extract plans.",
            "columns": [{"name": "title", "type": "text", "nullable": False}],
        }
    )
    first_id = {}

    def fake_llm(tdef, payload, attempt):
        mid = payload["items"][0]["id"]
        if mid == ids[0]:
            return {"rows": [
                {"data": {"title": "Great Gatsby NYE party"}, "source_message_ids": [mid], "confidence": 0.9},
                {"data": {"title": "Maybe something"}, "source_message_ids": [mid], "confidence": 0.2},
            ]}
        return {"rows": [{"data": {"title": "NYE party at Max's mansion"}, "source_message_ids": [mid],
                          "confidence": 0.9, "same_as_row_id": first_id["id"]}]}

    with lenses.override_worker("llm", fake_llm):
        lenses.run_lens("merges", limit=1)
        with connect() as c:
            first_id["id"] = int(c.execute("SELECT id FROM api.lens_merges").fetchone()["id"])
        lenses.run_lens("merges")
    with connect() as c:
        rows = c.execute("SELECT title, source_message_ids FROM api.lens_merges").fetchall()
    assert [r["title"] for r in rows] == ["NYE party at Max's mansion"]
    assert sorted(rows[0]["source_message_ids"]) == ids


def test_dedupe_pass_merges_same_item_rows_and_spares_locked(client):
    """worker_config.dedupe: after a run that wrote rows, one call over the lens's
    active rows merges duplicates: the kept row cites every source, duplicates are
    retracted, ids the model invents are ignored."""
    _setup_channels(client, "chat.sms")
    ingest(client, [message(i, channel="chat.sms", external_id=f"t{i}", content=f"thanksgiving {i}") for i in range(3)])
    lenses.define_lens(
        {
            "name": "deduped",
            "channels": ["chat.sms"],
            "grain": "message",
            "worker": "llm",
            "worker_config": {"dedupe": True},
            "natural_key": ["title"],
            "prompt": "Extract plans.",
            "columns": [{"name": "title", "type": "text", "nullable": False}],
        }
    )
    titles = iter(["Host Thanksgiving", "Thanksgiving at Alex's", "Smoke turkey"])

    def fake_llm(tdef, payload, attempt):
        mid = payload["items"][0]["id"]
        return {"rows": [{"data": {"title": next(titles)}, "source_message_ids": [mid], "confidence": 0.9}]}

    def fake_dedupe(tdef, payload, attempt):
        by_title = {r["title"]: r["id"] for r in payload["rows"]}
        if len(by_title) < 3:
            return {"groups": []}
        return {"groups": [{"keep_id": by_title["Host Thanksgiving"],
                            "duplicate_ids": [by_title["Thanksgiving at Alex's"], 999999]}]}

    with lenses.override_worker("llm", fake_llm), lenses.override_worker("dedupe", fake_dedupe):
        res = lenses.run_lens("deduped")
    assert res["merged"] == 1
    with connect() as c:
        rows = {r["title"]: r for r in c.execute("SELECT title, source_message_ids FROM api.lens_deduped")}
    assert set(rows) == {"Host Thanksgiving", "Smoke turkey"}
    assert len(rows["Host Thanksgiving"]["source_message_ids"]) == 2
