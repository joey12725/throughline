"""Semantic operator match: three-valued matching of every record kind with a
scope-checked verdict store (migration 0041, QRY-33, QRY-8, CST-5, CST-6,
CST-10, ACL-8, QRY-36, QRY-38, INT-10, ARC-6). Synthetic data only; the
semantic provider is the deterministic local rule model or a test double
installed with providers.override."""

import hashlib
import importlib.util
from contextlib import contextmanager

import psycopg
import pytest
from conftest import ingest, message
from psycopg import sql

from throughline import cost, passages, providers, resultsets
from throughline.db import connect
from throughline.execution import Execution
from throughline.providers.base import SemanticModel
from throughline.providers.local import LocalRuleModel
from throughline.retrieval import RecallRequest, recall
from throughline.retrieval import match as match_module
from throughline.retrieval.match import MatchUnavailable, match
from throughline.retrieval.types import RECORD_KEYS
from throughline.scope import Scope

OWNER = Scope.local()
GUEST = "fake-guest"


@pytest.fixture(autouse=True)
def clean(database, monkeypatch):
    for name in ("THROUGHLINE_MATCH_MAX_ROWS", "THROUGHLINE_MATCH_GROUP_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("THROUGHLINE_SEMANTIC_PROVIDER", raising=False)
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


# --- test doubles ------------------------------------------------------------


class Counting(LocalRuleModel):
    """The local rule model, counting requests and recording what was sent."""

    def __init__(self, base_version=None):
        self.calls = 0
        self.sent = []
        if base_version is not None:
            self.base_version = base_version

    def apply(self, operation, instruction, items, output_schema=None):
        self.calls += 1
        self.sent.extend(i["text"] for i in items)
        return super().apply(operation, instruction, items, output_schema)


class Remote(SemanticModel):
    """A non-local model with a price: every request needs the cost gate."""

    name = "fake-remote"
    version = "fake-remote-1"
    model = "fake-remote-model"
    local = False
    price_per_million = 1_000_000.0

    def __init__(self):
        self.calls = 0
        self.rule = LocalRuleModel()

    def apply(self, operation, instruction, items, output_schema=None):
        self.calls += 1
        results, usage = self.rule.apply(operation, instruction, items, output_schema)
        return results, self._usage(usage.tokens)


class Failing(Counting):
    """Fails every request whose batch holds one of the given texts."""

    def __init__(self, poison=(), mode="raise", batch_size=None):
        super().__init__()
        self.poison = set(poison)
        self.mode = mode
        if batch_size is not None:
            self.batch_size = batch_size

    def apply(self, operation, instruction, items, output_schema=None):
        texts = {i["text"] for i in items}
        if not self.poison or texts & self.poison:
            self.calls += 1
            if self.mode == "raise":
                raise providers.ProviderUnavailable("fake outage")
            if self.mode == "timeout":
                raise TimeoutError("fake timeout")
            results, usage = LocalRuleModel.apply(
                self, operation, instruction, items, output_schema
            )
            for r in results:
                r["output"] = "maybe"  # Not one of the three values.
            return results, usage
        return super().apply(operation, instruction, items, output_schema)


class Recorded(SemanticModel):
    """Replays recorded responses keyed by item text (no model at all)."""

    name = "recorded"
    version = "recorded-1"
    model = "recorded-model"
    local = True

    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    def apply(self, operation, instruction, items, output_schema=None):
        self.calls += 1
        out = []
        for item in items:
            output, evidence = self.responses[item["text"]]
            out.append(dict(id=item["id"], output=output, evidence=evidence, note=""))
        return out, self._usage(10)


@contextmanager
def model(provider):
    with providers.override("semantic", provider):
        yield provider


# --- helpers -----------------------------------------------------------------


def owner(statement, params=()):
    with connect() as c:
        cursor = c.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def register(client, name, **kwargs):
    r = client.post("/v1/channels", json=dict(name=name, plugin="synthetic", **kwargs))
    assert r.status_code == 200, r.text


def put(client, items):
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text


def ids(channel="synthetic.chat"):
    rows = owner(
        "SELECT m.id,m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL ORDER BY m.id",
        (channel,),
    )
    return {r["external_id"]: r["id"] for r in rows}


def seed(client, texts, channel="synthetic.chat", thread="fake-thread"):
    items = []
    for i, text in enumerate(texts):
        extra = {} if channel == "synthetic.chat" else dict(fields={})
        items.append(
            message(
                i,
                channel=channel,
                external_id=f"{thread}-{i}",
                thread={"external_id": thread},
                content=text,
                **extra,
            )
        )
    put(client, items)
    found = ids(channel)
    return [found[f"{thread}-{i}"] for i in range(len(texts))]


def make(scope, records, completeness="exhaustive", **kwargs):
    return resultsets.create(scope, records, completeness=completeness, **kwargs)


def refs(message_ids, **extra):
    return [dict(record_id=f"m:{i}", **extra) for i in message_ids]


def token_row():
    return owner(
        "SELECT * FROM tl.tokens WHERE kind='owner' AND revoked_at IS NULL ORDER BY id LIMIT 1"
    )[0]


def token_scope():
    return Scope.from_token(dict(token_row(), asker=None))


def run(scope, handle, condition="prototype review", max_rows=100, **kwargs):
    execution = kwargs.pop("execution", None) or Execution()
    out = match(
        scope,
        kwargs.pop("token", None),
        handle=handle,
        condition=condition,
        max_rows=max_rows,
        execution=execution,
        **kwargs,
    )
    return out, execution


def records(scope, handle):
    return resultsets.read(scope, handle)["records"]


def verdicts(scope, handle):
    return {
        r["record_id"]: (r["meta"] or {}).get("verdict") for r in records(scope, handle)
    }


def stored(where="", params=()):
    return owner("SELECT * FROM tl.verdicts " + where + " ORDER BY id", params)


def ledger_rows():
    return owner("SELECT count(*) n FROM tl.model_ledger")[0]["n"]


def restricted(*channels):
    owner("INSERT INTO tl.askers(name) VALUES (%s) ON CONFLICT DO NOTHING", (GUEST,))
    for channel in channels:
        owner(
            "INSERT INTO tl.asker_grants(asker,channel_id) SELECT %s,id FROM tl.channels WHERE name=%s",
            (GUEST, channel),
        )
    return Scope(
        token_id=None,
        token_kind="agent",
        asker=GUEST,
        mode="asker",
        unknown_asker="deny",
    )


TEXTS = [
    "Synthetic prototype review notes",
    "Synthetic gardening diary",
    "Synthetic prototype sketch",
]


# --- QRY-33 three values -----------------------------------------------------


def test_qry_33_three_values_with_offsets(client, channel):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m, score=0.5))
    with model(Counting()) as fake:
        out, execution = run(OWNER, handle)
    assert fake.calls == 1
    got = {r["record_id"]: r for r in records(OWNER, out)}
    assert [r["record_id"] for r in records(OWNER, out)] == [f"m:{i}" for i in m]
    first = got[f"m:{m[0]}"]
    assert first["meta"]["verdict"] == "match"
    assert first["meta"]["cached"] is False
    assert first["meta"]["provider"] == "local"
    assert first["meta"]["operator_version"].startswith("match-1/")
    content = first["content"]
    assert sorted(content[a:b].lower() for a, b in first["meta"]["evidence"]) == [
        "prototype",
        "review",
    ]
    assert got[f"m:{m[1]}"]["meta"]["verdict"] == "nonmatch"
    assert got[f"m:{m[2]}"]["meta"]["verdict"] == "uncertain"
    assert "reason" not in got[f"m:{m[2]}"]["meta"]
    # group = verdict; the generated flag and score are untouched.
    assert [g["grp"] for g in first["groups"]] == ["match"]
    assert first["generated"] is False and first["score"] == 0.5
    assert execution.to_dict()["counts"]["match"] == 1


def test_qry_33_evidence_offsets_clamped_and_flagged(client, channel):
    m = seed(client, ["Synthetic short text"])
    handle = make(OWNER, refs(m))
    text = "Synthetic short text"
    with model(Recorded({text: ("match", [[0, 9], [5, 500], [-3, 2]])})):
        out, _ = run(OWNER, handle)
    meta = records(OWNER, out)[0]["meta"]
    assert meta["evidence"] == [[0, 9], [5, len(text)], [0, 2]]
    assert meta["evidence_clamped"] is True


# --- bounds ------------------------------------------------------------------


def test_qry_33_bound_before_any_call(client, channel, monkeypatch):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m))
    with model(Counting()) as fake:
        with pytest.raises(MatchUnavailable, match="max_rows"):
            run(OWNER, handle, max_rows=2)
        with pytest.raises(MatchUnavailable):
            run(OWNER, handle, max_rows=501)
        monkeypatch.setenv("THROUGHLINE_MATCH_MAX_ROWS", "2")
        with pytest.raises(MatchUnavailable):
            run(OWNER, handle, max_rows=3)
        with pytest.raises(MatchUnavailable):
            run(OWNER, handle, condition="  ")
        with pytest.raises(MatchUnavailable):
            run(OWNER, handle, grain="day")
    assert fake.calls == 0
    assert stored() == []
    r = client.post(
        "/v1/match", json=dict(input=handle, condition="prototype", max_rows=2)
    )
    assert r.status_code in (400, 404), r.text


# --- every record kind ---------------------------------------------------------


TEXTLESS = (
    "fake_textless_records",
    "SELECT 'y:'||e.id AS record_id,'entity'::text AS kind,e.id AS ref_id,NULL::bigint AS message_id,NULL::int AS version,true AS is_current,NULL::text AS channel,NULL::text AS external_id,NULL::bigint AS thread_id,NULL::int AS thread_no,NULL::text AS content,NULL::timestamptz AS record_time,NULL::timestamptz AS ended_at,NULL::timestamptz AS ingested_at,NULL::timestamptz AS subject_from,NULL::timestamptz AS subject_to,NULL::text AS subject_from_precision,NULL::text AS subject_to_precision,NULL::bigint AS person_id,NULL::text AS author_handle,NULL::float8 AS lat,NULL::float8 AS lon,'not_applicable'::text AS loc_reason,false AS is_outside,NULL::text[] AS heading_path,NULL::int AS char_start,NULL::int AS char_end,NULL::jsonb AS claim,false AS generated,false AS unsupported,'{}'::jsonb AS provenance,'[]'::jsonb AS support,NULL::jsonb AS expansion FROM tl.entities e",
)


@contextmanager
def textless_view():
    view, definition = TEXTLESS
    with connect() as c:
        c.execute("SELECT tl.install_gate(%s,'false',%s)", (view, definition))
        c.execute(
            "INSERT INTO tl.record_sources VALUES ('y',%s) ON CONFLICT DO NOTHING",
            ("api." + view,),
        )
    try:
        yield
    finally:
        with connect() as c:
            c.execute("DELETE FROM tl.record_sources WHERE prefix='y'")
            c.execute(
                sql.SQL("DROP VIEW IF EXISTS {}, {}, {}").format(
                    sql.Identifier("api", view),
                    sql.Identifier("tl", view + "_gated"),
                    sql.Identifier("tl", view + "_all"),
                )
            )
            c.execute("DELETE FROM tl.gates WHERE view_name=%s", (view,))


def test_qry_33_claims_passages_links_evaluated_and_textless_members_uncertain_not_evaluable(
    client, channel
):
    register(client, "synthetic.pages", is_reference=True, record_kind="page_revision")
    m = seed(client, ["Synthetic prototype review in chat"])
    put(
        client,
        [
            message(
                5,
                channel="synthetic.pages",
                external_id="fake-page",
                thread={"external_id": "fake-page"},
                content="# Plans\n\nSynthetic prototype review checklist for the page.",
                fields={},
            )
        ],
    )
    with connect("app") as c:
        passages.build(c, 100)
    passage = owner("SELECT id FROM tl.passages ORDER BY id LIMIT 1")[0]["id"]
    review = owner(
        "INSERT INTO tl.entities(kind,name) VALUES ('concept','Synthetic prototype review') RETURNING id"
    )[0]["id"]
    link_id = owner(
        "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,method) VALUES ('about','message',%s,'entity',%s,'owner') RETURNING id",
        (m[0], review),
    )[0]["id"]
    members = [
        dict(record_id=f"m:{m[0]}"),
        dict(record_id=f"p:{passage}"),
        dict(record_id=f"l:{link_id}"),
        dict(record_id=f"e:{review}"),
    ]
    claim_id = _claim(m[0])
    if claim_id is not None:
        members.append(dict(record_id=f"c:{claim_id}"))
    with textless_view():
        members.append(dict(record_id=f"y:{review}"))
        handle = make(OWNER, members)
        with model(Counting()) as fake:
            out, execution = run(OWNER, handle, condition="prototype review")
        got = {r["record_id"]: r for r in records(OWNER, out)}
    assert fake.calls == 1
    judged = [f"m:{m[0]}", f"p:{passage}", f"e:{review}"]
    if claim_id is not None:
        judged.append(f"c:{claim_id}")
    for record_id in judged:
        assert got[record_id]["meta"]["verdict"] == "match", record_id
    assert got[f"l:{link_id}"]["meta"]["verdict"] in ("match", "uncertain", "nonmatch")
    assert got[f"y:{review}"]["meta"]["verdict"] == "uncertain"
    assert got[f"y:{review}"]["meta"]["reason"] == "not_evaluable"
    counts = execution.to_dict()["counts"]
    assert counts["not_evaluable"] == 1
    assert counts["semantically_evaluated"] == len(members) - 1
    # Sources of each stored verdict: the message, the revision and its
    # support for a passage, the link's message ends.
    rows = {(r["record_prefix"], r["ref_id"]): r for r in stored()}
    assert rows[("m", m[0])]["source_message_ids"] == [m[0]]
    page = owner("SELECT message_id FROM tl.passages WHERE id=%s", (passage,))[0]
    assert page["message_id"] in rows[("p", passage)]["source_message_ids"]
    assert rows[("l", link_id)]["source_message_ids"] == [m[0]]
    assert ("y", review) not in rows


def _claim(message_id):
    """A sourced claim when the claim store of claims is installed
    (optional-feature); None before it exists."""
    present = owner(
        "SELECT to_regclass('tl.claims') IS NOT NULL AND EXISTS(SELECT 1 FROM tl.record_sources WHERE prefix='c') AS ok"
    )[0]["ok"]
    if not present:
        return None
    try:
        claim = owner(
            "INSERT INTO tl.claims(subject_kind,subject_id,statement,claim_type,method,natural_key)"
            " VALUES ('message',%s,'Synthetic prototype review claim','observation','owner','fake-claim-key') RETURNING id",
            (message_id,),
        )[0]["id"]
        owner(
            "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) SELECT 'claim',%s,m.id,m.version,'owner' FROM tl.messages m WHERE m.id=%s",
            (claim, message_id),
        )
    except psycopg.Error:
        return None
    visible = owner("SELECT 1 FROM api.claim_records WHERE ref_id=%s", (claim,))
    return claim if visible else None


# --- QRY-8 reuse ---------------------------------------------------------------


def test_qry_8_second_run_no_calls_and_new_version_one_call(client, channel):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m))
    with model(Counting()) as fake:
        run(OWNER, handle)
        assert fake.calls == 1 and len(fake.sent) == 3
        again, execution = run(OWNER, handle)
        assert fake.calls == 1
        assert all(r["meta"]["cached"] for r in records(OWNER, again))
        assert execution.to_dict()["counts"]["cached"] == 3
        # A new version of one member is new content: one call, one item.
        put(
            client,
            [
                message(
                    0,
                    external_id="fake-thread-0",
                    thread={"external_id": "fake-thread"},
                    content="Synthetic prototype review notes, revised",
                )
            ],
        )
        current = ids()["fake-thread-0"]
        newer = make(OWNER, refs([current, m[1], m[2]]))
        out, execution = run(OWNER, newer)
        assert fake.calls == 2
        assert fake.sent[-1] == "Synthetic prototype review notes, revised"
        counts = execution.to_dict()["counts"]
        assert (counts["applied"], counts["cached"]) == (1, 2)


# --- CST-5 key -----------------------------------------------------------------


def test_cst_5_verdict_key_changes(client, channel):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m))
    with model(Counting()) as fake:
        run(OWNER, handle)
        run(OWNER, handle, condition="  prototype   review ")  # same rubric
        assert fake.calls == 1
        run(OWNER, handle, condition="gardening diary")
        assert fake.calls == 2
    with model(Counting(base_version="local-rule-v2")) as other:
        run(OWNER, handle)
        assert other.calls == 1
    keys = {(r["condition_hash"], r["model"], r["operator_version"]) for r in stored()}
    assert len(keys) == 3
    row = stored("WHERE ref_id=%s", (m[0],))[0]
    assert row["text_hash"] == hashlib.sha256(TEXTS[0].encode()).hexdigest()
    assert row["condition_hash"] == hashlib.sha256(b"prototype review").hexdigest()
    assert (row["record_prefix"], row["version"], row["operation"]) == ("m", 1, "match")


# --- CST-10 cost gate ----------------------------------------------------------


def test_cst_10_non_local_over_limit_approval_then_once(client, channel):
    m = seed(client, TEXTS)
    scope = token_scope()
    token = token_row()
    handle = make(scope, refs(m))
    with model(Remote()) as remote:
        before = ledger_rows()
        with pytest.raises(cost.ApprovalRequired) as required:
            run(scope, handle, token=token)
        assert remote.calls == 0 and ledger_rows() == before and stored() == []
        approval = required.value.approval_id
        with connect("app") as c:
            cost.decide(c, approval, "approved", by="synthetic owner")
        # The approval is bound to this request: another condition still asks.
        with pytest.raises(cost.ApprovalRequired):
            run(scope, handle, token=token, condition="gardening", approval_id=approval)
        out, _ = run(scope, handle, token=token, approval_id=approval)
        assert remote.calls == 1 and ledger_rows() == before + 1
        assert {v for v in verdicts(scope, out).values()} <= {
            "match",
            "nonmatch",
            "uncertain",
        }
        # Used once: the same approval cannot run new work, cached work is free.
        cached, _ = run(scope, handle, token=token, approval_id=approval)
        assert remote.calls == 1
        assert all(r["meta"]["cached"] for r in records(scope, cached))

    with model(Remote()) as remote:
        r = client.post(
            "/v1/match",
            json=dict(input=handle, condition="another remote rubric", max_rows=10),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["execution"]["status"] == "approval_required"
        assert body["execution"]["approval"]["approval_id"]
        assert body["records"] == [] and body["result_set"] is None
        assert remote.calls == 0


# --- NULL, not zero ------------------------------------------------------------


@pytest.mark.parametrize("mode", ["raise", "timeout", "invalid"])
def test_qry_33_failed_batch_null_not_uncertain_and_disclosed(client, channel, mode):
    texts = TEXTS + ["Synthetic prototype review again"]
    m = seed(client, texts)
    handle = make(OWNER, refs(m))
    with model(Failing(poison={texts[2]}, mode=mode, batch_size=2)) as fake:
        out, execution = run(OWNER, handle)
    assert fake.calls >= 1
    got = {r["record_id"]: r["meta"] for r in records(OWNER, out)}
    for i in (2, 3):
        meta = got[f"m:{m[i]}"]
        assert meta["verdict"] is None
        assert meta["error"] in ("provider_unavailable", "timeout", "invalid_output")
        assert "reason" not in meta
    assert got[f"m:{m[0]}"]["verdict"] == "match"
    assert got[f"m:{m[1]}"]["verdict"] == "nonmatch"
    wire = execution.to_dict()
    assert wire["counts"]["failed"] == 2
    assert wire["counts"]["semantically_evaluated"] == 2
    assert wire["partial"] is True and wire["status"] == "degraded"
    assert any(i["stage"] == "match" for i in wire["incomplete"])
    assert resultsets.info(OWNER, out)["counts"]["failed"] == 2
    # Nothing failed is stored.
    assert {r["ref_id"] for r in stored()} == {m[0], m[1]}
    # Output completeness is the input's; the failure is disclosed beside it.
    assert wire["completeness"] == "exhaustive"


# --- ACL-8 / CST-6 -------------------------------------------------------------


def test_acl_8_only_visible_text_sent(client, channel):
    register(client, "fake.hidden")
    seen = seed(client, ["Synthetic prototype review visible"])
    hidden = seed(client, ["Synthetic prototype review secret"], channel="fake.hidden")
    guest = restricted("synthetic.chat")
    handle = make(guest, refs(seen) + refs(hidden, version=1))
    with model(Counting()) as fake:
        out, execution = run(guest, handle)
    assert fake.sent == ["Synthetic prototype review visible"]
    assert [r["record_id"] for r in records(guest, out)] == [f"m:{seen[0]}"]
    assert execution.to_dict()["counts"]["retrieved"] == 1
    assert {r["ref_id"] for r in stored()} == {seen[0]}


def test_cst_6_cached_verdict_not_served_for_hidden_record_or_hidden_source(
    client, channel
):
    register(client, "fake.hidden")
    seen = seed(client, ["Synthetic prototype review visible"])
    hidden = seed(client, ["Synthetic prototype review secret"], channel="fake.hidden")
    with model(Counting()) as fake:
        run(OWNER, make(OWNER, refs(seen + hidden)))
        assert fake.calls == 1
        guest = restricted("synthetic.chat")
        out, _ = run(guest, make(guest, refs(seen) + refs(hidden, version=1)))
        # The visible member is served from the store; the hidden one never
        # surfaces, cached or not.
        assert fake.calls == 1
        assert verdicts(guest, out) == {f"m:{seen[0]}": "match"}
        # A stored verdict naming a hidden source is not served: it is judged
        # again under the guest's scope.
        owner(
            "UPDATE tl.verdicts SET source_message_ids=%s WHERE ref_id=%s",
            ([seen[0], hidden[0]], seen[0]),
        )
        again, _ = run(guest, make(guest, refs(seen)))
        assert fake.calls == 2
        assert records(guest, again)[0]["meta"]["cached"] is False


# --- QRY-38, QRY-36 ------------------------------------------------------------


def test_qry_38_match_over_topk_is_ranked_sample(client, channel):
    seed(client, TEXTS)
    response = recall(OWNER, RecallRequest(query="prototype", k=2, save=True))
    assert response.result_set
    with model(Counting()):
        out, execution = run(OWNER, response.result_set)
    assert resultsets.info(OWNER, out)["completeness"] == "ranked_sample"
    assert execution.to_dict()["completeness"] == "ranked_sample"


def test_qry_36_execution_counts(client, channel):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m))
    with model(Counting()):
        run(OWNER, handle)
        out, execution = run(OWNER, make(OWNER, refs(m[:2])), condition="gardening")
    wire = execution.to_dict()
    counts = wire["counts"]
    for key in (
        "retrieved",
        "mechanically_processed",
        "semantically_evaluated",
        "inspected_by_controller",
        "returned",
        "applied",
        "cached",
        "not_evaluable",
        "failed",
        "match",
        "nonmatch",
        "uncertain",
    ):
        assert key in counts, key
    assert counts["semantically_evaluated"] == counts["applied"] + counts["cached"]
    assert counts["returned"] == 2
    stage = [s for s in wire["stages"] if s["name"] == "match"][0]
    assert stage["status"] == "ok" and stage["exact"] is False
    assert wire["match"]["operator_version"].startswith("match-1/")
    info = resultsets.info(OWNER, out)
    assert info["origin"]["op"] == "match" and info["origin"]["parents"]


# --- recorded responses ----------------------------------------------------------


def test_recorded_response_via_override(client, channel):
    m = seed(client, TEXTS)
    recorded = Recorded(
        {
            TEXTS[0]: ("match", [[10, 19]]),
            TEXTS[1]: ("nonmatch", []),
            TEXTS[2]: ("uncertain", []),
        }
    )
    with model(recorded):
        out, _ = run(OWNER, make(OWNER, refs(m)), condition="any rubric")
    assert recorded.calls == 1
    got = {r["record_id"]: r["meta"] for r in records(OWNER, out)}
    assert got[f"m:{m[0]}"]["verdict"] == "match"
    assert got[f"m:{m[0]}"]["evidence"] == [[10, 19]]
    assert got[f"m:{m[0]}"]["model"] == "recorded-model"
    assert got[f"m:{m[1]}"]["verdict"] == "nonmatch"
    assert got[f"m:{m[2]}"]["verdict"] == "uncertain"


# --- purge -------------------------------------------------------------------


def test_purge_hook_removes_verdicts(client, channel):
    register(client, "fake.other")
    m = seed(client, TEXTS)
    other = seed(client, ["Synthetic prototype review elsewhere"], channel="fake.other")
    with model(Counting()):
        run(OWNER, make(OWNER, refs(m + other)))
    assert len(stored()) == 4
    hook = owner("SELECT * FROM tl.purge_hooks WHERE name='verdicts'")
    assert hook and "tl.verdicts" in hook[0]["statement"]
    channel_id = owner("SELECT id FROM tl.channels WHERE name='synthetic.chat'")[0][
        "id"
    ]
    owner("SELECT tl.run_purge_hooks(%s)", (channel_id,))
    assert [r["ref_id"] for r in stored()] == other


# --- INT-10, ARC-6 -----------------------------------------------------------------


def test_int_10_record_keys_equal_recall(client, channel):
    m = seed(client, TEXTS)
    scope = token_scope()
    handle = make(scope, refs(m))
    with model(Counting()):
        r = client.post(
            "/v1/match",
            json=dict(input=handle, condition="prototype review", max_rows=10),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"records", "result_set", "execution"}
    sets = client.get(f"/v1/sets/{body['result_set']}").json()["records"]
    recalled = client.post("/v1/recall", json=dict(query="prototype", k=5)).json()[
        "records"
    ]
    assert recalled
    for record in body["records"]:
        assert set(RECORD_KEYS) <= set(record)
        assert set(record) == set(sets[0])
        assert set(recalled[0]) <= set(record)
    assert body["records"][0]["meta"]["verdict"] == "match"


def table_counts():
    names = [
        r["tablename"]
        for r in owner(
            "SELECT tablename FROM pg_tables WHERE schemaname='tl' ORDER BY 1"
        )
    ]
    return {
        name: owner(
            sql.SQL("SELECT count(*) n FROM {}").format(sql.Identifier("tl", name))
        )[0]["n"]
        for name in names
    }


def test_arc_6_no_ground_truth_writes(client, channel):
    m = seed(client, TEXTS)
    handle = make(OWNER, refs(m))
    before = table_counts()
    payloads = owner(
        "SELECT md5(string_agg(content,'|' ORDER BY id)) h FROM tl.messages"
    )
    with model(Counting()):
        run(OWNER, handle)
    after = table_counts()
    changed = {name for name in after if after[name] != before.get(name)}
    assert changed <= {
        "verdicts",
        "result_sets",
        "result_set_members",
        "result_set_groups",
        "model_ledger",
        "traces",
        "trace_events",
    }, changed
    assert "verdicts" in changed
    assert (
        owner("SELECT md5(string_agg(content,'|' ORDER BY id)) h FROM tl.messages")
        == payloads
    )
    # The reader role cannot touch the verdict store at all.
    allowed = owner(
        "SELECT has_table_privilege('throughline_reader','tl.verdicts','SELECT') ok"
    )[0]["ok"]
    assert allowed is False


# --- HTTP dry run and SQL forms ----------------------------------------------------


def test_match_route_dry_run_and_cli(client, channel):
    m = seed(client, TEXTS)
    handle = make(token_scope(), refs(m))
    with model(Counting()) as fake:
        r = client.post(
            "/v1/match",
            json=dict(input=handle, condition="prototype", max_rows=10, dry_run=True),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["records"] == [] and body["execution"]["dry_run"] is True
    assert body["execution"]["counts"]["pending"] == 3
    assert fake.calls == 0 and stored() == []
    assert match_module.max_rows_limit() == 500


def _semantic_runtime():
    if importlib.util.find_spec("throughline.semantic") is None:
        return None
    module = importlib.import_module("throughline.semantic")
    return module if hasattr(module, "find_calls") else None


def test_wave_integration_sql_forms_over_http(client, channel):
    if _semantic_runtime() is None:
        pytest.skip("semantic SQL runtime (semantic runtime) not present")
    from throughline.semantic_ops import discover

    if not {"expand", "match"} <= set(discover()):
        pytest.skip("semantic operators not discovered")
    m = seed(client, TEXTS)
    handle = make(token_scope(), refs(m[:1]))
    r = client.post(
        "/v1/sql",
        json=dict(
            statement=f"SELECT record_id,role FROM api.expand(input => '{handle}', mode => 'context', before => 2, after => 2) ORDER BY ordinal"
        ),
    )
    assert r.status_code == 200, r.text
    assert {row["record_id"] for row in r.json()["rows"]} == {f"m:{i}" for i in m}
    with model(Counting()):
        r = client.post(
            "/v1/sql",
            json=dict(
                statement="SELECT record_id FROM api.match(condition => 'prototype review', input_sql => $$SELECT message_id AS id FROM api.search_records('prototype', NULL)$$, max_rows => 200) WHERE meta->>'verdict' = 'match'"
            ),
        )
    assert r.status_code == 200, r.text
    assert [row["record_id"] for row in r.json()["rows"]] == [f"m:{m[0]}"]
    # Match over recall: the recall handle is the statement's input.
    recalled = client.post(
        "/v1/recall", json=dict(query="prototype", k=10, save=True)
    ).json()["result_set"]
    with model(Counting()):
        r = client.post(
            "/v1/sql",
            json=dict(
                statement=f"SELECT record_id FROM api.match(condition => 'prototype review', input => '{recalled}', max_rows => 10) WHERE meta->>'verdict' = 'match'"
            ),
        )
    assert r.status_code == 200, r.text
    assert [row["record_id"] for row in r.json()["rows"]] == [f"m:{m[0]}"]


# --- CST-10 in the operator: SQL runtime and plans -------------------------------


def test_cst_10_match_operator_gates_itself_unless_estimated(client, channel):
    """The SQL runtime estimates and authorizes a statement with the context it
    later runs with; any other caller (a plan node whose input comes from an
    earlier node) is gated by the operator itself before any call."""
    from throughline import semantic_ops
    from throughline.semantic_ops import match as match_op

    m = seed(client, TEXTS)
    scope = token_scope()
    token = token_row()
    handle = make(scope, refs(m))
    args = dict(input=handle, condition="prototype review", max_rows=10)

    def ctx():
        return semantic_ops.OpContext(
            scope=scope, token_row=token, tracker=None, execution=Execution()
        )

    with model(Remote()) as remote:
        before = ledger_rows()
        with pytest.raises(cost.ApprovalRequired):
            match_op.OP.run(ctx(), dict(args))
        assert remote.calls == 0 and ledger_rows() == before and stored() == []
        # Estimated with this context: the caller's gate covered it.
        estimated = ctx()
        assert match_op.OP.estimates(estimated, dict(args))
        out = match_op.OP.run(estimated, dict(args))
        assert remote.calls == 1
        assert set(verdicts(scope, out)) == {f"m:{i}" for i in m}
        # Another input than the estimated one is gated again.
        other = make(scope, refs(m[:2]))
        with pytest.raises(cost.ApprovalRequired):
            match_op.OP.run(estimated, dict(args, input=other, condition="other"))
        assert remote.calls == 1


def test_cst_10_plan_match_after_recall_is_gated(client, channel):
    if importlib.util.find_spec("throughline.plans") is None:
        pytest.skip("plans (semantic runtime) not present")
    from throughline import plans

    seed(client, TEXTS)
    scope = token_scope()
    plan = dict(
        nodes=[
            dict(id="r", op="recall", args=dict(query="prototype", k=10)),
            dict(
                id="j",
                op="match",
                args=dict(condition="prototype review", max_rows=10),
                inputs=dict(input="r"),
            ),
        ]
    )
    with model(Remote()) as remote:
        before = ledger_rows()
        result = plans.execute(scope, token_row(), plan)
        assert remote.calls == 0 and ledger_rows() == before and stored() == []
        # The match node asked for approval instead of producing a set of
        # NULL verdicts (the tracker would refuse every call).
        assert not (result.get("handles") or {}).get("j")
        stage = [s for s in result["execution"]["stages"] if s["name"] == "node:j"][0]
        assert stage["status"] == "failed"
    with model(Counting()) as local:
        result = plans.execute(scope, token_row(), plan)
        assert local.calls == 1
        assert result["handles"]["j"]


def test_cst_10_sql_match_approval_then_once(client, channel):
    if _semantic_runtime() is None:
        pytest.skip("semantic SQL runtime (semantic runtime) not present")
    m = seed(client, TEXTS)
    handle = make(token_scope(), refs(m))
    statement = f"SELECT record_id FROM api.match(condition => 'prototype review', input => '{handle}', max_rows => 10) WHERE meta->>'verdict' = 'match'"
    with model(Remote()) as remote:
        r = client.post("/v1/sql", json=dict(statement=statement))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["execution"]["status"] == "approval_required"
        assert remote.calls == 0
        approval = body["execution"]["approval"]["approval_id"]
        with connect("app") as c:
            cost.decide(c, approval, "approved", by="synthetic owner")
        r = client.post("/v1/sql", json=dict(statement=statement, approval_id=approval))
        assert r.status_code == 200, r.text
        assert remote.calls == 1
        assert [row["record_id"] for row in r.json()["rows"]] == [f"m:{m[0]}"]
