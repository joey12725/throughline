"""Annotations (migration 0042, throughline.annotations): attributes keyed
by channel and external_id, precedence, ignored-by-default eligibility, the
effective time basis, known_by and as_of cutoffs, proposals, gating and
purge. Synthetic data only."""

import hashlib
import json
import statistics
import time
from contextlib import contextmanager

import psycopg
import pytest
from conftest import message
from psycopg import sql
from psycopg.types.json import Jsonb

from throughline import access, annotations, embeddings, lineage
from throughline.db import connect, create_token
from throughline.query import query

SECRET = "synthetic.secret"
WAVE = "synthetic.wave"
JOB = {"job": "synthetic-noise", "job_version": "1", "method": "rule"}
MODEL = {"job": "synthetic-model", "job_version": "1", "method": "model"}


def headers(kind, plugin=None, askers=None):
    token = create_token("synthetic-" + kind, kind, plugin, askers)["token"]
    return {"Authorization": "Bearer " + token}


@contextmanager
def reader(mode=None, asker=None, unknown=None):
    with connect("reader") as c:
        for name, value in (
            ("scope_mode", mode),
            ("asker", asker),
            ("unknown_asker", unknown),
        ):
            if value is not None:
                c.execute(
                    sql.SQL("SET LOCAL throughline.{}={}").format(
                        sql.SQL(name), sql.Literal(value)
                    )
                )
        yield c


def current(external_id, channel="synthetic.chat"):
    with connect() as c:
        return c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
            (channel, external_id),
        ).fetchone()["id"]


def clock():
    with connect() as c:
        return c.execute("SELECT clock_timestamp() t").fetchone()["t"]


def rows(statement, params=None):
    with connect() as c:
        return c.execute(statement, params).fetchall()


def ids(statement, params=None):
    return {r["message_id"] for r in rows(statement, params)}


def owner_token():
    with connect() as c:
        row = c.execute(
            "SELECT * FROM tl.tokens WHERE kind='owner' ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(row, asker=None)


def annotate(client, message_id, attribute, value, h=None, **extra):
    return client.post(
        "/v1/annotations",
        json={
            "message_id": message_id,
            "attribute": attribute,
            "value": value,
            **extra,
        },
        headers=h or {},
    )


def job_annotate(target_id, attribute, value, spec=JOB, **extra):
    with connect("app") as c:
        return annotations.create(
            c, spec, {"message_id": target_id}, attribute, value, **extra
        )


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def world(client, channel):
    for body in (
        {"name": SECRET, "plugin": "synthetic"},
        {
            "name": WAVE,
            "plugin": "synthetic",
            "fields": [{"name": "ignore_reason", "type": "text"}],
        },
    ):
        assert client.post("/v1/channels", json=body).status_code == 200
    msgs = [message(i) for i in range(6)] + [
        message(
            6,
            external_id="lonely",
            thread={"external_id": "lonely"},
            content="Synthetic orchard lonely note",
        ),
        message(
            0,
            channel=SECRET,
            external_id="secret-0",
            thread={"external_id": "secret"},
            content="Synthetic orchard zebra secret",
            fields={},
        ),
        message(
            0,
            channel=WAVE,
            external_id="wave-0",
            thread={"external_id": "rec"},
            content="Synthetic orchard crosstalk",
            fields={"ignore_reason": "crosstalk"},
        ),
        message(
            1,
            channel=WAVE,
            external_id="wave-1",
            thread={"external_id": "rec"},
            content="Synthetic orchard speech",
            fields={"ignore_reason": None},
        ),
    ]
    r = client.post(
        "/v1/ingest/messages", json={"plugin": "synthetic", "messages": msgs}
    )
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    m = {f"fake-{i}": current(f"fake-{i}") for i in range(6)}
    m["lonely"] = current("lonely")
    m["secret"] = current("secret-0", SECRET)
    m["wave-0"] = current("wave-0", WAVE)
    m["wave-1"] = current("wave-1", WAVE)
    return client, m


def scoped(kinds=None, **kwargs):
    args = ", ".join(f"{k}=>%({k})s" for k in kwargs)
    statement = (
        "SELECT message_id FROM api.list_records(kinds=>%(kinds)s::text[]"
        + (", " + args if args else "")
        + ")"
    )
    return ids(statement, {"kinds": kinds, **kwargs})


# --- survival and ground truth --------------------------------------------------------


def test_p4_annotation_survives_reimport_and_new_version(world):
    client, m = world
    every = ok(
        annotate(
            client,
            m["fake-0"],
            "effective_time",
            {"at": "2015-06-01T00:00:00Z", "precision": "day"},
        )
    )["annotation"]
    pinned = ok(
        annotate(
            client,
            m["fake-0"],
            "ignore",
            {"ignored": True, "reason": "draft"},
            target_version=1,
        )
    )["annotation"]
    assert every["target_version"] is None and pinned["target_version"] == 1
    # Re-import of the same delivery changes nothing; an edit makes version 2.
    for content in (None, "Synthetic orchard message 0, edited"):
        body = message(0) if content is None else message(0, content=content)
        assert (
            client.post(
                "/v1/ingest/messages", json={"plugin": "synthetic", "messages": [body]}
            ).status_code
            == 200
        )
    v2 = current("fake-0")
    assert v2 != m["fake-0"]
    effective = {
        r["message_id"]: r
        for r in ok(client.get("/v1/effective", params={"ids": f"{m['fake-0']},{v2}"}))[
            "rows"
        ]
    }
    assert effective[v2]["effective_time"].startswith("2015-06-01")
    assert effective[m["fake-0"]]["effective_time"].startswith("2015-06-01")
    assert (
        effective[m["fake-0"]]["ignored"] is True and effective[v2]["ignored"] is False
    )
    assert effective[v2]["annotation_ids"] == [every["id"]]
    assert v2 in scoped() and m["fake-0"] not in scoped()
    listed = ok(client.get("/v1/annotations", params={"message_id": v2}))["rows"]
    assert {a["id"] for a in listed} == {every["id"], pinned["id"]}
    assert {a["message_id"] for a in listed} == {v2}


def test_p4_ground_truth_unchanged_by_annotations(world):
    client, m = world

    def checksum():
        data = rows(
            "SELECT m.id,m.content,m.fields,m.happened_at,m.ended_at,m.payload_hash,m.lat,m.lon,p.payload FROM tl.messages m LEFT JOIN tl.raw_payloads p ON p.message_id=m.id ORDER BY m.id"
        )
        return hashlib.sha256(
            json.dumps(data, default=str, sort_keys=True).encode()
        ).hexdigest()

    before = checksum()
    ok(
        annotate(
            client,
            m["fake-1"],
            "effective_time",
            {"at": "2001-01-01T00:00:00Z", "precision": "year"},
        )
    )
    ok(
        annotate(
            client,
            m["fake-1"],
            "location",
            {"lat": 10.5, "lon": 20.25, "accuracy_m": 5},
        )
    )
    ok(annotate(client, m["fake-1"], "effective_place", {"name": "Synthetic Grove"}))
    ok(annotate(client, m["wave-0"], "ignore", {"ignored": False}))
    job_annotate(m["fake-2"], "media_noise", {"is_noise": True, "score": 0.9})
    a = ok(annotate(client, m["fake-3"], "ignore", {"ignored": True}))["annotation"]
    ok(
        client.post(
            f"/v1/annotations/{a['id']}/supersede", json={"value": {"ignored": False}}
        )
    )
    assert checksum() == before
    eff = ok(client.get("/v1/effective", params={"ids": str(m["fake-1"])}))["rows"][0]
    assert (eff["lat"], eff["lon"], eff["effective_place"]) == (
        10.5,
        20.25,
        "Synthetic Grove",
    )
    with connect() as c:
        with pytest.raises(psycopg.Error):
            c.execute(
                "UPDATE tl.annotations SET value='{\"ignored\": false}' WHERE id=%s",
                (a["id"],),
            )
    with pytest.raises(psycopg.Error):
        with connect("app") as c:
            c.execute("DELETE FROM tl.annotations WHERE id=%s", (a["id"],))


# --- eligibility -----------------------------------------------------------------------


def test_p4_ignored_excluded_by_default_in_scope_filter_search_records_list_records_vector_records(
    world, monkeypatch
):
    client, m = world
    ok(
        annotate(
            client, m["lonely"], "ignore", {"ignored": True, "reason": "test noise"}
        )
    )
    job_annotate(
        m["fake-1"],
        "media_noise",
        {"is_noise": True, "score": 0.8, "matched_record_id": "m:1"},
    )
    hidden = {m["lonely"], m["fake-1"], m["wave-0"]}
    defaults = ids("SELECT message_id FROM api.scope_filter(NULL)")
    assert not defaults & hidden and m["fake-0"] in defaults and m["wave-1"] in defaults
    for kinds in (["ignored"], ["life_event", "derived", "ignored"]):
        assert hidden <= ids(
            "SELECT message_id FROM api.scope_filter(NULL,kinds=>%s::text[])", (kinds,)
        )
        assert hidden <= scoped(kinds)
        assert hidden <= ids(
            "SELECT message_id FROM api.search_records('orchard',100,kinds=>%s::text[])",
            (kinds,),
        )
    assert not hidden & scoped()
    assert not hidden & scoped(["life_event"])
    assert scoped(["reference", "ignored"]) == set()
    assert not hidden & ids("SELECT message_id FROM api.search_records('orchard',100)")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "16")
    while embeddings.run_once():
        pass
    with connect() as c:
        vector = c.execute(
            "SELECT e.embedding::text AS v FROM tl.embeddings e JOIN tl.conversations k ON k.id=e.conversation_id WHERE k.thread_id=(SELECT thread_id FROM tl.messages WHERE id=%s)",
            (m["lonely"],),
        ).fetchone()["v"]
    found = ids("SELECT message_id FROM api.vector_records(%s::vector,100)", (vector,))
    assert found and m["lonely"] not in found
    assert m["lonely"] in ids(
        "SELECT message_id FROM api.vector_records(%s::vector,100,kinds=>ARRAY['ignored'])",
        (vector,),
    )


def test_p4_source_ignore_reason_field_excluded(world):
    client, m = world
    assert m["wave-0"] not in scoped() and m["wave-1"] in scoped()
    eff = {
        r["message_id"]: r
        for r in ok(
            client.get("/v1/effective", params={"ids": f"{m['wave-0']},{m['wave-1']}"})
        )["rows"]
    }
    assert eff[m["wave-0"]]["ignored"] is True and eff[m["wave-0"]][
        "ignore_reasons"
    ] == ["crosstalk"]
    assert eff[m["wave-0"]]["annotation_ids"] == []
    assert (
        eff[m["wave-1"]]["ignored"] is False
        and eff[m["wave-1"]]["ignore_reasons"] == []
    )


def test_p4_owner_unignore_overrides_source_ignore_reason_and_later_jobs(world):
    client, m = world
    ok(
        annotate(
            client, m["wave-0"], "ignore", {"ignored": False, "reason": "real speech"}
        )
    )
    assert m["wave-0"] in scoped()
    skipped = job_annotate(m["wave-0"], "ignore", {"ignored": True})
    assert skipped["annotation"] is None and skipped["skipped"] == "owner_annotation"
    job_annotate(m["wave-0"], "media_noise", {"is_noise": True})
    job_annotate(m["wave-0"], "ignore", {"ignored": True}, spec=MODEL)
    assert m["wave-0"] in scoped()
    eff = ok(client.get("/v1/effective", params={"ids": str(m["wave-0"])}))["rows"][0]
    assert eff["ignored"] is False and eff["media_noise"] is True
    # Without an owner decision a job's flag excludes.
    job_annotate(m["fake-5"], "ignore", {"ignored": True})
    assert m["fake-5"] not in scoped()
    # An owner media_noise false restores a job's noise flag.
    job_annotate(m["fake-4"], "media_noise", {"is_noise": True})
    assert m["fake-4"] not in scoped()
    ok(annotate(client, m["fake-4"], "media_noise", {"is_noise": False}))
    assert m["fake-4"] in scoped()


def test_p4_effective_time_basis(world):
    client, m = world
    ok(
        annotate(
            client,
            m["fake-2"],
            "effective_time",
            {
                "at": "2015-06-01T10:00:00Z",
                "ended_at": "2015-06-01T11:00:00Z",
                "precision": "exact",
            },
        )
    )
    window = {"after_at": "2015-05-01T00:00:00Z", "before_at": "2015-07-01T00:00:00Z"}
    assert scoped(time_basis="effective", **window) == {m["fake-2"]}
    assert scoped(time_basis="record", **window) == set()
    # Records without annotations keep their record time under the effective basis.
    record = {
        "after_at": "2020-01-01T00:00:00Z",
        "before_at": "2020-01-01T00:00:03Z",
        "channels": ["synthetic.chat"],
    }
    assert scoped(time_basis="effective", **record) == {
        m["fake-0"],
        m["fake-1"],
        m["fake-3"],
    }
    assert scoped(time_basis="record", **record) == {
        m["fake-0"],
        m["fake-1"],
        m["fake-2"],
        m["fake-3"],
    }
    late = {"after_at": "2015-06-01T10:30:00Z", "before_at": "2015-06-01T12:00:00Z"}
    assert scoped(time_basis="effective", **late) == {m["fake-2"]}


def test_scope_filter_time_basis_effective_direct(world):
    client, m = world
    ok(
        annotate(
            client,
            m["fake-3"],
            "effective_time",
            {"at": "1999-12-31T00:00:00Z", "precision": "day"},
        )
    )
    found = ids(
        "SELECT message_id FROM api.scope_filter(NULL,time_basis=>'effective',after_at=>'1999-12-01Z',before_at=>'2000-01-15Z')"
    )
    assert found == {m["fake-3"]}
    r = client.post(
        "/v1/sql",
        json={
            "statement": "SELECT message_id FROM api.scope_filter(NULL::bigint[],time_basis=>'effective',after_at=>'1999-12-01Z',before_at=>'2000-01-15Z')"
        },
    )
    assert r.status_code == 200, r.text
    assert [x["message_id"] for x in r.json()["rows"]] == [m["fake-3"]]


def test_recall_check_still_rejects_unknown_basis(world):
    for basis in ("bogus", "EFFECTIVE", "ignored"):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            rows("SELECT * FROM api.scope_filter(NULL,time_basis=>%s)", (basis,))
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        rows(
            "SELECT * FROM api.scope_filter(NULL,person_role=>'bystander',time_basis=>'effective')"
        )


def test_p4_annotation_cutoff_under_known_by_only(world):
    client, m = world
    before_annotation = clock()
    pinned = ok(
        annotate(client, m["fake-3"], "ignore", {"ignored": True}, target_version=1)
    )["annotation"]
    flagged = ok(annotate(client, m["fake-5"], "ignore", {"ignored": True}))[
        "annotation"
    ]
    after_annotation = clock()
    r = client.post(
        "/v1/ingest/messages",
        json={
            "plugin": "synthetic",
            "messages": [message(3, content="Synthetic orchard message 3, edited")],
        },
    )
    assert r.status_code == 200 and r.json()["new_versions"] == 1
    v2 = current("fake-3")
    ok(
        client.post(
            f"/v1/annotations/{flagged['id']}/retract", json={"reason": "was fine"}
        )
    )
    now = clock()
    # known_by: only annotations made by then, in the state they had then.
    early = scoped(known_by=before_annotation)
    assert m["fake-3"] in early and m["fake-5"] in early
    then = scoped(known_by=after_annotation)
    assert m["fake-3"] not in then and m["fake-5"] not in then
    later = scoped(known_by=now)
    assert v2 in later and m["fake-5"] in later
    # as_of alone: current annotations for the version valid at as_of.
    current_view = scoped(as_of="2030-01-01T00:00:00Z")
    assert v2 in current_view and m["fake-5"] in current_view
    assert pinned["status"] == "active"
    with connect() as c:
        state = c.execute(
            "SELECT status FROM tl.annotations WHERE id=%s", (flagged["id"],)
        ).fetchone()
        note = c.execute(
            "SELECT note FROM tl.status_history WHERE kind='annotation' AND ref_id=%s",
            (flagged["id"],),
        ).fetchone()
    assert state["status"] == "retracted" and note["note"] == "was fine"


def test_p4_precedence_owner_over_source_over_rule_over_model(world):
    client, m = world

    def eff(mid):
        return ok(client.get("/v1/effective", params={"ids": str(mid)}))["rows"][0]

    job_annotate(
        m["fake-4"],
        "effective_time",
        {"at": "2010-01-01T00:00:00Z", "precision": "year"},
        spec=MODEL,
    )
    job_annotate(
        m["fake-4"],
        "effective_time",
        {"at": "2009-01-01T00:00:00Z", "precision": "year"},
        spec=MODEL,
    )
    assert eff(m["fake-4"])["effective_time"].startswith(
        "2009"
    )  # latest within a method
    job_annotate(
        m["fake-4"],
        "effective_time",
        {"at": "2011-01-01T00:00:00Z", "precision": "year"},
    )
    assert eff(m["fake-4"])["effective_time"].startswith("2011")  # rule over model
    job_annotate(
        m["fake-4"],
        "effective_time",
        {"at": "2008-01-01T00:00:00Z", "precision": "year"},
        spec=MODEL,
    )
    assert eff(m["fake-4"])["effective_time"].startswith("2011")
    ok(
        annotate(
            client,
            m["fake-4"],
            "effective_time",
            {"at": "2012-01-01T00:00:00Z", "precision": "year"},
        )
    )
    assert eff(m["fake-4"])["effective_time"].startswith("2012")  # owner over rule
    # Source over rule and model, owner over source.
    job_annotate(m["wave-0"], "ignore", {"ignored": False})
    assert eff(m["wave-0"])["ignored"] is True
    job_annotate(m["wave-1"], "ignore", {"ignored": True}, spec=MODEL)
    assert eff(m["wave-1"])["ignored"] is True
    job_annotate(m["wave-1"], "ignore", {"ignored": False})
    assert eff(m["wave-1"])["ignored"] is False  # rule over model
    ok(annotate(client, m["wave-0"], "ignore", {"ignored": False}))
    assert eff(m["wave-0"])["ignored"] is False


def test_p4_agent_annotation_becomes_proposal_and_accept_creates_it(world):
    client, m = world
    agent = headers("agent")
    r = annotate(
        client,
        m["fake-1"],
        "ignore",
        {"ignored": True, "reason": "agent thinks noise"},
        agent,
    )
    assert r.status_code == 202, r.text
    proposal_id = r.json()["proposal_id"]
    assert m["fake-1"] in scoped()
    with connect() as c:
        assert not c.execute("SELECT 1 FROM tl.annotations").fetchone()
    access.register_asker("guest")
    access.grant("guest", channel="synthetic.chat")
    restricted = annotate(
        client,
        m["fake-2"],
        "ignore",
        {"ignored": True},
        {"X-Throughline-Asker": "guest"},
    )
    assert restricted.status_code == 202, restricted.text
    hidden = annotate(
        client,
        m["secret"],
        "ignore",
        {"ignored": True},
        {"X-Throughline-Asker": "guest"},
    )
    missing = annotate(
        client, 999999999, "ignore", {"ignored": True}, {"X-Throughline-Asker": "guest"}
    )
    assert (
        hidden.status_code == missing.status_code == 404
        and hidden.json() == missing.json()
    )
    assert (
        annotate(
            client,
            m["fake-1"],
            "ignore",
            {"ignored": True},
            headers("plugin", "synthetic"),
        ).status_code
        == 403
    )
    assert (
        annotate(client, m["fake-1"], "ignore", {"ignored": "yes"}).status_code == 400
    )
    decided = ok(
        client.post(f"/v1/proposals/{proposal_id}/decide", json={"accept": True})
    )
    created = rows(
        "SELECT * FROM tl.annotations WHERE id=%s",
        (decided["result"]["annotation_id"],),
    )[0]
    assert (created["method"], created["origin"], created["job"]) == (
        "model",
        "job",
        "agent",
    )
    assert m["fake-1"] not in scoped()
    # Agents cannot change annotations directly either.
    assert (
        client.post(
            f"/v1/annotations/{created['id']}/retract", json={}, headers=agent
        ).status_code
        == 403
    )


def test_p4_job_cannot_override_owner_annotation(world):
    client, m = world
    owned = ok(annotate(client, m["fake-0"], "ignore", {"ignored": False}))[
        "annotation"
    ]
    assert (
        job_annotate(m["fake-0"], "ignore", {"ignored": True})["skipped"]
        == "owner_annotation"
    )
    with connect("app") as c:
        with pytest.raises(PermissionError):
            annotations.supersede(c, JOB, owned["id"], {"ignored": True})
        with pytest.raises(PermissionError):
            annotations.retract(c, JOB, owned["id"], "job thinks so")
    # The database refuses it too.
    job_row = job_annotate(m["fake-1"], "ignore", {"ignored": True})["annotation"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect() as c:
            c.execute(
                "UPDATE tl.annotations SET status='superseded',superseded_by=%s WHERE id=%s",
                (job_row["id"], owned["id"]),
            )
    # A job may replace a job annotation; the owner may replace anything.
    with connect("app") as c:
        replaced = annotations.supersede(c, JOB, job_row["id"], {"ignored": False})
    assert replaced["superseded"] == job_row["id"]
    r = ok(
        client.post(
            f"/v1/annotations/{owned['id']}/supersede",
            json={"value": {"ignored": True}},
        )
    )
    assert r["annotation"]["method"] == "owner"


def test_provenance_accepts_annotation_kind_in_python_and_sql(world):
    client, m = world
    made = ok(annotate(client, m["fake-0"], "ignore", {"ignored": True}))["annotation"]
    assert "annotation" in lineage.DERIVED_KINDS
    prov = rows(
        "SELECT * FROM tl.provenance WHERE derived_kind='annotation' AND derived_id=%s",
        (made["id"],),
    )
    assert [
        (p["source_message_id"], p["source_version"], p["origin"]) for p in prov
    ] == [(m["fake-0"], 1, "owner")]
    with connect() as c:
        assert (
            lineage.add_provenance(
                c,
                "synthetic-job",
                "annotation",
                made["id"],
                [m["fake-1"]],
                origin="job",
            )
            == 1
        )
        c.execute(
            "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) VALUES('annotation',%s,%s,1,'owner')",
            (made["id"], m["fake-2"]),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) VALUES('bogus',1,%s,1,'owner')",
                (m["fake-2"],),
            )
    with connect() as c:
        names = [
            r["conname"]
            for r in c.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid='tl.provenance'::regclass AND contype='c' AND pg_get_constraintdef(oid) LIKE '%%derived_kind%%'"
            )
        ]
    assert names == ["provenance_derived_kind"]


def test_acl_9_annotations_and_effective_gated_by_target(world):
    client, m = world
    with connect() as c:
        person = c.execute(
            "INSERT INTO tl.people(handle) VALUES('synthetic-speaker') RETURNING id"
        ).fetchone()["id"]
    shown = ok(
        annotate(client, m["fake-0"], "effective_place", {"name": "Synthetic Grove"})
    )["annotation"]
    ok(annotate(client, m["fake-0"], "speaker", {"person_id": person}))
    ok(annotate(client, m["secret"], "ignore", {"ignored": True, "reason": "zebra"}))
    access.register_asker("guest")
    access.grant("guest", channel="synthetic.chat")
    g = {"X-Throughline-Asker": "guest"}
    listed = ok(
        client.get("/v1/annotations", params={"message_id": m["fake-0"]}, headers=g)
    )["rows"]
    assert [a["id"] for a in listed] == [shown["id"]]
    assert (
        ok(
            client.get("/v1/annotations", params={"message_id": m["secret"]}, headers=g)
        )["rows"]
        == []
    )
    eff = ok(
        client.get(
            "/v1/effective", params={"ids": f"{m['fake-0']},{m['secret']}"}, headers=g
        )
    )["rows"]
    assert [e["message_id"] for e in eff] == [m["fake-0"]]
    assert (
        eff[0]["speaker_person_id"] is None
        and eff[0]["effective_place"] == "Synthetic Grove"
    )
    full = ok(
        client.get("/v1/effective", params={"ids": f"{m['fake-0']},{m['secret']}"})
    )["rows"]
    assert full[0]["speaker_person_id"] == person and full[1]["ignore_reasons"] == [
        "zebra"
    ]
    with reader("asker", "guest", "deny") as c:
        assert (
            c.execute("SELECT count(*) AS n FROM api.annotations").fetchone()["n"] == 1
        )
        # No error oracle: the failing predicate never runs on hidden rows.
        n = c.execute(
            "SELECT count(*) AS n FROM api.annotations WHERE 1/(CASE WHEN reason='zebra' OR value->>'reason'='zebra' THEN 0 ELSE 1 END)=1"
        ).fetchone()["n"]
        assert n == 1
        assert (
            c.execute(
                "SELECT * FROM api.provenance WHERE derived_kind='annotation'"
            ).fetchall()
            != []
        )
    with reader("asker", "nobody", "deny") as c:
        assert (
            c.execute("SELECT count(*) AS n FROM api.annotations").fetchone()["n"] == 0
        )
        assert (
            c.execute(
                "SELECT count(*) AS n FROM api.effective(%s::bigint[])",
                ([m["fake-0"]],),
            ).fetchone()["n"]
            == 0
        )


def test_purge_removes_annotations(world):
    from throughline.cli import purge

    client, m = world
    gone = ok(annotate(client, m["secret"], "ignore", {"ignored": True}))["annotation"]
    ok(
        client.post(
            f"/v1/annotations/{gone['id']}/supersede",
            json={"value": {"ignored": False}},
        )
    )
    kept = ok(annotate(client, m["fake-0"], "ignore", {"ignored": True}))["annotation"]
    purge(SECRET, confirm=True)
    with connect() as c:
        assert [r["id"] for r in c.execute("SELECT id FROM tl.annotations")] == [
            kept["id"]
        ]
        left = c.execute(
            "SELECT derived_id FROM tl.provenance WHERE derived_kind='annotation'"
        ).fetchall()
        assert [r["derived_id"] for r in left] == [kept["id"]]
        assert not c.execute(
            "SELECT 1 FROM tl.status_history WHERE kind='annotation' AND ref_id=%s",
            (gone["id"],),
        ).fetchone()


def test_legacy_search_unaffected(world):
    client, m = world
    ok(annotate(client, m["fake-0"], "ignore", {"ignored": True}))
    legacy = {
        r["id"] for r in query("SELECT id FROM api.search('orchard',100)")["rows"]
    }
    assert m["fake-0"] in legacy and m["wave-0"] in legacy
    assert m["fake-0"] not in ids(
        "SELECT message_id FROM api.search_records('orchard',100)"
    )


def test_search_records_latency_with_and_without_annotations(client, channel):
    n, annotated = 20000, 2000
    with connect() as c:
        channel_id = c.execute(
            "SELECT id FROM tl.channels WHERE name='synthetic.chat'"
        ).fetchone()["id"]
        thread = c.execute(
            "INSERT INTO tl.threads(channel_id,external_id) VALUES(%s,'bulk') RETURNING id",
            (channel_id,),
        ).fetchone()["id"]
        c.execute(
            "INSERT INTO tl.messages(channel_id,external_id,version,thread_id,thread_no,happened_at,content,payload_hash) SELECT %s,'bulk-'||i,1,%s,i,'2021-01-01Z'::timestamptz+i*interval '1 second','Synthetic orchard bulk message '||i,'synthetic-hash' FROM generate_series(1,%s) i",
            (channel_id, thread, n),
        )
        c.execute("ANALYZE tl.messages")

    def timed():
        samples = []
        for _ in range(5):
            start = time.perf_counter()
            found = rows("SELECT message_id FROM api.search_records('orchard bulk',50)")
            samples.append(time.perf_counter() - start)
            assert len(found) == 50
        return statistics.median(samples)

    timed()
    base = timed()
    with connect() as c:
        c.execute(
            "INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin) SELECT %s,'bulk-'||i,'effective_time',jsonb_build_object('at','2015-01-01T00:00:00Z','precision','day'),'owner','owner' FROM generate_series(1,%s) i",
            (channel_id, annotated),
        )
        c.execute(
            "INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin) SELECT %s,'bulk-'||i,'ignore',%s,'owner','owner' FROM generate_series(1,%s,10) i",
            (channel_id, Jsonb({"ignored": True}), annotated),
        )
        c.execute("ANALYZE tl.annotations")
    timed()
    with_annotations = timed()
    print(
        f"\nsearch_records over {n} messages: {base:.3f}s plain, {with_annotations:.3f}s with {annotated} annotations"
    )
    assert with_annotations <= base * 1.25 or with_annotations - base < 0.25
    hidden = ids(
        "SELECT message_id FROM api.list_records(NULL,ARRAY['synthetic.chat'])"
    )
    assert len(hidden) == n - annotated // 10


def test_proposal_target_by_channel_and_external_id_checked_uniformly(world):
    client, m = world
    access.register_asker("guest")
    access.grant("guest", channel="synthetic.chat")
    g = {"X-Throughline-Asker": "guest"}
    agent = headers("agent", askers=["guest"])
    answers = []
    for target in (
        {"channel": SECRET, "external_id": "secret-0"},
        {"channel": "synthetic.chat", "external_id": "no-such-record"},
    ):
        for h in (g, agent):
            r = client.post(
                "/v1/annotations",
                json={
                    "target": target,
                    "attribute": "ignore",
                    "value": {"ignored": True},
                },
                headers=h,
            )
            answers.append((r.status_code, r.json()))
    assert {json.dumps(a, sort_keys=True) for a in answers} == {
        json.dumps((404, {"detail": "referenced record not found"}), sort_keys=True)
    }
    r = client.post(
        "/v1/annotations",
        json={
            "target": {"channel": "synthetic.chat", "external_id": "fake-1"},
            "attribute": "ignore",
            "value": {"ignored": True},
        },
        headers=agent,
    )
    assert r.status_code == 202, r.text
    # Concept proposals naming a hidden page by channel and external_id too.
    r = client.post(
        "/v1/proposals",
        json={
            "kind": "concept",
            "payload": {
                "target": {"channel": SECRET, "external_id": "secret-0"},
                "entity": {"id": 1},
            },
        },
        headers=agent,
    )
    assert r.status_code == 404 and r.json() == {
        "detail": "referenced record not found"
    }
    with connect() as c:
        assert not c.execute("SELECT 1 FROM tl.annotations").fetchone()


def test_owner_annotation_status_refused_for_non_owner_token_in_database(world):
    client, m = world
    owned = ok(annotate(client, m["fake-0"], "ignore", {"ignored": True}))["annotation"]
    agent_id = create_token("synthetic-agent-db", "agent")["id"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect("app") as c:
            c.execute(
                "SELECT set_config('throughline.token_id',%s,true)", (str(agent_id),)
            )
            c.execute(
                "UPDATE tl.annotations SET status='retracted' WHERE id=%s",
                (owned["id"],),
            )
    assert (
        rows("SELECT status FROM tl.annotations WHERE id=%s", (owned["id"],))[0][
            "status"
        ]
        == "active"
    )
    ok(
        client.post(
            f"/v1/annotations/{owned['id']}/retract",
            json={"reason": "owner changed mind"},
        )
    )
    assert (
        rows("SELECT status FROM tl.annotations WHERE id=%s", (owned["id"],))[0][
            "status"
        ]
        == "retracted"
    )


def test_purge_removes_proposals_naming_channel_records_by_name(world):
    from throughline.cli import purge

    client, m = world
    agent = headers("agent")
    gone = client.post(
        "/v1/annotations",
        json={
            "target": {"channel": SECRET, "external_id": "secret-0"},
            "attribute": "ignore",
            "value": {"ignored": True},
        },
        headers=agent,
    )
    kept = client.post(
        "/v1/annotations",
        json={
            "target": {"channel": "synthetic.chat", "external_id": "fake-0"},
            "attribute": "ignore",
            "value": {"ignored": True},
        },
        headers=agent,
    )
    assert gone.status_code == kept.status_code == 202, (gone.text, kept.text)
    purge(SECRET, confirm=True)
    assert [r["id"] for r in rows("SELECT id FROM tl.proposals ORDER BY id")] == [
        kept.json()["proposal_id"]
    ]
