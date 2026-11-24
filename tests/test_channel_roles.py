"""Channel roles: core features find channels by declared role, never by name.

Arbitrarily named channels with declared roles feed orient and find; two
restricted askers with different grants each see only the sections their
channels feed; an empty instance answers with empty sections; an install
without any declaration keeps the legacy naming conventions. Synthetic data
only.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from throughline import access, channel_roles
from throughline.db import connect, create_token

CAL = "caldav.events"
CHAT = "imessage.messages"
TRANSCRIPT = "audio_transcripts.segments"
VITALS = "watch.vitals"
VOICE = "dictaphone.summaries"
PLAIN = "misc.notes"
BASE = datetime(2030, 3, 10, 12, 0, tzinfo=timezone.utc)
AS_OF = (BASE + timedelta(hours=1)).isoformat()


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def register(client, name, plugin, **declared):
    ok(client.post("/v1/channels", json=dict(name=name, plugin=plugin, **declared)))


def item(channel, external_id, at, content, thread="t", **kwargs):
    return dict(
        channel=channel,
        external_id=external_id,
        thread={"external_id": thread},
        happened_at=at.isoformat(),
        content=content,
        author=dict(system="synthetic", kind="account", value="alex-rivera"),
        payload={"id": external_id},
        **kwargs,
    )


def deliver(client, plugin, items):
    result = ok(client.post("/v1/ingest/messages", json=dict(plugin=plugin, messages=items)))
    assert not result["rejected"], result


@pytest.fixture()
def world(client):
    register(client, CAL, "caldav", role="calendar")
    register(client, CHAT, "imessage", role="chat")
    register(client, TRANSCRIPT, "recorder", role="transcript", context_window_min=6)
    register(client, VITALS, "watch", role="telemetry", orient_priority=5)
    register(client, VOICE, "dictaphone", role="voice_summary")
    register(client, PLAIN, "misc")
    deliver(client, "caldav", [
        item(CAL, "ev-future", BASE + timedelta(days=2), "Dentist appointment downtown"),
        item(CAL, "ev-past", BASE - timedelta(days=2), "Past planning session"),
    ])
    deliver(client, "imessage", [
        item(CHAT, "im-1", BASE - timedelta(hours=3), "Are we still on for the kayak trip?", thread="kayak"),
    ])
    deliver(client, "recorder", [
        item(TRANSCRIPT, f"seg-{i}", BASE - timedelta(days=1, minutes=-i),
             "the lighthouse budget was approved" if i == 10 else f"filler segment number {i}",
             thread="meeting")
        for i in range(21)
    ])
    deliver(client, "watch", [
        item(VITALS, "v-1", BASE - timedelta(hours=8), "Resting heart rate 52"),
    ])
    deliver(client, "dictaphone", [
        item(VOICE, "memo-1", BASE - timedelta(hours=5), "Summary: call the plumber"),
    ])
    deliver(client, "misc", [item(PLAIN, "n-1", BASE - timedelta(hours=1), "A plain note")])
    access.register_asker("asker-planner")
    access.grant("asker-planner", channel=CAL)
    access.grant("asker-planner", channel=CHAT)
    access.register_asker("asker-scribe")
    access.grant("asker-scribe", channel=TRANSCRIPT)
    access.grant("asker-scribe", channel=VITALS)
    callers = {}
    for asker in ("asker-planner", "asker-scribe"):
        token = create_token(asker, kind="agent", askers=[asker])["token"]
        caller = TestClient(client.app)
        caller.__enter__()
        caller.headers["Authorization"] = "Bearer " + token
        callers[asker] = caller
    yield dict(owner=client, **callers)
    for caller in callers.values():
        caller.__exit__(None, None, None)


def orient(caller):
    return ok(caller.post("/v1/orient", json={"as_of": AS_OF, "days": 7}))


def test_roles_are_stored_and_kept_when_undeclared(world):
    owner = world["owner"]
    # Re-registering without a declaration keeps what is stored.
    register(owner, TRANSCRIPT, "recorder")
    with connect() as c:
        row = c.execute(
            "SELECT role, orient_priority, context_window_min FROM tl.channels WHERE name=%s",
            (TRANSCRIPT,),
        ).fetchone()
    assert dict(row) == dict(role="transcript", orient_priority=None, context_window_min=6)
    bad = owner.post("/v1/channels", json=dict(name="x.y", plugin="x", role="Not A Role"))
    assert bad.status_code == 422


def test_orient_owner_sections_follow_roles(world):
    snap = orient(world["owner"])
    assert [e["external_id"] for e in snap["upcoming_calendar"]] == ["ev-future"]
    assert snap["upcoming_calendar"][0]["channel"] == CAL
    assert [t["channel"] for t in snap["recent_threads"]] == [CHAT]
    assert list(snap["recent_telemetry"]) == [VITALS]
    assert [v["external_id"] for v in snap["recent_voice"]] == ["memo-1"]
    # A channel without a role feeds no section, but is listed.
    assert PLAIN in snap["channels"]
    assert PLAIN not in json.dumps(
        {k: snap[k] for k in ("recent_telemetry", "upcoming_calendar", "recent_voice", "recent_threads")}
    )


def test_orient_two_grants_see_only_their_sections(world):
    planner = orient(world["asker-planner"])
    assert set(planner["channels"]) == {CAL, CHAT}
    assert [e["external_id"] for e in planner["upcoming_calendar"]] == ["ev-future"]
    assert [t["channel"] for t in planner["recent_threads"]] == [CHAT]
    assert planner["recent_telemetry"] == {} and planner["recent_voice"] == []
    text = json.dumps(planner)
    for hidden in (TRANSCRIPT, VITALS, VOICE, PLAIN):
        assert hidden not in text

    scribe = orient(world["asker-scribe"])
    assert set(scribe["channels"]) == {TRANSCRIPT, VITALS}
    assert list(scribe["recent_telemetry"]) == [VITALS]
    assert scribe["upcoming_calendar"] == [] and scribe["recent_threads"] == []
    text = json.dumps(scribe)
    for hidden in (CAL, CHAT, VOICE, PLAIN):
        assert hidden not in text


def _episode_lines(found, channel):
    (episode,) = [e for e in found["episodes"] if e["channel"] == channel]
    return len(episode["messages"])


def test_find_context_window_min_and_explicit_window(world):
    scribe = world["asker-scribe"]
    query = {"query": "lighthouse budget", "strategy": "keyword"}
    # Default window (4) is raised to the channel's context_window_min (6).
    found = ok(scribe.post("/v1/find", json=query))
    assert _episode_lines(found, TRANSCRIPT) == 13
    # An explicit window is honoured as asked.
    found = ok(scribe.post("/v1/find", json=dict(query, window=2)))
    assert _episode_lines(found, TRANSCRIPT) == 5
    # The planner cannot see the transcript at all.
    found = ok(world["asker-planner"].post("/v1/find", json=query))
    assert TRANSCRIPT not in json.dumps(found)


def test_orient_on_empty_instance(client):
    snap = orient(client)
    assert snap["channels"] == {}
    assert snap["recent_telemetry"] == {}
    assert snap["upcoming_calendar"] == []
    assert snap["recent_voice"] == []
    assert snap["recent_threads"] == []


def test_resolve_declared_wins_over_legacy_names():
    legacy = channel_roles.resolve([dict(name="chat.sms"), dict(name="misc.notes")])
    assert legacy["chat.sms"].role == "chat" and not legacy["chat.sms"].declared
    assert legacy["misc.notes"].role is None
    # One declaration anywhere among the visible channels switches the
    # legacy conventions off for all of them.
    declared = channel_roles.resolve(
        [dict(name="chat.sms"), dict(name="imessage.messages", role="chat")]
    )
    assert declared["chat.sms"].role is None
    assert declared["imessage.messages"].role == "chat" and declared["imessage.messages"].declared


def test_legacy_conventions_without_declarations(client):
    """An install whose plugins declare no roles behaves as before roles."""
    for name in ("chat.sms", "calendar.events", "health.weight", "morning.news"):
        register(client, name, "legacy")
    deliver(client, "legacy", [
        item("chat.sms", "s-1", BASE - timedelta(hours=2), "See you at the pier", thread="pier"),
        item("calendar.events", "c-1", BASE + timedelta(days=1), "Team lunch"),
        item("health.weight", "w-1", BASE - timedelta(days=1), "Weight 70 kg"),
        item("morning.news", "n-1", BASE - timedelta(hours=4), "Headlines digest"),
    ])
    snap = orient(client)
    assert [t["channel"] for t in snap["recent_threads"]] == ["chat.sms"]
    assert [e["external_id"] for e in snap["upcoming_calendar"]] == ["c-1"]
    assert list(snap["recent_telemetry"]) == ["health.weight"]
