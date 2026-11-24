"""Benchmark harness on the public twin: generator, gold labels, scorers,
arms, runner, latency gate and scoreboard. Synthetic data only; the system is
driven over HTTP through the test client."""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from bench import arms, gold, latency, run, scenarios, score, scoreboard
from bench.gold import Cluster, Group, Question, Roles


def q(**labels):
    labels.setdefault("id", "q")
    labels.setdefault("text", "synthetic question")
    labels.setdefault("category", "unit")
    return Question(**labels)


def test_ben_3_scenarios_seeded_and_synthetic():
    first, again, other = scenarios.build(47), scenarios.build(47), scenarios.build(48)
    assert first.messages == again.messages and first.links == again.links
    assert [x.model_dump() for x in first.questions] == [
        x.model_dump() for x in again.questions
    ]
    assert first.messages != other.messages
    names = {c["name"] for c in first.channels}
    assert {"bench.chat_a", "bench.chat_b", "bench.notes", "bench.wiki"} <= names
    assert all(n.startswith("bench.") for n in names)
    # Every planted class is present with at least ten questions; the behaviour
    # segments add their own categories beside them.
    by_category = first.by_category()
    assert set(by_category) == set(scenarios.CATEGORIES) | set(scenarios.BEHAVIOUR_CATEGORIES)
    assert all(len(by_category[c]) >= 10 for c in scenarios.CATEGORIES)
    assert len({x.id for x in first.questions}) == len(first.questions)
    variants = {(x.category, x.variant) for x in first.questions}
    assert {("page_mutation", m) for m in scenarios.MUTATIONS} <= variants
    assert {("temporal", v) for v in scenarios.TEMPORAL_VARIANTS} <= variants
    assert {("leakage", v) for v in scenarios.LEAKAGE_VARIANTS} <= variants
    # Pages are revisions: one external_id, strictly increasing happened_at.
    revisions = {}
    for message in first.messages:
        assert message["payload"]["synthetic"] is True
        if message["channel"] == "bench.wiki":
            revisions.setdefault(message["external_id"], []).append(
                datetime.fromisoformat(message["happened_at"])
            )
    assert any(len(times) > 1 for times in revisions.values())
    assert all(times == sorted(set(times)) for times in revisions.values())
    # Late-arriving records and links exist, and only in a second phase.
    assert set(first.phases) == {1, 2} and 2 in first.link_phases
    # The declared import gap really is empty.
    gap = first.gaps[0]
    after, before = (datetime.fromisoformat(gap[k]) for k in ("after", "before"))
    inside = [
        m
        for m in first.messages
        if m["channel"] == gap["channel"]
        and after <= datetime.fromisoformat(m["happened_at"]) < before
    ]
    assert not inside
    assert any(
        m["channel"] == gap["channel"]
        and datetime.fromisoformat(m["happened_at"]) >= before
        for m in first.messages
    )
    # Paraphrase pairs share meaning only through the synonym file.
    synonym = json.loads(scenarios.SYNONYMS_PATH.read_text("utf-8"))
    contents = {(m["channel"], m["external_id"]): m["content"] for m in first.messages}
    for question in by_category["paraphrase"]:
        answer = contents[tuple(question.groups[0].alternatives[0])]
        asked = set(re.findall(r"[a-z]+", question.text.lower())) & set(synonym)
        told = set(re.findall(r"[a-z]+", answer.lower())) & set(synonym)
        assert asked and not asked & told
        assert {synonym[w] for w in asked} == {synonym[w] for w in told}
    # Scale adds distractors only.
    bigger = scenarios.build(47, scale=2)
    assert len(bigger.messages) == len(first.messages) + scenarios.FILLER_PER_SCALE
    assert len(bigger.questions) == len(first.questions)


def test_ben_10_group_coverage_and_f05():
    question = q(
        groups=[
            Group(id="fact", alternatives=[1, 2]),
            Group(id="context", alternatives=[3]),
            Group(id="optional", required=False, alternatives=[4]),
        ],
        helpful=[5],
    )
    # Both alternatives of one group count once; optional and helpful records
    # are neither credited nor penalised.
    assert score.precision_recall([1, 2, 4, 5], question) == (1.0, 0.5)
    assert score.f_beta([1, 2, 4, 5], question) == pytest.approx(1.25 * 0.5 / 0.75)
    assert score.f_beta([1, 3], question) == 1.0
    # One wrong record: precision 2/3, recall 1; beta 0.5 favours precision.
    assert score.f_beta([1, 3, 9], question) == pytest.approx(
        1.25 * (2 / 3) / (0.25 * (2 / 3) + 1)
    )
    assert score.f_beta([1, 3, 9], question) < score.f_beta([1, 3, 9], question, 2)
    assert score.f_beta([], question) == 0.0 and score.f_beta([9], question) == 0.0
    partial = score.group_coverage([2, 9], question)
    assert partial == dict(
        all_required_covered=False, fraction=0.5, covered=["fact"], missing=["context"]
    )
    full = score.group_coverage([9, 2, 3], question)
    assert full["all_required_covered"] and full["fraction"] == 1.0
    # High F0.5 and incomplete evidence are different facts about one result.
    assert score.f_beta([1], question) > 0.8
    assert not score.group_coverage([1], question)["all_required_covered"]
    # Span credit: a span that contains the gold message delivers it.
    spans = {7: [6, 7, 3]}
    assert score.group_coverage(score.with_spans([1, 7], spans), question)[
        "all_required_covered"
    ]
    assert score.f_beta(score.with_spans([1, 7], spans), question) == 1.0
    strict = score.with_spans([1, 7], spans, span_credit=False)
    assert not score.group_coverage(strict, question)["all_required_covered"]
    assert score.SPAN_CREDIT_DEFAULT is True


def test_ben_9_stage_recall_and_recall_at_k():
    question = q(
        groups=[
            Group(id="a", alternatives=[1]),
            Group(id="b", alternatives=[2, 3]),
            Group(id="c", alternatives=[4]),
            Group(id="d", alternatives=[5]),
        ]
    )
    stages = dict(
        before_rerank=[9, 1, 8, 3, 4, 5],
        after_rerank=[1, 3, 4, 9],
        after_selection=[1, 3],
        not_reported=None,
    )
    assert score.stage_recall(stages, question) == dict(
        before_rerank=1.0, after_rerank=0.75, after_selection=0.5
    )
    ranked = [9, 1, 8, 3, 4, 5]
    assert [score.recall_at_k(ranked, question, k) for k in (1, 2, 4, 6)] == [
        0.0,
        0.25,
        0.5,
        1.0,
    ]


def test_ben_13_cluster_scores():
    question = q(
        clusters=[
            Cluster(id="first", members=[1, 2, 3], ambiguous=[10]),
            Cluster(id="second", members=[4, 5]),
            Cluster(id="third", members=[6]),
        ]
    )
    coverage = score.cluster_coverage([2, 10, 99], question)
    assert coverage == dict(
        fraction=1 / 3, covered=["first"], uncertain=[], missing=["second", "third"]
    )
    # Reached only through an ambiguous member: uncertain, not covered.
    only_maybe = score.cluster_coverage([10, 4], question)
    assert only_maybe["covered"] == ["second"] and only_maybe["uncertain"] == ["first"]
    assert (
        score.duplicate_grouping_accuracy([[1, 2, 3, 10], [4, 5], [6]], question) == 1
    )
    # The ambiguous record may sit anywhere without cost.
    assert (
        score.duplicate_grouping_accuracy([[1, 2, 3], [4, 5, 10], [6]], question) == 1
    )
    # One merge of two experiences: pairs (1,2) right, (1,4) and (2,4) wrong.
    assert score.duplicate_grouping_accuracy([[1, 2, 4]], question) == pytest.approx(
        1 / 3
    )
    # A split duplicate: (1,2) wrong.
    assert score.duplicate_grouping_accuracy([[1], [2]], question) == 0.0
    assert score.duplicate_grouping_accuracy([[1], [99]], question) is None


def test_ben_14_role_coverage():
    question = q(
        roles=Roles(supporting=[1, 2, 3], counterexample=[4, 5], comparison=[6, 7])
    )
    result = score.role_coverage([1, 2, 6, 99], question)
    assert result["fraction"] == pytest.approx(2 / 3)
    assert result["counts"] == dict(supporting=2, counterexample=0, comparison=1)
    assert result["missing"] == ["counterexample"]
    assert score.role_coverage([1, 4, 7], question)["fraction"] == 1.0
    assert score.role_coverage([1, 4, 7], question, min_per_role=2)["fraction"] == 0.0
    # A question that names only some populations is scored on those.
    partial = q(roles=Roles(supporting=[1], counterexample=[2]))
    assert score.role_coverage([1, 2], partial)["fraction"] == 1.0
    assert score.role_coverage([1], q())["fraction"] == 0.0


def test_ben_20_abstention_and_incomplete_disclosure():
    unanswerable = q(no_answer=True, helpful=[5])
    answerable = q(groups=[Group(id="fact", alternatives=[1])])
    assert score.abstention([], unanswerable)["outcome"] == "correct_abstention"
    assert score.abstention([5], unanswerable)["correct"]
    assert score.abstention([9], unanswerable)["outcome"] == "false_answer"
    assert score.abstention([9], unanswerable, abstained=True)["correct"]
    assert score.abstention([], answerable)["outcome"] == "false_abstention"
    assert score.abstention([1], answerable)["outcome"] == "answered"
    assert not score.abstention([1], answerable, abstained=True)["correct"]

    gap = q(no_answer=True, expects_incomplete=True, expects_disclosure=["import_gap"])
    declared = score.incomplete_disclosure(True, ["import_gap:bench.chat_b"], gap)
    assert declared["correct"] and not declared["missing_terms"]
    assert not score.incomplete_disclosure(False, ["import_gap"], gap)["correct"]
    vague = score.incomplete_disclosure(True, ["deadline reached"], gap)
    assert not vague["correct"] and vague["missing_terms"] == ["import_gap"]
    assert not score.incomplete_disclosure(False, [], gap)["correct"]
    complete = score.incomplete_disclosure(False, [], answerable)
    assert complete["correct"] and not complete["false_alarm"]
    hedged = score.incomplete_disclosure(True, ["timeout"], answerable)
    assert hedged["false_alarm"] and not hedged["correct"]
    # A disclosure that is not about incompleteness: naming the term is right,
    # declaring the search incomplete as well is a false alarm.
    twins = q(expects_disclosure=["ambiguous_person"])
    assert score.incomplete_disclosure(False, ["ambiguous_person: two"], twins)[
        "correct"
    ]
    always = score.incomplete_disclosure(True, ["ambiguous_person"], twins)
    assert always["false_alarm"] and not always["correct"]


def test_ben_21_leakage_metric():
    question = q(groups=[Group(id="then", alternatives=[1])], forbidden=[7, 8])
    clean = score.leakage([1, 2], question, dict(before_rerank=[1, 2, 3]))
    assert clean == dict(clean=True, records=[], stages={}, count=0)
    leaked = score.leakage([1, 7], question)
    assert not leaked["clean"] and leaked["records"] == [7] and leaked["count"] == 1
    # A forbidden record seen by an earlier stage is a leak even when dropped.
    early = score.leakage([1], question, dict(before_rerank=[1, 8], after_rerank=[1]))
    assert not early["clean"] and early["stages"] == dict(before_rerank=[8])
    # Revisions are separate records: version 2 is forbidden, version 1 is not.
    page = q(forbidden=[("bench.wiki", "page", 2)])
    assert score.leakage([("bench.wiki", "page", 1)], page)["clean"]
    assert not score.leakage([("bench.wiki", "page", 2)], page)["clean"]
    # A span that contains a forbidden message leaks it.
    assert not score.leakage(score.with_spans([1], {1: [1, 7]}), question)["clean"]


def test_behaviour_scorers():
    """The behaviour scorers on hand-made inputs (no system needed)."""
    three = q(expects_incidents=3)
    good = dict(by="episode", unit="episode", incidents=3, records=9)
    assert score.incident_count_exact(good, three)["exact"]
    # Hits counted as incidents, another unit or no manifest: never exact.
    assert not score.incident_count_exact(dict(good, incidents=9), three)["exact"]
    assert not score.incident_count_exact(dict(good, unit="record"), three)["exact"]
    assert not score.incident_count_exact(None, three)["exact"]

    newest = q(expects_newest=7)
    assert score.newest_relevant_top1([7, 9], newest, newest_absolute=9)["top1"]
    wrong = score.newest_relevant_top1([9, 7], newest, newest_absolute=9)
    assert not wrong["top1"] and wrong["absolute_first"]
    assert not score.newest_relevant_top1([], newest)["top1"]

    gap = q(expects_absence_statement="not_found_in_coverage")
    said = dict(
        coverage=dict(absence="not_found_in_coverage"),
        warnings=["absence_is_not_evidence"],
    )
    assert score.absence_disclosed(said, gap)["correct"]
    # The warning is required inside a gap, and a negative assertion fails.
    assert not score.absence_disclosed(dict(said, warnings=[]), gap)["correct"]
    lied = score.absence_disclosed(dict(said, warnings=["did_not_happen"]), gap)
    assert lied["negative_assertion"] and not lied["correct"]
    covered = q(expects_absence_statement="not_found_in_covered_sources")
    plain = dict(coverage=dict(absence="not_found_in_covered_sources"))
    assert score.absence_disclosed(plain, covered)["correct"]

    assert score.dyad_both_sides(["a", "b", None], ("a", "b")) == 1.0
    assert score.dyad_both_sides(["a"], ("a", "b")) == 0.5
    assert score.dyad_both_sides([], None) == 0.0

    assert score.hidden_count_leak(3, 3) == 0
    assert score.hidden_count_leak(5, 3) == 2
    assert score.hidden_count_leak(dict(a=1, b=4), dict(a=1, b=2)) == 2
    assert score.hidden_count_leak(None, 0) == 0
    assert score.hidden_count_leak(None, 2) == 2


def test_citation_faithfulness_metric():
    question = q(
        groups=[Group(id="a", alternatives=[1, 2]), Group(id="b", alternatives=[3])],
        helpful=[4],
    )
    delivered = [1, 3, 4, 9]
    assert score.citation_faithfulness([1, 3], delivered, question) == 1.0
    # 2 is gold but was never delivered; 9 was delivered but is not evidence;
    # 4 is only helpful; 77 does not exist.
    assert score.citation_faithfulness([1, 2, 9, 4, 77], delivered, question) == 0.2
    assert score.citation_faithfulness([1, 1, 9], delivered, question) == 0.5
    assert score.citation_faithfulness([], delivered, question) == 0.0
    assert score.citation_faithfulness([], [], q(no_answer=True)) == 1.0
    assert score.citation_faithfulness([9], [9], q(no_answer=True)) == 0.0


def test_ben_19_percentiles_and_regression_gate():
    assert score.percentiles(range(1, 101)) == dict(p50=50.5, p95=pytest.approx(95.05))
    assert score.percentiles([7]) == dict(p50=7.0, p95=7.0)
    assert score.percentiles([]) == dict(p50=None, p95=None)
    assert score.percentiles([4, 1, 3, 2], (0, 100)) == dict(p0=1.0, p100=4.0)

    calls = []
    samples = latency.timed(lambda: calls.append(1), repeats=4, warmup=2)
    assert len(calls) == 6 and len(samples) == 4 and all(s >= 0 for s in samples)
    assert latency.summarize([10, 20, 30])["p50"] == 20

    baseline = dict(
        keyword=dict(p50=10.0, p95=20.0),
        recall=dict(p50=100.0, p95=200.0),
        librarian=dict(p50=None, p95=None),
    )
    within = dict(
        keyword=dict(p50=12.0, p95=24.0),
        recall=dict(p50=90.0, p95=150.0),
        match=dict(p50=5.0, p95=6.0),
    )
    passed = latency.compare(baseline, within)
    assert passed["ok"] and passed["tolerance"] == 0.20
    assert passed["checked"] == ["keyword", "recall"]
    # Operations measured on one side only are reported, not judged.
    assert passed["unmeasured"] == ["librarian", "match"]
    slower = dict(within, recall=dict(p50=100.0, p95=240.5))
    failed = latency.compare(baseline, slower)
    assert not failed["ok"]
    assert [(r["operation"], r["metric"]) for r in failed["regressions"]] == [
        ("recall", "p95")
    ]
    assert latency.compare(baseline, slower, tolerance=0.25)["ok"]


def test_ben_16_arm_matrix_on_twin(client):
    report, rows = run.run(client, scenarios.build(47))
    assert set(report["arms"]) == {"oracle", "none", "keyword", "meaning", "fused"}
    assert all(report["categories"][c] >= 10 for c in scenarios.CATEGORIES)
    assert set(report["categories"]) == set(scenarios.CATEGORIES) | set(
        scenarios.BEHAVIOUR_CATEGORIES
    )
    assert report["span_credit"] is True and report["tie_points"] == 5.0
    # The runner loaded in two phases and recorded the clock after each.
    assert sorted(report["loaded"]) == [1, 2] and report["loaded"][2]["links"] > 0
    assert report["phase_times"]["1"] < report["phase_times"]["2"]
    then = [r for r in rows if r["arm"] == "oracle" and r["variant"] == "late_backfill"]
    assert len(then) == 3 and all(r["leakage"]["clean"] for r in then)

    oracle = report["arms"]["oracle"]
    assert oracle["status"] == "ok" and oracle["overall"]["skipped"] == 0
    assert oracle["overall"]["f05"] == 1.0
    assert oracle["overall"]["group_coverage"] == 1.0
    assert oracle["overall"]["abstention"] == 1.0
    assert oracle["overall"]["disclosure"] == 1.0
    assert oracle["overall"]["leaked"] == 0
    assert oracle["overall"]["false_incomplete"] == 0.0
    assert oracle["overall"]["cluster_coverage"] == 1.0
    assert oracle["overall"]["role_coverage"] == 1.0
    assert all(m["f05"] in (1.0, None) for m in oracle["categories"].values())
    assert all(oracle["categories"][c]["scored"] >= 10 for c in scenarios.CATEGORIES)

    nothing = report["arms"]["none"]
    assert nothing["overall"]["f05"] == 0.0
    assert nothing["overall"]["group_coverage"] == 0.0
    # Returning nothing is right exactly where there is no answer.
    assert nothing["overall"]["abstention"] == 1.0
    assert nothing["overall"]["false_abstention"] == 1.0
    assert nothing["overall"]["tokens_per_covered_question"] is None

    keyword = report["arms"]["keyword"]
    assert keyword["status"] == "ok"
    assert keyword["categories"]["exact_token"]["f05"] == 1.0
    assert keyword["categories"]["exact_token"]["group_coverage"] == 1.0
    assert keyword["categories"]["paraphrase"]["f05"] < 0.5
    assert keyword["categories"]["no_answer"]["abstention"] == 1.0
    assert keyword["overall"]["leaked"] == 0
    assert (
        keyword["overall"]["latency_ms"]["p95"]
        >= keyword["overall"]["latency_ms"]["p50"]
    )
    assert keyword["overall"]["tokens_per_covered_question"] > 0
    measured = {
        c: round(m["f05"], 4)
        for c, m in keyword["categories"].items()
        if m["f05"] is not None
    }
    twin = {c: v for c, v in measured.items() if c in scenarios.CATEGORIES}
    assert twin == scenarios.KEYWORD_F05_SEED_47
    # The behaviour segments carry their own measured constants.
    behaviour = {c: v for c, v in measured.items() if c not in scenarios.CATEGORIES}
    assert behaviour == scenarios.KEYWORD_F05_BEHAVIOUR_SEED_47
    # Questions an arm cannot pose are skipped for it, never scored zero.
    assert (
        keyword["overall"]["skipped"]
        == scenarios.KEYWORD_SKIPPED_SEED_47 + scenarios.KEYWORD_SKIPPED_BEHAVIOUR_SEED_47
    )
    assert scenarios.KEYWORD_SKIPPED_SEED_47 == 12
    assert keyword["overall"]["scored"] + keyword["overall"]["skipped"] == len(
        scenarios.build(47).questions
    )
    cannot = [r for r in rows if r["arm"] == "keyword" and r["status"] == "skipped"]
    assert cannot and all("f05" not in r and "needs" in r["reason"] for r in cannot)

    # No embedding model is configured offline: the arms are reported as
    # skipped with a reason and without numbers.
    for name in ("meaning", "fused"):
        arm = report["arms"][name]
        assert arm["status"] == "skipped" and "embeddings" in arm["reason"]
        assert arm["overall"] is None and arm["categories"] == {}
        assert report["verdicts"][name]["overall"] == "skipped"
    assert "embeddings" not in report["available"] and "search" in report["available"]

    # The 5-point tie rule against the baseline arm.
    assert report["verdicts"]["oracle"]["overall"] == "better"
    assert report["verdicts"]["oracle"]["categories"]["exact_token"] == "tie"
    assert report["verdicts"]["none"]["categories"]["paraphrase"] == "tie"
    assert report["verdicts"]["none"]["categories"]["exact_token"] == "worse"
    assert run.verdict(0.549, 0.50) == "tie" and run.verdict(0.55, 0.50) == "better"
    assert run.verdict(0.40, 0.45) == "worse" and run.verdict(None, 0.5) == "skipped"

    # An arm that is asked for but answers 400 on every question is skipped too.
    direct = arms.ARMS["meaning"].run(client, gold.bind(q(), {}))
    assert direct.status == "skipped" and direct.records == []
    timings = latency.measure(client, [], ["keyword", "fused"], report["available"])
    assert timings["fused"]["status"] == "skipped" and "p50" not in timings["fused"]
    assert json.loads(json.dumps(report, default=str))["suite"] == "matrix"
    assert "keyword" in run.render(report) and "skipped" in run.render(report)


def test_scoreboard_append_only(tmp_path):
    board = tmp_path / "results" / "scoreboard.jsonl"
    first = scoreboard.append(board, dict(suite="matrix", seed=47, note="one\ntwo"))
    before = board.read_bytes()
    assert before.count(b"\n") == 1 and first["at"] and "commit" in first
    scoreboard.append(board, dict(suite="latency", seed=47, at="fixed"))
    after = board.read_bytes()
    # Earlier bytes are untouched; exactly one line was added.
    assert after.startswith(before) and after.count(b"\n") == 2
    rows = scoreboard.read(board)
    assert [r["suite"] for r in rows] == ["matrix", "latency"]
    assert rows[0]["note"] == "one\ntwo" and rows[1]["at"] == "fixed"
    # A torn last line is closed, not merged with the next row.
    with open(board, "ab") as handle:
        handle.write(b'{"suite":"torn"}')
    scoreboard.append(board, dict(suite="next"))
    assert [r["suite"] for r in scoreboard.read(board)][-2:] == ["torn", "next"]
    assert board.read_bytes().startswith(after)


def test_gold_keys_resolve_after_load(client):
    corpus = scenarios.build(47)
    scenarios.load(client, corpus)
    keys = list(
        dict.fromkeys(ref for question in corpus.questions for ref in question.refs())
    )
    ids = gold.resolve(client, keys)
    assert set(ids) == set(keys) and len(keys) > 300
    assert len(set(ids.values())) == len(ids)
    # A two-part key is the current version; a three-part key names a revision.
    page = next(k for k in keys if len(k) == 3 and k[2] == 1)
    both = gold.resolve(client, [page, page[:2], page[:2] + (2,)])
    assert both[page[:2]] == both[page[:2] + (2,)] != both[page]
    rows = client.post(
        "/v1/sql",
        json=dict(
            statement="SELECT id,channel,external_id,version FROM api.message_versions"
            f" WHERE id IN ({both[page]},{both[page[:2]]}) ORDER BY version"
        ),
    ).json()["rows"]
    assert [(r["channel"], r["external_id"], r["version"]) for r in rows] == [
        page,
        page[:2] + (2,),
    ]
    assert gold.resolve(client, [("bench.notes", "never-loaded")]) == {}
    # Properties the OpenAPI document exposes are used: the wiki is declared a
    # page_revision channel and subject intervals are stored.
    found = scenarios.features(client)
    assert {"record_kind", "subject", "path:/v1/search"} <= found

    def sql(statement):
        response = client.post("/v1/sql", json=dict(statement=statement))
        assert response.status_code == 200, response.text
        return response.json()["rows"]

    assert sql("SELECT record_kind FROM api.channels WHERE name='bench.wiki'") == [
        dict(record_kind="page_revision")
    ]
    subjects = sql(
        "SELECT DISTINCT channel,external_id,subject_from,subject_from_precision"
        " FROM api.record_versions WHERE subject_from IS NOT NULL"
        " AND channel LIKE 'bench.%'"
    )
    assert {(r["channel"], r["external_id"]) for r in subjects} == set(corpus.subjects)
    for row in subjects:
        planted = corpus.subjects[(row["channel"], row["external_id"])]
        assert datetime.fromisoformat(row["subject_from"]) == datetime.fromisoformat(
            planted["start"]
        )
        assert row["subject_from_precision"] == "day"
    # Binding swaps every key for its id and keeps the labels' shape.
    for question in corpus.questions:
        bound = gold.bind(question, ids)
        assert all(isinstance(r, int) for r in bound.refs())
        assert len(bound.refs()) == len(question.refs())
        assert [g.id for g in bound.groups] == [g.id for g in question.groups]
    with pytest.raises(KeyError):
        gold.bind(q(helpful=[("bench.notes", "never-loaded")]), ids)
    # Handles of one person resolve to one person; twins stay two people.
    people = client.post(
        "/v1/sql",
        json=dict(
            statement="SELECT handle,count(*) n FROM api.messages m JOIN api.people p"
            " ON p.id=m.person_id WHERE p.handle IN ('bench-handles-0','bench-twin-0-a',"
            "'bench-twin-0-b') GROUP BY handle ORDER BY handle"
        ),
    ).json()["rows"]
    assert people == [
        dict(handle="bench-handles-0", n=2),
        dict(handle="bench-twin-0-a", n=1),
        dict(handle="bench-twin-0-b", n=1),
    ]


def test_phased_load(client):
    corpus = scenarios.build(47)

    def count(where="true"):
        return client.post(
            "/v1/sql",
            json=dict(
                statement="SELECT count(*) n FROM api.message_versions"
                f" WHERE channel LIKE 'bench.%' AND {where}"
            ),
        ).json()["rows"][0]["n"]

    def links():
        return client.post(
            "/v1/sql",
            json=dict(
                statement="SELECT count(*) n FROM api.links WHERE type LIKE 'bench_%'"
            ),
        ).json()["rows"][0]["n"]

    with pytest.raises(ValueError):
        scenarios.load(client, corpus, batch=5001)
    first = scenarios.load(client, corpus, phase=1, batch=500)[1]
    expected = corpus.phases.count(1)
    assert first["messages"] == expected == count()
    # Batches never exceed the size asked for (and so never 5,000).
    assert first["batches"] == -(-expected // 500) and scenarios.MAX_BATCH == 5000
    assert first["inserted"] + first["new_versions"] == expected
    assert first["new_versions"] > 0 and first["rejected"] == 0
    assert links() == corpus.link_phases.count(1) > 0
    late = [
        (m["channel"], m["external_id"])
        for m, phase in zip(corpus.messages, corpus.phases)
        if phase == 2
    ]
    assert late and gold.resolve(client, late) == {}
    between = scenarios.server_time(client)
    second = scenarios.load(client, corpus, phase=2)[2]
    assert second["messages"] == len(late) and second["links"] > 0
    assert set(gold.resolve(client, late)) == set(late)
    assert links() == len(corpus.links)
    # The recorded instant separates the phases by ingest time, although the
    # late records happened long before it.
    stamp = between.isoformat()
    assert count(f"ingested_at <= '{stamp}'") == expected
    assert count(f"ingested_at > '{stamp}'") == len(late)
    assert count(f"ingested_at > '{stamp}' AND happened_at < '{stamp}'") == len(late)
    assert count() == len(corpus.messages)
    # The boundary is what a question "as known after phase 1" is bound to.
    questions, times, loaded = run.prepare(
        client, corpus, load=False, phase_times={1: between}
    )
    assert times == {1: between} and loaded == {}
    asked_then = [x for x in questions if x.known_by_phase == 1]
    assert asked_then and all(x.known_by == between for x in asked_then)
    assert all(x.known_by is None for x in questions if x.known_by_phase is None)
    # Loading again adds no record. Only pages replay: each earlier revision
    # differs from the current one, so it is stored as a further version.
    replayed = sum(
        1
        for m, phase in zip(corpus.messages, corpus.phases)
        if phase == 1
        and m["channel"] == "bench.wiki"
        and sum(
            o["channel"] == "bench.wiki" and o["external_id"] == m["external_id"]
            for o in corpus.messages
        )
        > 1
    )
    again = scenarios.load(client, corpus)
    assert again[1]["inserted"] == 0 and again[2]["inserted"] == 0
    assert again[1]["new_versions"] == replayed and again[2]["new_versions"] == 0


def test_no_real_data_markers():
    corpus = scenarios.build(47)
    synonym = json.loads(scenarios.SYNONYMS_PATH.read_text("utf-8"))
    invented = re.compile(r"(?:[bdfgjklmnprtvwz][aeiou]){3}[kx]")
    assert all(invented.fullmatch(word) for word in synonym)
    # Template words: everything else in the corpus must be an invented word.
    plain = set(
        """a about again ago also and another app as at back been before began
        between bring by came chat checked did do during external fine first
        for from give had has have i id imported in is it its linked log long
        looking may message moved my no notes nothing now of on one only opened
        other out page pages per pick place read revision same say second
        session settled side still synthetic take talk the then there this to
        today too use was we well went what when which who will with""".split()
    )
    # Template words of the behaviour segments.
    plain |= set(
        """account answer answers are asker asks can caption captions check
        complete consumer count coverage direct episode feed gap good group
        happened hidden how late many maybe media messages mine newest note
        open over owner part people person recording relevant reported
        restricted see send sent speakers suspected that thread three times
        transcript twice two unnamed whose written you""".split()
    )
    texts = [m["content"] for m in corpus.messages]
    texts += [m["content"] for rows in corpus.held.values() for m in rows]
    texts += [x.text for x in corpus.questions]
    texts += [p["display_name"] for p in corpus.people]
    texts += [c["description"] for c in corpus.channels]
    unknown = set()
    for text in texts:
        for word in re.findall(r"[A-Za-z]+", text.lower()):
            if word not in plain and not invented.fullmatch(word):
                unknown.add(word)
    assert not unknown, unknown
    blob = "\n".join(texts) + json.dumps(corpus.messages) + json.dumps(corpus.links)
    root = Path(scenarios.__file__).resolve().parent
    for path in sorted(root.glob("*.py")) + sorted(root.glob("*.json")):
        blob += path.read_text("utf-8")
    markers = dict(
        email=r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        url=r"https?://(?!127\.0\.0\.1|localhost)",
        phone=r"\+\d{7,}|\b\d{3}[ -]\d{3}[ -]\d{4}\b",
        home_directory=r"[A-Za-z]:[\\/]+Users|/home/\w|/Users/\w",
        credential=r"(?i)bearer [a-z0-9_\-]{16,}|password\s*=|api[_-]?key\s*=",
        private_network=r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+",
    )
    for name, pattern in markers.items():
        assert not re.search(pattern, blob), name
    # People, accounts and channels carry the bench prefix and nothing else.
    assert all(p["handle"].startswith("bench-") for p in corpus.people)
    assert all(
        a["system"] in ("bencha", "benchb", "benchj")
        and a["value"].startswith("bench-")
        for p in corpus.people
        for a in p["accounts"]
    )
    held = [m for rows in corpus.held.values() for m in rows]
    authors = [m["author"] for m in corpus.messages + held if "author" in m]
    assert all(a["value"].startswith("bench-") for a in authors)
    assert {m["channel"].split(".")[0] for m in corpus.messages} == {"bench"}


def test_cli_run_and_latency_gate(client, tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext

    import bench.__main__ as cli

    # Without a target the command refuses to start.
    monkeypatch.delenv("THROUGHLINE_URL", raising=False)
    monkeypatch.delenv("THROUGHLINE_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["run"])
    monkeypatch.setattr(cli, "_client", lambda args: nullcontext(client))
    board = tmp_path / "scoreboard.jsonl"
    capsys.readouterr()
    assert (
        cli.main(
            ["run", "--load", "--arms", "oracle,keyword", "--json"]
            + ["--scoreboard", str(board)]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert set(report["arms"]) == {"oracle", "keyword"}
    assert report["arms"]["oracle"]["overall"]["f05"] == 1.0
    assert [r["suite"] for r in scoreboard.read(board)] == ["matrix"]

    # The first latency run records the baseline; later runs are gated by it.
    baseline = tmp_path / "baseline.json"
    common = ["latency", "--arms", "keyword,fused", "--repeats", "1"]
    common += ["--warmup", "0", "--baseline", str(baseline)]
    assert cli.main(common + ["--scoreboard", str(board)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["baseline_written"] == str(baseline) and "gate" not in first
    assert first["operations"]["fused"]["status"] == "skipped"
    recorded = latency.read_baseline(baseline)
    assert recorded["keyword"]["p95"] >= recorded["keyword"]["p50"] > 0
    assert cli.main(common + ["--tolerance", "1000"]) == 0
    gated = json.loads(capsys.readouterr().out)
    assert gated["gate"]["ok"] and gated["gate"]["checked"] == ["keyword"]
    assert "baseline_written" not in gated
    assert latency.read_baseline(baseline) == recorded
    # A negative tolerance makes any measurement a regression: exit code 1.
    assert cli.main(common + ["--tolerance", "-1"]) == 1
    assert not json.loads(capsys.readouterr().out)["gate"]["ok"]
    assert [r["suite"] for r in scoreboard.read(board)] == ["matrix", "latency"]
