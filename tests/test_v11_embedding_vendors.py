"""Embedding choices: named vendors (Voyage, Gemini, OpenRouter), channels
that are not embedded, contextual embedding, vendor batch jobs and the
nearest-neighbour index (migration 0054).

Everything runs offline: the vendors are a loopback stub that answers with
the shapes of their published references. Nothing here proves a live vendor
accepts these requests; that needs credentials.
"""

import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest
from conftest import ingest, message
from test_v11_passages import build, page, page_channel, put
from throughline import (
    embedding_batches,
    embedding_context,
    embeddings,
    passages,
    providers,
)
from throughline.db import connect
from throughline.providers import batch, vendors
from throughline.providers.base import EgressDenied, ProviderUnavailable


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
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
                "OPENROUTER_",
            )
        ):
            monkeypatch.delenv(name)
    for name in (
        "VOYAGE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def vendor():
    """Loopback stand-in for a vendor: `routes` maps (METHOD, path) to a
    reply (JSON value, bytes, or a function of the decoded request)."""

    class Stub:
        routes = {}
        requests = []

    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            kind = self.headers.get("Content-Type") or ""
            body = json.loads(raw) if raw and "json" in kind else raw
            Stub.requests.append((method, self.path, body, dict(self.headers)))
            reply = Stub.routes.get((method, self.path))
            if callable(reply):
                reply = reply(body)
            if reply is None:
                self.send_response(404)
                self.end_headers()
                return
            data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self._serve("POST")

        def do_GET(self):
            self._serve("GET")

        def log_message(self, *args):
            pass

    Stub.routes, Stub.requests = {}, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Stub.base = f"http://127.0.0.1:{server.server_port}/v1"
    yield Stub
    server.shutdown()
    server.server_close()
    thread.join()


def axis(i, dimensions=4):
    """A unit vector pointing along axis i."""
    return [1.0 if n == i % dimensions else 0.0 for n in range(dimensions)]


def voyage_embeddings(body):
    return {
        "data": [
            {"index": i, "embedding": axis(len(text))}
            for i, text in enumerate(body["input"])
        ],
        "usage": {"total_tokens": 10 * len(body["input"])},
    }


def voyage_contextual(body):
    """One vector per chunk; its first number is the number of chunks that
    were sent with it, so a test can see the context a chunk had."""
    return {
        "data": [
            {
                "index": d,
                "data": [
                    {"index": c, "embedding": [float(len(chunks)), float(c), 0.0, 1.0]}
                    for c in range(len(chunks))
                ],
            }
            for d, chunks in enumerate(body["inputs"])
        ],
        "usage": {"total_tokens": 7 * sum(len(c) for c in body["inputs"])},
    }


def count(table, where="true", args=()):
    with connect() as c:
        return c.execute(
            f"SELECT count(*) n FROM tl.{table} WHERE {where}", args
        ).fetchone()["n"]


# --- selection ----------------------------------------------------------------


def test_vendor_selection_defaults_keys_and_errors(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "voyage")
    with pytest.raises(ProviderUnavailable, match="VOYAGE_API_KEY"):
        providers.get("embedding")
    monkeypatch.setenv("VOYAGE_API_KEY", "synthetic-voyage")
    provider = providers.get("embedding")
    assert isinstance(provider, vendors.VoyageEmbedder)
    assert (provider.name, provider.model, provider.dimensions) == (
        "voyage",
        "voyage-4-large",
        1024,
    )
    assert provider.base == "https://api.voyageai.com/v1"
    assert provider.key == "synthetic-voyage" and provider.price_per_million == 0.12
    assert provider.asymmetric and not provider.contextual and not provider.local
    assert provider.excerpts_leave_host and provider.batch_client() is not None

    # The kind's own key wins over the vendor's conventional variable.
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_KEY", "own-key")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "voyage-context-4")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "512")
    provider = providers.get("embedding")
    assert provider.key == "own-key" and provider.contextual
    assert (provider.model, provider.dimensions) == ("voyage-context-4", 512)

    monkeypatch.delenv("THROUGHLINE_EMBEDDING_KEY")
    monkeypatch.delenv("THROUGHLINE_EMBEDDING_MODEL")
    monkeypatch.delenv("THROUGHLINE_EMBEDDING_DIMENSIONS")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-google")
    provider = providers.get("embedding")
    assert isinstance(provider, vendors.GeminiEmbedder)
    assert (provider.model, provider.dimensions) == ("gemini-embedding-2", 1536)
    assert provider.batch_discount == 0.5

    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-or")
    with pytest.raises(ProviderUnavailable, match="THROUGHLINE_EMBEDDING_MODEL"):
        providers.get("embedding")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "some/unlisted-model")
    with pytest.raises(ProviderUnavailable, match="THROUGHLINE_EMBEDDING_DIMENSIONS"):
        providers.get("embedding")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "openai/text-embedding-3-small")
    provider = providers.get("embedding")
    assert isinstance(provider, vendors.OpenRouterEmbedder)
    assert provider.dimensions == 1536 and provider.batch_client() is None

    # Every kind a vendor serves, and none it does not.
    monkeypatch.setenv("THROUGHLINE_RERANK_PROVIDER", "voyage")
    reranker = providers.get("rerank")
    assert isinstance(reranker, vendors.VoyageReranker)
    assert (reranker.model, reranker.price_per_million) == ("rerank-2.5", 0.05)
    monkeypatch.setenv("THROUGHLINE_RERANK_PROVIDER", "gemini")
    with pytest.raises(ValueError, match="one of local, http, none, voyage"):
        providers.get("rerank")
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_PROVIDER", "openrouter")
    monkeypatch.setenv("THROUGHLINE_SEMANTIC_MODEL", "some/chat-model")
    semantic = providers.get("semantic")
    assert semantic.name == "openrouter" and semantic.key == "synthetic-or"
    assert semantic.base == "https://openrouter.ai/api/v1"
    monkeypatch.setenv("THROUGHLINE_CONTROLLER_PROVIDER", "gemini")
    monkeypatch.setenv("THROUGHLINE_CONTROLLER_MODEL", "some-gemini-chat")
    controller = providers.get("controller")
    assert controller.base.endswith("/v1beta/openai")
    assert controller.key == "synthetic-google"
    # The shared model key belongs to the shared endpoint, never to a vendor.
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("THROUGHLINE_MODEL_KEY", "shared-key")
    with pytest.raises(ProviderUnavailable, match="OPENROUTER_API_KEY"):
        providers.get("semantic")


def test_vendor_host_needs_the_egress_allow_list(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "synthetic-voyage")
    opened = []
    monkeypatch.setattr(
        providers.http, "client", lambda timeout: opened.append(timeout)
    )
    with pytest.raises(EgressDenied, match="api.voyageai.com:443"):
        providers.get("embedding").embed(["orchard"])
    assert opened == []


# --- request and response shapes ----------------------------------------------


def test_voyage_embeds_documents_and_queries(vendor):
    vendor.routes[("POST", "/v1/embeddings")] = voyage_embeddings
    provider = vendors.VoyageEmbedder(
        vendor.base, "voyage-4", 4, key="synthetic-key", price_per_million=0.06
    )
    vectors, usage = provider.embed(["ab", "abc"])
    assert vectors == [axis(2), axis(3)]
    assert (usage.model, usage.tokens, usage.calls) == ("voyage-4", 20, 1)
    assert usage.cost == pytest.approx(20 * 0.06 / 1e6)
    method, path, body, headers = vendor.requests[-1]
    assert (method, path) == ("POST", "/v1/embeddings")
    assert headers["Authorization"] == "Bearer synthetic-key"
    assert body == {
        "input": ["ab", "abc"],
        "model": "voyage-4",
        "input_type": "document",
        "output_dimension": 4,
        "truncation": True,
    }
    provider.embed(["where is the orchard"], input_type="query")
    assert vendor.requests[-1][2]["input_type"] == "query"
    # A wrong size, a missing vector or a non-finite number is refused.
    vendor.routes[("POST", "/v1/embeddings")] = {
        "data": [{"index": 0, "embedding": [1.0, 0.0]}]
    }
    with pytest.raises(ProviderUnavailable, match="invalid embedding response"):
        provider.embed(["ab"])


def test_voyage_requests_are_split_below_the_token_limit(vendor, monkeypatch):
    vendor.routes[("POST", "/v1/embeddings")] = voyage_embeddings
    monkeypatch.setattr(vendors, "REQUEST_TOKENS", 12)
    provider = vendors.VoyageEmbedder(vendor.base, "voyage-4", 4, key="k")
    texts = ["x" * 20, "y" * 21, "z" * 22]  # 5, 6 and 6 estimated tokens
    vectors, usage = provider.embed(texts)
    assert vectors == [axis(20), axis(21), axis(22)]
    assert [len(r[2]["input"]) for r in vendor.requests] == [2, 1]
    assert usage.calls == 2 and usage.tokens == 30


def test_voyage_contextual_documents_and_query(vendor):
    vendor.routes[("POST", "/v1/contextualizedembeddings")] = voyage_contextual
    provider = vendors.VoyageEmbedder(vendor.base, "voyage-context-4", 4, key="k")
    assert provider.contextual
    documents, usage = provider.embed_documents([["a", "b", "c"], ["d"]])
    assert [len(d) for d in documents] == [3, 1]
    assert documents[0][2] == [3.0, 2.0, 0.0, 1.0] and documents[1][0][0] == 1.0
    assert usage.tokens == 28
    assert vendor.requests[-1][2] == {
        "inputs": [["a", "b", "c"], ["d"]],
        "model": "voyage-context-4",
        "input_type": "document",
        "output_dimension": 4,
    }
    # A query is a document of one chunk, embedded as a query.
    vectors, _ = provider.embed(["where is the orchard"], input_type="query")
    assert vectors == [[1.0, 0.0, 0.0, 1.0]]
    assert vendor.requests[-1][2]["inputs"] == [["where is the orchard"]]
    assert vendor.requests[-1][2]["input_type"] == "query"


def test_gemini_task_as_field_or_prompt_and_normalised(vendor):
    def reply(body):
        return {
            "embeddings": [
                {"values": [3.0, 4.0, 0.0, 0.0, 9.0]} for _ in body["requests"]
            ],
            "usageMetadata": {"promptTokenCount": 11},
        }

    path = "/v1/models/gemini-embedding-001:batchEmbedContents"
    vendor.routes[("POST", path)] = reply
    provider = vendors.GeminiEmbedder(
        vendor.base, "gemini-embedding-001", 4, key="synthetic-google"
    )
    vectors, usage = provider.embed(["orchard"], input_type="query")
    # Longer than asked: cut to the size and scaled to length 1.
    assert vectors == [[0.6, 0.8, 0.0, 0.0]] and usage.tokens == 11
    method, _, body, headers = vendor.requests[-1]
    assert headers["x-goog-api-key"] == "synthetic-google"
    assert "Authorization" not in headers
    assert body == {
        "requests": [
            {
                "model": "models/gemini-embedding-001",
                "outputDimensionality": 4,
                "taskType": "RETRIEVAL_QUERY",
                "content": {"parts": [{"text": "orchard"}]},
            }
        ]
    }
    provider.embed(["orchard"])
    assert vendor.requests[-1][2]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"

    path = "/v1/models/gemini-embedding-2:batchEmbedContents"
    vendor.routes[("POST", path)] = reply
    provider = vendors.GeminiEmbedder(vendor.base, "gemini-embedding-2", 4, key="k")
    provider.embed(["orchard"], input_type="query")
    request = vendor.requests[-1][2]["requests"][0]
    assert "taskType" not in request
    assert request["content"]["parts"][0]["text"] == (
        "task: search result | query: orchard"
    )
    provider.embed(["orchard"])
    assert vendor.requests[-1][2]["requests"][0]["content"]["parts"][0]["text"] == (
        "title: none | text: orchard"
    )


def test_openrouter_data_policy_input_type_and_reported_cost(vendor, monkeypatch):
    vendor.routes[("POST", "/v1/embeddings")] = lambda body: {
        "data": [{"index": i, "embedding": axis(i)} for i in range(len(body["input"]))],
        "usage": {"prompt_tokens": 8, "total_tokens": 8, "cost": 0.004},
    }
    provider = vendors.OpenRouterEmbedder(
        vendor.base, "openai/text-embedding-3-small", 4, key="k", price_per_million=1
    )
    vectors, usage = provider.embed(["a", "b"])
    assert vectors == [axis(0), axis(1)]
    # What OpenRouter charged, not tokens times a configured price.
    assert usage.cost == pytest.approx(0.004) and usage.tokens == 8
    assert vendor.requests[-1][2] == {
        "model": "openai/text-embedding-3-small",
        "input": ["a", "b"],
        "dimensions": 4,
        "input_type": "search_document",
        "provider": {"data_collection": "deny"},
    }
    provider.embed(["a"], input_type="query")
    assert vendor.requests[-1][2]["input_type"] == "search_query"
    monkeypatch.setenv(
        "THROUGHLINE_OPENROUTER_PROVIDER", '{"zdr": true, "only": ["openai"]}'
    )
    provider.embed(["a"])
    assert vendor.requests[-1][2]["provider"] == {"zdr": True, "only": ["openai"]}
    monkeypatch.setenv("THROUGHLINE_OPENROUTER_PROVIDER", "{}")
    provider.embed(["a"])
    assert "provider" not in vendor.requests[-1][2]
    monkeypatch.setenv("THROUGHLINE_OPENROUTER_PROVIDER", "deny")
    with pytest.raises(ValueError, match="JSON object"):
        provider.embed(["a"])


def test_vendor_rerankers(vendor, monkeypatch):
    # Voyage answers in 'data', sorted by score; OpenRouter in 'results'.
    vendor.routes[("POST", "/v1/rerank")] = {
        "data": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.2},
        ],
        "usage": {"total_tokens": 12},
    }
    scores, usage = vendors.VoyageReranker(
        vendor.base, "rerank-2.5", key="k", price_per_million=0.05
    ).score("q", ["a", "b"])
    assert scores == [0.2, 0.9] and usage.tokens == 12
    assert vendor.requests[-1][2] == {
        "model": "rerank-2.5",
        "query": "q",
        "documents": ["a", "b"],
        "truncation": True,
    }
    vendor.routes[("POST", "/v1/rerank")] = {
        "results": [{"index": 0, "relevance_score": 0.7}]
    }
    scores, _ = vendors.OpenRouterReranker(
        vendor.base, "cohere/rerank-v3.5", key="k"
    ).score("q", ["a"])
    assert scores == [0.7]
    assert vendor.requests[-1][2]["provider"] == {"data_collection": "deny"}


def test_rate_limited_request_is_retried(vendor, monkeypatch):
    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            attempts.append(1)
            if len(attempts) == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0.01")
                self.end_headers()
                return
            data = json.dumps(
                {"data": [{"index": 0, "embedding": axis(1)}], "usage": {}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/v1"
        provider = vendors.VoyageEmbedder(base, "voyage-4", 4, key="k")
        assert provider.embed(["a"])[0] == [axis(1)]
        assert len(attempts) == 2
        # An interactive query does not wait out a rate limit.
        attempts.clear()
        with pytest.raises(ProviderUnavailable):
            provider.with_timeout(5).embed(["a"])
        assert len(attempts) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# --- worker: input types -------------------------------------------------------


def voyage(vendor, model="voyage-4", dimensions=4):
    return vendors.VoyageEmbedder(
        vendor.base, model, dimensions, key="k", price_per_million=1.0
    )


def test_worker_embeds_documents_and_search_embeds_a_query(client, channel, vendor):
    vendor.routes[("POST", "/v1/embeddings")] = voyage_embeddings
    ingest(client, [message(0), message(4000, content="ocean")])
    with providers.override("embedding", voyage(vendor)):
        assert embeddings.run_once() == 2
        assert [r[2]["input_type"] for r in vendor.requests] == ["document"]
        embeddings.embed_search("where is the orchard")
        assert vendor.requests[-1][2]["input_type"] == "query"
        assert vendor.requests[-1][2]["input"] == ["where is the orchard"]
    with connect() as c:
        ledger = c.execute("SELECT tokens,cost FROM tl.embedding_ledger").fetchall()
        model = c.execute("SELECT * FROM tl.embedding_models").fetchone()
    assert [(r["tokens"], float(r["cost"])) for r in ledger] == [(20, 20 / 1e6)]
    assert model["active"] and model["model"] == "voyage-4"


def test_monthly_cap_pauses_the_worker(client, channel, vendor, monkeypatch):
    vendor.routes[("POST", "/v1/embeddings")] = voyage_embeddings
    with connect() as c:
        c.execute(
            "INSERT INTO tl.embedding_ledger(model,tokens,cost) VALUES('voyage-4',1,4.0)"
        )
        c.commit()
    ingest(client, [message(0), message(4000, content="ocean")])
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MONTHLY_USD", "4")
    with providers.override("embedding", voyage(vendor)):
        assert embeddings.run_once() == 0 and vendor.requests == []
        # Spend before BUDGET_SINCE (a one-off load) is not counted.
        monkeypatch.setenv("THROUGHLINE_EMBEDDING_BUDGET_SINCE", "2999-01-01")
        assert embeddings.run_once() == 2
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MONTHLY_USD", "lots")
    with pytest.raises(ValueError, match="MONTHLY_USD"):
        embeddings.monthly_cap()


# --- channels that are not embedded ---------------------------------------------


def register(client, name, **kwargs):
    r = client.post("/v1/channels", json=dict(name=name, plugin="synthetic", **kwargs))
    assert r.status_code == 200, r.text
    return r.json()


def embed_flags():
    with connect() as c:
        return {
            r["name"]: r["embed"]
            for r in c.execute("SELECT name,embed FROM tl.channels")
        }


def test_channel_not_embedded_gets_no_vectors_and_model_still_activates(
    client, channel, monkeypatch
):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "16")
    register(client, "fake.noise", embed=False)
    assert embed_flags() == {"synthetic.chat": True, "fake.noise": False}
    ingest(
        client,
        [
            message(0, content="we walked through the orchard at dawn"),
            message(
                1,
                channel="fake.noise",
                external_id="n-1",
                thread={"external_id": "noise"},
                content="the orchard sensor reported nothing",
                fields={},
            ),
        ],
    )
    assert embeddings.run_once() == 1
    assert embeddings.run_once() == 0
    with connect() as c:
        embedded = c.execute(
            "SELECT ch.name FROM tl.embeddings e JOIN tl.conversations c ON c.id=e.conversation_id JOIN tl.threads t ON t.id=c.thread_id JOIN tl.channels ch ON ch.id=t.channel_id"
        ).fetchall()
        # Coverage counts embedded channels only, so the model serves search.
        assert c.execute(
            "SELECT active FROM tl.embedding_models WHERE model='local-hash-v1'"
        ).fetchone()["active"]
        noise = c.execute(
            "SELECT tl.channel_maintenance(id) m FROM tl.channels WHERE name='fake.noise'"
        ).fetchone()["m"]
    assert [r["name"] for r in embedded] == ["synthetic.chat"]
    assert noise["embed"] is False and noise["embedded_conversations"] == 0

    # Meaning search covers the embedded channel; keyword search covers both.
    meaning = client.get("/v1/search", params=dict(q="orchard at dawn", mode="meaning"))
    assert [r["channel"] for r in meaning.json()["rows"]] == ["synthetic.chat"]
    keyword = client.get("/v1/search", params=dict(q="orchard"))
    assert {r["channel"] for r in keyword.json()["rows"]} == {
        "synthetic.chat",
        "fake.noise",
    }
    # A recall scoped to the channel says why it has no meaning leg.
    recall = client.post(
        "/v1/recall", json=dict(query="orchard", scope=dict(channels=["fake.noise"]))
    )
    assert recall.status_code == 200, recall.text
    assert "channels_not_embedded" in json.dumps(recall.json())


def test_switching_a_channel_off_deletes_its_vectors_and_on_refills(
    client, channel, monkeypatch
):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "16")
    ingest(client, [message(0), message(4000, content="ocean")])
    assert embeddings.run_once() == 2 and count("embeddings") == 2
    with connect() as c:
        result = c.execute(
            "SELECT tl.set_channel_embedding('synthetic.chat',false) r"
        ).fetchone()["r"]
        assert result["conversation_vectors_deleted"] == 2
        # The database refuses a vector for a channel that is not embedded,
        # whoever writes it.
        c.execute(
            "INSERT INTO tl.embeddings SELECT id,'local-hash-v1',text_hash,'[1,0]'::vector FROM tl.conversations"
        )
    assert count("embeddings") == 0
    with connect() as c:
        with pytest.raises(psycopg.errors.NoDataFound):
            c.execute("SELECT tl.set_channel_embedding('no.such',false)")
        c.rollback()
    assert embeddings.run_once() == 0
    # A plugin registering the channel again does not undo the owner's choice.
    register(client, "synthetic.chat", embed=True)
    assert embed_flags() == {"synthetic.chat": False}
    with connect() as c:
        c.execute("SELECT tl.set_channel_embedding('synthetic.chat',true)")
    assert embeddings.run_once() == 2 and count("embeddings") == 2
    # Runtime roles cannot switch channels.
    with connect("app") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT tl.set_channel_embedding('synthetic.chat',false)")


def test_passages_of_a_channel_not_embedded(client, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "16")
    page_channel(client)
    page_channel(client, "fake.private", embed=False)
    put(
        client,
        [
            page(0, "kept", "# Orchard\n\nThe orchard has forty apple trees."),
            page(
                1,
                "skipped",
                "# Cellar\n\nThe cellar holds the cider barrels.",
                channel="fake.private",
            ),
            # The same text in both channels keeps its one vector.
            page(2, "both-a", "# Shared\n\nA paragraph that both pages hold."),
            page(
                3,
                "both-b",
                "# Shared\n\nA paragraph that both pages hold.",
                channel="fake.private",
            ),
        ],
    )
    assert build() == 4
    while passages.embed_once():
        pass
    with connect() as c:
        texts = {
            r["content"]
            for r in c.execute(
                "SELECT DISTINCT p.content FROM tl.passage_embeddings e JOIN tl.passages p ON p.text_hash=e.text_hash"
            )
        }
        assert passages.pending(c, "local-hash-v1")["texts_without_vector"] == 0
    assert any("forty apple trees" in t for t in texts)
    assert any("both pages hold" in t for t in texts)
    assert not any("cider barrels" in t for t in texts)
    # Switching the embedded channel off keeps nothing only it justified.
    with connect() as c:
        result = c.execute(
            "SELECT tl.set_channel_embedding(%s,false) r", ("fake.page",)
        ).fetchone()["r"]
    assert result["passage_vectors_deleted"] == 2 and count("passage_embeddings") == 0
    # The passages themselves stay: keyword search still finds the page.
    with connect() as c:
        found = c.execute(
            "SELECT * FROM api.search_passages('cider barrels')"
        ).fetchall()
    assert len(found) == 1


# --- contextual embedding -------------------------------------------------------


def test_windows_group_by_document_with_leading_context(monkeypatch):
    rows = [
        dict(doc="a", no=n, content=f"a{n}", token_estimate=40) for n in (3, 4, 5)
    ] + [dict(doc="b", no=0, content="b0", token_estimate=40)]
    before = {
        ("a", 3): [
            dict(content="a1", token_estimate=10),
            dict(content="a2", token_estimate=10),
        ],
        ("a", 5): [dict(content="a4", token_estimate=40)],
    }
    asked = []

    def context(row, tokens):
        asked.append((row["doc"], row["no"], tokens))
        return before.get((row["doc"], row["no"]), [])

    monkeypatch.setenv("THROUGHLINE_EMBEDDING_CONTEXT_TOKENS", "50")
    found = embedding_context.windows(
        rows, context, group=lambda r: r["doc"], budget=110
    )
    assert [embedding_context.texts(w) for w in found] == [
        ["a1", "a2", "a3", "a4"],  # 20 of context + 80: the next row would pass 110
        ["a4", "a5"],
        ["b0"],
    ]
    assert asked == [("a", 3, 50), ("a", 5, 50), ("b", 0, 50)]
    assert [[i for i, _ in w["rows"]] for w in found] == [[0, 1], [2], [3]]


def test_contextual_worker_sends_thread_chunks_together(client, channel, vendor):
    vendor.routes[("POST", "/v1/contextualizedembeddings")] = voyage_contextual
    provider = voyage(vendor, "voyage-context-4")
    other = dict(thread={"external_id": "other-thread"})
    ingest(
        client,
        [
            message(0, content="first chunk of the long thread"),
            message(4000, content="second chunk of the long thread"),
            message(1, external_id="o-1", content="another thread", **other),
        ],
    )
    with providers.override("embedding", provider):
        assert embeddings.run_once() == 3
        documents = vendor.requests[-1][2]["inputs"]
        assert sorted(map(len, documents)) == [1, 2]
        assert [
            "first chunk of the long thread",
            "second chunk of the long thread",
        ] in documents
        # A later chunk of the thread is embedded with the earlier ones in view,
        # and only its own vector is stored.
        ingest(client, [message(8000, content="third chunk of the long thread")])
        assert embeddings.run_once() == 1
        assert vendor.requests[-1][2]["inputs"] == [
            [
                "first chunk of the long thread",
                "second chunk of the long thread",
                "third chunk of the long thread",
            ]
        ]
        # A search is one chunk, embedded as a query.
        embeddings.embed_search("long thread")
        assert vendor.requests[-1][2] == {
            "inputs": [["long thread"]],
            "model": "voyage-context-4",
            "input_type": "query",
            "output_dimension": 4,
        }
    with connect() as c:
        stored = c.execute(
            "SELECT c.content,e.embedding::text v FROM tl.embeddings e JOIN tl.conversations c ON c.id=e.conversation_id ORDER BY c.thread_id,c.chunk_no"
        ).fetchall()
    by_text = {r["content"]: r["v"] for r in stored}
    assert len(stored) == 4
    # [chunks sent together, position in the document, 0, 1]
    assert by_text["second chunk of the long thread"] == "[2,1,0,1]"
    assert by_text["third chunk of the long thread"] == "[3,2,0,1]"
    assert by_text["another thread"] == "[1,0,0,1]"


def test_contextual_passages_are_embedded_per_page(client, vendor):
    vendor.routes[("POST", "/v1/contextualizedembeddings")] = voyage_contextual
    page_channel(client)
    put(
        client,
        [
            page(
                0,
                "manual",
                "# Pruning\n\n"
                + "Cut the old wood in winter. " * 30
                + "\n\n# Grafting\n\n"
                + "Join the scion to the rootstock. " * 30,
            )
        ],
    )
    assert build() == 1
    with providers.override("embedding", voyage(vendor, "voyage-context-4")):
        assert passages.embed_once() == 2
    documents = vendor.requests[-1][2]["inputs"]
    assert len(documents) == 1 and len(documents[0]) == 2
    assert documents[0][0].startswith("# Pruning")
    assert count("passage_embeddings") == 2


# --- historic load through a batch interface ---------------------------------------


class VoyageJobs:
    """The Files and Batches endpoints, answering every uploaded request."""

    def __init__(self, vendor, drop=()):
        self.lines = []
        self.params = None
        self.status = "in_progress"
        self.drop = set(drop)
        vendor.routes[("POST", "/v1/files")] = self.upload
        vendor.routes[("POST", "/v1/batches")] = self.create
        vendor.routes[("GET", "/v1/batches/batch_1")] = self.poll
        vendor.routes[("GET", "/v1/files/out_1/content")] = self.output
        self.vendor = vendor

    def upload(self, body):
        text = body.decode("utf-8")
        assert 'name="purpose"' in text and "batch" in text
        self.lines = [
            json.loads(line) for line in text.splitlines() if line.startswith("{")
        ]
        return {"id": "file_1"}

    def create(self, body):
        self.params = body
        return {"id": "batch_1", "status": "validating"}

    def poll(self, body):
        return {"id": "batch_1", "status": self.status, "output_file_id": "out_1"}

    def output(self, body):
        rows = []
        for line in self.lines:
            if line["custom_id"] in self.drop:
                rows.append(
                    {
                        "custom_id": line["custom_id"],
                        "response": None,
                        "error": {"code": "x"},
                    }
                )
                continue
            if "inputs" in line["body"]:
                answer = voyage_contextual(line["body"])
            else:
                answer = voyage_embeddings(line["body"])
            rows.append(
                {
                    "custom_id": line["custom_id"],
                    "response": {"status_code": 200, "body": answer},
                    "error": None,
                }
            )
        return "\n".join(json.dumps(r) for r in reversed(rows)).encode()


def test_historic_load_through_voyage_batch_jobs(client, channel, vendor, monkeypatch):
    jobs = VoyageJobs(vendor)
    vendor.routes[("POST", "/v1/embeddings")] = voyage_embeddings
    ingest(client, [message(n * 4000, content=f"note {'x' * n}") for n in range(5)])
    provider = voyage(vendor)
    with providers.override("embedding", provider):
        submitted = embedding_batches.submit()
        assert submitted["texts"] == 5 and len(submitted["jobs"]) == 1
        assert jobs.params == {
            "endpoint": "/v1/embeddings",
            "completion_window": "12h",
            "request_params": {
                "model": "voyage-4",
                "input_type": "document",
                "output_dimension": 4,
            },
            "input_file_id": "file_1",
        }
        assert sum(len(line["body"]["input"]) for line in jobs.lines) == 5
        # The job holds the texts: nothing is submitted or embedded twice.
        assert embedding_batches.submit()["texts"] == 0
        assert embeddings.run_once() == 0
        assert not any(r[1] == "/v1/embeddings" for r in vendor.requests)
        assert count("embeddings") == 0
        with connect() as c:
            assert not c.execute(
                "SELECT 1 FROM tl.embedding_models WHERE active"
            ).fetchone()
        # Not answered yet, and not asked again within the poll interval.
        assert embedding_batches.collect(force=True)["pending"] == 1
        polls = len([r for r in vendor.requests if r[0] == "GET"])
        assert embedding_batches.collect()["pending"] == 1
        assert len([r for r in vendor.requests if r[0] == "GET"]) == polls
        jobs.status = "completed"
        # The worker stores the answer on its next pass and the model serves.
        monkeypatch.setenv("THROUGHLINE_EMBEDDING_BATCH_POLL_S", "0")
        assert embeddings.run_once() == 5
    with connect() as c:
        job = c.execute("SELECT * FROM tl.embedding_batches").fetchone()
        ledger = c.execute("SELECT tokens,cost FROM tl.embedding_ledger").fetchall()
        assert c.execute("SELECT active FROM tl.embedding_models").fetchone()["active"]
    assert (job["status"], job["items"], job["stored"], job["tokens"]) == (
        "completed",
        5,
        5,
        50,
    )
    # Two thirds of the price of 1.0 per million tokens.
    assert [(r["tokens"], float(r["cost"])) for r in ledger] == [(50, 50 * 0.67 / 1e6)]
    assert count("embeddings") == 5 and count("embedding_batch_items") == 0


def test_batch_job_that_fails_or_answers_in_part_loses_nothing(
    client, channel, vendor, monkeypatch
):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_BATCH_POLL_S", "0")
    monkeypatch.setattr(batch.VoyageBatch, "request_items", 2)
    vendor.routes[("POST", "/v1/embeddings")] = voyage_embeddings
    ingest(client, [message(n * 4000, content=f"note {'x' * n}") for n in range(4)])
    with providers.override("embedding", voyage(vendor)):
        jobs = VoyageJobs(vendor, drop={"r1"})
        assert embedding_batches.submit()["jobs"][0]["requests"] == 2
        jobs.status = "completed"
        assert embedding_batches.collect() == dict(
            completed=1, failed=0, pending=0, other_provider=0, stored=2
        )
        assert count("embeddings") == 2
        # The unanswered half is pending again; a second job fails outright.
        jobs = VoyageJobs(vendor)
        assert embedding_batches.submit()["texts"] == 2
        jobs.status = "failed"
        assert embedding_batches.collect()["failed"] == 1
        assert count("embedding_batch_items") == 0
        # The ordinary worker finishes what the jobs did not.
        assert embeddings.run_once() == 2
    assert count("embeddings") == 4
    with connect() as c:
        assert [
            r["status"]
            for r in c.execute("SELECT status FROM tl.embedding_batches ORDER BY id")
        ] == ["completed", "failed"]


def test_worker_hands_a_backlog_over_only_in_batch_mode(
    client, channel, vendor, monkeypatch
):
    jobs = VoyageJobs(vendor)
    vendor.routes[("POST", "/v1/embeddings")] = voyage_embeddings
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_HISTORIC", "batch")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_BATCH_MIN", "3")
    with providers.override("embedding", voyage(vendor)):
        # Below the minimum: embedded directly, as records arrive.
        ingest(client, [message(0), message(4000, content="ocean")])
        assert embeddings.run_once() == 2 and count("embedding_batches") == 0
        # A backlog goes to the batch interface.
        ingest(
            client, [message(n * 4000, content=f"note {'x' * n}") for n in range(2, 6)]
        )
        assert embeddings.run_once() == 4
        assert count("embedding_batches", "status='submitted'") == 1
        assert count("embeddings") == 2 and jobs.params is not None
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_HISTORIC", "sometimes")
    with pytest.raises(ValueError, match="sync or batch"):
        embedding_batches.mode()


def test_contextual_batch_job_stores_only_the_pending_chunks(client, channel, vendor):
    jobs = VoyageJobs(vendor)
    vendor.routes[("POST", "/v1/contextualizedembeddings")] = voyage_contextual
    with providers.override("embedding", voyage(vendor, "voyage-context-4")):
        ingest(client, [message(0, content="first chunk")])
        assert embeddings.run_once() == 1
        ingest(client, [message(4000, content="second chunk")])
        assert embedding_batches.submit()["texts"] == 1
        assert jobs.params["endpoint"] == "/v1/contextualizedembeddings"
        assert jobs.lines[0]["body"] == {"inputs": [["first chunk", "second chunk"]]}
        jobs.status = "completed"
        assert embedding_batches.collect(force=True)["stored"] == 1
    with connect() as c:
        stored = {
            r["content"]: r["v"]
            for r in c.execute(
                "SELECT c.content,e.embedding::text v FROM tl.embeddings e JOIN tl.conversations c ON c.id=e.conversation_id"
            )
        }
    # The context chunk keeps the vector it had; the new one saw both chunks.
    assert stored == {"first chunk": "[1,0,0,1]", "second chunk": "[2,1,0,1]"}


def test_historic_load_through_a_gemini_batch_job(client, channel, vendor):
    state = dict(done=False, created=None)

    def create(body):
        state["created"] = body
        return {
            "name": "operations/op-1",
            "metadata": {"name": "batches/b-1", "state": "BATCH_STATE_PENDING"},
        }

    def poll(body):
        if not state["done"]:
            return {"name": "batches/b-1", "metadata": {"state": "BATCH_STATE_RUNNING"}}
        requests = state["created"]["batch"]["inputConfig"]["requests"]["requests"]
        return {
            "name": "batches/b-1",
            "metadata": {"state": "BATCH_STATE_SUCCEEDED"},
            "done": True,
            "response": {
                "output": {
                    "inlinedResponses": {
                        "inlinedResponses": [
                            {
                                "response": {
                                    "embedding": {"values": [0.0, 2.0, 0.0, 0.0]},
                                    "usageMetadata": {"promptTokenCount": 3},
                                }
                            }
                            for _ in requests
                        ]
                    }
                }
            },
        }

    path = "/v1/models/gemini-embedding-2:asyncBatchEmbedContent"
    vendor.routes[("POST", path)] = create
    vendor.routes[("GET", "/v1/batches/b-1")] = poll
    ingest(client, [message(0), message(4000, content="ocean")])
    provider = vendors.GeminiEmbedder(
        vendor.base,
        "gemini-embedding-2",
        4,
        key="synthetic-google",
        price_per_million=2,
    )
    with providers.override("embedding", provider):
        assert embedding_batches.submit()["texts"] == 2
        sent = state["created"]["batch"]
        assert sent["model"] == "models/gemini-embedding-2"
        first = sent["inputConfig"]["requests"]["requests"][0]
        assert first["request"]["content"]["parts"][0]["text"].startswith(
            "title: none | text: "
        )
        assert first["request"]["outputDimensionality"] == 4
        assert vendor.requests[-1][3]["x-goog-api-key"] == "synthetic-google"
        assert embedding_batches.collect(force=True)["pending"] == 1
        state["done"] = True
        assert embedding_batches.collect(force=True)["stored"] == 2
    with connect() as c:
        vectors = [
            r["v"] for r in c.execute("SELECT embedding::text v FROM tl.embeddings")
        ]
        ledger = c.execute("SELECT tokens,cost FROM tl.embedding_ledger").fetchone()
    assert vectors == ["[0,1,0,0]", "[0,1,0,0]"]  # normalised
    # Half the price of 2.0 per million tokens.
    assert (ledger["tokens"], float(ledger["cost"])) == (6, 6 / 1e6)


def test_batch_needs_a_provider_with_a_batch_interface(client, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    with pytest.raises(ValueError, match="no batch interface"):
        embedding_batches.submit()
    assert embedding_batches.collect() == dict(
        completed=0, failed=0, pending=0, other_provider=0, stored=0
    )


# --- nearest-neighbour index ------------------------------------------------------


def test_index_is_built_per_model_and_ranking_matches_exact(
    client, channel, monkeypatch
):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "32")
    words = "orchard ocean stone wall cider barrel ladder harvest frost river".split()
    ingest(
        client,
        [
            message(
                n * 4000, content=f"{words[n % 10]} {words[(n * 3 + 1) % 10]} note {n}"
            )
            for n in range(60)
        ],
    )
    while embeddings.run_once():
        pass
    query = embeddings.vector(embeddings.embed(["cider barrel in the orchard"])[0][0])
    with connect() as c:
        name = c.execute(
            "SELECT tl.embedding_index_name('embeddings','local-hash-v1',32) n"
        ).fetchone()["n"]
        index = c.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='tl' AND indexname=%s",
            (name,),
        ).fetchone()["indexdef"]
        assert "hnsw" in index and "halfvec(32)" in index and "local-hash-v1" in index
        ids = [
            r["id"] for r in c.execute("SELECT id FROM tl.conversations ORDER BY id")
        ]
        exact = c.execute(
            "SELECT cid,sim FROM tl.nearest_conversations(%s::vector,'local-hash-v1',NULL,%s) ORDER BY sim DESC,cid",
            (query, ids),
        ).fetchall()
        assert len(exact) == 60
        # Force the index path: no eligible set is small enough to be exact.
        c.execute("SET throughline.ann_exact_limit=0")
        plan = "\n".join(
            r["QUERY PLAN"]
            for r in c.execute(
                f"EXPLAIN SELECT e.conversation_id FROM tl.embeddings e WHERE e.model='local-hash-v1' AND vector_dims(e.embedding)=32 ORDER BY e.embedding::halfvec(32) <=> '{query}'::halfvec(32) LIMIT 5"
            )
        )
        assert name in plan
        near = c.execute(
            "SELECT cid,sim FROM tl.nearest_conversations(%s::vector,'local-hash-v1',5,%s) ORDER BY sim DESC,cid",
            (query, ids),
        ).fetchall()
        # Scored with the stored vector, so equal to the exact similarities.
        top = {r["cid"]: r["sim"] for r in exact[:5]}
        found = {r["cid"]: r["sim"] for r in near}
        assert len(near) >= 40 and set(top) <= set(found)
        assert all(found[cid] == pytest.approx(sim) for cid, sim in top.items())
        # The eligible set is applied inside the index scan: with ten of the
        # conversations eligible the scan cannot fill its quota, so the
        # exact path answers, with exactly those ten.
        few = c.execute(
            "SELECT cid FROM tl.nearest_conversations(%s::vector,'local-hash-v1',5,%s)",
            (query, ids[:10]),
        ).fetchall()
        assert {r["cid"] for r in few} == set(ids[:10])
        c.execute("RESET throughline.ann_exact_limit")
    # The same model name at another size replaces the index.
    with connect() as c:
        c.execute("TRUNCATE tl.embeddings")
        c.execute("UPDATE tl.embedding_models SET dimensions=8")
        c.execute("SELECT tl.ensure_embedding_index('local-hash-v1')")
        names = [
            r["indexname"]
            for r in c.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='tl' AND tablename='embeddings' AND indexname LIKE 'embeddings_ann_'||substr(md5('local-hash-v1'),1,16)||'%'"
            )
        ]
        assert names == [
            c.execute(
                "SELECT tl.embedding_index_name('embeddings','local-hash-v1',8) n"
            ).fetchone()["n"]
        ]


def test_recall_ranks_through_the_index_like_the_exact_path(
    client, channel, monkeypatch
):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "32")
    words = "orchard ocean stone wall cider barrel ladder harvest frost river".split()
    ingest(
        client,
        [
            message(
                n * 4000, content=f"{words[n % 10]} {words[(n * 7 + 3) % 10]} entry {n}"
            )
            for n in range(80)
        ],
    )
    while embeddings.run_once():
        pass

    query = embeddings.vector(embeddings.embed(["cider barrel"])[0][0])
    call = "SELECT message_id,score FROM api.vector_records(%s::vector,5,query_text=>'cider barrel')"
    with connect() as c:
        exact = c.execute(call, (query,)).fetchall()
        c.execute("SET throughline.ann_exact_limit=0")
        indexed = c.execute(call, (query,)).fetchall()
    assert len(exact) == 5
    assert [r["score"] for r in indexed] == pytest.approx([r["score"] for r in exact])
    # The HTTP search goes through the same function.
    rows = client.get("/v1/search", params=dict(q="cider barrel", mode="meaning"))
    assert rows.status_code == 200, rows.text
    assert rows.json()["rows"][0]["score"] == pytest.approx(exact[0]["score"])


def test_runtime_roles_cannot_rank_outside_the_gated_functions(client):
    for role in ("app", "reader"):
        with connect(role) as c:
            for call in (
                "tl.nearest_conversations('[1,0]'::public.vector,'m',5,ARRAY[1]::bigint[])",
                "tl.nearest_passage_texts('[1,0]'::public.vector,'m',5,ARRAY['x'])",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute("SELECT * FROM " + call)
                c.rollback()


def test_vector_passages_through_the_index(client, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "32")
    page_channel(client)
    put(
        client,
        [
            page(
                n,
                f"page-{n}",
                f"# Page {n}\n\nThe {word} is described here in entry {n}.",
            )
            for n, word in enumerate(
                "orchard ocean cellar ladder harvest frost river wall barrel stone".split()
                * 5
            )
        ],
    )
    assert build() == 50
    while embeddings.run_once():
        pass
    query = embeddings.vector(embeddings.embed(["the cellar is described"])[0][0])
    with connect() as c:
        exact = c.execute(
            "SELECT passage_id,score FROM api.vector_passages(%s::vector,5)", (query,)
        ).fetchall()
        c.execute("SET throughline.ann_exact_limit=0")
        indexed = c.execute(
            "SELECT passage_id,score FROM api.vector_passages(%s::vector,5)", (query,)
        ).fetchall()
    assert len(exact) == 5
    assert [r["score"] for r in indexed] == pytest.approx([r["score"] for r in exact])
    assert math.isclose(exact[0]["score"], max(r["score"] for r in exact))
