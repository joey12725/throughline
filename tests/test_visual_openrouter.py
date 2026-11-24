"""gemini-embedding-2 through OpenRouter: request shape, response parsing and
provider selection (offline; http.request is replaced)."""

import pytest

from throughline.providers import visual
from throughline.providers.base import ProviderUnavailable

DIMS = 4


def _fake(calls, dims=DIMS):
    def request(method, url, headers=None, payload=None, timeout=None, what=None, **kw):
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        n = len(payload["input"]) if isinstance(payload["input"], list) else 1
        rows = [{"index": i, "embedding": [float(i + 1)] + [0.0] * (dims - 1)} for i in range(n)]
        return {"data": list(reversed(rows)), "usage": {"prompt_tokens": 258 * n}}

    return request


def _provider(monkeypatch, **env):
    for name in ("THROUGHLINE_VISUAL_EMBEDDING_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                 "THROUGHLINE_VISUAL_EMBEDDING_BASE_URL", "THROUGHLINE_VISUAL_EMBEDDING_ROUTE",
                 "THROUGHLINE_VISUAL_EMBEDDING_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("THROUGHLINE_VISUAL_EMBEDDING_PROVIDER", "openrouter")
    monkeypatch.setenv("THROUGHLINE_VISUAL_EMBEDDING_DIMENSIONS", str(DIMS))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return visual.gemini()


def test_openrouter_selected_with_its_key_and_stored_model_name(monkeypatch):
    p = _provider(monkeypatch, OPENROUTER_API_KEY="sk-or-test")
    assert isinstance(p, visual.OpenRouterVisualEmbedder)
    assert p.model == "gemini-embedding-2" and p.route == "google/gemini-embedding-2"
    assert p.base == "https://openrouter.ai/api/v1" and p.price_per_million == 0.45


def test_openrouter_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable, match="OPENROUTER_API_KEY"):
        _provider(monkeypatch)


def test_openrouter_image_batch_request_and_order(monkeypatch):
    p = _provider(monkeypatch, OPENROUTER_API_KEY="sk-or-test")
    calls = []
    monkeypatch.setattr(visual.http, "request", _fake(calls))
    vectors, usage = p.embed_images([(b"\x89PNG", "image/png"), (b"\xff\xd8", None)])
    body = calls[0]["payload"]
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/embeddings"
    assert calls[0]["headers"] == {"Authorization": "Bearer sk-or-test"}
    assert body["model"] == "google/gemini-embedding-2" and body["dimensions"] == DIMS
    parts = [item["content"][0] for item in body["input"]]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # Rows come back out of order; they are placed by index.
    assert vectors[0][0] == pytest.approx(1.0) and vectors[1][0] == pytest.approx(1.0)
    assert len(vectors) == 2 and usage.tokens == 516


def test_openrouter_query_text_uses_search_prefix(monkeypatch):
    p = _provider(monkeypatch, OPENROUTER_API_KEY="sk-or-test")
    calls = []
    monkeypatch.setattr(visual.http, "request", _fake(calls))
    vectors, _ = p.embed(["red car"])
    assert calls[0]["payload"]["input"] == ["task: search result | query: red car"]
    assert len(vectors) == 1


def test_openrouter_short_vectors_rejected(monkeypatch):
    p = _provider(monkeypatch, OPENROUTER_API_KEY="sk-or-test")
    monkeypatch.setattr(visual.http, "request", _fake([], dims=DIMS - 1))
    with pytest.raises(ProviderUnavailable):
        p.embed_images([(b"x", "image/png")])


def test_image_batches_get_the_long_timeout(monkeypatch):
    _provider(monkeypatch, OPENROUTER_API_KEY="sk-or-test")
    seen = {}

    def fake_call(kind, purpose, run, **kw):
        return run()

    calls = []
    monkeypatch.setattr(visual, "provider_call", fake_call)
    monkeypatch.setattr(visual.http, "request", _fake(calls))
    visual.embed_images([(b"x", "image/png")], scope=object())
    seen["timeout"] = calls[0]["timeout"]
    assert seen["timeout"] == visual.IMAGE_TIMEOUT_S
