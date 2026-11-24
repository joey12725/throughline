"""Benchmark arms and suites for v1.1 on the public twin: stage diagnostics,
ablations, page-assisted and corrupted-page runs, temporal and leakage
cases, sufficiency, latency, the three LIB-13 configurations with their
failure cases and the portability contract. The twin is loaded once per
module under the offline-local configuration (deterministic local
providers); the system is driven over HTTP through the test client."""

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from conftest import truncate_tl
from fastapi.testclient import TestClient

from bench import (
    arms,
    behaviours,
    configs,
    faults,
    latency,
    portability,
    run,
    scenarios,
    score,
    scoreboard,
)
from throughline import embeddings
from throughline.api import create_app
from throughline.db import connect, create_token


@contextmanager
def environment(values):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def drain_worker():
    for _ in range(1000):
        if not embeddings.run_once():
            return


@contextmanager
def loaded_twin(seed=47, config=configs.DEFAULT):
    with environment(configs.environment(config)):
        with connect() as c:
            truncate_tl(c)
        token = create_token("synthetic bench owner")["token"]
        with TestClient(create_app()) as client:
            client.headers["Authorization"] = "Bearer " + token
            corpus = scenarios.build(seed)
            questions, phase_times, loaded = run.prepare(
                client, corpus, after_load=drain_worker
            )
            yield SimpleNamespace(
                client=client,
                corpus=corpus,
                questions=questions,
                phase_times=phase_times,
                loaded=loaded,
                available=arms.probe(client),
            )


@pytest.fixture(scope="module")
def twin(database):
    with loaded_twin() as world:
        yield world


@pytest.fixture(scope="module")
def quick(twin):
    """Two questions of every class: the matrix sample."""
    return run.sample(twin.questions, per_category=2)


def answerable(questions):
    return [q for q in questions if q.required_groups() and not q.needs]


def test_ben_9_three_stage_recall_reported(twin, quick):
    for name in ("recall", "recall_rerank"):
        arm = arms.get(name)
        for question in answerable(quick)[:8]:
            result = arm.run(twin.client, question)
            assert result.status == "ok", result.reason
            assert set(arms.STAGES) <= set(result.stages)
            # Stage ids are message ids, so they score like the records.
            stage = score.stage_recall(result.stages, question)
            assert set(stage) >= set(arms.STAGES)
            assert stage["before_rerank"] >= stage["after_selection"] - 1e-9
            assert set(result.records) <= set(result.stages["after_selection"]) | {
                m for ms in result.spans.values() for m in ms
            }
    summary, _ = run.run_arm(
        twin.client, arms.get("recall_rerank"), answerable(quick), twin.available
    )
    assert set(arms.STAGES) <= set(summary["overall"]["stage_recall"])
    # The reranker really ran: its stage is reported by the system.
    result = arms.get("recall_rerank").run(twin.client, answerable(quick)[0])
    assert "after_rerank" in result.execution["stage_candidates"]


def test_ben_10_group_coverage_beside_f05(twin, quick):
    summary, rows = run.run_arm(twin.client, arms.get("recall"), quick, twin.available)
    scored = [r for r in rows if r["status"] == "ok" and "f05" in r]
    assert scored and all("covered" in r and "group_fraction" in r for r in scored)
    overall = summary["overall"]
    assert overall["f05"] is not None and overall["group_coverage"] is not None
    # Two facts about one result: some question is fully covered at an F0.5
    # below one (noise kept), which F0.5 alone would hide.
    assert any(r["covered"] and r["f05"] < 1.0 for r in scored)
    for category, part in summary["categories"].items():
        if part["f05"] is not None:
            assert part["group_coverage"] is not None, category


def test_ben_16_matrix_arms_independent_settings_recorded(twin, quick):
    report, rows = run.matrix(twin.client, quick, twin.available)
    assert set(report["arms"]) == set(arms.V11_ARMS)
    assert report["baseline"] == "recall" and report["tie_points"] == 5.0
    assert report["config"]["name"] == "offline-local"
    assert report["config"]["settings"]["THROUGHLINE_EMBEDDING_PROVIDER"] == "local"
    assert "commit" in report["config"]
    base = report["arms"]["recall"]["settings"]
    for name, summary in report["arms"].items():
        assert summary["status"] == "ok", (name, summary.get("reason"))
        assert summary["overall"]["errors"] == 0, name
        assert summary["overall"]["failure_rate"] == 0.0
        if name.startswith("recall_"):
            # One switch differs from the baseline, and it is recorded.
            changed = {
                k
                for k in set(base) | set(summary["settings"])
                if base.get(k) != summary["settings"].get(k)
            }
            assert changed, name
            assert changed <= {"request", "follow_support", "link_expansion"}
        if name != "recall":
            verdict = report["verdicts"][name]["overall"]
            assert verdict in ("better", "worse", "tie", "skipped"), name
    assert json.loads(json.dumps(report, default=str))["suite"] == "ablation"
    assert {r["arm"] for r in rows} == set(arms.V11_ARMS)


def test_ben_11_pages_help_on_page_assisted_subset(twin):
    report = run.world_model_delta(twin.client, twin.questions, twin.available)
    assert report["questions"] == scenarios.PER_CLASS
    off, on = report["arms"]["recall_pages_off"], report["arms"]["recall_pages_on"]
    assert off["status"] == on["status"] == "ok"
    assert report["f05_delta_points"] > 5.0, report
    assert report["group_coverage_delta_points"] > 5.0
    assert report["verdict"] == "better"


def test_ben_12_raw_evidence_survives_each_mutation(twin):
    report = run.resilience(twin.client, twin.questions, twin.available)
    for name in ("recall", "recall_pages_on"):
        mutations = report["arms"][name]["mutations"]
        assert set(mutations) == set(scenarios.MUTATIONS)
        for mutation, part in mutations.items():
            assert part["scored"] == part["questions"] > 0, (name, mutation)
            assert part["raw_survives"] == 1.0, (name, mutation, part)
            assert part["page_only"] == 0


def test_ben_13_cluster_and_duplicate_scores(twin):
    report = run.exploration(twin.client, twin.questions, twin.available)
    assert report["status"] == "ok" and set(report["grains"]) == {"episode", "support"}
    for grain, part in report["grains"].items():
        assert part["scored"] == scenarios.PER_CLASS
        assert part["cluster_coverage"] is not None and part["cluster_coverage"] > 0
        assert part["duplicate_grouping_accuracy"] is not None
        assert all(m["by"] == grain for m in part["manifests"])
    episodes = report["grains"]["episode"]["manifests"]
    # The episode manifest counts incidents apart from records.
    assert all(m["unit"] == "episode" and m["incidents"] is not None for m in episodes)
    assert all(m["records"] >= m["incidents"] for m in episodes)


def test_ben_14_role_coverage(twin):
    report = run.patterns(twin.client, twin.questions, twin.available)
    part = report["arms"]["recall"]
    assert part["status"] == "ok" and len(part["per_question"]) == scenarios.PER_CLASS
    assert part["role_coverage"] is not None and part["role_coverage"] > 0.5


def test_ben_15_temporal_cases(twin):
    report = run.temporal(twin.client, twin.questions, twin.available)
    variants = report["arms"]["recall"]["variants"]
    assert set(variants) == set(scenarios.TEMPORAL_VARIANTS)
    for name, part in variants.items():
        # Recall can pose every temporal case (time basis, as_of, known_by).
        assert part["skipped"] == 0 and part["scored"] == part["questions"] > 0, name
        assert part["leaked"] == 0, (name, part)
        assert part["group_coverage"] is not None
    assert variants["subject_time"]["group_coverage"] == 1.0
    assert variants["late_backfill"]["group_coverage"] == 1.0
    # The keyword arm cannot pose subject time or known_by: skipped, not zero.
    keyword = run.temporal(
        twin.client, twin.questions, twin.available, arms=("keyword",)
    )["arms"]["keyword"]["variants"]
    assert keyword["subject_time"]["skipped"] == keyword["subject_time"]["questions"]
    assert keyword["subject_time"]["f05"] is None


def test_ben_21_no_future_leakage_in_records_or_stages_and_skipped_legs_reported(twin):
    report = run.leakage(
        twin.client,
        twin.questions,
        twin.available,
        arms=("recall", "recall_pages_on", "recall_link_expansion"),
    )
    assert report["not_covered"] == ["identity_state"]
    for name, part in report["arms"].items():
        assert part["status"] == "ok" and part["scored"] > 0
        assert part["leaked"] == 0, [
            (c["question"], c["leaked_records"], c["leaked_stages"])
            for c in part["cases"]
            if c.get("clean") is False
        ]
        assert part["unreported_legs"] == []
        variants = {c["variant"] for c in part["cases"] if c["status"] == "ok"}
        assert {"fact", "page_revision", "revision_as_of"} <= variants
        if name == "recall_link_expansion":
            # The link-derived case (a link added after the phase) is posed
            # only by the arm that follows links.
            assert "link" in variants
        # Every reported stage was checked, legs included.
        checked = set().union(
            *(c["stages_checked"] for c in part["cases"] if c["status"] == "ok")
        )
        assert {"before_rerank", "after_selection"} <= checked
        assert any(s.startswith("leg:") for s in checked)
        if name == "recall_link_expansion":
            assert "expand:links" in checked
    # The vector leg cannot honour as_of: it is reported, and it did not run.
    assert report["arms"]["recall"]["legs_not_honouring_as_of"].get("vector", 0) > 0


def test_ben_20_abstention_and_incomplete_disclosed(twin):
    subset = run.select(twin.questions, where=lambda q: q.no_answer) + answerable(
        run.sample(twin.questions, 1)
    )
    question = run.select(twin.questions, "exact_token")[0]
    faults_seen = faults.run_all(twin.client, question)
    report = run.sufficiency(
        twin.client, subset, twin.available, fault_cases=faults_seen
    )
    part = report["arms"]["recall"]
    assert part["status"] == "ok"
    assert part["abstention"] is not None and part["group_coverage"] is not None
    assert part["incomplete_cases"] == scenarios.PER_CLASS
    # Import-gap questions are declared incomplete with the gap named.
    assert part["incomplete_disclosed"] == 1.0, part
    by_fault = {c["fault"]: c for c in report["faults"]}
    assert set(by_fault) == {
        "tiny_deadline",
        "row_cap",
        "embedding_closed_port",
        "sql_row_cap",
    }
    for name, case in by_fault.items():
        assert case["status"] == "ok" and case["declared"], (name, case)
        assert case["correct"], (name, case)


def test_ben_17_librarian_value_report_fields(twin, quick):
    subset = answerable(quick)[:8]
    report = run.librarian_value(twin.client, subset, twin.available)
    for key in (
        "f05_delta_points",
        "group_coverage_delta_points",
        "latency_delta_ms",
        "cost_delta",
        "failure_rate",
        "threshold_points",
        "verdict",
        "paired",
    ):
        assert key in report, key
    assert report["threshold_points"] == 5.0
    assert report["paired"] == len(subset)
    assert report["verdict"] in ("benefit", "tie", "worse")
    assert report["failure_rate"]["delegated"] == 0.0
    # Without a librarian the comparison is skipped, never a zero.
    absent = run.librarian_value(twin.client, subset, twin.available - {"librarian"})
    assert absent["verdict"] == "skipped" and absent["f05_delta_points"] is None
    assert absent["arms"]["librarian"]["status"] == "skipped"


def test_lib_13_three_configurations_equal_budgets_with_citation_faithfulness_and_context_tokens(
    twin, quick
):
    report = run.lib13(twin.client, answerable(quick)[:10], twin.available)
    budgets = list(report["budgets"].values())
    assert len(budgets) == 3 and all(b == budgets[0] for b in budgets)
    assert set(report["configurations"]) == {"ordinary", "equipped", "delegated"}
    for name, summary in report["configurations"].items():
        assert summary["status"] == "ok", (name, summary.get("reason"))
        overall = summary["overall"]
        assert overall["errors"] == 0, name
        for key in (
            "f05",
            "group_coverage",
            "stage_recall",
            "tokens",
            "model_cost",
            "latency_ms",
            "turns",
            "failure_rate",
            "citation_faithfulness",
            "context_tokens",
        ):
            assert key in overall, (name, key)
        assert overall["citation_faithfulness"] is not None, name
        assert 0.0 <= overall["citation_faithfulness"] <= 1.0
        assert overall["context_tokens"] > 0 and overall["turns"] >= 1
    turns = {n: s["overall"]["turns"] for n, s in report["configurations"].items()}
    assert turns["delegated"] <= turns["equipped"]


def test_lib_13_failure_cases(twin):
    report = run.lib13_failures(
        twin.client,
        twin.corpus,
        twin.questions,
        twin.available,
        principals=behaviours.LocalPrincipals(),
        in_process=True,
    )
    cases = {c["case"]: c for c in report["cases"]}
    assert set(cases) == {
        "page_stale",
        "page_contradictory",
        "ambiguous_identity",
        "import_gap",
        "prompt_injection",
        "controller_invalid_sql",
        "controller_timeout",
        "permission_change",
    }
    for name, case in cases.items():
        assert case["status"] == "ok", (name, case)
        assert case["disclosed"] and not case["confident_wrong"], (name, case)
        assert case["correct"], name
    ambiguous = cases["ambiguous_identity"]
    assert ambiguous["alternatives"] == 2 and ambiguous["plain_both_covered"]
    if "librarian" in twin.available:
        assert ambiguous["librarian"]["both_covered"]
    # The broken statement is a failed avenue; the fallback packet has evidence.
    assert cases["controller_invalid_sql"]["records"] > 0
    assert cases["controller_invalid_sql"]["failed_avenues"] > 0
    assert cases["controller_timeout"]["execution_status"] in ("degraded", "failed")
    assert (
        cases["prompt_injection"]["writes_after"]
        == cases["prompt_injection"]["writes_before"]
    )


def test_ben_19_latency_separates_operations_and_gate(twin, quick):
    subset = answerable(quick)[:3]
    measured = latency.measure_operations(
        twin.client, subset, available=twin.available, repeats=1, warmup=0
    )
    assert set(measured) == set(latency.OPERATIONS)
    for name, value in measured.items():
        assert value["status"] == "ok", (name, value)
        assert value["p95"] >= value["p50"] > 0 and value["n"] == len(subset)
    baseline = latency.read_baseline(latency.BASELINE_PATH)
    assert set(baseline) == set(latency.OPERATIONS)
    numbers = {n: {m: v[m] for m in latency.METRICS} for n, v in measured.items()}
    gate = latency.compare(baseline, numbers, tolerance=1000)
    assert gate["ok"] and sorted(gate["checked"]) == sorted(latency.OPERATIONS)
    assert not latency.compare(baseline, numbers, tolerance=-1)["ok"]
    # An operation the system lacks is skipped with its reason.
    lacking = latency.measure_operations(
        twin.client, subset, ("librarian",), twin.available - {"librarian"}
    )
    assert (
        lacking["librarian"]["status"] == "skipped"
        and "p50" not in lacking["librarian"]
    )


def test_ben_18_two_adapters_gap_reported(twin, quick):
    subset = answerable(quick)[:6]
    report = portability.compare(
        [portability.HttpRunner(twin.client), portability.CliRunner(twin.client)],
        subset,
    )
    assert set(report["runners"]) == {"http", "cli"}
    for name, part in report["runners"].items():
        assert part["questions"] == len(subset) and part["failures"] == 0, name
        assert part["f05"] is not None
    assert report["gap"]["f05_points"] is not None
    assert isinstance(report["gap"]["differing_questions"], list)
    out = portability.HttpRunner(twin.client).answer(subset[0])
    assert set(out) == {"answer", "cited_ids", "tool_log"}
    assert out["tool_log"][0]["tool"] == "POST /v1/recall"


def test_selection_budget_arms_non_decreasing(twin, quick):
    subset = answerable(quick)
    values = []
    for name in run.SELECTION_ARMS:
        summary, _ = run.run_arm(twin.client, arms.get(name), subset, twin.available)
        assert summary["settings"]["request"]["budgets"]["tokens"] == int(
            name.rsplit("_", 1)[1]
        )
        values.append(summary["overall"])
    for smaller, larger in zip(values, values[1:]):
        assert larger["group_fraction"] >= smaller["group_fraction"] - 1e-9
        assert larger["tokens"] >= smaller["tokens"]


def test_skipped_arms_not_zeroed(twin, quick):
    lacking = twin.available - {"librarian", "match"}
    report, rows = run.matrix(
        twin.client, quick[:6], lacking, ["recall", "match", "librarian"]
    )
    for name in ("match", "librarian"):
        summary = report["arms"][name]
        assert summary["status"] == "skipped" and "system lacks" in summary["reason"]
        assert summary["overall"] is None and summary["categories"] == {}
        assert report["verdicts"][name]["overall"] == "skipped"
    assert {r["arm"] for r in rows} == {"recall"}
    # A question the arm cannot pose is skipped for it, not scored.
    needs = [q for q in twin.questions if "links" in q.needs][:2]
    summary, rows = run.run_arm(twin.client, arms.get("recall"), needs, twin.available)
    assert all(r["status"] == "skipped" and "f05" not in r for r in rows)
    assert summary["status"] == "skipped" and summary["overall"] is None
    linked = arms.get("recall_link_expansion").run(twin.client, needs[0])
    assert linked.status == "ok"


def test_scoreboard_rows_complete(twin, quick, tmp_path):
    board = tmp_path / "scoreboard.jsonl"
    report, _ = run.matrix(twin.client, quick[:4], twin.available, ["recall"])
    scoreboard.append(board, report)
    behaviour = run.behaviour_suite(
        twin.client,
        twin.corpus,
        twin.phase_times,
        available=twin.available,
        names=["episode_count", "dyad_vs_author"],
    )
    scoreboard.append(board, behaviour)
    rows = scoreboard.read(board)
    assert [r["suite"] for r in rows] == ["ablation", "behaviours"]
    for row in rows:
        assert row["at"] and "commit" in row
        assert row["config"]["name"] == "offline-local"
        assert row["config"]["settings"] and "commit" in row["config"]
    behaviours_row = rows[1]["behaviours"]
    assert behaviours_row["episode_count"]["status"] == "passed"
    assert behaviours_row["episode_count"]["metrics"]["manifest_incidents"] == 3
    dyad = behaviours_row["dyad_vs_author"]
    if "person_role_between" in twin.available:
        # w5-people delivered the need and bench-behaviours implemented the
        # check, so it now runs and reports a real outcome.
        assert dyad["status"] == "passed", dyad.get("reason")
    else:
        assert dyad["status"] == "skipped"
        assert "person_role_between" in dyad["reason"]
    assert rows[0]["arms"]["recall"]["settings"]["route"] == "POST /v1/recall"
