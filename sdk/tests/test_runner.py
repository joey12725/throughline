import json
import logging

import httpx
import pytest

from throughline_sdk import Channel, Checkpoint, Client, Declaration, Manifest, Plugin
from throughline_sdk.plugin import Redactor, contains_secret


def test_plg_6_secret_detection_handles_unicode_and_escaping():
    secret = "fake-é\\canary"
    assert contains_secret({"payload": [{"credential": secret}]}, [secret])
    assert not contains_secret({"payload": "invented"}, [secret])


def test_plg_6_contract_respects_http_batch_limit():
    from throughline_sdk._checks import ingest_messages

    def serve(request):
        count = len(json.loads(request.content)["messages"])
        if count > 5000:
            return httpx.Response(422)
        return httpx.Response(
            200,
            json={
                "received": count,
                "inserted": 0,
                "new_versions": 0,
                "unchanged": count,
                "rejected": [],
            },
        )

    with httpx.Client(
        base_url="http://fake.invalid/v1/", transport=httpx.MockTransport(serve)
    ) as client:
        assert ingest_messages(client, "fake", [{}] * 5001)["unchanged"] == 5001


class Synthetic(Plugin):
    manifest = Manifest(
        name="fake",
        version="1",
        channels=[Channel(name="fake.chat", description="Invented events")],
    )

    def records(self, cursor, mode):
        yield {
            "channel": "fake.chat",
            "external_id": "fake-1",
            "thread": {"external_id": "fake-thread"},
            "happened_at": "2020-01-01T00:00:00+00:00",
            "content": "Invented event",
            "payload": {"fake": True},
        }
        yield Checkpoint(cursor={"offset": 1})


def test_plg_4_retries_before_checkpoint(monkeypatch):
    attempts = []
    monkeypatch.setattr("throughline_sdk.client.time.sleep", lambda _: None)

    def serve(request):
        attempts.append((request.method, request.url.path))
        if request.url.path.endswith("/cursor/default") and request.method == "GET":
            return httpx.Response(404)
        if request.url.path.endswith("/ingest/messages"):
            count = sum(path.endswith("/ingest/messages") for _, path in attempts)
            if count == 1:
                return httpx.Response(503)
            if count == 2:
                raise httpx.ReadError("fake ambiguous write", request=request)
            return httpx.Response(
                200,
                json={
                    "received": 1,
                    "inserted": 0,
                    "unchanged": 1,
                    "new_versions": 0,
                    "rejected": [],
                },
            )
        return httpx.Response(200, json={})

    client = Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(serve),
    )
    try:
        Synthetic().execute(client, "backfill")
    finally:
        client.close()
    writes = [(method, path) for method, path in attempts if method == "PUT"]
    assert len(writes) == 1
    assert attempts.index(writes[0]) > max(
        i for i, (_, path) in enumerate(attempts) if path.endswith("/ingest/messages")
    )


def test_plg_4_rejection_never_advances_cursor():
    requests = []

    def serve(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        if request.url.path.endswith("/ingest/messages"):
            return httpx.Response(
                200, json={"rejected": [{"index": 0, "error": "fake rejection"}]}
            )
        return httpx.Response(200, json={})

    client = Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(serve),
    )
    try:
        with pytest.raises(ValueError, match="cursor retained"):
            Synthetic().execute(client, "backfill")
    finally:
        client.close()
    assert all(r.method != "PUT" for r in requests)


def test_plg_6_redacts_formatted_logs_and_tracebacks():
    try:
        raise ValueError("fake-canary")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "fake",
            logging.ERROR,
            "fake",
            1,
            "failure %s",
            ("fake-canary",),
            sys.exc_info(),
        )
    assert Redactor(["fake-canary"]).filter(record)
    assert "fake-canary" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()
    assert record.exc_info is None


def test_plg_3_dry_run_does_not_write_auxiliary_or_cursor():
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={"rejected": []})

    client = Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(serve),
    )
    try:
        report = Synthetic().execute(client, "backfill", dry_run=True)
    finally:
        client.close()
    assert report["counts"] == {"fake.chat": 1}
    assert all(
        "cursor" not in r.url.path and "heartbeat" not in r.url.path for r in requests
    )
    batch = next(r for r in requests if r.url.path.endswith("/ingest/messages"))
    assert json.loads(batch.content)["dry_run"] is True


def test_plg_6_runner_redacts_errors_and_failed_heartbeat(monkeypatch, capsys):
    from throughline_sdk.plugin import run

    class Broken(Synthetic):
        manifest = Synthetic.manifest.model_copy(
            update={"secrets": {"secret": Declaration(env="FAKE_TEST_SECRET")}}
        )

        def records(self, cursor, mode):
            logging.warning("source log %s", self.secrets["secret"])
            raise ValueError("source failure " + self.secrets["secret"])
            yield  # make this a generator

    class BrokenHeartbeat:
        def __init__(self, *_args):
            pass

        def register(self, manifest):
            pass

        def cursor(self):
            return {}

        def heartbeat(self, **_data):
            raise ValueError("fake-canary")

        def close(self):
            pass

    monkeypatch.setenv("FAKE_TEST_SECRET", "fake-canary")
    monkeypatch.setenv("THROUGHLINE_URL", "http://fake.invalid")
    monkeypatch.setenv("THROUGHLINE_TOKEN", "fake-token")
    monkeypatch.setattr("throughline_sdk.plugin.Client", BrokenHeartbeat)
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    try:
        with pytest.raises(SystemExit) as error:
            run(Broken, ["backfill"])
        assert error.value.code == 1
    finally:
        root.handlers = old_handlers
    output = capsys.readouterr()
    assert "fake-canary" not in output.err + output.out
    assert "[REDACTED]" in output.err


class Citing(Plugin):
    manifest = Manifest(
        name="fake",
        version="1",
        channels=[
            Channel(
                name="fake.pages",
                description="Invented pages",
                record_kind="derived",
            )
        ],
    )

    def records(self, cursor, mode):
        from throughline_sdk import provenance

        for n in (1, 2):
            yield {
                "channel": "fake.pages",
                "external_id": f"fake-page-{n}",
                "thread": {"external_id": "fake-thread"},
                "happened_at": "2020-01-01T00:00:00+00:00",
                "content": f"Invented page {n}",
                "payload": {"fake": n},
            }
            yield provenance(
                {"channel": "fake.pages", "external_id": f"fake-page-{n}"},
                [{"channel": "fake.notes", "external_id": f"fake-note-{n}"}],
                "cites",
            )
        yield Checkpoint(cursor={"offset": 2})


def test_brn_6_provenance_delivered_after_the_messages_it_cites():
    requests = []

    def serve(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        if request.url.path.endswith("/provenance"):
            return httpx.Response(200, json={"written": 1, "unresolved": 0})
        return httpx.Response(200, json={"rejected": []})

    client = Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(serve),
    )
    try:
        report = Citing().execute(client, "backfill")
    finally:
        client.close()
    order = [
        (r.url.path.rsplit("/", 1)[-1], json.loads(r.content) if r.content else None)
        for r in requests
        if r.url.path.endswith(("/ingest/messages", "/provenance", "/cursor/default"))
        and r.method != "GET"
    ]
    kinds = [path for path, _ in order]
    assert kinds == ["messages", "provenance", "messages", "provenance", "default"]
    # Each provenance record follows the batch holding the message it cites.
    for index in (1, 3):
        batch = order[index - 1][1]["messages"]
        derived = order[index][1]["derived"]
        assert derived["external_id"] in {m["external_id"] for m in batch}
        assert order[index][1]["relation"] == "cites"
    assert report["provenance"] == {"records": 2, "written": 2, "unresolved": 0}


def test_plg_3_dry_run_sends_no_provenance():
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={"rejected": []})

    client = Client(
        "http://fake.invalid",
        "fake-token",
        "fake",
        transport=httpx.MockTransport(serve),
    )
    try:
        report = Citing().execute(client, "backfill", dry_run=True)
    finally:
        client.close()
    assert report["counts"] == {"fake.pages": 2}
    assert "provenance" not in report
    assert all(not r.url.path.endswith("/provenance") for r in requests)


def test_sync_loop_survives_an_outage_and_backs_off():
    """A continuous sync keeps running through a source outage: each failed
    run sends a failure heartbeat and waits 1, 2, 4 ... minutes (capped at
    the interval); the first good run resumes and the wait resets."""
    from throughline_sdk.plugin import Redactor, _loop, retry_delays

    assert [d for _, d in zip(range(6), retry_delays(300))] == [
        60,
        120,
        240,
        300,
        300,
        300,
    ]
    assert next(retry_delays(30)) == 30

    outcomes = iter(["fail", "fail", "fail", "ok", "fail", "ok"])

    class Flaky(Synthetic):
        manifest = Synthetic.manifest.model_copy(update={"default_interval_s": 300})

        def execute(self, client, mode, dry_run=False):
            if next(outcomes) == "fail":
                raise ConnectionError("source unreachable")
            return {"counts": {}}

    class Recorder:
        def __init__(self):
            self.beats = []

        def heartbeat(self, **data):
            self.beats.append(data)

    client = Recorder()
    sleeps = []
    _loop(Flaky(), client, Redactor([]), sleep=sleeps.append, runs=6)
    assert sleeps == [60, 120, 240, 300, 60]
    assert [b.get("error") for b in client.beats] == ["source unreachable"] * 4


def test_catch_up_since_widens_a_fixed_lookback():
    from datetime import datetime, timedelta, timezone

    plugin = Synthetic()
    plugin.health = {}
    start = plugin.catch_up_since(timedelta(days=2))
    assert abs((datetime.now(timezone.utc) - start) - timedelta(days=2)) < timedelta(
        seconds=5
    )
    plugin.health = {"last_success_at": "2020-01-01T00:00:00Z"}
    assert plugin.catch_up_since(timedelta(days=2)) == datetime(
        2020, 1, 1, tzinfo=timezone.utc
    )


def test_catch_up_since_covers_a_stall_and_interval_override(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from throughline_sdk.plugin import interval_of

    plugin = Synthetic()
    plugin.health = {
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "outage": {"kind": "stalled", "last_good_at": "2020-02-01T00:00:00+00:00"},
    }
    assert plugin.catch_up_since(timedelta(days=1)) == datetime(
        2020, 2, 1, tzinfo=timezone.utc
    )
    assert interval_of(plugin) == plugin.manifest.default_interval_s
    monkeypatch.setenv("THROUGHLINE_INTERVAL_S", "900")
    assert interval_of(plugin) == 900
