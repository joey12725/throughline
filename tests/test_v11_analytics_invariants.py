"""Analytics invariants on the semantic provider, the verdict
store and the cost ledger of api.match: frozen population, content + rubric +
model key, NULL not zero, partial flagged, estimate before spend, verdicts
kept after a set expires, group-grain verdicts never copied to members, and
counts before text. Synthetic data only."""

import hashlib
from datetime import timedelta

import pytest
from conftest import message
from test_v11_match import (
    OWNER,
    TEXTS,
    Counting,
    Failing,
    Remote,
    ids,
    ledger_rows,
    make,
    model,
    owner,
    put,
    records,
    refs,
    run,
    seed,
    stored,
    token_row,
    token_scope,
)

from throughline import cost, resultsets
from throughline.db import connect
from throughline.query import query_ex
from throughline.retrieval import match as match_module


@pytest.fixture(autouse=True)
def clean(database, monkeypatch):
    for name in ("THROUGHLINE_MATCH_MAX_ROWS", "THROUGHLINE_MATCH_GROUP_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_p7_frozen_population(client, channel):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m))
    # The member is edited after the set was saved.
    put(
        client,
        [
            message(
                0,
                external_id="fake-thread-0",
                thread={"external_id": "fake-thread"},
                content="Synthetic gardening only now",
            )
        ],
    )
    assert ids()["fake-thread-0"] != m[0]
    with model(Counting()) as fake:
        out, _ = run(OWNER, handle)
    # The pinned version is judged, never the current text.
    assert TEXTS[0] in fake.sent and "Synthetic gardening only now" not in fake.sent
    first = records(OWNER, out)[0]
    assert first["record_id"] == f"m:{m[0]}" and first["meta"]["verdict"] == "match"
    row = stored("WHERE ref_id=%s", (m[0],))[0]
    assert row["text_hash"] == sha(TEXTS[0])
    assert row["version"] == 1


def test_p7_cache_key_content_rubric_model(client, channel):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m))
    with model(Counting()) as fake:
        run(OWNER, handle)
        assert fake.calls == 1
        # Nothing but text, condition or model misses the cache: another set
        # of the same members, another order, a subset, another max_rows, a
        # differently spaced condition, another session.
        run(OWNER, make(OWNER, refs(list(reversed(m)))), max_rows=3)
        run(OWNER, make(OWNER, refs(m[:1]), completeness="ranked_sample"))
        run(OWNER, handle, condition=" prototype " + chr(9) + " review ")
        session = resultsets.open_session(OWNER)["id"]
        run(OWNER, make(OWNER, refs(m), session=session))
        assert fake.calls == 1
        # Condition.
        run(OWNER, handle, condition="gardening")
        assert fake.calls == 2
        # Text: a new version of one member.
        put(
            client,
            [
                message(
                    1,
                    external_id="fake-thread-1",
                    thread={"external_id": "fake-thread"},
                    content="Synthetic gardening diary, revised",
                )
            ],
        )
        newer = make(OWNER, refs([m[0], ids()["fake-thread-1"], m[2]]))
        run(OWNER, newer)
        assert fake.calls == 3 and fake.sent[-1] == "Synthetic gardening diary, revised"
    # Model.
    with model(Counting(base_version="local-rule-other")) as other:
        run(OWNER, handle)
        assert other.calls == 1


def test_p7_null_not_zero_on_failure(client, channel):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m))
    with model(Failing(mode="raise")):
        out, execution = run(OWNER, handle)
    metas = [r["meta"] for r in records(OWNER, out)]
    assert [meta["verdict"] for meta in metas] == [None, None, None]
    assert all(meta["error"] == "provider_unavailable" for meta in metas)
    counts = execution.to_dict()["counts"]
    assert (counts["match"], counts["nonmatch"], counts["uncertain"]) == (0, 0, 0)
    assert counts["failed"] == 3 and counts["semantically_evaluated"] == 0
    # In SQL the failure is NULL: it never counts as nonmatch or uncertain.
    rows = query_ex(
        f"SELECT count(*) FILTER (WHERE meta->>'verdict' IS NULL) AS null_n,"
        f" count(*) FILTER (WHERE meta->>'verdict' IN ('nonmatch','uncertain')) AS zero_n"
        f" FROM api.result_set('{out}')",
        scope=OWNER,
    )["rows"]
    assert rows == [dict(null_n=3, zero_n=0)]
    assert stored() == []


def test_p7_partial_flagged_in_counts_and_execution(client, channel):
    texts = TEXTS + ["Synthetic prototype review again"]
    m = seed(client, texts)
    handle = make(OWNER, refs(m))
    with model(Failing(poison={texts[3]}, batch_size=2)):
        out, execution = run(OWNER, handle)
    wire = execution.to_dict()
    assert wire["partial"] is True
    assert wire["status"] == "degraded"
    assert wire["counts"]["failed"] == 2
    assert any("failed" in w for w in wire["warnings"])
    info = resultsets.info(OWNER, out)
    assert info["counts"]["failed"] == 2 and info["origin"]["partial"] is True
    assert info["completeness"] == "exhaustive"
    # A complete run is not flagged.
    with model(Counting()):
        _, fine = run(OWNER, handle, condition="gardening")
    assert "partial" not in fine.to_dict() and fine.to_dict()["status"] == "complete"


def test_p7_estimate_before_spend(client, channel):
    m = seed(client, TEXTS)
    scope = token_scope()
    handle = make(scope, refs(m))
    before = ledger_rows()
    with model(Remote()) as remote:
        planned = match_module.estimate(
            scope, handle=handle, condition="prototype", max_rows=10
        )
        assert planned["counts"]["pending"] == 3
        assert planned["summary"]["cost"] > 0
        with pytest.raises(cost.ApprovalRequired):
            run(scope, handle, token=token_row(), condition="prototype")
        r = client.post(
            "/v1/match",
            json=dict(input=handle, condition="prototype", max_rows=10, dry_run=True),
        )
        assert r.status_code == 200 and r.json()["execution"]["estimate"]["cost"] > 0
        r = client.post(
            "/v1/match", json=dict(input=handle, condition="prototype", max_rows=10)
        )
        assert r.json()["execution"]["status"] == "approval_required"
        assert remote.calls == 0
    assert ledger_rows() == before
    assert stored() == []


def test_p7_verdicts_kept_after_set_expires(client, channel):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m))
    with model(Counting()) as fake:
        out, _ = run(OWNER, handle)
        owner(
            "UPDATE tl.result_sets SET expires_at=now()-interval '1 second' WHERE handle IN (%s,%s)",
            (handle, out),
        )
        resultsets.expire()
        assert (
            owner(
                "SELECT count(*) n FROM tl.result_sets WHERE handle IN (%s,%s)",
                (handle, out),
            )[0]["n"]
            == 0
        )
        assert len(stored()) == 3
        again, execution = run(OWNER, make(OWNER, refs(m)))
        assert fake.calls == 1
        assert execution.to_dict()["counts"]["cached"] == 3
        assert all(r["meta"]["cached"] for r in records(OWNER, again))


def test_p7_episode_grain_verdict_not_copied_to_members(client, channel):
    present = owner(
        "SELECT to_regprocedure('api.episodes_of(bigint[])') IS NOT NULL AS ok"
    )[0]["ok"]
    if not present:
        pytest.skip("api.episodes_of (semantic runtime, 0040) not present")
    from datetime import datetime, timezone

    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    texts = [
        ("Synthetic prototype planning", start),
        ("Synthetic review tomorrow", start + timedelta(minutes=1)),
        ("Synthetic coffee break", start + timedelta(minutes=2)),
        ("Synthetic gardening later", start + timedelta(days=30)),
    ]
    put(
        client,
        [
            message(
                i,
                external_id=f"fake-ep-{i}",
                thread={"external_id": "fake-ep"},
                content=text,
                happened_at=at.isoformat(),
            )
            for i, (text, at) in enumerate(texts)
        ],
    )
    found = ids()
    m = [found[f"fake-ep-{i}"] for i in range(4)]
    handle = make(OWNER, refs(m))
    episodes = {
        r["message_id"]: r["episode_id"]
        for r in owner("SELECT * FROM api.episodes_of(%s::bigint[])", (m,))
    }
    assert episodes[m[0]] == episodes[m[1]] == episodes[m[2]] != episodes[m[3]]
    with model(Counting()) as fake:
        out, execution = run(OWNER, handle, grain="episode")
    # Each episode is judged once, on the concatenation in time order.
    assert fake.calls == 1
    assert fake.sent[0] == "\n\n".join(t for t, _ in texts[:3])
    got = {r["record_id"]: r for r in records(OWNER, out)}
    anchor = got[f"m:{m[0]}"]
    assert anchor["role"] == "hit"
    assert anchor["meta"]["grain"] == "episode"
    assert anchor["meta"]["episode_id"] == episodes[m[0]]
    assert anchor["meta"]["verdict"] == "match"
    assert anchor["meta"]["members"] == 3
    evidence = {e["record_id"] for e in anchor["meta"]["evidence"]}
    assert evidence == {f"m:{m[0]}", f"m:{m[1]}"}
    for e in anchor["meta"]["evidence"]:
        content = got[e["record_id"]]["content"]
        for a, b in e["offsets"]:
            assert content[a:b].lower() in ("prototype", "review")
    for i in (1, 2):
        member = got[f"m:{m[i]}"]
        assert member["role"] == "context"
        assert member["depends_on"] == f"m:{m[0]}"
        assert "verdict" not in member["meta"]
        assert not [g for g in member["groups"] if g["grp"] == "match"]
    # Only the anchors carry verdicts.
    carrying = [r for r in got.values() if "verdict" in (r["meta"] or {})]
    assert {r["record_id"] for r in carrying} == {f"m:{m[0]}", f"m:{m[3]}"}
    assert got[f"m:{m[3]}"]["meta"]["verdict"] == "nonmatch"
    rows = stored()
    assert {r["record_prefix"] for r in rows} == {"ep"}
    ep_row = [r for r in rows if r["ref_id"] == m[0]][0]
    assert ep_row["version"] == 0
    assert sorted(ep_row["source_message_ids"]) == sorted(m[:3])
    assert execution.to_dict()["counts"]["episodes"] == 2
    # Record grain over the same set still judges each member separately.
    with model(Counting()) as fake:
        run(OWNER, handle)
    assert len(fake.sent) == 4


def test_p7_episode_grain_text_capped_and_flagged(client, channel, monkeypatch):
    present = owner(
        "SELECT to_regprocedure('api.episodes_of(bigint[])') IS NOT NULL AS ok"
    )[0]["ok"]
    if not present:
        pytest.skip("api.episodes_of (semantic runtime, 0040) not present")
    monkeypatch.setenv("THROUGHLINE_MATCH_GROUP_MAX_TOKENS", "10")
    m = seed(client, ["Synthetic " + "word " * 10, "Synthetic prototype at the end"])
    with model(Counting()) as fake:
        out, _ = run(OWNER, make(OWNER, refs(m)), grain="episode")
    assert len(fake.sent[0]) <= 40
    anchor = records(OWNER, out)[0]
    assert anchor["meta"]["truncated"] is True


def test_p7_counts_before_text(client, channel):
    m = seed(client, TEXTS)
    scope = token_scope()
    handle = make(scope, refs(m))
    with model(Counting()) as fake:
        rows = query_ex(
            f"SELECT count(*) AS n FROM api.result_set('{handle}')", scope=scope
        )["rows"]
        assert rows == [dict(n=3)]
        r = client.post(
            "/v1/sql",
            json=dict(
                statement=f"SELECT count(*) AS n FROM api.result_set('{handle}')"
            ),
        )
        assert r.status_code == 200, r.text
        assert r.json()["rows"] == [dict(n=3)]
        assert (
            match_module.estimate(
                scope, handle=handle, condition="prototype", max_rows=3
            )["counts"]["members"]
            == 3
        )
    assert fake.calls == 0
    assert stored() == []
