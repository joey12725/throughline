"""Episode grain in recall and result sets (scenario episode_count).

Synthetic data only. Tests marked optional-feature need api.episodes_of
(migration 0040, semantic runtime) and skip without it; the fallback
test forces the documented fallback and runs alone.
"""

import importlib

import pytest
from conftest import ingest, message

from throughline import resultsets
from throughline.db import connect
from throughline.retrieval import RecallRequest, recall
from throughline.retrieval import planner as planner_module
from throughline.scope import Scope

group_stage = importlib.import_module("throughline.retrieval.stages.group")

OWNER = Scope.local()
GAP = 4000  # seconds between sessions; the default channel gap is 1,800 s


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_RUNTIME_PROFILES", "0")
    planner_module.PLANNER.reset_profile_clock()


def run(**kwargs):
    return recall(OWNER, RecallRequest(**kwargs))


def owner(statement, params=()):
    with connect() as c:
        cursor = c.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def has_episodes():
    from throughline import query as query_module

    row = owner(
        "SELECT to_regprocedure('api.episodes_of(bigint[])') IS NOT NULL AS ok"
    )[0]
    return bool(row["ok"]) and "episodes_of" in query_module.FUNCTIONS


def wave_integration():
    if not has_episodes():
        pytest.skip("optional-feature: needs api.episodes_of (migration 0040)")


def sessions(client, channel="synthetic.chat", count=3, per=3, thread="fake-a"):
    """count sessions of per keyword hits each in one thread."""
    items = []
    for s in range(count):
        for j in range(per):
            i = s * GAP + j * 60
            items.append(
                message(
                    i,
                    channel=channel,
                    content=f"orchard incident report {s} {j}",
                    thread={"external_id": thread},
                    **({} if channel == "synthetic.chat" else {"fields": {}}),
                )
            )
    assert ingest(client, items).status_code == 200
    rows = owner(
        "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL ORDER BY m.happened_at",
        (channel,),
    )
    return [r["id"] for r in rows]


def test_episode_fallback_without_function(client, channel, monkeypatch):
    monkeypatch.setattr(group_stage, "episodes_available", lambda ctx: False)
    ids = sessions(client, count=2, per=3)
    response = run(query="orchard incident", k=20, group_by="episode")
    manifest = response.manifest
    assert "episode_fallback" in response.execution["warnings"]
    assert manifest["unit"] == "episode"
    assert manifest["records"] == 6 and manifest["incidents"] == 2
    thread = response.records[0]["thread_id"]
    assert sorted(g["key"] for g in manifest["groups"]) == sorted(
        [f"ep:{thread}:{ids[0]}", f"ep:{thread}:{ids[3]}"]
    )
    for record in response.records:
        assert record["groups"][0].startswith(f"ep:{thread}:")
    # Other grains keep unit 'record' and never fill incidents.
    plain = run(query="orchard incident", k=20, group_by="thread")
    assert plain.manifest["unit"] == "record" and plain.manifest["incidents"] is None
    assert plain.manifest["records"] == 6
    # Result sets follow the same rule without the function: tentative groups.
    monkeypatch.setattr(
        resultsets,
        "reader_transaction",
        _without_episodes(resultsets.reader_transaction),
    )
    saved = run(query="orchard incident", k=20, save=True)
    grouped = resultsets.transform(OWNER, "group", saved.result_set, by="episode")
    groups = {
        g["grp"]: g["certain"]
        for m in resultsets.read(OWNER, grouped)["records"]
        for g in m["groups"]
    }
    assert groups == {f"ep:{thread}:{ids[0]}": False, f"ep:{thread}:{ids[3]}": False}


def _without_episodes(real):
    """reader_transaction whose catalogue says api.episodes_of is absent."""
    from contextlib import contextmanager

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, *args, **kwargs):
            if "to_regprocedure('api.episodes_of" in statement:
                statement = "SELECT false AS ok"
            return self.conn.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    @contextmanager
    def wrapped(scope, *args, **kwargs):
        with real(scope, *args, **kwargs) as conn:
            yield Connection(conn)

    return wrapped


# --- optional-feature (api.episodes_of) ---------------------------------------------


def test_episode_count_incidents_not_hits(client, channel):
    wave_integration()
    sessions(client, count=3, per=3)
    response = run(query="orchard incident", k=50, group_by="episode")
    manifest = response.manifest
    assert manifest["unit"] == "episode"
    assert manifest["records"] == 9 and manifest["incidents"] == 3
    assert "episode_fallback" not in response.execution["warnings"]
    assert sorted(g["count"] for g in manifest["groups"]) == [3, 3, 3]


def test_episode_ids_stable_across_reads(client, channel):
    wave_integration()
    ids = sessions(client, count=2, per=2)
    first = run(query="orchard incident", k=50, group_by="episode")
    second = run(query="orchard incident", k=50, group_by="episode")
    keys = sorted(g["key"] for g in first.manifest["groups"])
    assert keys == sorted(g["key"] for g in second.manifest["groups"])
    thread = first.records[0]["thread_id"]
    assert keys == sorted([f"ep:{thread}:{ids[0]}", f"ep:{thread}:{ids[2]}"])


def test_whole_thread_channel_one_episode_regardless_of_gaps(client, channel):
    wave_integration()
    r = client.post(
        "/v1/channels",
        json=dict(
            name="fake.recording", plugin="synthetic", conversation_whole_thread=True
        ),
    )
    assert r.status_code == 200, r.text
    sessions(client, channel="fake.recording", count=3, per=2)
    response = run(
        query="orchard incident",
        k=50,
        group_by="episode",
        scope=dict(channels=["fake.recording"]),
    )
    assert response.manifest["incidents"] == 1
    assert response.manifest["records"] == 6


def test_result_set_group_by_episode(client, channel):
    wave_integration()
    ids = sessions(client, count=2, per=2)
    saved = run(query="orchard incident", k=50, save=True)
    grouped = resultsets.transform(OWNER, "group", saved.result_set, by="episode")
    records = resultsets.read(OWNER, grouped)["records"]
    thread = records[0]["thread_id"]
    assert {g["grp"] for m in records for g in m["groups"]} == {
        f"ep:{thread}:{ids[0]}",
        f"ep:{thread}:{ids[2]}",
    }
    assert all(g["certain"] for m in records for g in m["groups"])
    # Reversible like the other groupings.
    ungrouped = resultsets.transform(OWNER, "ungroup", grouped, prefix="ep:")
    assert all(m["groups"] == [] for m in resultsets.read(OWNER, ungrouped)["records"])


def test_non_message_records_are_singletons_not_incidents():
    from throughline.retrieval import selection
    from throughline.retrieval.types import Candidate

    hits = [
        Candidate(record_id="m:1", message_id=1),
        Candidate(record_id="m:2", message_id=2),
        Candidate(record_id="p:7", kind="passage"),
        Candidate(record_id="e:3"),
    ]
    keys = {"m:1": "ep:5:1", "m:2": "ep:5:1", "p:7": None, "e:3": None}
    manifest = selection.group_manifest(hits, "episode", keys=keys)
    assert manifest["unit"] == "episode"
    assert manifest["records"] == 4 and manifest["incidents"] == 1
    singles = {g["key"]: g for g in manifest["groups"] if g.get("singleton")}
    assert set(singles) == {"p:7", "e:3"}
    assert all(g["count"] == 1 for g in singles.values())
    # Result sets: a non-message member is its own group (no statement runs).
    items = [dict(ordinal=1, prefix="p", record_id="p:7", message_id=None)]
    assert resultsets._episodes(OWNER, items) == {1: {"p:7": True}}
