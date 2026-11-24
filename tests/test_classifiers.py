"""Classifiers (migration 0068, throughline.classifiers): the attribute
registry, group annotations and their fan-out onto messages, message
classifiers (Jev/LLM per message) and sandboxed python classifiers over whole
threads, the approval flow and the HTTP surface. Model calls go through
bridge.set_override; nothing leaves the machine."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import ingest

from throughline import classifiers
from throughline.classifiers import bridge, groups, sandbox
from throughline.db import connect, create_token

WAVE = "synthetic.wave"
CHAT = "synthetic.chat"
T0 = datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)
MEDIA = (Path(classifiers.__file__).parent / "examples" / "background_media.py").read_text()


@pytest.fixture(autouse=True)
def no_models():
    bridge.set_override(None)
    yield
    bridge.set_override(None)


def ok(r):
    assert r.status_code in (200, 202), r.text
    return r.json()


def sql(statement, params=()):
    with connect() as c:
        cur = c.execute(statement, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else None


def owner_token():
    return dict(sql("SELECT * FROM tl.tokens WHERE kind='owner' ORDER BY id LIMIT 1")[0])


def agent_headers():
    token = create_token("synthetic agent", "agent")["token"]
    return {"Authorization": "Bearer " + token}


def wave_channel(client):
    ok(client.post("/v1/channels", json=dict(
        name=WAVE, plugin="synthetic", conversation_whole_thread=True,
        fields=[dict(name="start_s", type="number"), dict(name="end_s", type="number"),
                dict(name="speaker_label", type="text")])))
    sql("INSERT INTO tl.people(handle,display_name,is_owner) VALUES ('me','Me',true) ON CONFLICT DO NOTHING")
    pid = sql("SELECT id FROM tl.people WHERE handle='me'")[0]["id"]
    sql("""INSERT INTO tl.identifiers(person_id,system,kind,value,value_norm,status)
        VALUES (%s,'wave','voice_profile','me-voice','me-voice','confirmed') ON CONFLICT DO NOTHING""", (pid,))
    return pid


def turn(i, text, label, thread="rec-1", kind="speaker_label"):
    value = label if kind == "voice_profile" else f"{thread}:{label}"
    return dict(
        channel=WAVE, external_id=f"{thread}-{i}",
        thread={"external_id": thread, "title": f"Recording {thread}"},
        happened_at=(T0 + timedelta(seconds=10 * i)).isoformat(),
        content=text, author=dict(system="wave", kind=kind, value=value),
        fields=dict(start_s=10 * i, end_s=10 * i + 8, speaker_label=label),
        payload={"t": f"{thread}-{i}"},
    )


def recording(client, thread="rec-1"):
    """Me talking with Dad while a TV plays (Speaker 3)."""
    r = ingest(client, [
        turn(0, "should we turn here or keep going", "me-voice", thread, "voice_profile"),
        turn(1, "keep going, it's the next street", "Dad", thread),
        turn(2, "previously on the great kitchen showdown, the judges were stunned", "Speaker 3", thread),
        turn(3, "okay next one then", "me-voice", thread, "voice_profile"),
        turn(4, "tonight, one baker will go home", "Speaker 3", thread),
    ])
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    return sql("SELECT id FROM tl.threads WHERE external_id=%s", (thread,))[0]["id"]


def flagged(thread_id, attribute="media_noise"):
    rows = sql("""SELECT m.external_id,a.value,a.method,a.group_annotation_id FROM tl.messages m
        JOIN tl.annotations a ON a.target_channel_id=m.channel_id AND a.target_external_id=m.external_id
        WHERE m.thread_id=%s AND a.attribute=%s AND a.status='active' ORDER BY m.external_id""",
               (thread_id, attribute))
    return {r["external_id"]: r for r in rows}


def jev_media(level, speaker3=0.95, dad=0.05):
    """A fake Jev for the media example: conversation level, kind, speakers."""
    levels = ["none", "partial", "full"]

    def answer(q, text):
        if q["type"] == "score" and q["criteria"] == levels:
            return {"type": "score", "score": levels.index(level),
                    "probabilities": {str(i): (0.9 if c == level else 0.05) for i, c in enumerate(levels)}}
        if q["type"] == "score":
            return {"type": "score", "score": 0,
                    "probabilities": {str(i): (1.0 if i == 0 else 0.0) for i in range(len(q["criteria"]))}}
        who = text.split("\n", 1)[0]
        p = speaker3 if "Speaker 3" in who else dad
        return {"type": "noul", "noul": p}

    def fake(op, args):
        assert op == "jev"
        text = args["about"] if isinstance(args["about"], str) else str(args["about"])
        return {k: answer(q, text) for k, q in args["questions"].items()}

    return fake


def define_media(client, **extra):
    body = {"name": "is_it_media", "kind": "python", "attribute": "media_noise", "channels": [WAVE],
            "code": MEDIA, "spec": {"settle_minutes": 0}, **extra}
    return ok(client.post("/v1/classifiers", json=body))


# --- registry ---------------------------------------------------------------------


def test_registry_seeds_core_and_checks_registered_values(client, channel):
    names = {a["name"] for a in ok(client.get("/v1/annotation-attributes"))["attributes"]}
    assert {"ignore", "media_noise", "speaker", "effective_time"} <= names
    ok(client.post("/v1/annotation-attributes", json={
        "name": "automated", "value_schema": {"is_automated": {"type": "boolean", "required": True},
                                              "score": {"type": "number", "min": 0, "max": 1}}}))
    ok(ingest(client, [dict(channel=CHAT, external_id="m1", thread={"external_id": "t"},
                            happened_at=T0.isoformat(), content="your receipt", author=dict(system="x", kind="account", value="shop"),
                            payload={"a": 1})]))
    mid = sql("SELECT id FROM tl.messages WHERE external_id='m1'")[0]["id"]
    good = client.post("/v1/annotations", json={"message_id": mid, "attribute": "automated", "value": {"is_automated": True}})
    assert good.status_code == 200, good.text
    bad = client.post("/v1/annotations", json={"message_id": mid, "attribute": "automated", "value": {"is_automatic": True}})
    assert bad.status_code == 400 and "no field" in bad.text
    unknown = client.post("/v1/annotations", json={"message_id": mid, "attribute": "nonsense", "value": {}})
    assert unknown.status_code == 400
    core = client.post("/v1/annotation-attributes", json={"name": "media_noise", "value_schema": {"x": "boolean"}})
    assert core.status_code == 422


# --- group annotations --------------------------------------------------------------


def test_owner_group_annotations_fan_out_and_win(client):
    wave_channel(client)
    tid = recording(client)
    token = owner_token()
    participants = {p["label"]: p["key"] for p in classifiers.threads.load(connect_app(), tid)[0]["participants"]}
    assert participants["Me"].startswith("p:")
    with connect("app") as conn:
        groups.owner_set(conn, token, thread_id=tid, attribute="media_noise",
                         participant_key=participants["Speaker 3"], value={"is_noise": True})
    marks = flagged(tid)
    assert set(marks) == {"rec-1-2", "rec-1-4"}
    assert all(m["method"] == "owner" and m["group_annotation_id"] for m in marks.values())
    # a whole-thread owner verdict covers everything; the participant one stays more specific
    with connect("app") as conn:
        groups.owner_set(conn, token, thread_id=tid, attribute="media_noise", value={"is_noise": False})
    marks = flagged(tid)
    assert len(marks) == 5
    assert marks["rec-1-2"]["value"]["is_noise"] is True and marks["rec-1-0"]["value"]["is_noise"] is False
    gid = [g for g in groups.active(connect_app(), tid) if g["target_kind"] == "thread"][0]["id"]
    with connect("app") as conn:
        groups.retract(conn, token, gid)
    assert set(flagged(tid)) == {"rec-1-2", "rec-1-4"}


def connect_app():
    """A short-lived app connection for reads in assertions."""
    import psycopg

    from throughline.db import database_url, role_password
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg.rows import dict_row

    s = conninfo_to_dict(database_url())
    s.update(user="throughline_app", password=role_password("app"))
    return psycopg.connect(make_conninfo(**s), row_factory=dict_row, autocommit=True)


# --- python classifiers -------------------------------------------------------------


def test_media_classifier_partial_marks_only_the_tv_speaker(client):
    wave_channel(client)
    tid = recording(client)
    out = define_media(client)
    assert out["status"] == "active"
    bridge.set_override(jev_media("partial"))
    res = classifiers.run("is_it_media")
    assert res["status"] == "done" and res["threads"] == 1, res
    marks = flagged(tid)
    assert set(marks) == {"rec-1-2", "rec-1-4"}
    value = marks["rec-1-2"]["value"]
    assert value["is_noise"] is True and value["basis"] == "participant" and value["classifier"] == "is_it_media"
    # flagged turns leave default recall (core media_noise)
    eff = {r["message_id"]: r for r in sql("SELECT * FROM api.effective((SELECT array_agg(id) FROM tl.messages WHERE thread_id=%s))", (tid,))}
    assert sum(1 for r in eff.values() if r["media_noise"]) == 2
    result = sql("SELECT result FROM tl.classifier_threads WHERE thread_id=%s", (tid,))[0]["result"]
    assert result["verdict"] == "partial"
    # nothing new: not judged again
    assert classifiers.run("is_it_media")["threads"] == 0


def test_media_classifier_full_marks_every_turn_and_none_marks_nothing(client):
    wave_channel(client)
    full = recording(client, "rec-full")
    none = recording(client, "rec-none")
    define_media(client)
    fake, calls = jev_media("full"), []
    bridge.set_override(lambda op, args: calls.append(sorted(args["questions"])) or fake(op, args))
    classifiers.run("is_it_media", thread_ids=[full])
    assert calls == [["kind", "level"]]  # level and kind in one call about the recording
    assert len(flagged(full)) == 5
    assert all(m["value"]["basis"] == "thread" for m in flagged(full).values())
    bridge.set_override(jev_media("none"))
    classifiers.run("is_it_media", thread_ids=[none])
    assert flagged(none) == {}
    assert sql("SELECT result->>'verdict' AS v FROM tl.classifier_threads WHERE thread_id=%s", (none,))[0]["v"] == "none"


def test_rerun_after_new_turns_supersedes_and_covers_them(client):
    wave_channel(client)
    tid = recording(client)
    define_media(client)
    bridge.set_override(jev_media("partial"))
    classifiers.run("is_it_media")
    ok(ingest(client, [turn(5, "and after the break, the final challenge", "Speaker 3")]))
    res = classifiers.run("is_it_media")
    assert res["threads"] == 1
    assert set(flagged(tid)) == {"rec-1-2", "rec-1-4", "rec-1-5"}
    # the verdict flips to none: the job's marks are retracted
    bridge.set_override(jev_media("none"))
    ok(ingest(client, [turn(6, "anyway", "me-voice", kind="voice_profile")]))
    classifiers.run("is_it_media")
    assert flagged(tid) == {}


def test_owner_turn_correction_survives_classifier(client):
    wave_channel(client)
    tid = recording(client)
    mid = sql("SELECT id FROM tl.messages WHERE external_id='rec-1-4'")[0]["id"]
    ok(client.post("/v1/annotations", json={"message_id": mid, "attribute": "media_noise", "value": {"is_noise": False}}))
    define_media(client)
    bridge.set_override(jev_media("partial"))
    classifiers.run("is_it_media")
    marks = flagged(tid)
    assert marks["rec-1-4"]["method"] == "owner" and marks["rec-1-4"]["value"]["is_noise"] is False
    assert marks["rec-1-2"]["method"] == "model"


def test_budget_stop_leaves_thread_unjudged(client):
    wave_channel(client)
    recording(client)
    define_media(client, spec={"settle_minutes": 0, "max_cost_per_run": 0.0})

    def pricey(op, args):
        raise bridge.Stopped("cost limit reached")

    bridge.set_override(pricey)
    res = classifiers.run("is_it_media")
    assert res["status"] == "partial"
    assert sql("SELECT count(*) AS n FROM tl.classifier_threads")[0]["n"] == 0


def test_code_errors_are_reported_not_written(client):
    wave_channel(client)
    tid = recording(client)
    code = "from throughline_classify import classifier\n@classifier\ndef f(thread, ctx):\n    return [thread.participants[0].mark(is_noise='yes')]\n"
    ok(client.post("/v1/classifiers", json={"name": "broken", "attribute": "media_noise", "channels": [WAVE],
                                            "code": code, "spec": {"settle_minutes": 0}}))
    res = classifiers.run("broken")
    assert res["errors"] and "is_noise" in res["errors"][0]["error"] or "media_noise" in res["errors"][0]["error"]
    assert flagged(tid) == {}


def test_sandbox_refuses_imports_and_has_no_files():
    bad = sandbox.Session if False else None
    assert classifiers.check.check_code("import os\ndef classify(t, c):\n    return []")["ok"] is False
    assert classifiers.check.check_code("def classify(t, c):\n    return open('/etc/passwd').read()")["ok"] is False
    assert classifiers.check.check_code("def classify(t, c):\n    return ().__class__")["ok"] is False
    if sandbox.mode() == "bwrap":
        code = "def classify(t, c):\n    return [t.note(env=len(str(1)))]\n"
        with sandbox.Session(code) as s:
            status, marks = s.run({"id": 1, "participants": [], "messages": []}, lambda op, a: None)
        assert status == "done" and marks[0]["detail"] == {"env": 1}
    assert bad is None


# --- message classifiers -------------------------------------------------------------


def automated_definition(**spec):
    return {
        "name": "automated_mail", "kind": "message", "channels": [CHAT],
        "attribute": {"name": "automated", "value_schema": {
            "is_automated": {"type": "boolean", "required": True}, "score": {"type": "number", "min": 0, "max": 1}},
            "recall_effect": "exclude"},
        "spec": {"worker": "jev", "question": "Was this sent by an automated system?", "threshold": 0.7, **spec},
    }


def chat(i, text):
    return dict(channel=CHAT, external_id=f"c{i}", thread={"external_id": "inbox"},
                happened_at=(T0 + timedelta(minutes=i)).isoformat(), content=text,
                author=dict(system="x", kind="account", value=f"a{i}"), payload={"i": i})


def test_message_classifier_flags_new_messages_and_hides_excluded(client, channel):
    out = ok(client.post("/v1/classifiers", json=automated_definition()))
    assert out["classifier"]["kind"] == "message" and out["status"] == "active"
    ok(ingest(client, [chat(1, "Your order has shipped. Do not reply."), chat(2, "lunch tomorrow?")]))
    bridge.set_override(lambda op, args: {"q": {"type": "noul", "noul": 0.9 if "shipped" in args["about"]["message"] else 0.1}})
    res = classifiers.run("automated_mail")
    assert res["judged"] == 2 and res["annotated"] == 1, res
    rows = sql("SELECT a.attribute,a.value,m.external_id FROM tl.annotations a JOIN tl.messages m ON m.external_id=a.target_external_id WHERE a.status='active' ORDER BY a.attribute")
    assert {(r["attribute"], r["external_id"]) for r in rows} == {("automated", "c1"), ("ignore", "c1")}
    assert [r for r in rows if r["attribute"] == "automated"][0]["value"]["score"] == 0.9
    # the cursor moved: nothing to judge
    assert classifiers.run("automated_mail")["judged"] == 0


def test_message_classifier_stops_on_provider_outage_and_resumes(client, channel):
    ok(client.post("/v1/classifiers", json=automated_definition()))
    ok(ingest(client, [chat(1, "a"), chat(2, "b")]))

    def down(op, args):
        raise bridge.Stopped("Jev unavailable")

    bridge.set_override(down)
    assert classifiers.run("automated_mail")["status"] == "partial"
    bridge.set_override(lambda op, args: {"q": {"type": "noul", "noul": 0.1}})
    assert classifiers.run("automated_mail")["judged"] == 2


def test_message_classifier_choice_and_backfill(client, channel):
    ok(ingest(client, [chat(1, "standup at 10"), chat(2, "mom called")]))
    body = {
        "name": "topic", "kind": "message", "channels": [CHAT],
        "attribute": {"name": "topic", "value_schema": {"topic": {"type": "enum", "values": ["work", "family"], "required": True}}},
        "spec": {"worker": "jev", "answer": "choice", "question": "What is this message about?",
                 "options": ["work", "family", "other"], "values": {"work": {"topic": "work"}, "family": {"topic": "family"}}},
        "backfill": "all",
    }
    ok(client.post("/v1/classifiers", json=body))

    def pick(op, args):
        idx = 0 if "standup" in args["about"]["message"] else 1
        return {"q": {"type": "score", "score": idx, "probabilities": {str(i): float(i == idx) for i in range(3)}}}

    bridge.set_override(pick)
    res = classifiers.run("topic")
    assert res["backfill"] == 2
    vals = {r["external_id"]: r["value"]["topic"] for r in sql(
        "SELECT m.external_id,a.value FROM tl.annotations a JOIN tl.messages m ON m.external_id=a.target_external_id WHERE a.attribute='topic'")}
    assert vals == {"c1": "work", "c2": "family"}
    assert sql("SELECT backfill_until_id FROM tl.classifiers WHERE name='topic'")[0]["backfill_until_id"] is None


# --- approval, check, try, HTTP ----------------------------------------------------------


def test_agent_definitions_are_proposed_until_owner_approves(client, channel):
    agent = agent_headers()
    out = ok(client.post("/v1/classifiers", json=automated_definition(), headers=agent))
    assert out["status"] == "proposed"
    with pytest.raises(classifiers.ClassifierError):
        classifiers.run("automated_mail")
    refused = client.post("/v1/classifiers/automated_mail/status", json={"status": "active"}, headers=agent)
    assert refused.status_code == 403
    ok(client.post("/v1/classifiers/automated_mail/status", json={"status": "active"}))
    assert ok(client.get("/v1/classifiers/automated_mail"))["status"] == "active"
    # an agent's change sends it back for approval
    out = ok(client.post("/v1/classifiers", json=automated_definition(threshold=0.9), headers=agent))
    assert out["status"] == "proposed" and out["classifier"]["version"] == 2


def test_check_names_problems_and_try_writes_nothing(client):
    wave_channel(client)
    tid = recording(client)
    bad = ok(client.post("/v1/classifiers/check", json={"definition": {
        "kind": "python", "attribute": "media_noise", "code": "import socket\ndef classify(t):\n    return []"}}))
    assert not bad["ok"] and any("socket" in e for e in bad["errors"]) and any("two arguments" in e for e in bad["errors"])
    good = ok(client.post("/v1/classifiers/check", json={"definition": {"kind": "python", "attribute": "media_noise",
                                                                           "code": MEDIA, "channels": [WAVE]}}))
    assert good["ok"], good
    bridge.set_override(jev_media("partial"))
    out = ok(client.post("/v1/classifiers/try", json={"definition": {
        "kind": "python", "attribute": "media_noise", "code": MEDIA, "channels": [WAVE]}, "sample": {"thread_ids": [tid]}}))
    item = out["items"][0]
    assert item["status"] == "done" and item["verdict"] == "partial"
    assert item["would_cover"] == {"messages": 2, "of": 5}
    assert flagged(tid) == {}
    assert out["projection"]["threads_to_judge"] == 1


def test_thread_classification_view_and_owner_override_route(client):
    wave_channel(client)
    tid = recording(client)
    define_media(client)
    bridge.set_override(jev_media("partial"))
    classifiers.run("is_it_media")
    view = ok(client.get(f"/v1/threads/{tid}/classification", params={"classifier": "is_it_media"}))
    assert view["flagged_total"] == 2 and view["verdicts"][0]["result"]["verdict"] == "partial"
    speaker3 = [p for p in view["participants"] if p["label"] == "Speaker 3"][0]
    assert speaker3["flagged"] == 2
    ok(client.post("/v1/group-annotations", json={"thread_id": tid, "attribute": "media_noise",
                                                  "participant_key": speaker3["key"], "value": {"is_noise": False}}))
    marks = flagged(tid)
    assert all(m["value"]["is_noise"] is False and m["method"] == "owner" for m in marks.values())
    # the owner's verdict holds when the classifier runs again
    ok(ingest(client, [turn(9, "more tv", "Speaker 3")]))
    classifiers.run("is_it_media")
    assert flagged(tid)["rec-1-9"]["value"]["is_noise"] is False
    lst = ok(client.get("/v1/classifiers"))
    assert lst["classifiers"][0]["stats"]["threads_judged"] == 1
    assert ok(client.get("/v1/classifiers/guide"))["examples"]["python"][0]["code"].startswith('"""Is it media')


def test_retract_marks_takes_back_everything(client):
    wave_channel(client)
    tid = recording(client)
    define_media(client)
    bridge.set_override(jev_media("full"))
    classifiers.run("is_it_media")
    assert len(flagged(tid)) == 5
    out = ok(client.post("/v1/classifiers/is_it_media/retract-marks", json={}))
    assert out["annotations"] >= 0
    assert flagged(tid) == {}


def test_draft_repairs_a_failing_definition(client, monkeypatch):
    wave_channel(client)
    from throughline.classifiers import draft as draft_mod
    from throughline.lenses import workers

    replies = [
        {"definition": {"name": "media", "kind": "python", "attribute": "media_noise", "channels": [WAVE],
                        "code": "import os\ndef classify(thread, ctx):\n    return []"}, "notes": "first"},
        {"definition": {"name": "media", "kind": "python", "attribute": "media_noise", "channels": [WAVE],
                        "code": MEDIA}, "notes": "fixed"},
    ]
    seen = []

    def fake_chat(**kw):
        seen.append(kw["user_payload"])
        return replies[len(seen) - 1], 100, 0.0

    monkeypatch.setattr(workers, "_chat_structured", fake_chat)
    monkeypatch.setattr(draft_mod, "_endpoint", lambda: ("http://127.0.0.1:9/v1", "fake", None, 0.0))
    out = ok(client.post("/v1/classifiers/draft", json={"goal": "flag TV and music picked up by my recorder", "kind": "python"}))
    assert out["check"]["ok"] and out["notes"] == "fixed" and out["calls"] == 2
    assert "os" in seen[1]["previous_attempts"][0]["problems"][0]


def test_concurrent_runs_of_one_classifier_are_refused(client):
    wave_channel(client)
    recording(client)
    define_media(client)
    cid = sql("SELECT id FROM tl.classifiers WHERE name='is_it_media'")[0]["id"]
    with connect("app") as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (f"classifier:{cid}",))
        assert classifiers.run("is_it_media")["status"] == "busy"
        other.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (f"classifier:{cid}",))
    bridge.set_override(jev_media("none"))
    assert classifiers.run("is_it_media")["status"] == "done"


def test_resaved_versions_with_same_text_are_not_judged_again(client):
    wave_channel(client)
    recording(client)
    define_media(client)
    bridge.set_override(jev_media("partial"))
    classifiers.run("is_it_media")
    # the source writes a new version of a turn with the same text but another payload
    again = turn(1, "keep going, it's the next street", "Dad")
    again["payload"] = {"t": "rec-1-1", "sync": 2}
    ok(ingest(client, [again]))
    assert sql("SELECT count(*) AS n FROM tl.messages WHERE external_id='rec-1-1'")[0]["n"] == 2
    assert classifiers.run("is_it_media")["threads"] == 0
    changed = turn(1, "keep going, it's the street after", "Dad")
    changed["payload"] = {"t": "rec-1-1", "sync": 3}
    ok(ingest(client, [changed]))
    assert classifiers.run("is_it_media")["threads"] == 1


def test_message_classifiers_share_one_jev_call_per_message(client, channel):
    ok(client.post("/v1/classifiers", json=automated_definition()))
    promo = automated_definition(question="Is this a promotion?")
    promo["name"] = "promo_mail"
    promo["attribute"] = {"name": "promo", "value_schema": {"is_promo": {"type": "boolean", "required": True},
                                                             "score": {"type": "number", "min": 0, "max": 1}}}
    promo["spec"]["value"] = {"is_promo": True}
    ok(client.post("/v1/classifiers", json=promo))
    ctx = automated_definition(question="Is this spam?", context_messages=3)  # renders differently
    ctx["name"] = "spam_ctx"
    ctx["attribute"] = {"name": "spam", "value_schema": {"is_spam": {"type": "boolean", "required": True},
                                                          "score": {"type": "number", "min": 0, "max": 1}}}
    ctx["spec"]["value"] = {"is_spam": True}
    ok(client.post("/v1/classifiers", json=ctx))
    ok(ingest(client, [chat(1, "Your order has shipped. Do not reply."), chat(2, "lunch tomorrow?")]))
    asked = []

    def fake(op, args):
        asked.append(sorted(args["questions"]))
        p = 0.9 if "shipped" in args["about"]["message"] else 0.1
        return {k: {"type": "noul", "noul": p} for k in args["questions"]}

    bridge.set_override(fake)
    out = classifiers.tick()
    # automated_mail + promo_mail: one call per message (two questions);
    # spam_ctx sees earlier messages too, so it asks on its own.
    shared = [a for a in asked if len(a) == 2]
    alone = [a for a in asked if a == ["q"]]
    assert len(shared) == 2 and len(alone) == 2, asked
    assert out["shared"] == {"shared_calls": 2, "answers_given": 2, "answers_used": 2}
    by = {r["classifier"]: r for r in out["results"]}
    assert by["automated_mail"]["annotated"] == 1 and by["promo_mail"]["annotated"] == 1
    assert by["promo_mail"]["judged"] == 2
    rows = sql("SELECT attribute FROM tl.annotations WHERE status='active' AND attribute IN ('automated','promo')")
    assert sorted(r["attribute"] for r in rows) == ["automated", "promo"]
