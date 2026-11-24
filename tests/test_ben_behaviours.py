"""Behaviour scenarios on the public twin: twelve
planted segments, the skip rule, the six checks of w4-bench-arms and the six
of bench-behaviours (with the person-level restricted inference and the
last_contact extension). Synthetic data; the system is driven over HTTP
through the test client, under the offline-local configuration."""

import json

import pytest
from test_ben_v11_arms import loaded_twin

from bench import arms, behaviours, scenarios, score


@pytest.fixture(scope="module")
def twin(database):
    with loaded_twin() as world:
        world.principals = behaviours.LocalPrincipals()
        yield world


def one(twin, name, available=None, principals="default"):
    principals = twin.principals if principals == "default" else principals
    return behaviours.run_all(
        twin.client,
        twin.corpus,
        twin.phase_times,
        principals=principals,
        available=twin.available if available is None else available,
        names=[name],
    )[name]


def test_behaviour_corpus_deterministic_and_calibration_unchanged(twin):
    first, again = scenarios.build(47), scenarios.build(47)
    assert first.messages == again.messages and first.held == again.held
    assert first.behaviours == again.behaviours
    assert set(first.behaviours) == set(scenarios.BEHAVIOUR_SCENARIOS)
    # The twin's constants are the wave-1 values, untouched by the segments.
    assert scenarios.KEYWORD_F05_SEED_47 == {
        "exact_token": 1.0,
        "cross_platform": 1.0,
        "changed_fact": 1.0,
        "duplicate_cluster": 1.0,
        "page_mutation": 1.0,
        "leakage": 1.0,
        "temporal": 1.0,
        "context_dependent": 0.8333,
        "cross_source_time": 0.8333,
        "pattern": 0.9091,
        "paraphrase": 0.0,
        "person_handles": 0.0,
        "ambiguous_person": 0.0,
        "page_assisted": 0.0,
    }
    assert scenarios.KEYWORD_SKIPPED_SEED_47 == 12
    # Segments come after the twin and do not depend on scale.
    twin_only = [
        m for m in first.messages if not m["payload"]["bench"].startswith("behaviour_")
    ]
    bigger = scenarios.build(47, scale=2)
    assert [m for m in bigger.messages if m["payload"]["bench"].startswith("behaviour_")] == [
        m for m in first.messages if m["payload"]["bench"].startswith("behaviour_")
    ]
    assert first.messages[: len(twin_only)] == twin_only
    # Each segment has its own channels and vocabulary.
    channels = {}
    for name, segment in first.behaviours.items():
        for channel in segment["channels"]:
            assert channel.startswith("bench.behaviour_") and channel not in channels
            channels[channel] = name
    behaviour_words = set()
    twin_words = set()
    for message in first.messages:
        words = set(message["content"].lower().replace(".", " ").split())
        if message["payload"]["bench"].startswith("behaviour_"):
            behaviour_words |= words
        else:
            twin_words |= words
    invented = {w for w in behaviour_words if len(w) == 7 and w[-1] in "kx"}
    assert invented and not invented & twin_words
    # Held rows are never part of the loaded corpus.
    held = {
        (m["channel"], m["external_id"]) for m in first.held["consumer_exactly_once"]
    }
    assert held and not held & {
        (m["channel"], m["external_id"]) for m in first.messages
    }
    # The measured keyword constants of the behaviour questions.
    keyword = arms.get("keyword")
    measured = {}
    for question in twin.questions:
        if question.category in scenarios.KEYWORD_F05_BEHAVIOUR_SEED_47:
            result = keyword.run(twin.client, question)
            measured.setdefault(question.category, []).append(
                score.f_beta(result.records, question)
            )
    assert {
        c: round(sum(v) / len(v), 4) for c, v in measured.items()
    } == scenarios.KEYWORD_F05_BEHAVIOUR_SEED_47


def test_behaviour_skipped_when_need_absent_never_zero(twin):
    lacking = twin.available - {"episodes"}
    out = one(twin, "episode_count", available=lacking)
    assert out["status"] == "skipped" and out["missing"] == ["episodes"]
    assert "episodes" in out["reason"] and out["metrics"] == {}
    # Without other callers the behaviours that need them are skipped too.
    out = one(twin, "restricted_inference", principals=None)
    assert out["status"] == "skipped" and out["missing"] == ["principals"]
    # A need that is met but whose check belongs to a later package.
    pending = behaviours.Behaviour("later", ["recall"], "defined only")
    ctx = behaviours.Context({}, {}, None, set(twin.available))
    out = behaviours.run_one(pending, twin.client, twin.corpus, {}, ctx)
    assert out["status"] == "skipped" and out["reason"] == behaviours.PENDING
    # A check that raises is a failure with the reason, never a zero.
    broken = behaviours.Behaviour("broken", ["recall"], "raises", lambda *a: 1 / 0)
    out = behaviours.run_one(broken, twin.client, twin.corpus, {}, ctx)
    assert out["status"] == "failed" and "ZeroDivisionError" in out["reason"]


def test_scenario_episode_count(twin):
    out = one(twin, "episode_count")
    assert out["status"] == "passed", out
    metrics = out["metrics"]
    assert metrics["manifest_incidents"] == 3 and metrics["manifest_records"] == 9
    assert metrics["manifest_unit"] == "episode"
    assert metrics["sql_incidents"] == 3 and metrics["sql_hits"] == 9
    assert metrics["f05"] is not None


def test_scenario_newest_relevant(twin):
    out = one(twin, "newest_relevant")
    assert out["status"] == "passed", out
    assert out["metrics"]["top1"] and not out["metrics"]["newest_absolute_first"]
    assert out["metrics"]["order"] == "newest_relevant"


def test_scenario_link_vocabulary(twin):
    out = one(twin, "link_vocabulary")
    assert out["status"] == "passed", out
    metrics = out["metrics"]
    assert metrics["free_text_refused"] and metrics["synonym_folded"]
    assert metrics["type_as_written_kept"] and metrics["replay_no_duplicate"]
    assert metrics["candidate_listed"] and metrics["collapsed"] == 1
    assert metrics["active_links_after_merge"] == 1


def test_scenario_restricted_inference_gate_counts(twin):
    out = one(twin, "restricted_inference")
    assert out["status"] == "passed", out
    leaks = out["metrics"]["hidden_count_leak"]
    assert set(leaks) >= {
        "sql_count",
        "recall_records",
        "manifest_records",
        "episode_rows",
        "episodes",
        "link_type_leaks",
    }
    # Coverage rows and consumer pending counts are gated too when installed.
    if "function:coverage_for" in twin.available:
        assert "coverage_hidden_rows" in leaks
    if "consumers" in twin.available:
        assert "pending" in leaks
    if "function:pending" in twin.available:
        assert "sql_pending" in leaks
    assert all(v == 0 for v in leaks.values())
    seen = out["metrics"]["seen"]
    assert seen["sql_count"] == 3 and seen["episodes"] == 1
    assert "person_level:checked" in out["disclosures"]


# --- wave integration (w4-coverage) ---------------------------------------------


def test_scenario_consumer_exactly_once(twin):
    if "consumers" not in twin.available:
        pytest.skip("named consumers are not installed (w4-coverage)")
    out = one(twin, "consumer_exactly_once")
    assert out["status"] == "passed", out
    metrics = out["metrics"]
    assert metrics["first_news"] == 3 and len(metrics["late_news"]) == 3
    assert metrics["redelivered_without_mark"] and metrics["repeat_mark_same_response"]
    assert metrics["pending_after_mark"] == 0 and metrics["news_after_mark"] == 0
    # Seconds, not minutes: the settle window was set over HTTP.
    assert out["ms"] < 30000
    restored = twin.client.get("/v1/consumers/settings").json()
    assert restored["settle_s"] != behaviours.SETTLE["settle_s"]


def test_scenario_coverage_honesty(twin):
    if "coverage" not in twin.available:
        pytest.skip("coverage reports are not installed (w4-coverage)")
    out = one(twin, "coverage_honesty")
    assert out["status"] == "passed", out
    metrics = out["metrics"]
    assert metrics["partial"]["statement"] == "not_found_in_coverage"
    assert metrics["partial"]["warned"] and metrics["partial"]["gap_listed"]
    assert metrics["complete"]["statement"] == "not_found_in_covered_sources"
    assert not metrics["partial"]["negative_assertion"]
    assert not metrics["complete"]["negative_assertion"]
    assert metrics["gap_closed"]


# --- the six checks of bench-behaviours ---------------------------------------------


def need(twin, *features):
    missing = [f for f in features if f not in twin.available]
    if missing:
        pytest.skip("not installed: " + ", ".join(missing))


def test_scenario_dyad_vs_author(twin):
    need(twin, "recall", "person_role_between")
    out = one(twin, "dyad_vs_author")
    assert out["status"] == "passed", out
    metrics = out["metrics"]
    for name in ("between", "recall"):
        assert metrics[name]["both_sides"] == 1.0 and metrics[name]["exact"], name
        assert metrics[name]["third_person_rows"] == 0, name
        # Four direct messages and four group messages by A or B.
        assert metrics[name]["returned"] == 8, name
    # Author scope is one side, and it is not presented as a dyad.
    assert metrics["author"]["only_a"] and not metrics["author"]["presented_as_dyad"]
    # One person is never a dyad: 'between' with A alone is refused (4xx).
    assert metrics["single_person_between"]["refused"], metrics
    # The owner side by direction: outgoing rows without a person id count.
    for name in ("owner_between", "owner_recall"):
        assert metrics[name]["both_sides"] == 1.0 and metrics[name]["exact"], name
        assert metrics[name]["outgoing_rows"] == 2, name
    # Planting is idempotent: a second run writes nothing.
    again = one(twin, "dyad_vs_author")
    assert again["status"] == "passed" and again["planted"] == [], again


def test_scenario_open_loops(twin):
    need(twin, "open_loops")
    out = one(twin, "open_loops")
    metrics = out["metrics"]
    assert set(metrics["gold"].values()) == {"owner_owes", "they_owe"}
    for name in ("all_threads", "person"):
        part = metrics[name]
        # Never a wrong row: answered asks and wound-down chats are absent and
        # every returned loop has the right direction.
        assert part["closed_returned"] == 0 and part["wrong_direction"] == 0, out
        assert part["precision"] == 1.0 or not part["returned"], out
    assert "they_owe" in metrics["all_threads"]["returned"].values(), out
    assert out["status"] == "passed", out
    assert metrics["all_threads"]["recall"] == metrics["person"]["recall"] == 1.0


def test_scenario_media_contaminated(twin):
    need(twin, "media_noise", "recall")
    out = one(twin, "media_contaminated")
    assert out["status"] == "passed", out
    metrics = out["metrics"]
    assert metrics["played_flagged"] == 2 and metrics["talk_flagged"] == 0
    assert metrics["played_in_default_recall"] == 0
    assert metrics["played_with_include_ignored"] == 2
    assert metrics["talk_in_default_recall"] == 2
    assert metrics["content_unchanged"]
    if "function:around" in twin.available:
        assert metrics["played_in_default_around"] == 0
        assert metrics["played_in_around_include_ignored"] == 2
        assert metrics["talk_in_default_around"] == 2
    # A second run judges nothing new and the flags stay.
    again = one(twin, "media_contaminated")
    assert again["status"] == "passed" and again["planted"] == [], again


def test_scenario_unenrolled_voice(twin):
    need(twin, "speaker_inbox")
    out = one(twin, "unenrolled_voice")
    assert out["status"] == "passed", out
    metrics = out["metrics"]
    labels = twin.corpus.behaviours["unenrolled_voice"]["labels"]
    assert metrics["in_inbox"] == sorted(labels)
    # The label exists as an identifier and the attach is a client error.
    assert metrics["label_identifiers"] == len(labels)
    assert 400 <= metrics["attach_status"] < 500
    assert metrics["attach_refused"] and metrics["person_rows_before"] == 0
    assert metrics["annotated"] == ["annotated", "annotated"]
    assert metrics["people_created"] == 0 and metrics["person_rows_after"] == 0
    assert metrics["label_identifier_person"] is None
    assert metrics["ignored_left_inbox"]
    # Rerun: the ignored label no longer waits; the check still holds.
    again = one(twin, "unenrolled_voice")
    assert again["status"] == "passed", again
    assert again["metrics"]["in_inbox"] == [labels[0]]


def test_scenario_suspected_link(twin):
    need(twin, "identifier_status", "recall")
    out = one(twin, "suspected_link")
    assert out["status"] == "passed", out
    metrics = out["metrics"]
    for phase in ("suspected", "suspected_again"):
        assert metrics[phase]["suspected_in_timeline"] == 0, phase
        assert metrics[phase]["suspected_in_recall"] == 0, phase
        assert metrics[phase]["brief_messages"] == 2, phase
    assert metrics["confirmed"]["suspected_in_timeline"] == 2
    assert metrics["confirmed"]["suspected_in_recall"] == 2
    assert metrics["confirmed"]["brief_messages"] == 4
    assert metrics["confirm_attributed"] == 2


def test_scenario_write_replay(twin):
    need(twin, "idempotency")
    out = one(twin, "write_replay")
    assert out["status"] == "passed", out
    metrics = out["metrics"]
    for kind in ("link", "annotation"):
        assert metrics[kind]["rows_added"] == 1, kind
        assert metrics[kind]["rows_added_by_replay"] == 0, kind
        assert metrics[kind]["replayed"] == [False, True], kind
        assert metrics[kind]["same_receipt"], kind
    assert metrics["different_body"] == {"status": 409, "rows_added": 0}
    assert metrics["batch"]["indexes"] == [0, 1, 2]
    assert metrics["batch"]["statuses"] == ["existing", "created", "error"]
    # A new key for the same link is still one row.
    again = one(twin, "write_replay")
    assert again["status"] == "passed", again
    assert again["metrics"]["link"]["rows_added"] == 0


def test_scenario_restricted_inference_person_level(twin):
    need(twin, "function:person_brief")
    out = one(twin, "restricted_inference")
    assert out["status"] == "passed", out
    assert "person_level:checked" in out["disclosures"]
    metrics = out["metrics"]
    leaks = metrics["hidden_count_leak"]
    person = {k for k in leaks if k.startswith(("person_", "circle_"))}
    assert person >= {
        "person_brief_messages",
        "person_brief_hidden_channels",
        "person_brief_hidden_threads",
        "person_arc_messages",
        "person_rhythm_messages",
        "person_last_contact_hidden",
        "person_open_loops_hidden",
        "circle_hidden_people",
        "circle_rank_of_b",
    }
    assert all(leaks[k] == 0 for k in person)
    assert metrics["seen"]["person_brief_messages"] == 3
    # The owner sees the hidden rows that the restricted asker does not.
    segment = twin.corpus.behaviours["restricted_inference"]
    person_id = behaviours._author_person(
        twin.client, twin.corpus, segment["visible"][0]
    )
    owner = twin.client.get(f"/v1/people/{person_id}/brief").json()["brief"]
    assert owner["message_count"] == 7


def test_scenario_newest_relevant_last_contact(twin):
    need(twin, "recall", "function:last_contact")
    out = one(twin, "newest_relevant")
    assert out["status"] == "passed", out
    assert out["metrics"]["last_contact_is_answer"]
    assert not out["metrics"]["last_contact_is_newest_absolute"]


def test_behaviour_suite_all_twelve_reported_with_status(twin, tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext

    import bench.__main__ as cli
    from bench import scoreboard

    assert list(behaviours.BEHAVIOURS) == list(scenarios.BEHAVIOUR_SCENARIOS)
    for name, behaviour in behaviours.BEHAVIOURS.items():
        assert behaviour.needs and behaviour.description, name
        assert behaviour.check is not None, name
        assert name in twin.corpus.behaviours and twin.corpus.behaviours[name]["channels"]
    assert {
        n for n, b in behaviours.BEHAVIOURS.items() if b.checked_by == "bench-behaviours"
    } == {
        "dyad_vs_author",
        "open_loops",
        "media_contaminated",
        "unenrolled_voice",
        "suspected_link",
        "write_replay",
    }
    # Through the CLI against this process, with local principals.
    monkeypatch.setattr(cli, "_client", lambda args: nullcontext(twin.client))
    board = tmp_path / "scoreboard.jsonl"
    capsys.readouterr()
    command = ["behaviours", "--json", "--local-principals", "--config", "offline-local"]
    command += ["--scoreboard", str(board)]
    assert cli.main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert list(report["behaviours"]) == list(scenarios.BEHAVIOUR_SCENARIOS)
    counts = report["counts"]
    assert sum(counts.values()) == 12
    # On this twin every need is met and, run again in suite order after the
    # single-scenario tests, every check holds except the two documented as
    # once per loaded instance (a second run sees what the first wrote).
    once = {"consumer_exactly_once", "link_vocabulary"}
    failed = {
        n: o
        for n, o in report["behaviours"].items()
        if o["status"] != "passed" and n not in once
    }
    assert not failed, failed
    for name, out in report["behaviours"].items():
        assert out["status"] in ("passed", "failed", "skipped"), name
        if out["status"] == "skipped":
            # Only for a need this system lacks, named; never a zero.
            assert out["missing"] and out["metrics"] == {}, name
            assert out["reason"].startswith("missing need"), name
        else:
            # A check that raised is a failure with its reason.
            assert out["metrics"] or out["status"] == "failed" and out["reason"], name
    rows = scoreboard.read(board)
    assert rows[0]["suite"] == "behaviours" and rows[0]["counts"] == counts
    per = [r for r in rows if r["suite"] == "behaviour"]
    assert [r["behaviour"] for r in per] == list(scenarios.BEHAVIOUR_SCENARIOS)
    for row in per:
        assert row["status"] == report["behaviours"][row["behaviour"]]["status"]
        assert "metrics" in row and row["config"]["name"] == "offline-local"
    # Without other callers only the behaviours that need them are skipped.
    report = behaviours.run_all(
        twin.client, twin.corpus, twin.phase_times, available=twin.available
    )
    skipped = {n for n, o in report.items() if o["status"] == "skipped"}
    assert skipped == {"link_vocabulary", "restricted_inference"}
    assert all(report[n]["missing"] == ["principals"] for n in skipped)


# --- wave integration (w6-disclosure, w6-commitments) ----------------------------


def test_scenario_restricted_inference_labels(twin):
    if not twin.principals.can_label():
        pytest.skip("labels are not installed (w6-disclosure)")
    out = one(twin, "restricted_inference")
    assert out["status"] == "passed", out
    assert "labels:checked" in out["disclosures"]
    leaks = out["metrics"]["hidden_count_leak"]
    labelled = {k for k in leaks if k.startswith("labelled_")}
    assert labelled >= {
        "labelled_sql_count",
        "labelled_recall_records",
        "labelled_episode_rows",
        "labelled_brief_messages",
        "labelled_arc_messages",
        "labelled_rhythm_messages",
    }
    assert all(leaks[k] == 0 for k in labelled)
    # Taking the label off restores the granted rows.
    assert out["metrics"]["seen"]["unlabelled_sql_count"] == 3



def test_behaviour_unknown_behaviour_refused(twin, monkeypatch, capsys):
    from contextlib import nullcontext

    import bench.__main__ as cli

    with pytest.raises(ValueError, match="unknown behaviour: nope"):
        behaviours.run_all(
            twin.client, twin.corpus, available=twin.available, names=["nope"]
        )
    monkeypatch.setattr(cli, "_client", lambda args: nullcontext(twin.client))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["behaviours", "--behaviours", "episode_count,nope"])
    assert exit_info.value.code == 2
    assert "unknown behaviour: nope" in capsys.readouterr().err
