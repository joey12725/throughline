"""The SDK side of the write contract: idempotency keys on
record writes, consecutive links through POST /v1/links/batch, vocabulary
refusals counted instead of failing, authorization failures raised, message
aliases passed through. Fake transport only."""

import json

import httpx
import pytest

from throughline_sdk import Channel, Checkpoint, Client, Manifest, Plugin, Record
from throughline_sdk import coverage, provenance


def fake_message(n, **extra):
    return {
        "channel": "fake.chat",
        "external_id": f"fake-{n}",
        "thread": {"external_id": "fake-thread"},
        "happened_at": f"2020-01-0{n}T00:00:00+00:00",
        "content": f"Invented event {n}",
        "payload": {"fake": n},
        **extra,
    }


def fake_link(a, b, type_="about"):
    return Record(
        kind="link",
        data={
            "type": type_,
            "from": {"channel": "fake.chat", "external_id": f"fake-{a}"},
            "to": {"channel": "fake.chat", "external_id": f"fake-{b}"},
        },
    )


class Linking(Plugin):
    manifest = Manifest(
        name="fake",
        version="1",
        channels=[Channel(name="fake.chat", description="Invented events")],
    )

    def records(self, cursor, mode):
        yield fake_message(1, aliases=["fake-provisional-1"])
        yield fake_message(2)
        yield fake_link(1, 2)
        yield fake_link(2, 1, "replies_to")
        yield fake_link(1, 2, "fake_hummed")
        yield provenance(
            {"channel": "fake.chat", "external_id": "fake-2"},
            [{"channel": "fake.chat", "external_id": "fake-1"}],
        )
        yield fake_link(2, 2, "fake_guess")
        yield coverage("fake.chat", status="complete")
        yield Checkpoint(cursor={"offset": 2})


def serve_with(batch_answer):
    requests = []

    def serve(request):
        requests.append(request)
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(404)
        if path.endswith("/links/batch"):
            return batch_answer(json.loads(request.content))
        if path.endswith("/provenance"):
            return httpx.Response(200, json={"written": 1, "unresolved": 0})
        if path.endswith("/coverage"):
            return httpx.Response(200, json={"stored": 1, "refused": 0})
        return httpx.Response(200, json={"rejected": []})

    return requests, serve


def answer_all(status="created"):
    def answer(body):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": i, "status": status, "reason": None, "link_id": i + 1}
                    for i in range(len(body["links"]))
                ]
            },
        )

    return answer


def run(plugin, serve):
    client = Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(serve),
    )
    try:
        return client, plugin.execute(client, "backfill")
    finally:
        client.close()


def posts(requests, suffix):
    return [r for r in requests if r.method != "GET" and r.url.path.endswith(suffix)]


def test_sdk_idempotency_keys_and_link_batches():
    requests, serve = serve_with(answer_all())
    client, report = run(Linking(), serve)
    order = [
        r.url.path.rsplit("/v1/", 1)[-1]
        for r in requests
        if r.method != "GET"
        and r.url.path.endswith(
            ("ingest/messages", "links/batch", "provenance", "cursor/default")
        )
    ]
    # Messages first, then the three consecutive links in one batch, the
    # provenance record, the single link, and the checkpoint last.
    assert order == [
        "ingest/messages",
        "links/batch",
        "provenance",
        "links/batch",
        "plugins/fake/cursor/default",
    ]
    batches = posts(requests, "/links/batch")
    first, second = (json.loads(r.content) for r in batches)
    assert [link["type"] for link in first["links"]] == [
        "about",
        "replies_to",
        "fake_hummed",
    ]
    assert [link["type"] for link in second["links"]] == ["fake_guess"]
    keys = []
    for request in batches:
        key = request.headers["Idempotency-Key"]
        assert key.startswith("tl-") and 8 <= len(key) <= 200
        keys.append(key)
    (prov,) = posts(requests, "/provenance")
    keys.append(prov.headers["Idempotency-Key"])
    keyed_coverage = [
        r for r in posts(requests, "/coverage") if "Idempotency-Key" in r.headers
    ]
    assert keyed_coverage
    assert len(set(keys)) == len(keys)
    # Keys are deterministic per plugin, route, delivery and record, and
    # differ across each of them.
    data = {"type": "about", "from": {"kind": "entity", "id": 1}}
    assert client.idempotency_key("links", data, "d1") == client.idempotency_key(
        "links", dict(reversed(list(data.items()))), "d1"
    )
    assert client.idempotency_key("links", data, "d1") != client.idempotency_key(
        "provenance", data, "d1"
    )
    assert client.idempotency_key("links", data, "d1") != client.idempotency_key(
        "links", data, "d2"
    )
    other = Client("http://fake.invalid", "fake-token", "other")
    assert other.idempotency_key("links", data, "d1") != client.idempotency_key(
        "links", data, "d1"
    )
    other.close()
    # Message aliases pass through unchanged.
    (ingested,) = posts(requests, "/ingest/messages")
    messages = json.loads(ingested.content)["messages"]
    assert messages[0]["aliases"] == ["fake-provisional-1"]
    assert "aliases" not in messages[1]
    assert report["links"]["records"] == 4 and report["links"]["created"] == 4


def test_sdk_refused_links_counted_not_fatal():
    statuses = iter(["created", "folded", "candidate", "refused"])

    def answer(body):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "index": i,
                        "status": next(statuses),
                        "reason": None,
                        "link_id": None,
                    }
                    for i in range(len(body["links"]))
                ]
            },
        )

    requests, serve = serve_with(answer)
    _, report = run(Linking(), serve)
    assert report["links"] == {
        "records": 4,
        "created": 1,
        "existing": 0,
        "folded": 1,
        "candidate": 1,
        "refused": 1,
        "error": 0,
    }
    # The run finished: the checkpoint was written after the links.
    assert posts(requests, "/cursor/default")


def test_sdk_foreign_channel_link_raises():
    def answer(body):
        return httpx.Response(
            403, json={"detail": "a link of this batch is not allowed for this token"}
        )

    requests, serve = serve_with(answer)
    with pytest.raises(httpx.HTTPStatusError) as error:
        run(Linking(), serve)
    assert error.value.response.status_code == 403
    # Nothing after the refused batch was delivered, the cursor included.
    assert not posts(requests, "/cursor/default")
    assert not posts(requests, "/provenance")


def test_sdk_auth_failure_still_raises(monkeypatch):
    monkeypatch.setattr("throughline_sdk.client.time.sleep", lambda _: None)

    def answer(body):
        return httpx.Response(401, json={"detail": "invalid or revoked token"})

    requests, serve = serve_with(answer)
    with pytest.raises(httpx.HTTPStatusError) as error:
        run(Linking(), serve)
    assert error.value.response.status_code == 401
    # Not retried: an authentication failure is not transient.
    assert len(posts(requests, "/links/batch")) == 1
    assert not posts(requests, "/cursor/default")


def test_sdk_link_batch_falls_back_to_single_links_on_older_cores():
    singles = []

    def serve(request):
        if request.url.path.endswith("/links/batch"):
            return httpx.Response(404, json={"detail": "Not Found"})
        body = json.loads(request.content)
        singles.append(request)
        if body["type"] == "fake_guess":
            return httpx.Response(
                400, json={"detail": "database operation rejected", "code": "23514"}
            )
        return httpx.Response(200, json={"id": len(singles)})

    client = Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(serve),
    )
    try:
        results = client.link_batch(
            [fake_link(1, 2).data, fake_link(2, 2, "fake_guess").data]
        )
    finally:
        client.close()
    assert [r["status"] for r in results] == ["created", "refused"]
    assert all("Idempotency-Key" in r.headers for r in singles)


def test_sdk_key_kept_across_retries_new_for_each_delivery():
    """A retried request keeps its key, so a retry of a write the core already
    committed replays; a later delivery of the same record gets a new key, so
    the core resolves it again (a re-sent citation whose source has been
    imported since, BRN-6)."""
    seen = []

    def serve(request):
        seen.append(request.headers["Idempotency-Key"])
        if len(seen) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"written": 1, "unresolved": 0})

    client = Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(serve),
    )
    record = {
        "derived": {"channel": "fake.chat", "external_id": "fake-2"},
        "sources": [{"channel": "fake.chat", "external_id": "fake-1"}],
    }
    try:
        client.provenance(record)
        client.provenance(record)
    finally:
        client.close()
    assert len(seen) == 3
    assert seen[0] == seen[1]
    assert seen[2] != seen[0]
