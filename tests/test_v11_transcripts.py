"""Transcript-native behaviour (migration 0049, throughline.transcripts):
per-segment media-noise flags through a fail-open provider operation, the
speaker inbox, 'only enrolled voices become people' on the identifier kind,
and recording-level grouping (IDN-6, IDN-8, MED-3,
ARC-6, CST-1; scenarios media_contaminated and unenrolled_voice).

Synthetic whole-thread channels shaped like transcripts.wave (author kinds
speaker_label and voice_profile; fields start_s, end_s, speaker_label,
ignore_reason, place) and a synthetic reference channel of media plays. The
semantic provider is the deterministic local rule or a fake defined here.
"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from conftest import ingest

from throughline import cost, embeddings, providers, transcripts
from throughline.db import connect, create_token
from throughline.derived import materialize
from throughline.providers.base import SemanticModel
from throughline.retrieval import RecallRequest, recall
from throughline.scope import Scope

WAVE = "synthetic.wave"
GAPPY = "synthetic.gappy"
PLAYS = "synthetic.plays"
T0 = datetime(2021, 3, 1, 20, 0, tzinfo=timezone.utc)
TITLE = "Moonlight Orchard"
LYRICS = (
    "under the silver moon we dance along the orchard road tonight and the "
    "lanterns keep on calling every one of us back home again"
)
OWNER = Scope.local()
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "throughline"
    / "migrations"
    / "0049_transcripts.sql"
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No provider configuration leaks in from the machine running the tests."""
    import os

    for name in list(os.environ):
        if name.startswith("THROUGHLINE_") and any(
            part in name
            for part in ("SEMANTIC_", "MODEL_", "EMBEDDING_", "RERANK", "MEDIA_NOISE")
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("THROUGHLINE_RUNTIME_PROFILES", "0")


# --- helpers ------------------------------------------------------------------------


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def headers(kind, plugin=None, askers=None):
    token = create_token("synthetic-" + kind, kind, plugin, askers)["token"]
    return {"Authorization": "Bearer " + token}


def owner(statement, params=()):
    with connect() as c:
        cursor = c.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def owner_token():
    row = owner("SELECT * FROM tl.tokens WHERE kind='owner' ORDER BY id LIMIT 1")[0]
    return dict(row, asker=None)


def channels(client):
    for body in (
        dict(
            name=WAVE,
            plugin="synthetic",
            conversation_whole_thread=True,
            fields=[
                dict(name="start_s", type="number"),
                dict(name="end_s", type="number"),
                dict(name="speaker_label", type="text"),
                dict(name="ignore_reason", type="text"),
                dict(name="place", type="text"),
            ],
        ),
        dict(
            name=GAPPY,
            plugin="synthetic",
            fields=[
                dict(name="start_s", type="number"),
                dict(name="end_s", type="number"),
                dict(name="speaker_label", type="text"),
            ],
        ),
        dict(
            name=PLAYS,
            plugin="synthetic",
            is_reference=True,
            record_kind="reference",
            fields=[dict(name="title", type="text")],
        ),
    ):
        ok(client.post("/v1/channels", json=body))


def turn(
    i,
    text,
    start,
    end,
    label="Speaker 1",
    kind="speaker_label",
    thread="rec-1",
    channel=WAVE,
    base=T0,
    **fields,
):
    value = dict(start_s=start, end_s=end, speaker_label=label, **fields)
    return dict(
        channel=channel,
        external_id=f"{thread}-{i}",
        thread={"external_id": thread, "title": f"Synthetic recording {thread}"},
        happened_at=(base + timedelta(seconds=start)).isoformat(),
        ended_at=(base + timedelta(seconds=end)).isoformat(),
        content=text,
        author=dict(system="wave", kind=kind, value=label),
        fields=value,
        payload={"synthetic": f"{thread}-{i}", "text": text},
    )


def play(i, text, start, end, title=TITLE):
    return dict(
        channel=PLAYS,
        external_id=f"play-{i}",
        thread={"external_id": "plays"},
        happened_at=(T0 + timedelta(seconds=start)).isoformat(),
        ended_at=(T0 + timedelta(seconds=end)).isoformat(),
        content=text,
        fields={"title": title},
        payload={"synthetic": f"play-{i}"},
    )


def put(client, items):
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    return r.json()


def current(external_id, channel=WAVE):
    return owner(
        "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
        (channel, external_id),
    )[0]["id"]


def thread_id(external_id="rec-1"):
    return owner("SELECT id FROM tl.threads WHERE external_id=%s", (external_id,))[0][
        "id"
    ]


def recording(client):
    """rec-1: talk, the playing song's lyrics, talk about the song, a marker,
    and a second speaker; plus the play of the song."""
    channels(client)
    put(
        client,
        [
            turn(0, "hey are you ready to start cooking dinner now", 0, 8),
            turn(1, " ".join(LYRICS.split()[:13]), 120, 130),
            turn(
                2,
                f"did you hear that {TITLE} song earlier? I love the chorus",
                200,
                210,
            ),
            turn(3, "[music]", 220, 225),
            turn(4, "turn the volume down please", 230, 236, label="Speaker 2"),
            play(0, LYRICS, 60, 300),
        ],
    )
    return {
        name: current(name)
        for name in ("rec-1-0", "rec-1-1", "rec-1-2", "rec-1-3", "rec-1-4")
    } | {"play": current("play-0", PLAYS)}


def run(policy=None, budget=None, dry_run=False, token=None):
    with connect("app") as c:
        return transcripts.run_noise(c, policy, budget, dry_run, token=token)


def checksum():
    return owner(
        "SELECT count(*) AS n, md5(string_agg(id||':'||version||':'||coalesce(content,'')||':'||payload_hash||':'||fields::text, ',' ORDER BY id)) AS h FROM tl.messages"
    )[0]


def flags(message_id=None):
    return owner(
        """SELECT a.* FROM tl.annotations a JOIN tl.messages m ON m.channel_id=a.target_channel_id AND m.external_id=a.target_external_id
        WHERE a.attribute='media_noise' AND a.job='transcripts' AND a.status='active' AND m.superseded_by IS NULL
        AND (%s::bigint IS NULL OR m.id=%s) ORDER BY a.id""",
        (message_id, message_id),
    )


def flagged_ids():
    return {
        r["id"]
        for r in owner(
            """SELECT m.id FROM tl.annotations a JOIN tl.messages m ON m.channel_id=a.target_channel_id AND m.external_id=a.target_external_id
            WHERE a.attribute='media_noise' AND a.job='transcripts' AND a.status='active' AND m.superseded_by IS NULL
            AND a.value->'is_noise'='true'::jsonb"""
        )
    }


def recalled(query, **scope):
    response = recall(
        OWNER, RecallRequest(query=query, k=50, scope=dict(channels=[WAVE], **scope))
    )
    return {
        r["message_id"] for r in response.records if r["record_id"].startswith("m:")
    }


def effective(ids):
    with connect("reader") as c:
        return {
            r["message_id"]: r
            for r in c.execute("SELECT * FROM api.effective(%s::bigint[])", (ids,))
        }


def api_function(name):
    return owner(
        "SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.proname=%s) AS ok",
        (name,),
    )[0]["ok"]


class Fake(SemanticModel):
    """A semantic provider that raises, or answers 'media' for chosen ids."""

    name = "fake"
    version = "fake-v1"
    model = "fake-model"
    batch_size = 100

    def __init__(self, error=None, media=(), local=True, price=0.0):
        self.error = error
        self.media = set(media)
        self.local = local
        self.price_per_million = price
        self.calls = 0

    def apply(self, operation, instruction, items, output_schema=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        items = list(items)
        return [
            dict(
                id=i["id"],
                output="media" if i["id"] in self.media else "conversation",
                evidence=[],
                note="fake",
                score=0.75,
            )
            for i in items
        ], self._usage(40 * len(items))


# --- media noise ----------------------------------------------------------------------


def test_media_contaminated_played_media_flagged_not_rewritten(client):
    m = recording(client)
    before = checksum()
    assert m["rec-1-1"] in recalled("silver moon dance orchard road")
    result = ok(client.post("/v1/transcripts/noise/run", json={}))
    assert result["status"] == "done" and not result["failures"]
    assert result["recordings"] == 1 and result["turns_judged"] == 5
    assert result["flagged"] == 2 and result["method"] == "rule"
    assert flagged_ids() == {m["rec-1-1"], m["rec-1-3"]}
    (flag,) = flags(m["rec-1-1"])
    assert flag["method"] == "rule" and flag["origin"] == "job"
    assert flag["job_version"] == transcripts.JOB_VERSION
    assert flag["value"]["is_noise"] is True
    assert flag["value"]["matched_record_id"] == f"m:{m['play']}"
    assert flag["value"]["score"] >= 0.5 and flag["value"]["evidence"]
    # Ground truth is untouched: same rows, versions, text, payload hashes.
    assert checksum() == before
    # Excluded by default like an ignored turn; back with include_ignored.
    assert m["rec-1-1"] not in recalled("silver moon dance orchard road")
    assert m["rec-1-1"] in recalled(
        "silver moon dance orchard road", include_ignored=True
    )
    state = effective([m["rec-1-1"], m["rec-1-0"]])
    assert state[m["rec-1-1"]]["media_noise"] and state[m["rec-1-1"]]["ignored"]
    assert "media_noise" in state[m["rec-1-1"]]["ignore_reasons"]
    assert not state[m["rec-1-0"]]["ignored"]
    if api_function("around"):
        # optional-feature with people: api.around excludes it by default.
        with connect("reader") as c:
            near = {
                r["message_id"]
                for r in c.execute(
                    "SELECT * FROM api.around(at=>%s::timestamptz,window_s=>3600)",
                    (T0,),
                )
            }
            every = {
                r["message_id"]
                for r in c.execute(
                    "SELECT * FROM api.around(at=>%s::timestamptz,window_s=>3600,include_ignored=>true)",
                    (T0,),
                )
            }
        assert m["rec-1-1"] not in near and m["rec-1-0"] in near
        assert m["rec-1-1"] in every
    # Judged since its last ingest: the next run has nothing to do.
    again = run()
    assert again["recordings"] == 0 and again["status"] == "done"
    status = ok(client.get("/v1/transcripts/noise/status"))
    assert status["judged_recordings"] == 1 and status["pending_recordings"] == 0
    assert status["active_flags"] == 2 and status["runs"]
    # Owner tokens only; restricted askers never.
    assert (
        client.post(
            "/v1/transcripts/noise/run", json={}, headers=headers("agent")
        ).status_code
        == 403
    )


def test_conversation_about_media_kept(client):
    m = recording(client)
    run()
    assert m["rec-1-2"] not in flagged_ids() and m["rec-1-0"] not in flagged_ids()
    assert m["rec-1-2"] in recalled("chorus")
    media = [dict(record_id="m:9", text=LYRICS, title=TITLE)]
    about = transcripts.classify(
        dict(
            id=1,
            text=f"that {TITLE} track was playing at the party, the moon one",
            context=dict(overlapping_media=media, duration_s=6),
        )
    )
    assert about["output"] == "conversation" and "kept" in about["note"]
    sung = transcripts.classify(
        dict(
            id=2,
            text=" ".join(LYRICS.split()[5:18]),
            context=dict(overlapping_media=media, duration_s=6),
        )
    )
    assert sung["output"] == "media" and sung["matched_record_id"] == "m:9"
    # Lyric-like repetition needs at least 40 tokens.
    chorus = "oh la la we go round and round " * 6
    assert transcripts.classify(dict(id=3, text=chorus))["output"] == "media"
    assert (
        transcripts.classify(dict(id=4, text=chorus[:60]))["output"] == "conversation"
    )
    # The overlap threshold is configurable.
    half = " ".join(LYRICS.split()[:6]) + " and then we argued about the dinner plans"
    item = dict(id=5, text=half, context=dict(overlapping_media=media, duration_s=4))
    assert transcripts.classify(item)["output"] == "conversation"
    assert transcripts.classify(item, threshold=0.1)["output"] == "media"


def test_flags_per_segment_short_recordings_covered(client):
    channels(client)
    long_base = T0 - timedelta(days=1)
    put(
        client,
        [
            turn(0, "[applause]", 0, 3, thread="rec-short"),
            turn(1, "thanks everyone that was lovely", 4, 9, thread="rec-short"),
            turn(
                0,
                "let us get the meeting going",
                0,
                30,
                thread="rec-long",
                base=long_base,
            ),
            turn(1, "♪ la la ♪", 7000, 7100, thread="rec-long", base=long_base),
        ],
    )
    # Two turns at most: the recording of an hour or more goes first; the
    # short one is not dropped, only left for the next run.
    first = run({"max_items": 2})
    assert first["status"] == "max_items" and first["recordings"] == 1
    assert first["by_length_bucket"]["1h_or_more"] == dict(
        recordings=1, turns=2, flagged=1
    )
    assert first["pending_recordings"] == 1
    second = run()
    assert second["status"] == "done" and second["recordings"] == 1
    assert second["by_length_bucket"]["under_10m"] == dict(
        recordings=1, turns=2, flagged=1
    )
    # Flags are per segment: one turn of each recording, never the recording.
    assert flagged_ids() == {current("rec-short-0"), current("rec-long-1")}
    assert transcripts.policy_from(None).min_duration_s == 0
    assert run({"min_duration_s": 0})["recordings"] == 0
    with pytest.raises(ValueError):
        transcripts.policy_from({"cutoff_s": 3600})


def test_noise_fail_open_on_provider_error_timeout_and_approval(client):
    m = recording(client)
    before = checksum()
    for provider, reason in (
        (Fake(error=providers.ProviderUnavailable("down")), "provider_error"),
        (Fake(error=TimeoutError("timed out")), "provider_timeout"),
        (Fake(local=False, price=1e7, media=[m["rec-1-1"]]), "approval_required"),
    ):
        with providers.override("semantic", provider):
            result = run()
        assert result["status"] == "partial", result
        assert result["flagged"] == 0
        assert [f["reason"] for f in result["failures"]] == [reason]
        if reason == "approval_required":
            assert provider.calls == 0  # refused before anything was sent
            assert result["failures"][0]["approval_id"]
        assert not flags()
        assert owner("SELECT count(*) AS n FROM tl.transcript_noise_state")[0]["n"] == 0
        assert checksum() == before
        # Nothing hidden by a failure.
        assert m["rec-1-1"] in recalled("silver moon dance orchard road")
    with providers.override("semantic", None):
        unavailable = run()
    assert unavailable["status"] == "partial"
    assert unavailable["failures"][0]["reason"] == "provider_unavailable"
    # The recording is judged by the next working run.
    done = run()
    assert done["status"] == "done" and done["flagged"] == 2
    assert owner("SELECT count(*) AS n FROM tl.transcript_noise_state")[0]["n"] == 1
    runs = owner("SELECT status FROM tl.transcript_noise_runs ORDER BY id")
    assert [r["status"] for r in runs] == ["partial"] * 4 + ["done"]


def test_owner_unflag_respected_by_later_runs(client):
    m = recording(client)
    run()
    assert m["rec-1-1"] in flagged_ids()
    ok(
        client.post(
            "/v1/annotations",
            json={
                "message_id": m["rec-1-1"],
                "attribute": "media_noise",
                "value": {"is_noise": False},
                "reason": "we were singing along",
            },
        )
    )
    ok(
        client.post(
            "/v1/annotations",
            json={
                "message_id": m["rec-1-3"],
                "attribute": "ignore",
                "value": {"ignored": False},
            },
        )
    )
    assert m["rec-1-1"] in recalled("silver moon dance orchard road")
    # A new ingest makes the recording due again; the owner's turns are
    # skipped before anything is written.
    put(client, [turn(0, "hey are you ready to start cooking dinner soon", 0, 8)])
    count = len(flags())
    again = run()
    assert again["recordings"] == 1 and again["owner_kept"] == 2
    assert again["turns_judged"] == 3 and len(flags()) == count
    state = effective([m["rec-1-1"], m["rec-1-3"]])
    assert not state[m["rec-1-1"]]["ignored"] and not state[m["rec-1-1"]]["media_noise"]
    assert not state[m["rec-1-3"]]["ignored"]
    assert m["rec-1-1"] in recalled("silver moon dance orchard road")


def test_noise_run_cost_gated_for_non_local(client):
    m = recording(client)
    expensive = Fake(local=False, price=1e6, media=[m["rec-1-1"]])
    with providers.override("semantic", expensive):
        refused = run()
    assert refused["status"] == "partial" and expensive.calls == 0
    approval = refused["failures"][0]["approval_id"]
    pending = owner("SELECT status,cost FROM tl.approvals WHERE id=%s", (approval,))
    assert pending[0]["status"] == "pending" and pending[0]["cost"] > 0.1
    with connect("app") as c:
        cost.decide(c, approval, "approved", by="owner")
    with providers.override("semantic", expensive):
        approved = run({"approval_id": approval})
    assert approved["status"] == "done" and expensive.calls == 1
    assert approved["method"] == "model" and approved["model_calls"] == 1
    assert approved["cost"] > 0
    (flag,) = flags()
    assert flag["method"] == "model" and flag["value"]["score"] == 0.75
    ledger = owner(
        "SELECT purpose,origin,cost FROM tl.model_ledger WHERE purpose=%s",
        (transcripts.OPERATION,),
    )
    assert ledger and ledger[0]["origin"] == "job"
    # A cheap remote call under the limit needs no approval.
    put(client, [turn(5, "one more cheap line", 240, 244)])
    cheap = Fake(local=False, price=1.0)
    with providers.override("semantic", cheap):
        assert run()["status"] == "done"
    assert cheap.calls == 1
    # The local rule is free and never asks.
    assert run({"max_cost": 0})["status"] == "done"


def test_noise_dry_run_writes_nothing(client):
    recording(client)
    result = run(dry_run=True)
    assert result["would_judge"] == 5 and result["estimate"]["local"]
    assert not flags()
    assert owner("SELECT count(*) AS n FROM tl.transcript_noise_runs")[0]["n"] == 0


# --- speakers -------------------------------------------------------------------------


def test_unenrolled_voice_never_becomes_person(client):
    m = recording(client)
    label = owner(
        "SELECT id FROM tl.identifiers WHERE kind='speaker_label' AND value='Speaker 1'"
    )[0]["id"]
    person = owner(
        "INSERT INTO tl.people(handle) VALUES ('synthetic-alex') RETURNING id"
    )[0]["id"]
    assert not owner("SELECT 1 FROM tl.identifier_schemes WHERE kind='speaker_label'")
    agent = headers("agent")

    def refused_everywhere():
        for h in ({}, agent):
            r = client.post(
                "/v1/identity/attach",
                json={"identifier_id": label, "person_id": person},
                headers=h,
            )
            assert r.status_code == 400, r.text
        for role in ("app", None):
            with connect(role) as c:
                with pytest.raises(
                    psycopg.errors.CheckViolation, match="only enrolled voices"
                ):
                    c.execute(
                        "UPDATE tl.identifiers SET person_id=%s WHERE id=%s",
                        (person, label),
                    )
        with connect("app") as c:
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(
                    "INSERT INTO tl.identifiers(system,kind,value,value_norm,person_id) VALUES ('wave','speaker_label','Speaker 9','Speaker 9',%s)",
                    (person,),
                )

    refused_everywhere()
    # The CHECK holds by itself (even without the trigger), for the owner role.
    with connect() as c:
        c.execute("ALTER TABLE tl.identifiers DISABLE TRIGGER unenrolled_voice")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "UPDATE tl.identifiers SET person_id=%s WHERE id=%s", (person, label)
            )
        c.rollback()
    # A scheme row created at runtime changes nothing.
    owner(
        "INSERT INTO tl.identifier_schemes(system,kind,normalizer) VALUES ('wave','speaker_label','lower')"
    )
    refused_everywhere()
    # A voice profile stays attachable and resolves its turns.
    put(
        client,
        [
            turn(
                6,
                "the enrolled voice speaking",
                300,
                305,
                label="voice-alex",
                kind="voice_profile",
            )
        ],
    )
    voice = owner("SELECT id FROM tl.identifiers WHERE kind='voice_profile'")[0]["id"]
    ok(
        client.post(
            "/v1/identity/attach", json={"identifier_id": voice, "person_id": person}
        )
    )
    with connect("reader") as c:
        theirs = {
            r["id"]
            for r in c.execute("SELECT * FROM api.person_messages(%s)", (person,))
        }
        persons = {
            r["id"]: r["person_id"]
            for r in c.execute(
                "SELECT id,person_id FROM api.messages WHERE id=ANY(%s)",
                (list(m.values()),),
            )
        }
    assert theirs == {current("rec-1-6")}
    assert all(persons[m[k]] is None for k in m if k.startswith("rec-1"))
    assert (
        owner("SELECT person_id FROM tl.identifiers WHERE id=%s", (label,))[0][
            "person_id"
        ]
        is None
    )


def test_speaker_inbox_lists_unenrolled_labels_unrestricted_only(client):
    m = recording(client)
    put(
        client,
        [
            turn(
                6,
                "the enrolled voice speaking",
                300,
                305,
                label="voice-x",
                kind="voice_profile",
            )
        ],
    )
    rows = ok(client.get("/v1/transcripts/speakers"))["speakers"]
    by_label = {r["label"]: r for r in rows}
    assert set(by_label) == {"Speaker 1", "Speaker 2"}
    one = by_label["Speaker 1"]
    assert one["thread_id"] == thread_id() and one["turns"] == 4
    assert one["recording_title"] == "Synthetic recording rec-1"
    assert one["total_s"] == pytest.approx(8 + 10 + 10 + 5)
    assert one["sample_message_ids"] == [m["rec-1-0"], m["rec-1-1"], m["rec-1-2"]]
    assert one["annotated"] is False
    assert datetime.fromisoformat(one["first_at"]) == T0
    # The same label in another recording is its own row.
    put(client, [turn(0, "a different evening", 0, 4, thread="rec-2")])
    rows = ok(client.get("/v1/transcripts/speakers"))["speakers"]
    assert sorted((r["label"], r["thread_id"]) for r in rows) == sorted(
        [
            ("Speaker 1", thread_id()),
            ("Speaker 2", thread_id()),
            ("Speaker 1", thread_id("rec-2")),
        ]
    )
    # Restricted askers: 403 on the route, nothing from the function, and it
    # is not on the SQL allow-list.
    restricted = headers("agent", askers=["synthetic-guest"])
    assert client.get("/v1/transcripts/speakers", headers=restricted).status_code == 403
    guest = Scope(
        token_id=None,
        token_kind="agent",
        asker="synthetic-guest",
        mode="asker",
        unknown_asker="deny",
    )
    assert transcripts.speaker_inbox(guest) == []
    assert transcripts.speaker_inbox(OWNER)
    r = client.post("/v1/sql", json={"statement": "SELECT * FROM api.speaker_inbox()"})
    assert r.status_code == 400
    assert not owner("SELECT 1 FROM tl.api_functions WHERE name='speaker_inbox'")


def test_ignored_speaker_absent_from_inbox(client):
    m = recording(client)
    result = ok(
        client.post(
            "/v1/transcripts/speakers/ignore",
            json={
                "thread_id": thread_id(),
                "label": "Speaker 2",
                "reason": "television",
            },
        )
    )
    assert result["turns"] == 1 and result["written"] == 1
    labels = {
        r["label"] for r in ok(client.get("/v1/transcripts/speakers"))["speakers"]
    }
    assert labels == {"Speaker 1"}
    unclaimed = {r["value"] for r in ok(client.get("/v1/identity/unclaimed"))}
    assert "Speaker 2" not in unclaimed and "Speaker 1" in unclaimed
    state = effective([m["rec-1-4"]])[m["rec-1-4"]]
    assert state["ignored"] and "television" in state["ignore_reasons"]
    assert m["rec-1-4"] not in recalled("volume")


def test_ignore_shared_label_leaves_only_that_recording(client):
    # A label identifier shared by two recordings ('Speaker 1' here; Wave
    # itself names labels per recording): ignoring it in one recording must
    # not hide it from the other recording's inbox row.
    recording(client)
    put(client, [turn(0, "a different evening", 0, 4, thread="rec-2")])
    result = ok(
        client.post(
            "/v1/transcripts/speakers/ignore",
            json={"thread_id": thread_id(), "label": "Speaker 1", "reason": "tv"},
        )
    )
    assert result["turns"] == 4 and result["identifier_ids"] == []
    rows = ok(client.get("/v1/transcripts/speakers"))["speakers"]
    assert sorted((r["label"], r["thread_id"]) for r in rows) == sorted(
        [("Speaker 2", thread_id()), ("Speaker 1", thread_id("rec-2"))]
    )
    status = owner(
        "SELECT status FROM tl.identifiers WHERE kind='speaker_label' AND value='Speaker 1'"
    )[0]["status"]
    assert status != "ignored"
    # Ignored in the other recording too: now it is heard nowhere else.
    done = ok(
        client.post(
            "/v1/transcripts/speakers/ignore",
            json={"thread_id": thread_id("rec-2"), "label": "Speaker 1"},
        )
    )
    assert len(done["identifier_ids"]) == 1
    status = owner(
        "SELECT status FROM tl.identifiers WHERE kind='speaker_label' AND value='Speaker 1'"
    )[0]["status"]
    assert status == "ignored"
    rows = ok(client.get("/v1/transcripts/speakers"))["speakers"]
    assert [(r["label"], r["thread_id"]) for r in rows] == [("Speaker 2", thread_id())]


def test_assign_speaker_writes_annotations(client):
    m = recording(client)
    person = owner(
        "INSERT INTO tl.people(handle) VALUES ('synthetic-sam') RETURNING id"
    )[0]["id"]
    body = {"thread_id": thread_id(), "label": "Speaker 1", "person_id": person}
    assert (
        client.post(
            "/v1/transcripts/speakers/assign", json=body, headers=headers("agent")
        ).status_code
        == 403
    )
    result = ok(client.post("/v1/transcripts/speakers/assign", json=body))
    assert result["turns"] == 4 and result["written"] == 4
    turns = [m[f"rec-1-{i}"] for i in range(4)]
    rows = owner(
        """SELECT m.id,a.method,a.value FROM tl.annotations a JOIN tl.messages m ON m.channel_id=a.target_channel_id
        AND m.external_id=a.target_external_id WHERE a.attribute='speaker' AND a.status='active'"""
    )
    assert {r["id"] for r in rows} == set(turns)
    assert all(
        r["method"] == "owner" and r["value"] == {"person_id": person} for r in rows
    )
    state = effective(turns)
    assert {state[i]["speaker_person_id"] for i in turns} == {person}
    # Annotation, not attribution: the label still carries no person.
    assert {
        r["person_id"]
        for r in owner("SELECT person_id FROM tl.messages WHERE id=ANY(%s)", (turns,))
    } == {None}
    again = ok(client.post("/v1/transcripts/speakers/assign", json=body))
    assert again["written"] == 0 and again["unchanged"] == 4
    inbox = {
        r["label"]: r for r in ok(client.get("/v1/transcripts/speakers"))["speakers"]
    }
    assert inbox["Speaker 1"]["annotated"] is True
    assert inbox["Speaker 2"]["annotated"] is False
    missing = client.post(
        "/v1/transcripts/speakers/assign", json=dict(body, label="Speaker 7")
    )
    assert missing.status_code == 404
    assert (
        client.post(
            "/v1/transcripts/speakers/assign", json=dict(body, person_id=999999)
        ).status_code
        == 404
    )
    events = owner(
        "SELECT operation FROM tl.identity_events WHERE operation='speaker_assign'"
    )
    assert len(events) == 2
    if api_function("attribution"):
        # optional-feature with people: an annotated speaker shows as such.
        with connect("reader") as c:
            statuses = {
                r["attribution_status"]
                for r in c.execute(
                    "SELECT * FROM api.attribution(%s::bigint[])", (turns,)
                )
            }
        assert statuses == {"annotated"}


def test_ignore_speaker_sets_ignored(client):
    m = recording(client)
    with connect("app") as c:
        result = transcripts.ignore_speaker(
            c, owner_token(), thread_id(), "Speaker 2", "television"
        )
    assert result["identifier_ids"]
    status = owner(
        "SELECT status,asserted_by FROM tl.identifiers WHERE kind='speaker_label' AND value='Speaker 2'"
    )[0]
    assert status == {"status": "ignored", "asserted_by": "owner"}
    (annotation,) = owner(
        """SELECT a.* FROM tl.annotations a JOIN tl.messages m ON m.channel_id=a.target_channel_id
        AND m.external_id=a.target_external_id WHERE m.id=%s AND a.attribute='ignore'""",
        (m["rec-1-4"],),
    )
    assert annotation["method"] == "owner"
    assert annotation["value"] == {"ignored": True, "reason": "television"}
    assert owner("SELECT 1 FROM tl.identity_events WHERE operation='speaker_ignore'")
    with connect("app") as c, pytest.raises(PermissionError):
        transcripts.ignore_speaker(
            c, dict(owner_token(), kind="agent"), thread_id(), "Speaker 1"
        )


def test_migration_converts_existing_label_attachments(client):
    m = recording(client)
    text = MIGRATION.read_text(encoding="utf-8")
    # Conversion first, then the rule; the rule is on the identifier kind.
    assert text.index("SELECT tl.convert_speaker_label_attachments();") < text.index(
        "ADD CONSTRAINT identifiers_unenrolled_voice"
    )
    assert owner(
        "SELECT 1 FROM pg_constraint WHERE conname='identifiers_unenrolled_voice' AND conrelid='tl.identifiers'::regclass"
    )
    label = owner(
        "SELECT id FROM tl.identifiers WHERE kind='speaker_label' AND value='Speaker 1'"
    )[0]["id"]
    with connect() as c:
        alex = c.execute(
            "INSERT INTO tl.people(handle) VALUES ('synthetic-alex') RETURNING id"
        ).fetchone()["id"]
        other = c.execute(
            "INSERT INTO tl.people(handle) VALUES ('synthetic-other') RETURNING id"
        ).fetchone()["id"]
        # The state before 0049: a label attached to a person.
        c.execute(
            "ALTER TABLE tl.identifiers DROP CONSTRAINT identifiers_unenrolled_voice"
        )
        c.execute("ALTER TABLE tl.identifiers DISABLE TRIGGER unenrolled_voice")
        c.execute("UPDATE tl.identifiers SET person_id=%s WHERE id=%s", (alex, label))
        c.execute(
            "UPDATE tl.messages SET person_id=%s WHERE author_identifier_id=%s",
            (alex, label),
        )
        # One turn already carries the owner's own speaker annotation.
        c.execute(
            """INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin)
            SELECT channel_id,external_id,'speaker',jsonb_build_object('person_id',%s::bigint),'owner','owner'
            FROM tl.messages WHERE id=%s""",
            (other, m["rec-1-0"]),
        )
        written = c.execute(
            "SELECT tl.convert_speaker_label_attachments() AS n"
        ).fetchone()["n"]
        assert written == 3
        assert (
            c.execute(
                "SELECT person_id FROM tl.identifiers WHERE id=%s", (label,)
            ).fetchone()["person_id"]
            is None
        )
        assert {
            r["person_id"]
            for r in c.execute(
                "SELECT person_id FROM tl.messages WHERE author_identifier_id=%s",
                (label,),
            )
        } == {None}
        speakers = {
            r["id"]: r["value"]["person_id"]
            for r in c.execute(
                """SELECT m.id,a.value FROM tl.annotations a JOIN tl.messages m ON m.channel_id=a.target_channel_id
                AND m.external_id=a.target_external_id WHERE a.attribute='speaker' AND a.method='owner' AND a.status='active'"""
            )
        }
        assert speakers == {
            m["rec-1-0"]: other,
            m["rec-1-1"]: alex,
            m["rec-1-2"]: alex,
            m["rec-1-3"]: alex,
        }
        event = c.execute(
            "SELECT detail FROM tl.identity_events WHERE operation='speaker_label_conversion'"
        ).fetchone()["detail"]
        assert event["identifiers"][0]["identifier_id"] == label
        assert event["annotations"] == 3
        # No person was created from a label.
        assert c.execute("SELECT count(*) AS n FROM tl.people").fetchone()["n"] == 2
        # Nothing left to convert: re-running is a no-op.
        assert (
            c.execute("SELECT tl.convert_speaker_label_attachments() AS n").fetchone()[
                "n"
            ]
            == 0
        )
        c.rollback()
    assert owner(
        "SELECT 1 FROM pg_constraint WHERE conname='identifiers_unenrolled_voice'"
    )


def test_late_enrolment_resolves_without_reimport(client):
    channels(client)
    put(
        client,
        [
            turn(0, "first thing I said", 0, 5, label="Speaker 3"),
            turn(1, "second thing I said", 6, 10, label="Speaker 3"),
        ],
    )
    first = {i: current(f"rec-1-{i}") for i in range(2)}
    ok(
        client.post(
            "/v1/annotations",
            json={
                "message_id": first[1],
                "attribute": "effective_place",
                "value": {"name": "Synthetic kitchen"},
            },
        )
    )
    assert any(
        r["label"] == "Speaker 3"
        for r in ok(client.get("/v1/transcripts/speakers"))["speakers"]
    )
    person = owner(
        "INSERT INTO tl.people(handle) VALUES ('synthetic-kim') RETURNING id"
    )[0]["id"]
    owner(
        "INSERT INTO tl.identifiers(system,kind,value,value_norm,person_id) VALUES ('wave','voice_profile','voice-kim','voice-kim',%s)",
        (person,),
    )
    # The source re-sends the same turns, now with the enrolled voice.
    put(
        client,
        [
            turn(
                0, "first thing I said", 0, 5, label="voice-kim", kind="voice_profile"
            ),
            turn(
                1, "second thing I said", 6, 10, label="voice-kim", kind="voice_profile"
            ),
        ],
    )
    rows = owner(
        "SELECT external_id,count(*) AS versions,max(thread_id) AS thread FROM tl.messages GROUP BY external_id ORDER BY external_id"
    )
    assert [(r["external_id"], r["versions"]) for r in rows] == [
        ("rec-1-0", 2),
        ("rec-1-1", 2),
    ]
    now = {i: current(f"rec-1-{i}") for i in range(2)}
    assert {
        r["person_id"]
        for r in owner(
            "SELECT person_id FROM tl.messages WHERE id=ANY(%s)", (list(now.values()),)
        )
    } == {person}
    with connect("reader") as c:
        theirs = {
            r["id"]
            for r in c.execute("SELECT * FROM api.person_messages(%s)", (person,))
        }
    assert theirs == set(now.values())
    # Curation keyed on the record follows the new version.
    assert effective([now[1]])[now[1]]["effective_place"] == "Synthetic kitchen"
    assert not any(
        r["label"] == "Speaker 3"
        for r in ok(client.get("/v1/transcripts/speakers"))["speakers"]
    )


def test_ignored_turn_survives_reimport_with_curation(client):
    m = recording(client)
    put(
        client,
        [turn(5, "crosstalk from the next table", 240, 244, ignore_reason="crosstalk")],
    )
    noisy = current("rec-1-5")
    assert noisy not in recalled("crosstalk")
    person = owner(
        "INSERT INTO tl.people(handle) VALUES ('synthetic-ida') RETURNING id"
    )[0]["id"]
    ok(
        client.post(
            "/v1/transcripts/speakers/assign",
            json={"thread_id": thread_id(), "label": "Speaker 1", "person_id": person},
        )
    )
    run()
    # Re-import: the same payloads change nothing; an edited payload makes
    # new versions. Curation (owner speaker, job flag, source ignore) holds.
    put(
        client,
        [turn(5, "crosstalk from the next table", 240, 244, ignore_reason="crosstalk")],
    )
    put(
        client,
        [
            turn(1, " ".join(LYRICS.split()[:13]), 120, 131),
            turn(
                5, "crosstalk from the next table!", 240, 244, ignore_reason="crosstalk"
            ),
        ],
    )
    new = {k: current(k) for k in ("rec-1-1", "rec-1-5")}
    assert new["rec-1-1"] != m["rec-1-1"]
    state = effective(list(new.values()))
    assert state[new["rec-1-5"]]["ignored"]
    assert state[new["rec-1-1"]]["media_noise"] and state[new["rec-1-1"]]["ignored"]
    assert state[new["rec-1-1"]]["speaker_person_id"] == person
    assert new["rec-1-5"] not in recalled("crosstalk")
    assert new["rec-1-5"] in recalled("crosstalk", include_ignored=True)
    # The re-imported recording is judged again without duplicating flags.
    again = run()
    assert again["recordings"] == 1 and again["already_flagged"] >= 1
    assert len(flags(new["rec-1-1"])) == 1


def test_purge_removes_noise_state(client):
    from throughline.cli import purge

    recording(client)
    put(client, [turn(0, "[applause]", 0, 3, thread="rec-gappy", channel=GAPPY)])
    run({"channels": [WAVE, GAPPY]})
    assert owner("SELECT count(*) AS n FROM tl.transcript_noise_state")[0]["n"] == 2
    assert owner("SELECT 1 FROM tl.purge_hooks WHERE name='transcript_noise_state'")
    purge(WAVE, confirm=True)
    left = owner(
        "SELECT t.external_id FROM tl.transcript_noise_state s JOIN tl.threads t ON t.id=s.thread_id"
    )
    assert [r["external_id"] for r in left] == ["rec-gappy"]
    assert not owner(
        "SELECT 1 FROM tl.annotations a JOIN tl.channels c ON c.id=a.target_channel_id WHERE c.name=%s",
        (WAVE,),
    )


def test_arc_6_ground_truth_unchanged(client):
    m = recording(client)
    before = checksum()
    raw = owner("SELECT message_id,payload FROM tl.raw_payloads ORDER BY message_id")
    person = owner(
        "INSERT INTO tl.people(handle) VALUES ('synthetic-lee') RETURNING id"
    )[0]["id"]
    run()
    with connect("app") as c:
        transcripts.assign_speaker(c, owner_token(), thread_id(), "Speaker 1", person)
        transcripts.ignore_speaker(c, owner_token(), thread_id(), "Speaker 2", "tv")
    assert checksum() == before
    assert (
        owner("SELECT message_id,payload FROM tl.raw_payloads ORDER BY message_id")
        == raw
    )
    # The app role cannot rewrite a transcript either.
    with connect("app") as c, pytest.raises(psycopg.Error):
        c.execute(
            "UPDATE tl.messages SET content='rewritten' WHERE id=%s", (m["rec-1-1"],)
        )


# --- recording-level grouping -----------------------------------------------


def test_recording_never_split_by_gaps(client, monkeypatch):
    channels(client)
    # Turns two hours apart: far beyond the default 1,800 s conversation gap.
    spaced = [
        turn(i, f"orchard planning note number {i}", i * 7200, i * 7200 + 30)
        for i in range(3)
    ]
    put(client, spaced)
    put(
        client,
        [
            turn(
                i,
                f"orchard planning note number {i}",
                i * 7200,
                i * 7200 + 30,
                thread="rec-gappy",
                channel=GAPPY,
            )
            for i in range(3)
        ],
    )
    ids = [current(f"rec-1-{i}") for i in range(3)]
    gappy = [current(f"rec-gappy-{i}", GAPPY) for i in range(3)]
    with connect("reader") as c:
        episodes = {
            r["message_id"]: r["episode_id"]
            for r in c.execute(
                "SELECT * FROM api.episodes_of(%s::bigint[])", (ids + gappy,)
            )
        }
    assert len({episodes[i] for i in ids}) == 1
    assert len({episodes[i] for i in gappy}) == 3  # the control channel splits
    grouped = recall(
        OWNER,
        RecallRequest(
            query="orchard planning note",
            k=50,
            group_by="episode",
            scope=dict(channels=[WAVE]),
        ),
    )
    assert grouped.manifest["unit"] == "episode"
    assert grouped.manifest["incidents"] == 1 and grouped.manifest["records"] == 3
    # Conversations: one per recording, however long the gaps.
    materialize(wait=True)
    rows = owner(
        "SELECT t.external_id,count(*) AS n,min(c.first_thread_no) AS first,max(c.last_thread_no) AS last FROM tl.conversations c JOIN tl.threads t ON t.id=c.thread_id GROUP BY t.external_id"
    )
    by_thread = {r["external_id"]: r for r in rows}
    assert by_thread["rec-1"]["n"] == 1
    assert (by_thread["rec-1"]["first"], by_thread["rec-1"]["last"]) == (1, 3)
    assert by_thread["rec-gappy"]["n"] == 3
    # Spans stay inside the one conversation of the recording.
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "64")
    deadline = time.monotonic() + 60
    while embeddings.run_once() and time.monotonic() < deadline:
        pass
    vector = recall(
        OWNER,
        RecallRequest(
            query="orchard planning note",
            legs=["vector"],
            k=20,
            scope=dict(channels=[WAVE]),
        ),
    )
    conversations = {
        (r.get("conversation") or {}).get("id")
        for r in vector.records
        if r["record_id"].startswith("m:")
    }
    assert vector.records and len(conversations) == 1


# --- optional-feature: write contract -------------------------------------------------


def test_transcript_writes_idempotent_with_receipts(client):
    try:
        from throughline import writes
    except ImportError:
        pytest.skip("optional-feature: throughline.writes (write contract) missing")
    m = recording(client)
    person = owner(
        "INSERT INTO tl.people(handle) VALUES ('synthetic-ada') RETURNING id"
    )[0]["id"]
    body = {"thread_id": thread_id(), "label": "Speaker 1", "person_id": person}
    key = {writes.HEADER: "synthetic-key-0001"}
    first = ok(client.post("/v1/transcripts/speakers/assign", json=body, headers=key))
    assert len(first["receipts"]) == 4
    assert all(
        r["kind"] == "annotation" and r["replayed"] is False for r in first["receipts"]
    )
    assert all(r["stored_hash"] for r in first["receipts"])
    count = owner("SELECT count(*) AS n FROM tl.annotations")[0]["n"]
    second = ok(client.post("/v1/transcripts/speakers/assign", json=body, headers=key))
    assert all(r["replayed"] is True for r in second["receipts"])
    assert [r["id"] for r in second["receipts"]] == [r["id"] for r in first["receipts"]]
    assert owner("SELECT count(*) AS n FROM tl.annotations")[0]["n"] == count
    conflict = client.post(
        "/v1/transcripts/speakers/assign",
        json=dict(body, label="Speaker 2"),
        headers=key,
    )
    assert conflict.status_code == 409
    ignored = ok(
        client.post(
            "/v1/transcripts/speakers/ignore",
            json={"thread_id": thread_id(), "label": "Speaker 2", "reason": "tv"},
            headers={writes.HEADER: "synthetic-key-0002"},
        )
    )
    assert ignored["receipts"][0]["kind"] == "annotation"
    assert m["rec-1-4"] not in recalled("volume")


def test_split_of_old_merge_restores_speaker_label_without_person(client):
    """A merge snapshot taken before 0049 may hold a speaker_label identifier
    attached to the merged person. Splitting it must not trip the enrolment
    CHECK: the label comes back unattached, everything else is restored."""
    p = client.post("/v1/identity/people", json={"handle": "synthetic-p"}).json()["id"]
    q = client.post("/v1/identity/people", json={"handle": "synthetic-q"}).json()["id"]
    with connect() as c:
        email = c.execute(
            "INSERT INTO tl.identifiers(system,kind,value,value_norm,person_id) VALUES ('synthetic','email','p@invalid.example','p@invalid.example',%s) RETURNING id",
            (p,),
        ).fetchone()["id"]
        label = c.execute(
            "INSERT INTO tl.identifiers(system,kind,value,value_norm) VALUES ('wave','speaker_label','Speaker 7','Speaker 7') RETURNING id"
        ).fetchone()["id"]
    merge = client.post("/v1/identity/merge", json={"source": p, "target": q})
    assert merge.status_code == 200, merge.text
    event_id = merge.json()["id"]
    with connect() as c:
        # Plant the pre-0049 shape: the label attached to p in the snapshot.
        c.execute(
            "UPDATE tl.identity_events SET detail=jsonb_set(detail,'{identifiers}',detail->'identifiers' || jsonb_build_array(to_jsonb(i) || jsonb_build_object('person_id',%s::bigint))) FROM tl.identifiers i WHERE tl.identity_events.id=%s AND i.id=%s",
            (p, event_id, label),
        )
    r = client.post("/v1/identity/split", json={"event_id": event_id})
    assert r.status_code == 200, r.text
    with connect() as c:
        rows = {
            row["id"]: row["person_id"]
            for row in c.execute(
                "SELECT id,person_id FROM tl.identifiers WHERE id=ANY(%s)",
                ([email, label],),
            )
        }
    assert rows == {email: p, label: None}


def test_annotation_status_change_with_empty_token_setting(client, channel):
    """A job on a pooled connection that served a token request earlier sees
    throughline.token_id as ''. Changing an annotation's status must not
    fail on the owner guard's token cast (repeated, so the trigger's cached
    generic plan is exercised too); an owner annotation's rule still holds."""
    with connect() as c:
        channel_id = c.execute(
            "SELECT id FROM tl.channels ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        c.execute("SELECT set_config('throughline.token_id','',false)")
        for n in range(8):
            for method, origin in (("rule", "job"), ("owner", "owner")):
                ident = c.execute(
                    "INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin,job,job_version) VALUES (%s,%s,'ignore',jsonb_build_object('ignored',true),%s,%s,%s,%s) RETURNING id",
                    (
                        channel_id,
                        f"synthetic-{n}",
                        method,
                        origin,
                        None if method == "owner" else "synthetic-job",
                        None if method == "owner" else "1",
                    ),
                ).fetchone()["id"]
                c.execute(
                    "UPDATE tl.annotations SET status='retracted' WHERE id=%s", (ident,)
                )
        agent = create_token("synthetic-agent-guard", "agent")["id"]
        c.execute("SELECT set_config('throughline.token_id',%s,true)", (str(agent),))
        owned = c.execute(
            "INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin) VALUES (%s,'synthetic-x','ignore',jsonb_build_object('ignored',true),'owner','owner') RETURNING id",
            (channel_id,),
        ).fetchone()["id"]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "UPDATE tl.annotations SET status='retracted' WHERE id=%s", (owned,)
            )
        c.rollback()
