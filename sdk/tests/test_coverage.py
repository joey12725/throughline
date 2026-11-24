import json
from datetime import datetime, timezone

import httpx
import pytest

from throughline_sdk import (
    Channel,
    Checkpoint,
    Client,
    Declaration,
    Manifest,
    Plugin,
    coverage,
)


def fake_message(n, channel="fake.chat"):
    return {
        "channel": channel,
        "external_id": f"fake-{n}",
        "thread": {"external_id": "fake-thread"},
        "happened_at": f"2020-01-0{n}T00:00:00+00:00",
        "content": f"Invented event {n}",
        "payload": {"fake": n},
    }


class Synthetic(Plugin):
    manifest = Manifest(
        name="fake",
        version="1",
        channels=[
            Channel(name="fake.chat", description="Invented events"),
            Channel(name="fake.notes", description="Invented notes"),
        ],
        secrets={"secret": Declaration(env="FAKE_COVERAGE_SECRET", required=False)},
    )

    def records(self, cursor, mode):
        yield fake_message(1)
        yield fake_message(2)
        yield Checkpoint(cursor={"offset": 2})


class Failing(Synthetic):
    def records(self, cursor, mode):
        yield fake_message(1)
        raise RuntimeError("source broke with " + (self.secrets["secret"] or ""))


class Reporting(Synthetic):
    def records(self, cursor, mode):
        yield fake_message(1)
        yield coverage(
            "fake.chat",
            status="partial",
            reasons=["rate_limited"],
            window=(
                datetime(2020, 1, 1, tzinfo=timezone.utc),
                datetime(2020, 1, 2, tzinfo=timezone.utc),
            ),
            rows_delivered=1,
        )
        yield fake_message(2)
        yield fake_message(3, channel="fake.notes")


def serve_with(requests, answers=None):
    answers = answers or {}

    def serve(request):
        requests.append(request)
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/coverage/gaps"):
                return httpx.Response(200, json=answers.get("gaps", {"gaps": []}))
            return httpx.Response(404)
        if path.endswith("/coverage"):
            return httpx.Response(
                200,
                json=answers.get(
                    "coverage",
                    {
                        "stored": 1,
                        "degraded": [],
                        "refused": 0,
                        "gaps_opened": [],
                        "gaps_closed": [],
                    },
                ),
            )
        return httpx.Response(200, json={"rejected": []})

    return serve


def client_for(requests, answers=None):
    return Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(serve_with(requests, answers)),
    )


def calls(requests):
    out = []
    for r in requests:
        if r.method == "GET":
            continue
        tail = r.url.path.split("/v1/", 1)[1]
        out.append((tail, json.loads(r.content) if r.content else None))
    return out


def test_runner_reports_attempt_success_and_failure(monkeypatch):
    requests = []
    client = client_for(requests)
    try:
        report = Synthetic().execute(client, "backfill")
    finally:
        client.close()
    sent = calls(requests)
    paths = [p for p, _ in sent]
    assert paths.index("plugins/fake/coverage/attempt") < paths.index("ingest/messages")
    attempt = dict(sent)["plugins/fake/coverage/attempt"]
    assert attempt == {"channels": ["fake.chat", "fake.notes"]}
    # A channel that delivered without a coverage record gets best_effort.
    (body,) = [b for p, b in sent if p == "plugins/fake/coverage"]
    assert body["reports"] == [
        {
            "channel": "fake.chat",
            "status": "best_effort",
            "rows_delivered": 2,
            "high_water": "2020-01-02T00:00:00+00:00",
        }
    ]
    assert paths.index("plugins/fake/coverage") < paths.index("plugins/fake/heartbeat")
    assert report["coverage"]["automatic"] == 1

    monkeypatch.setenv("FAKE_COVERAGE_SECRET", "fake-canary")
    requests.clear()
    client = client_for(requests)
    try:
        with pytest.raises(RuntimeError, match="source broke"):
            Failing().execute(client, "backfill")
    finally:
        client.close()
    sent = calls(requests)
    failures = [b for p, b in sent if p == "plugins/fake/coverage"]
    assert len(failures) == 1
    reports = failures[0]["reports"]
    assert [r["channel"] for r in reports] == ["fake.chat", "fake.notes"]
    for r in reports:
        assert r["status"] == "partial" and r["reasons"] == ["run_failed"]
        assert r["detail"]["error"] == "RuntimeError"
    assert "fake-canary" not in json.dumps(failures)
    assert "[REDACTED]" in reports[0]["detail"]["message"]

    # A failing coverage endpoint never masks the original error.
    def broken(request):
        if "/coverage" in request.url.path:
            return httpx.Response(500)
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200, json={"rejected": []})

    monkeypatch.setattr("throughline_sdk.client.time.sleep", lambda _: None)
    client = Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(broken),
    )
    try:
        with pytest.raises(RuntimeError, match="source broke"):
            Failing().execute(client, "backfill")
    finally:
        client.close()


def test_coverage_record_delivered_after_preceding_messages():
    requests = []
    answers = {
        "coverage": {
            "stored": 1,
            "degraded": [],
            "refused": 0,
            "gaps_opened": [{"index": 0, "id": 7, "from": "a", "to": "b"}],
            "gaps_closed": [],
        }
    }
    client = client_for(requests, answers)
    try:
        report = Reporting().execute(client, "backfill")
    finally:
        client.close()
    sent = [
        (p, b)
        for p, b in calls(requests)
        if p in ("ingest/messages", "plugins/fake/coverage")
    ]
    kinds = [p.rsplit("/", 1)[-1] for p, _ in sent]
    assert kinds[:3] == ["messages", "coverage", "messages"]
    first_batch = sent[0][1]["messages"]
    assert [m["external_id"] for m in first_batch] == ["fake-1"]
    reported = sent[1][1]["reports"][0]
    assert reported["channel"] == "fake.chat" and reported["status"] == "partial"
    assert reported["window"] == {
        "from": "2020-01-01T00:00:00+00:00",
        "to": "2020-01-02T00:00:00+00:00",
    }
    # Only the channel without a coverage record gets the automatic report.
    automatic = [b for p, b in sent if p == "plugins/fake/coverage"][1]["reports"]
    assert [r["channel"] for r in automatic] == ["fake.notes"]
    assert report["coverage"]["records"] == 1
    assert report["coverage"]["gaps_opened"] == [7, 7]


def test_dry_run_sends_no_coverage():
    requests = []
    client = client_for(requests)
    try:
        report = Reporting().execute(client, "backfill", dry_run=True)
    finally:
        client.close()
    assert all("/coverage" not in r.url.path for r in requests)
    assert "coverage" not in report
    requests.clear()
    client = client_for(requests)
    try:
        with pytest.raises(RuntimeError):
            Failing().execute(client, "backfill", dry_run=True)
    finally:
        client.close()
    assert all("/coverage" not in r.url.path for r in requests)


def test_client_coverage_gaps_and_close_by_id():
    requests = []
    gap = {
        "id": 12,
        "channel": "fake.chat",
        "scope_key": "",
        "from": "2020-01-01T00:00:00+00:00",
        "to": "2020-01-02T00:00:00+00:00",
        "reason": "rate_limited",
    }
    answers = {
        "gaps": {"gaps": [gap]},
        "coverage": {
            "stored": 1,
            "degraded": [],
            "refused": 0,
            "gaps_opened": [],
            "gaps_closed": [12],
        },
    }
    client = client_for(requests, answers)
    try:
        gaps = client.coverage_gaps("fake.chat", scope_key="")
        assert gaps == [gap]
        answer = client.coverage(
            [
                coverage(
                    "fake.chat", status="complete", closes=[g["id"] for g in gaps]
                ),
                coverage(
                    "fake.chat",
                    status="best_effort",
                    closes=[
                        {
                            "channel": "fake.chat",
                            "scope_key": "",
                            "from": datetime(2020, 1, 1, tzinfo=timezone.utc),
                            "to": datetime(2020, 1, 2, tzinfo=timezone.utc),
                        }
                    ],
                    verified=True,
                ),
            ]
        )
    finally:
        client.close()
    assert answer["gaps_closed"] == [12]
    listing = next(r for r in requests if r.url.path.endswith("/coverage/gaps"))
    assert listing.url.params["channel"] == "fake.chat"
    body = json.loads(
        next(r for r in requests if r.url.path.endswith("/coverage")).content
    )
    first, second = body["reports"]
    assert first["closes"] == [12] and first["status"] == "complete"
    assert second["closes"][0]["from"] == "2020-01-01T00:00:00+00:00"
    assert second["verified"] is True
