"""v1.1 model providers: offline defaults, egress, ledger, cache, cost gate.

Everything runs offline: deterministic local providers, and recorded responses
served from a loopback stub for the HTTP providers.
"""

import json
import math
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from conftest import ingest, message
from throughline import cost, embeddings, providers
from throughline.budget import Budget, BudgetTracker, clamp
from throughline.db import connect, create_token
from throughline.execution import Trace
from throughline.providers import cache, egress, http, local
from throughline.providers.base import (
    EgressDenied,
    Estimate,
    ProviderUnavailable,
    Usage,
)
from throughline.scope import Scope

OWNER = Scope.local()


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    """No provider configuration leaks in from the machine running the tests."""
    import os

    for name in list(os.environ):
        if name.startswith("THROUGHLINE_") and any(
            part in name
            for part in (
                "EMBEDDING_",
                "RERANK_",
                "SEMANTIC_",
                "CONTROLLER_",
                "MODEL_",
                "EGRESS_",
                "LOCAL_SYNONYMS",
            )
        ):
            monkeypatch.delenv(name)


def restricted(asker="stranger", token_id=None):
    return Scope(
        token_id=token_id,
        token_kind="agent",
        asker=asker,
        mode="asker",
        unknown_asker="deny",
    )


def count(table, where="true", args=()):
    with connect() as c:
        return c.execute(
            f"SELECT count(*) n FROM tl.{table} WHERE {where}", args
        ).fetchone()["n"]


def token_row(token_id):
    with connect() as c:
        return c.execute("SELECT * FROM tl.tokens WHERE id=%s", (token_id,)).fetchone()


def remote_estimate(amount=0.5, purpose="match"):
    return Estimate(
        purpose=purpose,
        provider="http",
        model="recorded-model",
        calls=2,
        tokens=1000,
        cost=amount,
        local=False,
    )


@pytest.fixture
def outbound(monkeypatch):
    """Records every attempt to open a model connection or resolve a name."""
    seen = []
    real = http.client

    def client(timeout):
        seen.append(("client", timeout))
        return real(timeout)

    def getaddrinfo(host, *args, **kwargs):
        seen.append(("resolve", host))
        raise socket.gaierror("name resolution is not available in tests")

    real_connect = socket.socket.connect

    def connect_(self, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            seen.append(("connect", address[0]))
            raise OSError("outbound connections are not available in tests")
        return real_connect(self, address)

    monkeypatch.setattr(http, "client", client)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect_)
    return seen


@pytest.fixture
def recorded():
    """Loopback stub that answers each path with a recorded JSON response."""

    class Stub:
        responses = {}
        requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Stub.requests.append((self.path, body, self.headers.get("Authorization")))
            reply = Stub.responses.get(self.path)
            if callable(reply):
                reply = reply(body)
            if reply is None:
                self.send_response(404)
                self.end_headers()
                return
            data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Stub.base = f"http://127.0.0.1:{server.server_port}/v1"
    yield Stub
    server.shutdown()
    server.server_close()
    thread.join()


# --- selection and offline defaults ------------------------------------------


def test_arc_4_offline_defaults(monkeypatch):
    assert isinstance(providers.get("rerank"), local.LocalLexicalReranker)
    assert isinstance(providers.get("semantic"), local.LocalRuleModel)
    assert isinstance(providers.get("controller"), local.LocalPlanController)
    with pytest.raises(ProviderUnavailable, match="embeddings unavailable: configure"):
        providers.get("embedding")
    # The same condition through the module the search routes use.
    with pytest.raises(ValueError, match="unavailable"):
        embeddings.settings()
    for kind in ("rerank", "semantic", "controller", "assistant"):
        provider = providers.get(kind)
        assert provider.local and provider.name == "local" and provider.version
    rows = {r["kind"]: r for r in providers.describe()}
    assert set(rows) == {"embedding", "rerank", "semantic", "controller", "assistant"}
    assert rows["embedding"]["provider"] == "none"
    assert all(r["local"] and not r["excerpts_leave_host"] for r in rows.values())

    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    embedder = providers.get("embedding")
    assert isinstance(embedder, local.LocalHashEmbedder) and embedder.dimensions == 256
    assert embeddings.settings() == ("local", "local-hash-v1", 256)

    monkeypatch.setenv("THROUGHLINE_SEMANTIC_PROVIDER", "none")
    with pytest.raises(ProviderUnavailable):
        providers.get("semantic")
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_PROVIDER", "http")
    with pytest.raises(ProviderUnavailable, match="THROUGHLINE_SEMANTIC_BASE_URL"):
        providers.get("semantic")
    # semantic and controller share THROUGHLINE_MODEL_* unless set per kind.
    monkeypatch.setenv("THROUGHLINE_MODEL_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("THROUGHLINE_MODEL_MODEL", "shared-model")
    monkeypatch.setenv("THROUGHLINE_MODEL_PRICE_PER_MILLION", "3")
    semantic = providers.get("semantic")
    assert isinstance(semantic, http.HttpSemanticModel)
    assert (semantic.model, semantic.price_per_million) == ("shared-model", 3.0)
    assert not semantic.local and not semantic.excerpts_leave_host
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_MODEL", "own-model")
    assert providers.get("semantic").model == "own-model"
    monkeypatch.setenv("THROUGHLINE_RERANK_PROVIDER", "cloud")
    with pytest.raises(ValueError, match="one of local, http, none"):
        providers.get("rerank")
    with pytest.raises(ValueError, match="provider kind"):
        providers.get("judge")


def test_override_is_scoped_and_can_disable():
    class Scripted(providers.Reranker):
        name, version, model = "scripted", "1", "scripted"

        def score(self, query, passages):
            return [1.0] * len(passages), self._usage(0)

    scripted = Scripted()
    with providers.override("rerank", scripted) as active:
        assert active is scripted and providers.get("rerank") is scripted
        with providers.override("rerank", None):
            with pytest.raises(ProviderUnavailable):
                providers.get("rerank")
        assert providers.get("rerank") is scripted
    assert isinstance(providers.get("rerank"), local.LocalLexicalReranker)


def test_int_11_no_outbound_calls_by_default(client, outbound):
    reranker = providers.get("rerank")
    scores, _ = providers.call(
        "rerank",
        "rerank",
        lambda: reranker.score("orchard gate", ["the orchard gate", "a stone wall"]),
        estimate=reranker.estimate("orchard gate", ["the orchard gate", "a wall"]),
        scope=OWNER,
    )
    assert scores[0] > scores[1]
    model = providers.get("semantic")
    items = [dict(id="m:1", text="the orchard gate")]
    results, _ = providers.call(
        "semantic",
        "match",
        lambda: model.apply("match", "orchard", items, None),
        estimate=model.estimate("match", "orchard", items),
        scope=OWNER,
    )
    assert results[0]["output"] == "match"
    controller = providers.get("controller")
    action, _ = providers.call(
        "controller",
        "controller",
        lambda: controller.turn("brief", {"goal": "orchard"}),
        estimate=controller.estimate("brief", {"goal": "orchard"}),
        scope=OWNER,
    )
    assert "plan" in action
    # Meaning search stays unavailable instead of reaching for an endpoint.
    response = client.get("/v1/search", params=dict(q="orchard", mode="meaning"))
    assert response.status_code == 400 and "unavailable" in response.text
    assert outbound == []
    with connect() as c:
        rows = c.execute(
            "SELECT provider,cost,origin FROM tl.model_ledger ORDER BY id"
        ).fetchall()
    assert len(rows) == 3
    assert all(r["provider"] == "local" and r["cost"] == 0 for r in rows)


# --- local implementations ---------------------------------------------------


def test_local_embedder_deterministic_normalised_synonyms(monkeypatch, tmp_path):
    embedder = local.LocalHashEmbedder()
    texts = ["The orchard gate was open", "Ocean stone", ""]
    first, usage = embedder.embed(texts)
    second, _ = local.LocalHashEmbedder().embed(texts)
    assert first == second
    assert isinstance(usage, Usage) and usage.cost == 0 and usage.tokens > 0
    assert usage.model == "local-hash-v1"
    assert all(len(v) == 256 for v in first)
    for v in first:
        assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0)
    # Case and punctuation do not matter; word order does (bigrams).
    assert embedder.embed(["the ORCHARD gate, was open!"])[0][0] == first[0]
    assert embedder.embed(["gate orchard the open was"])[0][0] != first[0]
    assert len(embedder.embed(["orchard"], dimensions=16)[0][0]) == 16
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "32")
    assert local.LocalHashEmbedder().dimensions == 32

    def cosine(a, b):
        va, vb = embedder.embed([a, b])[0]
        return sum(x * y for x, y in zip(va, vb))

    plain = cosine("we walked to the grove", "we walked to the orchard")
    assert cosine("grove", "orchard") < 0.5
    path = tmp_path / "synonyms.json"
    path.write_text(json.dumps({"orchard": ["grove", "plantation"]}), "utf-8")
    monkeypatch.setenv("THROUGHLINE_LOCAL_SYNONYMS", str(path))
    assert cosine("grove", "orchard") == pytest.approx(1.0)
    assert cosine("we walked to the grove", "we walked to the orchard") > plain
    # The two other accepted shapes.
    monkeypatch.setenv("THROUGHLINE_LOCAL_SYNONYMS", '{"grove": "orchard"}')
    assert cosine("grove", "orchard") == pytest.approx(1.0)
    monkeypatch.setenv("THROUGHLINE_LOCAL_SYNONYMS", '[["orchard", "grove"]]')
    assert cosine("grove", "orchard") == pytest.approx(1.0)
    monkeypatch.setenv("THROUGHLINE_LOCAL_SYNONYMS", str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="THROUGHLINE_LOCAL_SYNONYMS"):
        embedder.embed(["grove"])


def test_local_reranker_order():
    reranker = local.LocalLexicalReranker()
    passages = [
        "nothing relevant here",  # 0
        "the gate of the orchard",  # 2/2 tokens, no bigram
        "an orchard gate stood open",  # 2/2 tokens + bigram
        "orchard only",  # 1/2
        "only the orchard",  # 1/2, tie with the one before
        "gate after gate",  # 1/2, same tie
    ]
    scores, usage = reranker.score("orchard gate", passages)
    assert scores == pytest.approx([0.0, 1.0, 1.1, 0.5, 0.5, 0.5])
    assert usage.cost == 0 and usage.calls == 1
    order, _ = reranker.rank("orchard gate", passages)
    # Equal scores keep their input order.
    assert order == [2, 1, 3, 4, 5, 0]
    assert reranker.score("", passages)[0] == [0.0] * 6
    assert reranker.score("orchard", [])[0] == []
    estimate = reranker.estimate("orchard gate", passages)
    assert estimate.local and estimate.cost == 0 and estimate.provider == "local"


def test_local_match_three_values_with_offsets():
    model = local.LocalRuleModel()
    text = "We met at the Orchard gate; the orchard was quiet."
    items = [
        dict(id="m:1", text=text),
        dict(id="m:2", text="A stone wall by the ocean"),
        dict(id="m:3", text="The gate was locked"),
    ]
    results, usage = model.apply(
        "match", "mentions the orchard gate", items, {"enum": ["match"]}
    )
    assert [r["id"] for r in results] == ["m:1", "m:2", "m:3"]
    assert [r["output"] for r in results] == ["match", "nonmatch", "uncertain"]
    # First occurrence of each content word, as character offsets.
    assert results[0]["evidence"] == [[14, 21], [22, 26]]
    assert [text[a:b].lower() for a, b in results[0]["evidence"]] == [
        "orchard",
        "gate",
    ]
    assert results[1]["evidence"] == [] and results[2]["evidence"] == []
    assert all(isinstance(r["note"], str) for r in results)
    assert usage.cost == 0 and usage.model == "local-rule-v1"
    # A condition without content words decides nothing.
    assert model.apply("match", "is it the", items[:1])[0][0]["output"] == "uncertain"
    assert (
        model.apply("match", "orchard", items)[0]
        == model.apply("match", "orchard", items)[0]
    )


def test_local_plan_controller_is_data_driven(monkeypatch):
    controller = local.LocalPlanController()
    first, usage = controller.turn("brief", {"goal": "find the orchard", "turn": 0})
    assert first["plan"]["ops"][0]["query"] == "find the orchard"
    assert usage.cost == 0
    second, _ = controller.turn("brief", {"turn": 1, "last_handle": "rs_1"})
    assert second == {"deliver": {"selection": "rs_1", "notes": [], "unresolved": []}}
    # Later turns repeat the last entry; the plan itself is never mutated.
    assert "deliver" in controller.turn("brief", {"turn": 9})[0]
    assert local.PLAN[0]["plan"]["ops"][0]["query"] == "$goal"
    monkeypatch.setattr(local, "PLAN", ({"deliver": {"selection": "$handle"}},))
    assert controller.turn("brief", {"handle": "rs_2"})[0] == {
        "deliver": {"selection": "rs_2"}
    }


def test_semantic_interface_rejects_unregistered_operation(outbound):
    remote = http.HttpSemanticModel("http://127.0.0.1:9/v1", "recorded-model")
    for model in (local.LocalRuleModel(), remote):
        assert model.operations == ("match",)
        for operation in ("extract", "compare", "relate", "summarize"):
            with pytest.raises(ValueError, match="reserved"):
                model.apply(operation, "x", [dict(id=1, text="t")], None)
            with pytest.raises(ValueError, match="reserved"):
                model.estimate(operation, "x", [dict(id=1, text="t")])
        with pytest.raises(ValueError, match="not registered"):
            model.apply("translate", "x", [dict(id=1, text="t")], None)
    # Refused before any request is prepared.
    assert outbound == []


# --- egress ------------------------------------------------------------------


def test_lib_6_egress_denied_before_socket(monkeypatch, outbound):
    remote = "https://models.example.invalid/v1"
    built = [
        http.HttpEmbedder(remote, "m", 3),
        http.HttpReranker(remote, "m"),
        http.HttpSemanticModel(remote, "m"),
        http.HttpController(remote, "m"),
    ]
    assert all(p.excerpts_leave_host and not p.local for p in built)
    calls = [
        lambda: built[0].embed(["orchard"]),
        lambda: built[1].score("orchard", ["orchard"]),
        lambda: built[2].apply("match", "orchard", [dict(id=1, text="orchard")]),
        lambda: built[3].turn("brief", {}),
    ]
    for run in calls:
        with pytest.raises(EgressDenied, match="THROUGHLINE_EGRESS_ALLOW"):
            run()
    # A host that only looks similar, another port, and odd URLs stay refused.
    monkeypatch.setenv(
        "THROUGHLINE_EGRESS_ALLOW", "example.invalid, other.invalid:8443"
    )
    for url in (
        remote,
        "https://other.invalid/v1",
        "https://other.invalid:9000/v1",
        "https://models.example.invalid.evil.invalid/v1",
        "ftp://example.invalid/v1",
        "https://user:secret@example.invalid/v1",
        "https:///v1",
    ):
        with pytest.raises(EgressDenied):
            egress.check(url)
    for run in calls:
        with pytest.raises(EgressDenied):
            run()
    assert outbound == []
    # Denial is a ProviderUnavailable, so callers degrade the same way.
    assert issubclass(EgressDenied, ProviderUnavailable)
    assert issubclass(ProviderUnavailable, ValueError)

    # Through the call wrapper: nothing spent, nothing in the ledger.
    with connect() as c:
        before = c.execute("SELECT count(*) n FROM tl.model_ledger").fetchone()["n"]
    tracker = BudgetTracker(Budget(model_cost=1.0, model_calls=5))
    with pytest.raises(EgressDenied):
        providers.call(
            "rerank",
            "rerank",
            calls[1],
            estimate=remote_estimate(0.2, "rerank"),
            scope=OWNER,
            tracker=tracker,
        )
    assert tracker.snapshot()["spent"]["model_calls"] == 0
    assert count("model_ledger") == before
    assert outbound == []


def test_lib_6_egress_allowed_for_listed_host_and_loopback(monkeypatch, recorded):
    for url in (
        "http://127.0.0.1:9/v1",
        "http://127.8.9.10/v1",
        "http://localhost:11434/v1",
        "http://[::1]:8000/v1",
    ):
        assert egress.check(url) == url and not egress.leaves_host(url)
    monkeypatch.setenv(
        "THROUGHLINE_EGRESS_ALLOW", "Models.Example.invalid other.invalid:8443"
    )
    assert egress.check("https://models.example.invalid/v1/embeddings")
    assert egress.check("https://models.example.invalid:444/v1")
    assert egress.check("https://other.invalid:8443/v1")
    assert egress.leaves_host("https://models.example.invalid/v1")

    # A listed host is contacted (the transport is replaced, nothing leaves).
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
                "usage": {"total_tokens": 5},
            },
        )

    monkeypatch.setattr(
        http,
        "client",
        lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    embedder = http.HttpEmbedder(
        "https://models.example.invalid/v1",
        "m",
        3,
        key="synthetic-key",
        price_per_million=2,
    )
    vectors, usage = embedder.embed(["orchard"])
    assert vectors == [[1.0, 0.0, 0.0]]
    assert (usage.tokens, usage.cost) == (5, pytest.approx(0.00001))
    assert seen == [("models.example.invalid", "Bearer synthetic-key")]
    monkeypatch.undo()

    # Loopback needs no entry: a real request to the stub.
    recorded.responses["/v1/rerank"] = {"results": [{"index": 0, "score": 0.5}]}
    assert http.HttpReranker(recorded.base, "m").score("q", ["p"])[0] == [0.5]


def test_lib_6_egress_parsers_must_agree(monkeypatch, outbound):
    """The checked host is the one httpx would contact, or the URL is refused."""
    monkeypatch.setenv("THROUGHLINE_EGRESS_ALLOW", "models.example.invalid")
    for url in (
        "http://evil.invalid\\@127.0.0.1/v1",
        "http://127.0.0.1\\@evil.invalid/v1",
        "http://127.0.0.1\t.evil.invalid/v1",
        "http://127.0.0.1:80@evil.invalid/v1",
        "https://models.example.invalid\\@evil.invalid/v1",
        "http://127.0.0.1:99999/v1",
    ):
        with pytest.raises(EgressDenied):
            egress.check(url)
        assert egress.leaves_host(url)
    assert egress.check("HTTPS://Models.Example.invalid/v1")
    assert egress.target("http://[::1]:8000/v1") == ("::1", 8000)
    assert outbound == []


def test_shared_model_key_stays_with_shared_endpoint(monkeypatch):
    """THROUGHLINE_MODEL_KEY is never sent to another kind-specific endpoint."""
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_PROVIDER", "http")
    monkeypatch.setenv("THROUGHLINE_CONTROLLER_PROVIDER", "http")
    monkeypatch.setenv("THROUGHLINE_MODEL_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("THROUGHLINE_MODEL_MODEL", "shared-model")
    monkeypatch.setenv("THROUGHLINE_MODEL_KEY", "synthetic-shared-key")
    assert providers.get("semantic").key == "synthetic-shared-key"
    # Same endpoint written per kind (trailing slash aside): the key follows.
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_BASE_URL", "http://127.0.0.1:9/v1/")
    assert providers.get("semantic").key == "synthetic-shared-key"
    # Another endpoint: no key unless its own is configured.
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_BASE_URL", "http://127.0.0.1:10/v1")
    assert providers.get("semantic").key is None
    assert providers.get("controller").key == "synthetic-shared-key"
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_KEY", "synthetic-own-key")
    assert providers.get("semantic").key == "synthetic-own-key"


def test_http_providers_parse_recorded_responses(recorded, monkeypatch):
    recorded.requests.clear()
    recorded.responses.update(
        {
            "/v1/embeddings": {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ],
                "usage": {"total_tokens": 9},
            },
            "/v1/rerank": {
                "results": [
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                    {"index": 1, "relevance_score": 0.4},
                ],
                "usage": {"total_tokens": 30},
            },
        }
    )
    embedder = http.HttpEmbedder(
        recorded.base, "recorded-embed", 2, key="synthetic-key", price_per_million=10
    )
    vectors, usage = embedder.embed(["first", "second"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert (usage.model, usage.tokens, usage.calls) == ("recorded-embed", 9, 1)
    assert usage.cost == pytest.approx(0.00009)
    path, body, authorization = recorded.requests[-1]
    assert path == "/v1/embeddings" and authorization == "Bearer synthetic-key"
    assert body == {
        "model": "recorded-embed",
        "input": ["first", "second"],
        "dimensions": 2,
    }
    estimate = embedder.estimate(["first", "second"])
    assert not estimate.local and estimate.cost > 0 and estimate.provider == "http"

    reranker = http.HttpReranker(recorded.base, "recorded-rerank")
    scores, usage = reranker.score("q", ["a", "b", "c"])
    assert scores == [0.1, 0.4, 0.9] and usage.tokens == 30 and usage.cost == 0
    assert recorded.requests[-1][2] is None
    assert reranker.rank("q", ["a", "b", "c"])[0] == [2, 1, 0]

    def chat(content, tokens=40):
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"total_tokens": tokens},
        }

    text = "the orchard gate"
    recorded.responses["/v1/chat/completions"] = chat(
        json.dumps(
            {
                "results": [
                    {"id": "m:2", "output": "nonmatch", "evidence": [], "note": "no"},
                    {
                        "id": "m:1",
                        "output": "match",
                        # The second span is outside the text and is dropped.
                        "evidence": [[4, 11], [90, 99], "junk"],
                        "note": "names the orchard",
                    },
                    {"id": "m:9", "output": "match"},  # Not an item that was sent.
                    {"id": "m:4", "output": "definitely"},  # Not a valid output.
                ]
            }
        )
    )
    model = http.HttpSemanticModel(recorded.base, "recorded-chat", price_per_million=5)
    items = [
        dict(id="m:1", text=text),
        dict(id="m:2", text="stone"),
        dict(id="m:3", text="left out by the model"),
        dict(id="m:4", text="invalid output"),
    ]
    results, usage = model.apply("match", "mentions an orchard", items, None)
    assert results == [
        dict(id="m:1", output="match", evidence=[[4, 11]], note="names the orchard"),
        dict(id="m:2", output="nonmatch", evidence=[], note="no"),
        dict(
            id="m:3",
            output="uncertain",
            evidence=[],
            note="the model gave no valid answer for this item",
        ),
        dict(
            id="m:4",
            output="uncertain",
            evidence=[],
            note="the model gave no valid answer for this item",
        ),
    ]
    assert (usage.tokens, usage.calls) == (40, 1)
    assert usage.cost == pytest.approx(0.0002)
    path, body, _ = recorded.requests[-1]
    assert path == "/v1/chat/completions"
    assert body["response_format"] == {"type": "json_object"}
    # Excerpts travel in a data field, apart from the instructions.
    sent = json.loads(body["messages"][1]["content"])
    assert sent["instruction"] == "mentions an orchard"
    assert sent["data"][0] == {"id": "m:1", "text": text}
    assert text not in body["messages"][0]["content"]
    # One request per batch.
    model.batch_size = 2
    assert model.estimate("match", "x", items).calls == 2
    assert model.apply("match", "x", items, None)[1].calls == 2

    controller = http.HttpController(recorded.base, "recorded-chat")
    recorded.responses["/v1/chat/completions"] = chat(
        json.dumps({"deliver": {"selection": "rs_1"}})
    )
    action, usage = controller.turn("brief", {"turn": 1})
    assert action == {"deliver": {"selection": "rs_1"}} and usage.tokens == 40

    # Anything else is an unavailable provider, never a partial answer.
    for content in ("not json", "[1]", json.dumps({"answer": 1}), json.dumps({})):
        recorded.responses["/v1/chat/completions"] = chat(content)
        with pytest.raises(ProviderUnavailable):
            controller.turn("brief", {})
    recorded.responses["/v1/chat/completions"] = chat(json.dumps({"results": "no"}))
    with pytest.raises(ProviderUnavailable):
        model.apply("match", "x", items, None)
    recorded.responses["/v1/chat/completions"] = b"<html>busy</html>"
    with pytest.raises(ProviderUnavailable):
        model.apply("match", "x", items, None)
    recorded.responses["/v1/embeddings"] = {"data": [{"index": 0, "embedding": [1.0]}]}
    with pytest.raises(ProviderUnavailable, match="invalid embedding response"):
        embedder.embed(["first", "second"])
    recorded.responses["/v1/embeddings"] = None
    with pytest.raises(ProviderUnavailable, match="embedding endpoint unavailable"):
        embedder.embed(["first"])
    recorded.responses["/v1/rerank"] = {"results": [{"index": 7, "score": 1}]}
    with pytest.raises(ProviderUnavailable, match="invalid rerank response"):
        reranker.score("q", ["a"])

    # Selection builds the same classes from the environment.
    monkeypatch.setenv("THROUGHLINE_RERANK_PROVIDER", "http")
    monkeypatch.setenv("THROUGHLINE_RERANK_BASE_URL", recorded.base)
    monkeypatch.setenv("THROUGHLINE_RERANK_MODEL", "recorded-rerank")
    recorded.responses["/v1/rerank"] = {"results": [{"index": 0, "score": 0.25}]}
    assert providers.get("rerank").score("q", ["a"])[0] == [0.25]
    row = {r["kind"]: r for r in providers.describe()}["rerank"]
    assert row == dict(
        kind="rerank",
        provider="http",
        model="recorded-rerank",
        local=False,
        excerpts_leave_host=False,
    )


# --- query embedding: ledger and cache ---------------------------------------


def serving(model, dimensions=3):
    with connect() as c:
        c.execute("UPDATE tl.embedding_models SET active=false")
        c.execute(
            "INSERT INTO tl.embedding_models(model,dimensions,active) VALUES(%s,%s,true) ON CONFLICT(model) DO UPDATE SET active=true",
            (model, dimensions),
        )


@pytest.fixture
def counting_embed(monkeypatch):
    """A fake with the documented contract, assigned to embeddings.embed."""
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    calls = []

    def embed(texts, model=None, dimensions=None):
        calls.append((list(texts), model, dimensions))
        marker = 1.0 if model == "serving-a" else 2.0
        return [[marker, 0.0, 0.0] for _ in texts], 7

    monkeypatch.setattr(embeddings, "embed", embed)
    return calls


def test_cst_3_query_embedding_ledger_row(client, counting_embed):
    serving("serving-a")
    token_id = create_token("synthetic agent", kind="agent")["id"]
    scope = Scope.from_token(token_row(token_id))
    trace = Trace(scope, "test")
    vector, model = embeddings.embed_search(
        "where is the orchard", with_model=True, scope=scope, trace=trace
    )
    assert (vector, model) == ([1.0, 0.0, 0.0], "serving-a")
    assert counting_embed == [(["where is the orchard"], "serving-a", 3)]
    with connect() as c:
        rows = c.execute("SELECT * FROM tl.model_ledger").fetchall()
        spend = c.execute("SELECT * FROM tl.spend").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["purpose"], row["provider"], row["model"], row["origin"]) == (
        "query_embedding",
        "local",
        "serving-a",
        "query",
    )
    assert (row["tokens"], row["calls"], row["cost"]) == (7, 1, 0)
    assert row["token_id"] == token_id and str(row["trace_id"]) == trace.id
    assert len(spend) == 1 and spend[0]["purpose"] == "query_embedding"
    event = trace.events[-1]
    assert (event["op"], event["status"], event["tokens"]) == (
        "model.embedding",
        "ok",
        7,
    )
    # The route's default (no scope given) is the in-process owner scope.
    assert embeddings.embed_search("another question") == [1.0, 0.0, 0.0]
    assert count("model_ledger", "token_id IS NULL") == 1
    # The worker's batches stay in their own ledger; tl.spend shows both.
    with connect() as c:
        c.execute(
            "INSERT INTO tl.embedding_ledger(model,tokens,cost) VALUES('m',10,0.5)"
        )
        totals = c.execute(
            "SELECT count(*) n,sum(cost) cost FROM tl.spend WHERE origin='worker'"
        ).fetchone()
    assert (totals["n"], float(totals["cost"])) == (1, 0.5)


def test_cst_5_query_embedding_cache_hit_no_call(client, counting_embed):
    serving("serving-a")
    first = embeddings.embed_search("where is the orchard")
    assert embeddings.embed_search("where is the orchard") == first == [1.0, 0.0, 0.0]
    assert len(counting_embed) == 1 and count("model_ledger") == 1
    # A restricted asker's own query text is no one's hidden content.
    assert embeddings.embed_search("where is the orchard", scope=restricted()) == first
    assert len(counting_embed) == 1
    assert embeddings.embed_search("where is the ocean") == first
    assert len(counting_embed) == 2
    # The entry keeps a hash of the query and the vector, never the text.
    with connect() as c:
        rows = c.execute("SELECT * FROM tl.semantic_cache").fetchall()
    assert len(rows) == 2
    assert all(
        r["operator"] == "query_embedding"
        and r["model"] == "serving-a"
        and r["source_message_ids"] == []
        and r["expires_at"] is not None
        and "orchard" not in json.dumps(r, default=str)
        for r in rows
    )

    # Model handover: the vector of the previous serving model is not reused.
    serving("serving-b")
    vector, model = embeddings.embed_search("where is the orchard", with_model=True)
    assert (vector, model) == ([2.0, 0.0, 0.0], "serving-b")
    assert counting_embed[-1] == (["where is the orchard"], "serving-b", 3)
    assert len(counting_embed) == 3
    assert embeddings.embed_search("where is the orchard") == [2.0, 0.0, 0.0]
    assert len(counting_embed) == 3
    # Switching back finds the first model's entry again.
    serving("serving-a")
    assert embeddings.embed_search("where is the orchard") == [1.0, 0.0, 0.0]
    assert len(counting_embed) == 3
    # The key also separates provider, dimensions and text.
    keys = {
        cache.key("query_embedding", "v1", "serving-a", "local", 3, "h"),
        cache.key("query_embedding", "v1", "serving-b", "local", 3, "h"),
        cache.key("query_embedding", "v1", "serving-a", "http", 3, "h"),
        cache.key("query_embedding", "v1", "serving-a", "local", 4, "h"),
        cache.key("query_embedding", "v2", "serving-a", "local", 3, "h"),
        cache.key("query_embedding", "v1", "serving-a", "local", 3, "i"),
    }
    assert len(keys) == 6
    with pytest.raises(ValueError):
        cache.key("query_embedding", "v1", "serving-a")


def test_query_embedding_uses_interactive_timeout(client, monkeypatch, recorded):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_BASE_URL", recorded.base)
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "recorded-embed")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "3")
    recorded.responses["/v1/embeddings"] = lambda body: {
        "data": [
            {"index": i, "embedding": [1.0, 2.0, 3.0]}
            for i in range(len(body["input"]))
        ],
        "usage": {"total_tokens": 1000},
    }
    timeouts = []
    real = http.client

    def client_(timeout):
        timeouts.append(timeout)
        return real(timeout)

    monkeypatch.setattr(http, "client", client_)
    serving("recorded-embed")
    assert embeddings.embed_search("orchard") == [1.0, 2.0, 3.0]
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_QUERY_TIMEOUT_S", "2.5")
    assert embeddings.embed_search("ocean") == [1.0, 2.0, 3.0]
    vectors, tokens = embeddings.embed(["a batch"])
    assert (len(vectors), tokens) == (1, 1000)
    assert timeouts == [5.0, 2.5, 60]
    # Remote query embeddings are priced like the worker's batches.
    with connect() as c:
        costs = [
            float(r["cost"])
            for r in c.execute("SELECT cost FROM tl.model_ledger ORDER BY id")
        ]
    assert costs == [pytest.approx(0.00002)] * 2
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_QUERY_TIMEOUT_S", "0")
    with pytest.raises(ValueError, match="QUERY_TIMEOUT_S"):
        embeddings.embed_search("stone")


def test_embed_monkeypatch_contract(client, channel, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "3")
    # The real function has the documented shape: (vectors, int).
    vectors, tokens = embeddings.embed(["orchard", "ocean"])
    assert len(vectors) == 2 and all(len(v) == 3 for v in vectors)
    assert isinstance(tokens, int) and not isinstance(tokens, bool)
    assert embeddings.embed(["orchard"], "any-model", 5)[0][0] != vectors[0]
    assert len(embeddings.embed(["orchard"], "any-model", 5)[0][0]) == 5

    ingest(client, [message(0), message(4000, content="ocean")])
    seen = []

    def fake(texts, model=None, dimensions=None):
        seen.append(list(texts))
        return [[0.0, 1.0, 0.0] for _ in texts], 11

    monkeypatch.setattr(embeddings, "embed", fake)
    assert embeddings.run_once() == 2
    assert len(seen) == 1 and len(seen[0]) == 2
    with connect() as c:
        stored = c.execute(
            "SELECT DISTINCT embedding::text e,model FROM tl.embeddings"
        ).fetchall()
        ledger = c.execute("SELECT * FROM tl.embedding_ledger").fetchall()
    assert stored == [dict(e="[0,1,0]", model="local-hash-v1")]
    # The fake's token count is what the batch ledger records; local is free.
    assert [(r["tokens"], r["cost"]) for r in ledger] == [(11, 0)]


def test_embedding_provider_local_end_to_end(client, channel, monkeypatch, outbound):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "64")
    ingest(
        client,
        [
            message(0, content="we walked through the orchard at dawn"),
            message(4000, content="the ocean was loud that night"),
            message(8000, content="a stone wall along the road"),
        ],
    )
    assert embeddings.run_once() == 3
    assert embeddings.run_once() == 0
    with connect() as c:
        assert c.execute(
            "SELECT model,dimensions FROM tl.embedding_models WHERE active"
        ).fetchone() == dict(model="local-hash-v1", dimensions=64)
        assert (
            float(
                c.execute("SELECT sum(cost) s FROM tl.embedding_ledger").fetchone()["s"]
            )
            == 0
        )
    for query, word in (("the ocean was loud", "ocean"), ("stone wall", "stone")):
        response = client.get("/v1/search", params=dict(q=query, mode="meaning"))
        assert response.status_code == 200, response.text
        assert word in response.json()["rows"][0]["content"]
    response = client.post("/v1/embed", json={"text": "the ocean was loud"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "local-hash-v1" and len(body["embedding"]) == 64
    assert (
        body["embedding"]
        == local.LocalHashEmbedder(64).embed(["the ocean was loud"])[0][0]
    )
    fused = client.get("/v1/search", params=dict(q="orchard", mode="fused"))
    assert fused.status_code == 200 and fused.json()["row_count"] >= 1
    # Two distinct query texts were embedded; the repeats came from the cache.
    assert count("model_ledger", "purpose='query_embedding'") == 3
    assert count("model_ledger", "cost<>0") == 0
    assert outbound == []


# --- cache -------------------------------------------------------------------


def test_cst_6_cache_get_requires_scope_and_visible_sources(client, channel):
    ingest(client, [message(0), message(1)])
    client.post(
        "/v1/channels",
        json=dict(name="synthetic.hidden", plugin="synthetic", fields=[]),
    )
    ingest(client, [message(2, channel="synthetic.hidden", fields={})])
    with connect() as c:
        ids = {
            r["name"]: r["ids"]
            for r in c.execute(
                "SELECT ch.name,array_agg(m.id ORDER BY m.id) ids FROM tl.messages m JOIN tl.channels ch ON ch.id=m.channel_id GROUP BY ch.name"
            )
        }
    seen, hidden = ids["synthetic.chat"], ids["synthetic.hidden"]

    def entry(name, sources=(), ttl_s=None):
        key = cache.key("match", "v1", "local-rule-v1", name)
        assert cache.put(
            key, "match", "v1", "local-rule-v1", name, {"verdict": name}, sources, ttl_s
        )
        return key

    free = entry("no sources")
    visible = entry("visible", seen)
    mixed = entry("mixed", seen + hidden)
    with pytest.raises(TypeError):
        cache.get(visible)
    with pytest.raises(ValueError, match="scope"):
        cache.get(visible, None)

    assert cache.get(visible, OWNER) == {"verdict": "visible"}
    assert cache.get(mixed, scope=OWNER) == {"verdict": "mixed"}
    assert cache.get(cache.key("match", "v1", "m", "absent"), OWNER) is None

    from throughline import access

    access.register_asker("reader-a", "restricted")
    access.grant("reader-a", channel="synthetic.chat")
    granted, stranger = restricted("reader-a"), restricted("stranger")
    deny = Scope(token_id=None, token_kind="agent", asker=None, mode="deny")
    # An entry is returned only when every source message is visible.
    assert cache.get(visible, granted) == {"verdict": "visible"}
    assert cache.get(mixed, granted) is None
    for scope in (stranger, deny):
        assert cache.get(visible, scope) is None
        assert cache.get(mixed, scope) is None
    # Without sources there is nothing to hide.
    for scope in (granted, stranger, deny):
        assert cache.get(free, scope) == {"verdict": "no sources"}
    access.grant("reader-a", channel="synthetic.hidden")
    assert cache.get(mixed, granted) == {"verdict": "mixed"}
    access.revoke("reader-a", channel="synthetic.chat")
    assert cache.get(visible, granted) is None

    # Expiry, replacement and sweeping.
    expired = entry("expired", ttl_s=-1)
    lasting = entry("lasting", ttl_s=3600)
    assert cache.get(expired, OWNER) is None
    assert cache.get(lasting, OWNER) == {"verdict": "lasting"}
    assert cache.put(lasting, "match", "v1", "local-rule-v1", "lasting", {"n": 2})
    assert cache.get(lasting, OWNER) == {"n": 2}
    assert cache.sweep() == 1
    assert count("semantic_cache") == 4


def test_cache_read_fails_closed_without_gate_function(client, monkeypatch):
    key = cache.key("match", "v1", "m", "x")
    cache.put(key, "match", "v1", "m", "x", {"v": 1}, [1])
    statements = []

    class Missing:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, args=None):
            import psycopg

            statements.append(statement)
            raise psycopg.errors.UndefinedFunction("no such function")

    import throughline.query as query

    monkeypatch.setattr(query, "reader_transaction", lambda scope: Missing())
    # Before the asker gate exists only the owner mode reads sourced entries.
    assert cache.get(key, OWNER) == {"v": 1}
    assert cache.get(key, restricted()) is None
    assert statements == ["SELECT api.sources_visible(%s)"] * 2


def test_purge_hook_clears_cache(client, channel):
    ingest(client, [message(0)])
    client.post(
        "/v1/channels",
        json=dict(name="synthetic.kept", plugin="synthetic", fields=[]),
    )
    ingest(client, [message(1, channel="synthetic.kept", fields={})])
    with connect() as c:
        rows = c.execute(
            "SELECT ch.id channel_id,ch.name,m.id FROM tl.messages m JOIN tl.channels ch ON ch.id=m.channel_id"
        ).fetchall()
    purged = next(r for r in rows if r["name"] == "synthetic.chat")
    kept = next(r for r in rows if r["name"] == "synthetic.kept")
    keys = {}
    for name, sources in (
        ("purged", [purged["id"]]),
        ("both", [kept["id"], purged["id"]]),
        ("kept", [kept["id"]]),
        ("free", []),
    ):
        keys[name] = cache.key("match", "v1", "m", name)
        cache.put(keys[name], "match", "v1", "m", name, {"text": name}, sources)
    with connect() as c:
        hook = c.execute(
            "SELECT ord,statement FROM tl.purge_hooks WHERE name='semantic_cache'"
        ).fetchone()
        assert hook and "$1" in hook["statement"]
        c.execute("SELECT tl.run_purge_hooks(%s)", (purged["channel_id"],))
    assert cache.get(keys["purged"], OWNER) is None
    assert cache.get(keys["both"], OWNER) is None
    assert cache.get(keys["kept"], OWNER) == {"text": "kept"}
    assert cache.get(keys["free"], OWNER) == {"text": "free"}

    # The purge command runs the hooks itself.
    from typer.testing import CliRunner
    from throughline.cli import app

    result = CliRunner().invoke(app, ["purge", "synthetic.kept", "--confirm"])
    assert result.exit_code == 0, result.output
    assert cache.get(keys["kept"], OWNER) is None
    assert count("semantic_cache") == 1


# --- cost gate ---------------------------------------------------------------


def test_cst_10_authorize(client):
    agent = token_row(create_token("synthetic agent", kind="agent")["id"])
    assert (float(agent["max_model_cost"]), float(agent["approve_limit"])) == (0.10, 0)
    scope = Scope.from_token(agent)
    budgets = {"model_cost": None, "tokens": 2000}
    inputs = [("rs_a", 3), ("rs_b", 10)]
    statement = "SELECT * FROM api.match('rs_a','mentions the orchard')"
    digest = cost.request_hash(statement, scope, inputs, budgets)
    calls = []

    def run(estimates, **kwargs):
        """What every caller does: authorize, then (only then) call."""
        kwargs.setdefault("request_hash", digest)
        result = cost.authorize(agent, estimates, scope=scope, **kwargs)
        calls.append(result["cost"])
        return result

    # Under the limit: passes, nothing is recorded.
    assert run([remote_estimate(0.04), remote_estimate(0.05)])["cost"] == 0.09
    assert run([remote_estimate(0.10)])["approval_id"] is None
    # Local work is free whatever its nominal size, even with a zero limit.
    free = Estimate("match", "local", "local-rule-v1", 5, 10**9, 99.0, True)
    assert run([free], caller_limit=0)["cost"] == 0
    assert count("approvals") == 0 and len(calls) == 3

    # Over the limit: refused before any call, with a pending approval.
    with pytest.raises(cost.ApprovalRequired) as raised:
        run([remote_estimate(0.5), free])
    assert len(calls) == 3
    required = raised.value
    assert required.limit == 0.10 and required.estimate["cost"] == 0.5
    assert required.estimate["calls"] == 2 and len(required.estimate["parts"]) == 2
    assert "0.5000 USD" in required.explanation and "0.1000 USD" in str(required)
    assert required.to_dict()["approval_id"] == required.approval_id
    with connect() as c:
        row = c.execute("SELECT * FROM tl.approvals").fetchone()
    assert str(row["id"]) == required.approval_id and row["status"] == "pending"
    assert (row["token_id"], row["asker"]) == (agent["id"], None)
    assert (float(row["cost"]), float(row["limit_cost"])) == (0.5, 0.10)
    assert row["request_hash"] == digest and row["explanation"] == required.explanation
    # The explanation is deterministic, and asking again renews the same row.
    with pytest.raises(cost.ApprovalRequired) as again:
        run([remote_estimate(0.5), free])
    assert again.value.approval_id == required.approval_id
    assert again.value.explanation == required.explanation
    assert count("approvals") == 1
    # A caller can lower the limit, never raise it.
    with pytest.raises(cost.ApprovalRequired) as lowered:
        run([remote_estimate(0.05)], caller_limit=0.01, request_hash="other-request")
    assert lowered.value.limit == 0.01
    with pytest.raises(cost.ApprovalRequired):
        run([remote_estimate(0.5)], caller_limit=5, request_hash="third-request")
    # A pending approval authorizes nothing.
    with pytest.raises(cost.ApprovalRequired):
        run([remote_estimate(0.5)], approval_id=required.approval_id)
    assert len(calls) == 3

    def approve(approval_id, **kwargs):
        with connect() as c:
            return cost.decide(c, approval_id, "approved", by="owner", **kwargs)

    def status(approval_id):
        with connect() as c:
            return c.execute(
                "SELECT status FROM tl.approvals WHERE id=%s", (approval_id,)
            ).fetchone()["status"]

    def approved(amount=0.5, request=digest, scope_=scope):
        """A fresh approved approval for one request of this token."""
        with pytest.raises(cost.ApprovalRequired) as r:
            cost.authorize(
                agent, [remote_estimate(amount)], scope=scope_, request_hash=request
            )
        approve(r.value.approval_id)
        return r.value.approval_id

    # Approved: runs exactly once, and may reserve its estimate on the tracker.
    row = approve(required.approval_id)
    assert row["status"] == "approved" and row["approved_by"] == "owner"
    assert row["decided_at"] is not None
    tracker = BudgetTracker(clamp(Budget(), agent))
    assert not tracker.reserve(0.5, 2)
    result = run(
        [remote_estimate(0.5)], approval_id=required.approval_id, tracker=tracker
    )
    assert result["approval_id"] == required.approval_id and len(calls) == 4
    assert tracker.reserve(0.5, 2) and not tracker.reserve(0.2, 1)
    assert status(required.approval_id) == "used"
    with pytest.raises(cost.ApprovalRequired) as second:
        run([remote_estimate(0.5)], approval_id=required.approval_id)
    assert second.value.approval_id != required.approval_id and len(calls) == 4
    with pytest.raises(ValueError, match="already used"):
        approve(required.approval_id)

    # The estimate may move a little, not a lot.
    grown = approved(0.5)
    with pytest.raises(cost.ApprovalRequired):
        run([remote_estimate(0.61)], approval_id=grown)
    assert status(grown) == "approved"
    assert run([remote_estimate(0.6)], approval_id=grown)["approval_id"] == grown

    # Another asker of the same token.
    owner = token_row(create_token("second owner")["id"])
    as_alice = Scope.from_token(dict(owner, asker="alice"))
    as_bob = Scope.from_token(dict(owner, asker="bob"))
    for_alice = cost.request_hash(statement, as_alice, inputs, budgets)
    assert for_alice != cost.request_hash(statement, as_bob, inputs, budgets)
    with pytest.raises(cost.ApprovalRequired) as r:
        cost.authorize(
            owner, [remote_estimate(0.5)], scope=as_alice, request_hash=for_alice
        )
    alice_approval = r.value.approval_id
    approve(alice_approval)
    with pytest.raises(cost.ApprovalRequired):
        cost.authorize(
            owner,
            [remote_estimate(0.5)],
            scope=as_bob,
            request_hash=for_alice,
            approval_id=alice_approval,
        )
    # Another token, same asker.
    with pytest.raises(cost.ApprovalRequired):
        cost.authorize(
            agent,
            [remote_estimate(0.5)],
            scope=as_alice,
            request_hash=for_alice,
            approval_id=alice_approval,
        )
    assert status(alice_approval) == "approved"

    # Another request: a different statement, budget, or input member count.
    other = approved(0.5)
    changed = [
        cost.request_hash(statement + " ", scope, inputs, budgets),
        cost.request_hash(statement, scope, inputs, {**budgets, "tokens": 2001}),
        cost.request_hash(statement, scope, [("rs_a", 4), ("rs_b", 10)], budgets),
        cost.request_hash(statement, scope, [("rs_a", 3)], budgets),
    ]
    assert len({digest, *changed}) == 5
    # Order of the inputs, and a mapping instead of pairs, are the same request.
    assert digest == cost.request_hash(statement, scope, inputs[::-1], budgets)
    assert digest == cost.request_hash(statement, scope, dict(inputs), budgets)
    for value in changed:
        with pytest.raises(cost.ApprovalRequired):
            run([remote_estimate(0.5)], approval_id=other, request_hash=value)
    assert status(other) == "approved" and len(calls) == 5

    # A changed access policy voids the approval.
    with connect() as c:
        c.execute("UPDATE tl.policy_state SET version=version+1")
    with pytest.raises(cost.ApprovalRequired) as after_change:
        run([remote_estimate(0.5)], approval_id=other)
    assert status(other) == "approved" and len(calls) == 5
    with connect() as c:
        assert (
            c.execute(
                "SELECT policy_version FROM tl.approvals WHERE id=%s",
                (after_change.value.approval_id,),
            ).fetchone()["policy_version"]
            == c.execute("SELECT api.policy_version() v").fetchone()["v"]
        )
    # Rejected and unknown ids authorize nothing.
    with connect() as c:
        cost.decide(c, after_change.value.approval_id, "rejected", by="owner")
    for approval_id in (after_change.value.approval_id, "not-a-uuid"):
        with pytest.raises(cost.ApprovalRequired):
            run([remote_estimate(0.5)], approval_id=approval_id)

    # An agent approves only its own requests, and only up to its limit.
    with pytest.raises(cost.ApprovalRequired) as r:
        run([remote_estimate(0.3)], request_hash="agent-request")
    mine = r.value.approval_id
    own = dict(token_id=agent["id"], asker=None, own=True)
    with pytest.raises(PermissionError, match="approve_limit"):
        approve(mine, limit=float(agent["approve_limit"]), **own)
    with pytest.raises(PermissionError):
        approve(mine, limit=0.29, **own)
    assert status(mine) == "pending"
    with pytest.raises(LookupError):
        approve(alice_approval, limit=100, **own)
    with pytest.raises(LookupError):
        approve(mine, limit=100, token_id=agent["id"], asker="someone", own=True)
    assert approve(mine, limit=0.3, **own)["status"] == "approved"
    assert run([remote_estimate(0.3)], approval_id=mine, request_hash="agent-request")
    with pytest.raises(ValueError):
        cost.authorize(agent, [], scope=scope, request_hash="")
    with pytest.raises(TypeError):
        cost.authorize(agent, [], scope=scope)


def test_call_wrapper_budget_and_trace(client):
    token_id = create_token("synthetic agent", kind="agent")["id"]
    scope = Scope.from_token(token_row(token_id))
    trace = Trace(scope, "test")
    tracker = BudgetTracker(Budget(model_cost=0.25, model_calls=3))
    ran = []

    def fn(result="ok", tokens=12, amount=0.2):
        def run():
            ran.append(result)
            return result, Usage("recorded-model", tokens, amount, 1, 3.0)

        return run

    # Over the remaining budget: refused before the call, nothing recorded.
    with pytest.raises(ProviderUnavailable, match="budget exhausted"):
        providers.call(
            "semantic",
            "match",
            fn(),
            estimate=remote_estimate(0.5),
            scope=scope,
            tracker=tracker,
            trace=trace,
        )
    assert ran == [] and count("model_ledger") == 0
    assert tracker.snapshot()["spent"]["model_calls"] == 0

    result, usage = providers.call(
        "semantic",
        "match",
        fn(),
        estimate=Estimate("match", "http", "recorded-model", 1, 10, 0.2, False),
        scope=scope,
        tracker=tracker,
        trace=trace,
        origin="curator",
        origin_ref="job-7",
    )
    assert (result, usage.tokens, ran) == ("ok", 12, ["ok"])
    with connect() as c:
        row = c.execute("SELECT * FROM tl.model_ledger").fetchone()
    assert {
        k: row[k] for k in ("purpose", "provider", "model", "tokens", "calls", "origin")
    } == dict(
        purpose="match",
        provider="http",
        model="recorded-model",
        tokens=12,
        calls=1,
        origin="curator",
    )
    assert float(row["cost"]) == 0.2 and row["origin_ref"] == "job-7"
    assert row["token_id"] == token_id and str(row["trace_id"]) == trace.id
    assert tracker.snapshot()["spent"] | dict(ms=0) == dict(
        ms=0, tokens=0, model_cost=0.2, model_calls=1, controller_turns=0
    )
    # The cost budget is spent: the next paid call does not start.
    with pytest.raises(ProviderUnavailable, match="budget exhausted"):
        providers.call(
            "semantic",
            "match",
            fn(),
            estimate=remote_estimate(0.1),
            scope=scope,
            tracker=tracker,
        )
    # Local estimates reserve calls but no cost, whatever number they carry.
    reranker = providers.get("rerank")
    local_estimate = Estimate("rerank", "local", "local-lexical-v1", 1, 5, 9.0, True)
    for _ in range(2):
        providers.call(
            "rerank",
            "rerank",
            lambda: reranker.score("orchard", ["orchard"]),
            estimate=local_estimate,
            scope=scope,
            tracker=tracker,
            trace=trace,
        )
    with pytest.raises(ProviderUnavailable, match="budget exhausted"):
        providers.call(
            "rerank",
            "rerank",
            fn(),
            estimate=local_estimate,
            scope=scope,
            tracker=tracker,
        )
    assert ran == ["ok"] and count("model_ledger") == 3

    # Transport and decoding errors become ProviderUnavailable, without text.
    def broken(error):
        def run():
            raise error

        return run

    secret = "synthetic excerpt that must not leak"
    for error in (
        httpx.ConnectError(secret),
        httpx.ReadTimeout(secret),
        json.JSONDecodeError(secret, secret, 0),
        ConnectionResetError(secret),
    ):
        with pytest.raises(ProviderUnavailable) as raised:
            providers.call(
                "controller",
                "controller",
                broken(error),
                estimate=local_estimate,
                scope=scope,
                trace=trace,
            )
        assert secret not in str(raised.value) and raised.value.__cause__ is None
    # Other errors are the caller's and pass through unchanged.
    with pytest.raises(KeyError):
        providers.call(
            "controller",
            "controller",
            broken(KeyError("x")),
            estimate=local_estimate,
            scope=scope,
            trace=trace,
        )
    assert count("model_ledger") == 3

    events = trace.events
    assert [(e["op"], e["status"]) for e in events] == [
        ("model.semantic", "refused"),
        ("model.semantic", "ok"),
        ("model.rerank", "ok"),
        ("model.rerank", "ok"),
        *[("model.controller", "failed")] * 5,
    ]
    paid = events[1]
    assert (paid["model"], paid["tokens"], paid["cost"]) == ("recorded-model", 12, 0.2)
    assert paid["detail"] == dict(
        purpose="match", provider="http", origin="curator", calls=1
    )
    # Trace events carry names, counts and reasons only.
    assert secret not in json.dumps(events)
    assert all(e["statement"] is None for e in events)
    assert trace.flush()
    assert count("trace_events", "trace_id=%s", (trace.id,)) == len(events)

    with pytest.raises(ValueError, match="origin"):
        providers.call(
            "rerank", "rerank", fn(), estimate=local_estimate, scope=scope, origin="x"
        )
    with pytest.raises(ValueError, match="scope"):
        providers.call("rerank", "rerank", fn(), estimate=local_estimate, scope=None)
    with pytest.raises(TypeError):
        providers.call("rerank", "rerank", fn(), estimate=local_estimate)
    assert ran == ["ok"]


# --- surface: routes, command line, describe ---------------------------------


def test_approval_routes_and_spend(client):
    created = create_token("synthetic agent", kind="agent")
    agent = token_row(created["id"])
    agent_headers = {"Authorization": "Bearer " + created["token"]}
    scope = Scope.from_token(agent)

    def pending(amount, request):
        with pytest.raises(cost.ApprovalRequired) as raised:
            cost.authorize(
                agent, [remote_estimate(amount)], scope=scope, request_hash=request
            )
        return raised.value.approval_id

    first, second = pending(0.5, "request-one"), pending(0.3, "request-two")
    other = create_token("other agent", kind="agent")
    other_headers = {"Authorization": "Bearer " + other["token"]}

    rows = client.get("/v1/approvals").json()
    assert {r["id"] for r in rows} == {first, second}
    assert rows[0]["estimate"]["parts"][0]["purpose"] == "match"
    assert [
        r["id"] for r in client.get("/v1/approvals", headers=agent_headers).json()
    ] == [r["id"] for r in rows]
    assert client.get("/v1/approvals", headers=other_headers).json() == []
    assert client.get("/v1/approvals", params={"status": "used"}).json() == []
    assert client.get("/v1/approvals", params={"status": "x"}).status_code == 400

    # An agent cannot approve above its approve_limit (default 0), nor decide
    # for another token; unknown ids are not confirmed.
    url = "/v1/approvals/{}/{}".format
    assert client.post(url(first, "approve"), headers=agent_headers).status_code == 403
    assert client.post(url(first, "approve"), headers=other_headers).status_code == 404
    assert client.post(url("nope", "approve")).status_code == 404
    from throughline import tokens

    tokens.set_limits(agent["id"], approve_limit="0.3")
    assert client.post(url(first, "approve"), headers=agent_headers).status_code == 403
    response = client.post(url(second, "approve"), headers=agent_headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert response.json()["approved_by"] == f"agent token {agent['id']}"

    response = client.post(url(first, "approve"))
    assert response.status_code == 200 and response.json()["status"] == "approved"
    assert client.post(url(first, "approve")).status_code == 409
    assert client.post(url(first, "reject")).status_code == 409
    third = pending(0.4, "request-three")
    response = client.post(url(third, "reject"), headers=agent_headers)
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    # The approved one now authorizes its request once.
    assert (
        cost.authorize(
            agent,
            [remote_estimate(0.5)],
            scope=scope,
            request_hash="request-one",
            approval_id=first,
        )["approval_id"]
        == first
    )
    assert [
        r["id"] for r in client.get("/v1/approvals", params={"status": "used"}).json()
    ] == [first]

    # A plugin token has no part in this.
    plugin = create_token("synthetic plugin", kind="plugin", plugin="synthetic")
    plugin_headers = {"Authorization": "Bearer " + plugin["token"]}
    assert client.get("/v1/approvals", headers=plugin_headers).status_code == 403
    assert client.post(url(third, "approve"), headers=plugin_headers).status_code == 403

    # Spend: owner only.
    providers.ledger.record(
        purpose="match", provider="http", model="recorded-model", tokens=100, cost=0.25
    )
    with connect() as c:
        c.execute(
            "INSERT INTO tl.embedding_ledger(model,tokens,cost) VALUES('e',50,0.5)"
        )
    spend = client.get("/v1/spend")
    assert spend.status_code == 200, spend.text
    body = spend.json()
    assert (float(body["cost"]), body["tokens"], body["calls"]) == (0.75, 150, 2)
    assert [(r["purpose"], r["origin"], float(r["cost"])) for r in body["by"]] == [
        ("embedding", "worker", 0.5),
        ("match", "query", 0.25),
    ]
    future = client.get("/v1/spend", params={"since": "2999-01-01T00:00:00Z"}).json()
    assert float(future["cost"]) == 0 and future["by"] == []
    assert client.get("/v1/spend", headers=agent_headers).status_code == 403


def test_approval_routes_refuse_restricted_asker(client):
    from throughline import access

    access.register_asker("reader-a", "restricted")
    restricted_headers = {"X-Throughline-Asker": "reader-a"}
    with connect() as c:
        owner_id = c.execute(
            "SELECT * FROM tl.tokens WHERE kind='owner' ORDER BY id LIMIT 1"
        ).fetchone()
    owner = dict(owner_id, asker="reader-a")
    scope = Scope.from_token(owner)
    with pytest.raises(cost.ApprovalRequired) as raised:
        cost.authorize(
            owner, [remote_estimate(0.5)], scope=scope, request_hash="as-reader"
        )
    with pytest.raises(cost.ApprovalRequired) as other:
        cost.authorize(
            dict(owner_id, asker=None),
            [remote_estimate(0.5)],
            scope=Scope.from_token(owner_id),
            request_hash="as-owner",
        )
    # Its own approvals are its working state; nothing else is listed.
    rows = client.get("/v1/approvals", headers=restricted_headers).json()
    assert [r["id"] for r in rows] == [raised.value.approval_id]
    assert len(client.get("/v1/approvals").json()) == 2
    # Deciding and spend are refused before any lookup: same answer for an
    # approval that exists and one that does not.
    for approval_id in (other.value.approval_id, raised.value.approval_id, "nope"):
        for action in ("approve", "reject"):
            response = client.post(
                f"/v1/approvals/{approval_id}/{action}", headers=restricted_headers
            )
            assert response.status_code == 403
    assert client.get("/v1/spend", headers=restricted_headers).status_code == 403
    assert count("approvals", "status<>'pending'") == 0


def test_cost_command_line(client):
    from typer.testing import CliRunner
    from throughline.cli import app

    agent = token_row(create_token("synthetic agent", kind="agent")["id"])
    with pytest.raises(cost.ApprovalRequired) as raised:
        cost.authorize(
            agent,
            [remote_estimate(0.5)],
            scope=Scope.from_token(agent),
            request_hash="cli-request",
        )
    approval_id = raised.value.approval_id
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(app, ["lib", "cost", *args, "--json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    listed = run("approvals", "--status", "pending")
    assert [r["id"] for r in listed] == [approval_id]
    row = run("approve", approval_id)
    assert (row["status"], row["approved_by"]) == ("approved", "owner (command line)")
    assert runner.invoke(app, ["lib", "cost", "approve", approval_id]).exit_code != 0
    assert runner.invoke(app, ["lib", "cost", "approve", "nope"]).exit_code != 0
    second = None
    with pytest.raises(cost.ApprovalRequired) as raised:
        cost.authorize(
            agent,
            [remote_estimate(0.5)],
            scope=Scope.from_token(agent),
            request_hash="cli-second",
        )
    second = raised.value.approval_id
    assert run("approve", second, "--reject")["status"] == "rejected"

    providers.ledger.record(
        purpose="rerank", provider="http", model="recorded-rerank", tokens=40, cost=0.1
    )
    spend = run("spend")
    assert (float(spend["cost"]), spend["tokens"], spend["calls"]) == (0.1, 40, 1)
    assert spend["by"][0]["purpose"] == "rerank"
    plain = runner.invoke(app, ["lib", "cost", "spend"])
    assert plain.exit_code == 0 and "purpose=rerank" in plain.output


def test_describe_egress_names_no_host_or_key(client, monkeypatch):
    # Written at start by the fixture's app: offline defaults.
    described = client.get("/v1/describe", params={"subject": "egress"})
    assert described.status_code == 200, described.text
    state = {p["kind"]: p for p in described.json()["providers"]}
    assert state["embedding"]["provider"] == "none"
    assert {state[k]["provider"] for k in ("rerank", "semantic", "controller")} == {
        "local"
    }
    assert described.json()["excerpts_leave_host"] is False

    monkeypatch.setenv("THROUGHLINE_EGRESS_ALLOW", "models.example.invalid")
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_PROVIDER", "http")
    monkeypatch.setenv(
        "THROUGHLINE_SEMANTIC_BASE_URL", "https://models.example.invalid/v1"
    )
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_MODEL", "remote-chat")
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_KEY", "synthetic-secret-key")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    with connect("app") as c:
        rows = providers.sync_state(c)
    assert len(rows) == len(providers.KINDS)
    from throughline.query import reader_transaction

    # Readable on the reader path, by a restricted asker too.
    for scope in (OWNER, restricted()):
        with reader_transaction(scope) as c:
            value = c.execute("SELECT api.describe_egress() d").fetchone()["d"]
        state = {p["kind"]: p for p in value["providers"]}
        assert state["semantic"] | dict(updated_at=None) == dict(
            kind="semantic",
            provider="http",
            model="remote-chat",
            local=False,
            excerpts_leave_host=True,
            updated_at=None,
        )
        assert state["embedding"]["model"] == "local-hash-v1"
        assert value["excerpts_leave_host"] is True
        text = json.dumps(value)
        assert "example.invalid" not in text and "synthetic-secret-key" not in text
    with connect() as c:
        stored = json.dumps(
            c.execute("SELECT * FROM tl.provider_state").fetchall(), default=str
        )
        before = c.execute(
            "SELECT updated_at FROM tl.provider_state WHERE kind='semantic'"
        ).fetchone()
    assert "example.invalid" not in stored and "synthetic-secret-key" not in stored
    # Unchanged configuration rewrites nothing.
    with connect("app") as c:
        providers.sync_state(c)
    with connect() as c:
        assert (
            c.execute(
                "SELECT updated_at FROM tl.provider_state WHERE kind='semantic'"
            ).fetchone()
            == before
        )


def test_runtime_roles_cannot_bypass_the_tables(client):
    import psycopg

    for statement in (
        "UPDATE tl.model_ledger SET cost=0",
        "DELETE FROM tl.model_ledger",
        "DELETE FROM tl.approvals",
        "UPDATE tl.approvals SET request_hash='x'",
        "UPDATE tl.tokens SET max_model_cost=1000",
        "UPDATE tl.tokens SET approve_limit=1000",
        "INSERT INTO tl.purge_hooks VALUES('x',1,'SELECT 1')",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect("app") as c:
                c.execute(statement)
    for table in (
        "model_ledger",
        "semantic_cache",
        "approvals",
        "provider_state",
        "spend",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect("reader") as c:
                c.execute(f"SELECT 1 FROM tl.{table}")


# --- checker additions -------------------------------------------------------


def test_local_synonym_map_is_part_of_version_and_model(monkeypatch, client):
    plain = [
        local.LocalHashEmbedder(),
        local.LocalLexicalReranker(),
        local.LocalRuleModel(),
    ]
    assert [(p.version, p.model) for p in plain] == [
        ("local-hash-v1", "local-hash-v1"),
        ("local-lexical-v1", "local-lexical-v1"),
        ("local-rule-v1", "local-rule-v1"),
    ]
    monkeypatch.setenv("THROUGHLINE_LOCAL_SYNONYMS", '{"orchard": ["grove"]}')
    tagged = [p.version for p in plain]
    assert all(
        v.startswith(b + "+syn-")
        for v, b in zip(tagged, ("local-hash-v1", "local-lexical-v1", "local-rule-v1"))
    )
    assert (
        plain[0].model == tagged[0] and plain[0].embed(["grove"])[1].model == tagged[0]
    )
    # The same map in another shape is the same version; another map is not.
    monkeypatch.setenv("THROUGHLINE_LOCAL_SYNONYMS", '[["orchard", "grove"]]')
    assert [p.version for p in plain] == tagged
    monkeypatch.setenv("THROUGHLINE_LOCAL_SYNONYMS", '{"orchard": ["plantation"]}')
    assert plain[0].version not in tagged

    # The worker stores vectors under the tagged model, and the query cache
    # never hands back a vector made under another map.
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "16")
    assert embeddings.settings()[1] == plain[0].model
    serving("local-hash-v1", 16)
    monkeypatch.delenv("THROUGHLINE_LOCAL_SYNONYMS")
    first = embeddings.embed_search("the grove")
    monkeypatch.setenv("THROUGHLINE_LOCAL_SYNONYMS", '{"orchard": ["grove"]}')
    second = embeddings.embed_search("the grove")
    assert first != second
    assert second == local.LocalHashEmbedder(16).embed(["the orchard"])[0][0]
    assert count("model_ledger", "purpose='query_embedding'") == 2


def test_call_wrapper_charges_cost_above_estimate(client):
    tracker = BudgetTracker(Budget(model_cost=0.25, model_calls=5))
    trace = Trace(OWNER, "test")

    def run(amount):
        return lambda: ("ok", Usage("recorded-model", 10, amount, 1, 1.0))

    estimate = Estimate("match", "http", "recorded-model", 1, 10, 0.05, False)
    providers.call(
        "semantic",
        "match",
        run(0.15),
        estimate=estimate,
        scope=OWNER,
        tracker=tracker,
        trace=trace,
    )
    assert tracker.snapshot()["spent"]["model_cost"] == pytest.approx(0.15)
    # Cheaper than estimated: the reservation stands (nothing is given back).
    providers.call(
        "semantic",
        "match",
        run(0.01),
        estimate=estimate,
        scope=OWNER,
        tracker=tracker,
        trace=trace,
    )
    assert tracker.snapshot()["spent"]["model_cost"] == pytest.approx(0.20)
    # The next one overruns what is left: it already happened, so it is
    # ledgered and the trace says the budget was exceeded.
    providers.call(
        "semantic",
        "match",
        run(0.30),
        estimate=estimate,
        scope=OWNER,
        tracker=tracker,
        trace=trace,
    )
    assert trace.events[-1]["status"] == "ok"
    assert trace.events[-1]["detail"].get("over_budget") is True
    assert "over_budget" not in trace.events[0]["detail"]
    assert count("model_ledger") == 3
    with pytest.raises(ProviderUnavailable, match="budget exhausted"):
        providers.call(
            "semantic",
            "match",
            run(0.0),
            estimate=estimate,
            scope=OWNER,
            tracker=tracker,
        )


def test_query_cache_ttl_setting_never_fails_a_search(
    client, counting_embed, monkeypatch
):
    serving("serving-a")
    for value in ("soon", "-5", "inf", "0"):
        monkeypatch.setenv("THROUGHLINE_EMBEDDING_QUERY_CACHE_TTL_S", value)
        assert embeddings.query_cache_ttl() == embeddings.QUERY_CACHE_TTL_S
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_QUERY_CACHE_TTL_S", "soon")
    assert embeddings.embed_search("orchard") == [1.0, 0.0, 0.0]
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_QUERY_CACHE_TTL_S", "60")
    assert embeddings.query_cache_ttl() == 60
    assert count("semantic_cache", "expires_at IS NOT NULL") == 1


def test_cost_command_line_rejects_unknown_status(client):
    from typer.testing import CliRunner
    from throughline.cli import app

    result = CliRunner().invoke(app, ["lib", "cost", "approvals", "--status", "x"])
    assert result.exit_code != 0
