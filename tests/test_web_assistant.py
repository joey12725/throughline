"""The query helper (docs/web/assistant.md): runs, tools, tiers, providers.

No network: the remote providers talk to a fake loopback server that answers
with recorded Messages API and chat/completions shapes.
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import ingest, message
from fastapi.testclient import TestClient

from throughline import access, assistant, providers
from throughline.assistant import lenses, runs, sqlfix
from throughline.db import connect, create_token

OPEN, HIDDEN, CANARY = "synthetic.open", "synthetic.hidden", "zzassistcanary"
GUEST = "assist-guest"
ROOT = Path(__file__).resolve().parents[1]


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("THROUGHLINE_ASSISTANT_") or name in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        ):
            monkeypatch.delenv(name, raising=False)
    with runs._lock:
        runs._runs.clear()
    yield
    with runs._lock:
        for run in runs._runs.values():
            run.cancelled.set()
        runs._runs.clear()


@pytest.fixture()
def world(client):
    for name in (OPEN, HIDDEN):
        ok(
            client.post(
                "/v1/channels",
                json=dict(
                    name=name,
                    plugin="synthetic",
                    description=f"Synthetic {name} sleep diary",
                    fields=[
                        dict(name="rating", type="integer", description="Sleep score"),
                        dict(name="mood", type="text", description="Mood word"),
                    ],
                ),
            )
        )
    items = [
        message(
            i * 3600,
            channel=OPEN,
            external_id=f"open-{i}",
            fields={"rating": i, "mood": f"{CANARY}mood"},
        )
        for i in range(30)
    ] + [
        message(
            i * 3600 + 5,
            channel=HIDDEN,
            external_id=f"hidden-{i}",
            thread={"external_id": "hidden-thread"},
            content=f"Synthetic {CANARY} secret {i}",
            fields={"rating": 100 + i, "mood": "hidden"},
        )
        for i in range(4)
    ]
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    plugin = create_token("plugin", kind="plugin", plugin="synthetic")["token"]
    access.register_asker(GUEST)
    access.grant(GUEST, channel=OPEN)
    agent = create_token("guest agent", kind="agent", askers=[GUEST])["token"]
    other = create_token("other owner")["token"]
    # A run may spend the token's max_model_cost in all (default 0.10 USD);
    # these runs are about the loop, so the tokens may spend the run ceiling.
    with connect() as c:
        c.execute("UPDATE tl.tokens SET max_model_cost=1")
    with TestClient(client.app) as guest:
        guest.headers["Authorization"] = "Bearer " + agent
        yield dict(
            client=client,
            guest=guest,
            plugin={"Authorization": "Bearer " + plugin},
            other={"Authorization": "Bearer " + other},
        )


def follow(client, run, after=0, wait=25, until_done=True, limit=60, headers=None):
    events = []
    deadline = time.monotonic() + limit
    while True:
        body = ok(
            client.get(
                f"/v1/web/assistant/{run}",
                params={"after": after, "wait": wait},
                headers=headers or {},
            )
        )
        events += body["events"]
        if body["events"]:
            after = body["events"][-1]["seq"]
        if body["status"] != "running" or not until_done:
            body["events"] = events
            return body
        assert time.monotonic() < deadline, "run did not end"


def start(client, status=202, headers=None, **body):
    r = client.post("/v1/web/assistant", json=body, headers=headers or {})
    assert r.status_code == status, r.text
    return r.json()


# --- a fake model endpoint on loopback ---------------------------------------------


class Fake:
    """A loopback HTTP server that records every request and answers with
    the next scripted response (a dict, or a callable of the request body)."""

    def __init__(self, script, delay=0.0, gate=None):
        self.script = list(script)
        self.requests = []
        self.headers = []
        self.delay = delay
        self.gate = gate
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(body)
                fake.headers.append({k.lower(): v for k, v in self.headers.items()})
                if fake.gate is not None:
                    fake.gate.wait(20)
                if fake.delay:
                    time.sleep(fake.delay)
                index = len(fake.requests) - 1
                item = fake.script[min(index, len(fake.script) - 1)]
                answer = item(body) if callable(item) else item
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake(monkeypatch):
    made = []

    def make(kind, script, **kwargs):
        server = Fake(script, **kwargs)
        made.append(server)
        monkeypatch.setenv("THROUGHLINE_ASSISTANT_PROVIDER", kind)
        monkeypatch.setenv(
            "THROUGHLINE_ASSISTANT_BASE_URL",
            server.url + ("/v1" if kind == "openai" else ""),
        )
        monkeypatch.setenv("THROUGHLINE_ASSISTANT_API_KEY", "synthetic-key")
        if kind == "openai":
            monkeypatch.setenv("THROUGHLINE_ASSISTANT_MODEL", "synthetic-model")
        return server

    yield make
    for server in made:
        if server.gate is not None:
            server.gate.set()
        server.close()


def anthropic(content, stop="tool_use", tokens=(1000, 200), **extra):
    return dict(
        id="msg_synthetic",
        type="message",
        role="assistant",
        model="claude-opus-5-5",
        content=content,
        stop_reason=stop,
        usage=dict(
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        **extra,
    )


def tool_use(name, arguments, id="toolu_1"):
    return dict(type="tool_use", id=id, name=name, input=arguments)


def final_call(statement, answer="Synthetic answer.", spec=None, id="toolu_final"):
    return tool_use(
        "final",
        dict(answer=answer, statement=statement, spec=spec, notes=["synthetic note"]),
        id=id,
    )


def openai(message, finish="tool_calls", tokens=(500, 50)):
    return dict(
        id="chatcmpl-synthetic",
        model="synthetic-model",
        choices=[
            dict(
                index=0, message=dict(role="assistant", **message), finish_reason=finish
            )
        ],
        usage=dict(prompt_tokens=tokens[0], completion_tokens=tokens[1]),
    )


def oa_call(name, arguments, id="call_1"):
    return dict(
        id=id,
        type="function",
        function=dict(name=name, arguments=json.dumps(arguments)),
    )


SERIES = (
    "SELECT date_trunc('day', happened_at) AS day, avg(rating)::float8 AS rating "
    "FROM api.ch_synthetic_open GROUP BY 1 ORDER BY 1"
)


# --- configuration and bootstrap ------------------------------------------------------


def test_describe_defaults_local_and_bootstrap(client, monkeypatch):
    described = assistant.describe()
    assert described == dict(
        provider="local",
        model="local-finder-v1",
        loopback=True,
        default_tier="samples",
        tiers=["schema", "samples"],
        configured=True,
    )
    body = ok(client.get("/v1/web/bootstrap"))
    assert body["features"]["assistant"] == described
    # A remote endpoint defaults to the schema tier and offers nothing else.
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_PROVIDER", "anthropic")
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_API_KEY", "synthetic-key")
    remote = assistant.describe()
    assert remote["provider"] == "anthropic" and remote["model"] == "claude-opus-5-5"
    assert remote["loopback"] is False and remote["default_tier"] == "schema"
    assert remote["tiers"] == ["schema"] and remote["configured"] is True
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_TIER", "samples")
    assert assistant.describe()["tiers"] == ["schema", "samples"]
    monkeypatch.delenv("THROUGHLINE_ASSISTANT_API_KEY")
    assert assistant.describe()["configured"] is False
    # The provider kind is part of the egress description.
    rows = {r["kind"]: r for r in providers.describe()}
    assert (
        rows["assistant"]["provider"] == "anthropic"
        and rows["assistant"]["excerpts_leave_host"]
    )
    text = json.dumps(rows)
    assert "synthetic-key" not in text and "api.anthropic.com" not in text


def test_vendor_key_goes_only_to_its_own_host(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-vendor-key")
    assert providers.get("assistant").key == "synthetic-vendor-key"
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_BASE_URL", "http://127.0.0.1:9")
    assert providers.get("assistant").key is None
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_PROVIDER", "openai")
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_MODEL", "m")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    assert providers.get("assistant").key is None


def test_lens_ids_match_the_front_end():
    source = (ROOT / "web/src/lens/types.ts").read_text()
    block = re.search(r"export const LENS_IDS[^=]*=\s*\[(.*?)\];", source, re.S).group(
        1
    )
    ids = re.findall(r'"([a-z_]+)"', block)
    assert ids and set(ids) == set(lenses.lens_ids())
    for lens in ids:
        assert (ROOT / f"web/src/lens/lenses/{lens}.ts").exists() or lens in source


# --- request validation, tokens, ownership --------------------------------------------


def test_request_validation_and_plugin_token(world):
    client = world["client"]
    assert client.post("/v1/web/assistant", json={}).status_code == 422
    assert client.post("/v1/web/assistant", json={"verb": "ask"}).status_code == 422
    assert client.post("/v1/web/assistant", json={"verb": "fix"}).status_code == 422
    assert (
        client.post(
            "/v1/web/assistant", json={"verb": "dance", "question": "x"}
        ).status_code
        == 422
    )
    big = [
        {"role": "user", "text": "x" * 5000},
        {"role": "assistant", "text": "y" * 5000},
    ]
    assert (
        client.post(
            "/v1/web/assistant", json={"verb": "ask", "question": "q", "history": big}
        ).status_code
        == 422
    )
    r = client.post(
        "/v1/web/assistant",
        json={"verb": "ask", "question": "q"},
        headers=world["plugin"],
    )
    assert r.status_code == 403
    assert client.get("/v1/web/assistant/0", headers=world["plugin"]).status_code == 403
    assert client.get("/v1/web/assistant/0").status_code == 404
    assert client.post("/v1/web/assistant/0/cancel").status_code == 404
    assert client.get("/v1/web/assistant/x", params={"wait": 26}).status_code == 422


def test_samples_refused_on_a_remote_endpoint(world, monkeypatch):
    client = world["client"]
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_PROVIDER", "anthropic")
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_API_KEY", "synthetic-key")
    r = client.post(
        "/v1/web/assistant",
        json={"verb": "ask", "question": "sleep", "tier": "samples"},
    )
    assert r.status_code == 400 and "tier" in r.json()["detail"]
    # Allowed once the owner opts in; the request is then refused by the
    # egress allow-list before any socket is opened.
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_TIER", "samples")
    started = start(client, verb="ask", question="sleep", tier="samples")
    assert started["tier"] == "samples"
    body = follow(client, started["run"])
    assert body["status"] == "failed" and "egress" in body["error"]
    monkeypatch.delenv("THROUGHLINE_ASSISTANT_API_KEY")
    r = client.post("/v1/web/assistant", json={"verb": "ask", "question": "sleep"})
    assert r.status_code == 400 and "not configured" in r.json()["detail"]


def test_runs_belong_to_their_token(world):
    client = world["client"]
    started = start(client, verb="explain", statement="SELECT 1 AS one")
    assert set(started) == {"run", "status", "provider", "model", "tier"}
    assert (
        client.get(
            f"/v1/web/assistant/{started['run']}", headers=world["other"]
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/web/assistant/{started['run']}/cancel", headers=world["other"]
        ).status_code
        == 404
    )
    # The same token with another asker is someone else too.
    other_asker = client.get(
        f"/v1/web/assistant/{started['run']}",
        headers={"X-Throughline-Asker": "someone"},
    )
    assert other_asker.status_code == 404
    body = follow(client, started["run"])
    assert body["status"] == "done"


# --- the local helper ---------------------------------------------------------------


def test_local_ask_finds_channel_field_and_builds_a_statement(world):
    client = world["client"]
    started = start(
        client, verb="ask", question="How did my sleep score change per day?"
    )
    assert started["provider"] == "local" and started["tier"] == "samples"
    body = follow(client, started["run"])
    assert body["status"] == "done", body
    final = body["final"]
    assert "synthetic.open" in final["answer"] and "rating" in final["answer"]
    statement = final["statement"]
    assert "rating" in statement and "date_trunc('day'" in statement
    check = ok(
        client.post("/v1/sql", json={"statement": statement, "validate_only": True})
    )
    assert check["valid"] is True
    spec = final["spec"]
    assert (
        spec["v"] == 1 and spec["lens"] == "timeseries" and spec["encode"]["x"] == "day"
    )
    assert spec["provenance"]["by"] == "agent" and spec["source"]["sql"] == statement
    kinds = [e["kind"] for e in body["events"]]
    assert kinds[-1] == "final" and kinds.count("final") == 1
    steps = [e for e in body["events"] if e["kind"] == "step"]
    assert steps and all(e["sent"].startswith("nothing") for e in steps)
    assert [e["seq"] for e in body["events"]] == list(range(1, len(body["events"]) + 1))
    assert body["usage"]["cost_usd"] == 0 and body["usage"]["steps"] == len(steps)


def test_local_ask_without_matches_falls_back_to_an_overview(world):
    body = follow(
        world["client"], start(world["client"], verb="ask", question="qqqq zzzz")["run"]
    )
    assert body["status"] == "done"
    assert "api.messages" in body["final"]["statement"]
    assert body["final"]["spec"]["lens"] == "top_values"


@pytest.mark.parametrize(
    "statement, error, expect",
    [
        ("SELECT ratng FROM api.ch_synthetic_open", None, "rating"),
        (
            "SELECT florr(rating::float8) AS f FROM api.ch_synthetic_open",
            None,
            "floor(",
        ),
        ("SELECT count(*) AS n FROM messages", None, "api.messages"),
        ("SELECT channel, count(*) AS n FROM api.messages", None, "GROUP BY channel"),
        (
            "SELECT pg_catalog.lower(content) AS c FROM api.messages",
            None,
            "lower(content)",
        ),
        (
            "SELECT median(rating) AS m FROM api.ch_synthetic_open",
            None,
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY rating)",
        ),
        ("SELECT ifnull(mood, 'x') AS m FROM api.ch_synthetic_open", None, "coalesce(mood"),
    ],
)
def test_local_fix_rules(world, statement, error, expect):
    client = world["client"]
    body = follow(
        client,
        start(
            client,
            verb="fix",
            statement=statement,
            error=error or {"code": "42703", "message": "database operation rejected"},
        )["run"],
    )
    final = body["final"]
    assert expect in final["statement"], final
    assert final["answer"].startswith("**Fixed:**")
    assert ok(
        client.post(
            "/v1/sql", json={"statement": final["statement"], "validate_only": True}
        )
    )["valid"]


def test_local_fix_explains_the_read_only_surface(world):
    client = world["client"]
    body = follow(
        client, start(client, verb="fix", statement="DELETE FROM api.messages")["run"]
    )
    assert "read-only" in body["final"]["answer"]
    assert body["final"]["statement"] == "DELETE FROM api.messages"


def test_local_explain_and_refine(world):
    client = world["client"]
    body = follow(
        client, start(client, verb="explain", statement=SERIES + " LIMIT 10")["run"]
    )
    answer = body["final"]["answer"]
    assert (
        "api.ch_synthetic_open" in answer
        and "Groups the rows" in answer
        and "avg" in answer
    )
    assert (
        "at most 10 rows" in answer
        and body["final"]["statement"] == SERIES + " LIMIT 10"
    )
    body = follow(
        client,
        start(
            client, verb="refine", statement=SERIES, question="by week, last 30 days"
        )["run"],
    )
    refined = body["final"]["statement"]
    assert "date_trunc('week'" in refined and "interval '30 days'" in refined
    # The bucket column named after the grain follows it.
    assert "AS week" in refined and "AS day" not in refined
    body = follow(
        client,
        start(
            client,
            verb="explain",
            statement="SELECT channel, count(*) AS n FROM api.messages GROUP BY 1 ORDER BY 2 DESC",
        )["run"],
    )
    answer = body["final"]["answer"]
    assert "Groups the rows by `channel`" in answer and "Orders by `n` descending" in answer


def test_rule_table_units():
    assert sqlfix.distance("ratng", "rating") == 1
    assert sqlfix.closest("florr", {"floor", "flow", "count"})[0] == "floor"
    text = sqlfix.explain("WITH a AS (SELECT 1 AS x) SELECT x FROM a")
    assert "WITH" in text


# --- remote providers against a fake endpoint -----------------------------------------


def test_anthropic_tool_loop_schema_tier_sends_no_row_values(world, fake):
    client = world["client"]
    peek = "SELECT content, mood FROM api.ch_synthetic_open UNION ALL SELECT content, mood FROM api.ch_synthetic_hidden"
    server = fake(
        "anthropic",
        [
            anthropic(
                [
                    dict(type="thinking", thinking="", signature="sig-synthetic"),
                    tool_use("run_sql", {"statement": peek}),
                ]
            ),
            anthropic(
                [
                    tool_use("profile", {"channel": OPEN}, id="toolu_2"),
                    tool_use("recall", {"text": "secret orchard"}, id="toolu_3"),
                ]
            ),
            anthropic(
                [
                    dict(type="text", text="Here is the series."),
                    final_call(
                        SERIES,
                        spec=dict(
                            lens="timeseries",
                            encode=dict(x="day", y="rating"),
                            title="Rating per day",
                        ),
                    ),
                ],
                stop="tool_use",
            ),
        ],
    )
    started = start(client, verb="ask", question="sleep per day", tier="schema")
    assert started["provider"] == "anthropic" and started["model"] == "claude-opus-5-5"
    body = follow(client, started["run"])
    assert body["status"] == "done", body
    final = body["final"]
    assert final["statement"] == SERIES and final["spec"]["lens"] == "timeseries"
    assert final["spec"]["encode"] == {"x": "day", "y": "rating"}
    assert "synthetic note" in final["notes"]
    sql_step = next(e for e in body["events"] if e.get("tool") == "run_sql")
    assert sql_step["row_count"] == 34 and sql_step["error"] is None
    assert "withheld (schema tier)" in sql_step["sent"]
    assert [c["name"] for c in sql_step["columns"]] == ["content", "mood"]
    assert any(
        e["kind"] == "text" and e["text"] == "Here is the series."
        for e in body["events"]
    )
    # Nothing the model received carries a row value.
    sent = json.dumps(server.requests)
    assert CANARY not in sent and "Synthetic orchard" not in sent
    assert len(server.requests) == 3
    first, second = server.requests[0], server.requests[1]
    assert first["model"] == "claude-opus-5-5" and first["thinking"] == {
        "type": "adaptive"
    }
    assert first["tool_choice"] == {"type": "auto"} and "temperature" not in first
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in first["tools"]} >= {
        "describe",
        "run_sql",
        "final",
        "chart",
    }
    # The thinking block is passed back unchanged.
    assert second["messages"][1]["content"][0] == dict(
        type="thinking", thinking="", signature="sig-synthetic"
    )
    assert second["messages"][2]["content"][0]["type"] == "tool_result"
    headers = server.headers[0]
    assert (
        headers["x-api-key"] == "synthetic-key"
        and headers["anthropic-version"] == "2026-10-21"
    )
    assert "authorization" not in headers
    # Spend: three turns of 1000 in / 200 out at 4 / 20 USD per million.
    assert body["usage"]["tokens_in"] == 3000 and body["usage"]["tokens_out"] == 600
    assert body["usage"]["cost_usd"] == pytest.approx(3 * (1000 * 4 + 200 * 20) / 1e6)
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM tl.model_ledger WHERE origin='assistant'"
        ).fetchall()
    assert len(rows) == 3 and all(r["origin_ref"] == started["run"] for r in rows)
    assert body["trace_id"]


def test_samples_tier_clips_previews(world, fake, monkeypatch):
    client = world["client"]
    # The model is declared to run on this machine: labels do not apply.
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_MODEL_IS_LOCAL", "1")
    long = "SELECT repeat_text FROM (SELECT content || content || content || content || content || content || content || content AS repeat_text FROM api.ch_synthetic_open) s"
    server = fake(
        "anthropic",
        [
            anthropic([tool_use("run_sql", {"statement": long})]),
            anthropic([final_call(SERIES)]),
        ],
    )
    body = follow(
        client, start(client, verb="ask", question="x", tier="samples")["run"]
    )
    assert body["status"] == "done"
    result = json.loads(server.requests[1]["messages"][2]["content"][0]["content"])
    assert len(result["rows"]) == 20 and result["row_count"] == 30
    assert all(len(r["repeat_text"]) <= 200 for r in result["rows"])
    step = next(e for e in body["events"] if e.get("tool") == "run_sql")
    assert "20 of 30 rows clipped to 200 characters" in step["sent"]


def test_restricted_asker_scope_applies_to_run_sql(world, fake, monkeypatch):
    guest = world["guest"]
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_MODEL_IS_LOCAL", "1")
    server = fake(
        "openai",
        [
            openai(
                dict(
                    content=None,
                    tool_calls=[
                        oa_call(
                            "run_sql",
                            {
                                "statement": "SELECT count(*)::bigint AS n FROM api.messages"
                            },
                        )
                    ],
                )
            ),
            openai(
                dict(
                    content=None,
                    tool_calls=[
                        oa_call(
                            "run_sql",
                            {"statement": "SELECT id FROM api.ch_synthetic_hidden"},
                            id="call_2",
                        )
                    ],
                )
            ),
            openai(
                dict(
                    content=None,
                    tool_calls=[
                        oa_call(
                            "final",
                            dict(
                                answer="done",
                                statement="SELECT count(*)::bigint AS n FROM api.messages",
                            ),
                            id="call_3",
                        )
                    ],
                )
            ),
        ],
    )
    body = follow(
        guest, start(guest, verb="ask", question="how many", tier="samples")["run"]
    )
    assert body["status"] == "done", body
    steps = [e for e in body["events"] if e.get("tool") == "run_sql"]
    counted = json.loads(server.requests[1]["messages"][-1]["content"])
    assert counted["rows"] == [{"n": 30}]
    assert steps[1]["error"] is not None or steps[1]["row_count"] == 0
    assert CANARY not in json.dumps(server.requests)
    # The model was told only about the channels the asker sees.
    system = server.requests[0]["messages"][0]["content"]
    assert OPEN in system and HIDDEN not in system


def test_openai_tool_loop_and_final_retry(world, fake):
    client = world["client"]
    server = fake(
        "openai",
        [
            openai(
                dict(
                    content="Checking.",
                    tool_calls=[oa_call("describe", {"subject": "channel:" + OPEN})],
                )
            ),
            openai(
                dict(
                    content=None,
                    tool_calls=[
                        oa_call(
                            "final",
                            dict(
                                answer="x",
                                statement="SELECT nope FROM api.ch_synthetic_open",
                            ),
                            id="call_2",
                        )
                    ],
                )
            ),
            openai(
                dict(
                    content=None,
                    tool_calls=[
                        oa_call(
                            "final",
                            dict(answer="Fixed it.", statement=SERIES),
                            id="call_3",
                        )
                    ],
                )
            ),
        ],
    )
    body = follow(client, start(client, verb="ask", question="sleep")["run"])
    assert body["status"] == "done" and body["final"]["statement"] == SERIES
    assert body["final"]["answer"] == "Fixed it."
    retry = server.requests[2]["messages"][-1]
    assert (
        retry["role"] == "tool"
        and "does not validate" in retry["content"]
        and "42703" in retry["content"]
    )
    assert server.requests[0]["tools"][0]["type"] == "function"
    assert server.headers[0]["authorization"] == "Bearer synthetic-key"
    described = next(e for e in body["events"] if e.get("tool") == "describe")
    assert (
        described["input"] == {"subject": "channel:" + OPEN}
        and described["summary"] == "2 fields"
    )


def test_final_that_never_validates_is_returned_with_the_error(world, fake):
    client = world["client"]
    fake(
        "openai",
        [
            openai(
                dict(
                    content=None,
                    tool_calls=[
                        oa_call(
                            "final",
                            dict(
                                answer="x",
                                statement="SELECT nope FROM api.ch_synthetic_open",
                            ),
                        )
                    ],
                )
            )
        ],
    )
    body = follow(client, start(client, verb="ask", question="sleep")["run"])
    assert body["status"] == "done"
    assert body["final"]["statement"] == "SELECT nope FROM api.ch_synthetic_open"
    assert any("does not validate" in n for n in body["final"]["notes"])


def test_refusal_ends_the_run(world, fake):
    client = world["client"]
    fake(
        "anthropic",
        [
            anthropic(
                [],
                stop="refusal",
                stop_details=dict(type="refusal", category="cyber", explanation="x"),
            )
        ],
    )
    body = follow(client, start(client, verb="fix", statement=SERIES)["run"])
    assert body["status"] == "done" and "declined" in body["final"]["answer"]
    assert "cyber" in body["final"]["answer"] and body["final"]["statement"] == SERIES


def test_max_steps_ends_with_a_final_answer(world, fake, monkeypatch):
    client = world["client"]
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_MAX_STEPS", "2")
    server = fake(
        "anthropic",
        [
            lambda body: anthropic(
                [tool_use("describe", {}, id=f"toolu_{len(body['messages'])}")]
            )
        ],
    )
    body = follow(client, start(client, verb="ask", question="sleep")["run"])
    assert body["status"] == "done"
    assert "2 tool steps" in body["final"]["answer"]
    assert body["usage"]["steps"] == 2 and len(server.requests) == 4


def test_spend_ceiling_ends_with_a_final_answer(world, fake, monkeypatch):
    client = world["client"]
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_MAX_USD", "0.15")
    server = fake(
        "anthropic",
        [
            lambda body: anthropic(
                [tool_use("describe", {}, id=f"toolu_{len(body['messages'])}")],
                tokens=(10000, 1000),
            )
        ],
    )
    body = follow(client, start(client, verb="ask", question="sleep")["run"])
    assert body["status"] == "done", body
    assert "spend ceiling" in body["final"]["answer"]
    # Each turn reserves its estimate first: the ceiling is never passed.
    assert 0 < body["usage"]["cost_usd"] <= 0.15
    assert 1 <= len(server.requests) <= 3


def test_long_poll_answers_when_an_event_arrives_and_cancel(world, fake):
    client = world["client"]
    fake(
        "anthropic",
        [anthropic([tool_use("describe", {})]), anthropic([final_call(SERIES)])],
        delay=1.0,
    )
    started = start(client, verb="ask", question="sleep")
    began = time.monotonic()
    body = ok(
        client.get(
            f"/v1/web/assistant/{started['run']}", params={"after": 0, "wait": 20}
        )
    )
    took = time.monotonic() - began
    assert body["events"] and took < 10, took
    # Nothing new: wait=0 answers at once.
    last = body["events"][-1]["seq"]
    began = time.monotonic()
    ok(
        client.get(
            f"/v1/web/assistant/{started['run']}",
            params={"after": last + 100, "wait": 0},
        )
    )
    assert time.monotonic() - began < 2
    # A client ahead of the run waits for an event after its cursor, not
    # for any event: the next turn's events come about a second later.
    began = time.monotonic()
    ahead = ok(client.get(f"/v1/web/assistant/{started['run']}", params={"after": last + 100, "wait": 0.5}))
    assert ahead["events"] == [] and time.monotonic() - began >= 0.45
    cancelled = ok(client.post(f"/v1/web/assistant/{started['run']}/cancel"))
    assert cancelled == {"status": "cancelled"}
    body = ok(client.get(f"/v1/web/assistant/{started['run']}", params={"after": 0}))
    assert body["status"] == "cancelled" and body["events"][-1]["kind"] == "cancelled"
    time.sleep(1.5)
    assert (
        ok(client.get(f"/v1/web/assistant/{started['run']}"))["status"] == "cancelled"
    )


def test_fifth_concurrent_run_is_429(world, fake):
    client = world["client"]
    gate = threading.Event()
    fake("anthropic", [anthropic([final_call(SERIES)])], gate=gate)
    started = [start(client, verb="ask", question=f"q{i}")["run"] for i in range(4)]
    r = client.post("/v1/web/assistant", json={"verb": "ask", "question": "q5"})
    assert r.status_code == 429 and r.headers["retry-after"] == "5"
    # Another token is not affected.
    assert (
        client.post(
            "/v1/web/assistant",
            json={"verb": "explain", "statement": "SELECT 1 AS a"},
            headers=world["other"],
        ).status_code
        == 202
    )
    gate.set()
    for run in started:
        assert follow(client, run)["status"] == "done"
    # A run holds its slot until its worker has returned.
    _settle()
    assert (
        client.post(
            "/v1/web/assistant", json={"verb": "ask", "question": "q6"}
        ).status_code
        == 202
    )


def test_audit_rows_hold_ids_only(world):
    client = world["client"]
    started = start(client, verb="ask", question=f"secret question {CANARY}")
    follow(client, started["run"])
    with connect() as c:
        rows = c.execute(
            "SELECT statement, detail FROM tl.audit_log WHERE statement LIKE '%%/v1/web/assistant%%'"
        ).fetchall()
    assert rows and CANARY not in json.dumps(rows, default=str)
    assert any((r["detail"] or {}).get("run") == started["run"] for r in rows)


# --- security review regressions ------------------------------------------------------


def _tool_results(server):
    """Every tool result the fake model received, parsed where it is JSON."""
    found = []
    # The last request holds the whole conversation.
    for request in server.requests[-1:]:
        for turn in request.get("messages") or []:
            content = turn.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    try:
                        found.append(json.loads(block["content"]))
                    except (TypeError, ValueError):
                        found.append(block["content"])
    return found


def _owner_token_id(client):
    return ok(client.get("/v1/web/bootstrap"))["token"]["id"]


def _settle(timeout=10):
    """Wait until every run's worker has returned."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with runs._lock:
            if all(r.worker_done for r in runs._runs.values()):
                return
        time.sleep(0.02)
    raise AssertionError("workers did not return")


SECRET_TEXT = "Hi there\nmy bank PIN is 4321 " + "z" * 4100
SECRET_MOOD = 'she said "call me" at 5pm'


def test_schema_tier_database_errors_carry_no_row_values(world, fake):
    client = world["client"]
    r = ingest(
        client,
        [
            message(
                99999,
                channel=OPEN,
                external_id="leak-1",
                content=SECRET_TEXT,
                fields={"rating": 1, "mood": SECRET_MOOD},
            )
        ],
    )
    assert r.status_code == 200, r.text
    newest = "FROM api.messages WHERE id = (SELECT max(id) FROM api.messages)"
    server = fake(
        "anthropic",
        [
            anthropic([tool_use("run_sql", {"statement": f"SELECT content::int AS x {newest}"}, id="t1")]),
            anthropic(
                [tool_use("run_sql", {"statement": f"SELECT (fields->>'mood')::int AS x {newest}"}, id="t2")]
            ),
            anthropic([tool_use("run_sql", {"statement": "SELECT nope FROM api.ch_synthetic_open"}, id="t3")]),
            # The final statement fails validation with a data exception.
            anthropic([final_call("SELECT 'not a number'::int AS x")]),
            anthropic([final_call(SERIES, id="toolu_final_2")]),
        ],
    )
    body = follow(client, start(client, verb="ask", question="anything", tier="schema")["run"])
    assert body["status"] == "done", body
    sent = json.dumps(server.requests)
    for leaked in ("4321", "PIN", "call me", "zzzzzzzz", "Hi there"):
        assert leaked not in sent, leaked
    results = _tool_results(server)
    data = [r for r in results if isinstance(r, dict) and r.get("code") == "22P02"]
    assert len(data) == 2
    assert all("data exception" in r["detail"] and "withheld" in r["detail"] for r in data)
    # Class 42 keeps the names the model wrote itself.
    named = next(r for r in results if isinstance(r, dict) and r.get("code") == "42703")
    assert named["detail"] == 'column "nope" does not exist'
    steps = [e for e in body["events"] if e.get("tool") == "run_sql"]
    assert "fixed description" in steps[0]["sent"]
    retry = server.requests[4]["messages"][-1]["content"][0]["content"]
    assert "22P02" in retry and "data exception" in retry


def test_scrub_removes_every_quoted_text_but_the_statements_names():
    from throughline.assistant.tools import database_error, scrub

    statement = "SELECT nope, m.channel FROM api.messages m"
    assert scrub('column "nope" does not exist', statement) == 'column "nope" does not exist'
    assert (
        scrub('column "m.channel" must appear in the GROUP BY clause', statement)
        == 'column "m.channel" must appear in the GROUP BY clause'
    )
    # A name the statement does not hold is a value.
    assert scrub('relation "secret_word" does not exist', statement) == "relation <value> does not exist"
    # Multi-line, embedded quotes, very long, unclosed.
    for text in (
        'invalid input syntax: "line one\nline two"',
        'invalid input syntax: "she said "call me" at 5pm"',
        'bad: "' + "q" * 5000 + '"',
        "bad: 'open quote that never closes 4321",
        'x "nope" then "she said "call me"" and \'4321\'',
    ):
        out = scrub(text, statement)
        for leaked in ("line two", "call me", "qqqq", "4321", "never closes"):
            assert leaked not in out, (text, out)

    class Diag:
        message_primary = 'invalid input syntax for type integer: "my PIN 4321"'

    class Data(Exception):
        sqlstate = "22P02"
        diag = Diag()

    class Raised(Exception):
        sqlstate = "P0001"
        diag = Diag()

    assert "4321" not in json.dumps(database_error(Data(), statement))
    assert database_error(Raised(), statement)[1].startswith("raised by a database function")
    # The librarian's redact() is unchanged.
    from throughline.librarian.controller import redact

    assert redact('relation "x" does not exist') == 'relation "x" does not exist'


def _label_hidden_channel():
    with connect() as c:
        c.execute("INSERT INTO tl.labels(label) VALUES ('synthetic_secret') ON CONFLICT DO NOTHING")
        c.execute(
            "INSERT INTO tl.channel_labels(channel_id,label) SELECT id,'synthetic_secret' FROM tl.channels WHERE name=%s",
            (HIDDEN,),
        )
        open_id = c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels ch ON ch.id=m.channel_id WHERE ch.name=%s LIMIT 1",
            (OPEN,),
        ).fetchone()["id"]
    return open_id


def test_samples_tier_labels_hold_rows_whatever_the_id_column(world, fake):
    client = world["client"]
    open_id = _label_hidden_channel()
    # A loopback endpoint that is not declared local: labels apply.
    disguised = f"SELECT {open_id}::bigint AS id, content FROM api.ch_synthetic_hidden"
    server = fake(
        "anthropic",
        [
            anthropic([tool_use("run_sql", {"statement": disguised}, id="t1")]),
            anthropic([tool_use("run_sql", {"statement": "SELECT id AS message_id, content FROM api.ch_synthetic_hidden"}, id="t2")]),
            anthropic([tool_use("profile", {"channel": HIDDEN}, id="t3")]),
            anthropic([tool_use("recall", {"text": "secret"}, id="t4")]),
            anthropic([tool_use("recall", {"text": "orchard"}, id="t5")]),
            anthropic([final_call(SERIES)]),
        ],
    )
    body = follow(client, start(client, verb="ask", question="x", tier="samples")["run"])
    assert body["status"] == "done", body
    assert CANARY not in json.dumps(server.requests)
    results = _tool_results(server)
    assert results[0]["rows"] == [] and "sensitivity labels" in results[0]["note"]
    assert results[1]["rows"] == []
    profile = results[2]
    assert all("top" not in f and "max" not in f for f in profile["fields"])
    assert "withheld" in profile["note"]
    # Recall builds its rows from real record ids: held ones are withheld,
    # others are sent.
    assert all(r.get("withheld") == "egress_held" for r in results[3]["rows"])
    assert results[4]["rows"] and all("content" in r for r in results[4]["rows"])
    steps = [e for e in body["events"] if e.get("tool") == "run_sql"]
    assert "rows withheld" in steps[0]["sent"] and "sensitivity labels" in steps[0]["sent"]


def test_loopback_endpoint_defaults_to_schema_and_labels_apply(world, fake, monkeypatch):
    from throughline.assistant.catalog import Catalog
    from throughline.assistant.tools import Belt
    from throughline.scope import Scope

    fake("openai", [openai(dict(content="x"), finish="stop")])
    found = providers.get("assistant")
    described = assistant.describe()
    assert described["loopback"] is True and described["default_tier"] == "schema"
    assert described["tiers"] == ["schema", "samples"]
    assert not assistant.model_is_local(found)
    scope = Scope.local()
    belt = Belt({"id": 1, "kind": "owner"}, scope, "samples", found, Catalog(scope))
    assert belt.labels_apply is True
    # A run started without a tier gets the schema tier.
    started = start(world["client"], verb="ask", question="sleep")
    assert started["tier"] == "schema"
    # The owner states that the model runs on this machine.
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_MODEL_IS_LOCAL", "1")
    assert assistant.describe()["default_tier"] == "samples"
    assert Belt({"id": 1, "kind": "owner"}, scope, "samples", providers.get("assistant"), Catalog(scope)).labels_apply is False
    # The statement means nothing for an endpoint off this machine.
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_BASE_URL", "https://models.invalid/v1")
    remote = providers.get("assistant")
    assert not assistant.model_is_local(remote)
    assert assistant.tiers_for(remote) == (["schema"], "schema")
    # The local helper needs no statement.
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_PROVIDER", "local")
    assert assistant.model_is_local(providers.get("assistant"))


def test_vendor_key_needs_https(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-vendor-key")
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_BASE_URL", "https://api.anthropic.com")
    assert providers.get("assistant").key == "synthetic-vendor-key"
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_BASE_URL", "http://api.anthropic.com")
    assert providers.get("assistant").key is None
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_PROVIDER", "openai")
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_MODEL", "m")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_BASE_URL", "http://api.openai.com/v1")
    assert providers.get("assistant").key is None
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_BASE_URL", "https://api.openai.com/v1")
    assert providers.get("assistant").key == "synthetic-openai-key"


def test_token_cost_limit_holds_for_the_whole_run(world, fake):
    client = world["client"]
    with connect() as c:
        c.execute("UPDATE tl.tokens SET max_model_cost=0.05 WHERE kind='owner'")
    server = fake(
        "anthropic",
        [
            lambda body: anthropic(
                [tool_use("describe", {}, id=f"toolu_{len(body['messages'])}")],
                tokens=(1000, 200),
            )
        ],
    )
    body = follow(client, start(client, verb="ask", question="sleep")["run"])
    assert body["status"] == "done", body
    answer = body["final"]["answer"]
    assert "spend ceiling of 0.05 USD" in answer and "max_model_cost" in answer
    assert 0 < body["usage"]["cost_usd"] <= 0.05
    # Under the old per-turn rule the run went on to its step limit.
    assert 1 <= len(server.requests) < 8
    # Each turn's output cap keeps its worst case within what was left (L7).
    spent = 0.0
    for request in server.requests:
        assert request["max_tokens"] < 16000
        assert spent + request["max_tokens"] * 20 / 1e6 <= 0.05
        spent += (1000 * 4 + 200 * 20) / 1e6


def test_daily_spend_cap_per_token(world, fake, monkeypatch):
    client = world["client"]
    token_id = _owner_token_id(client)
    with connect() as c:
        c.execute(
            "INSERT INTO tl.model_ledger(purpose,provider,model,tokens,cost,calls,token_id,origin,origin_ref)"
            " VALUES ('assistant','anthropic','claude-opus-5-5',1,4.99,1,%s,'assistant','r_old')",
            (token_id,),
        )
    server = fake("anthropic", [anthropic([final_call(SERIES)])])
    body = follow(client, start(client, verb="ask", question="sleep")["run"])
    assert "5.00 USD per 24 hours" in body["final"]["answer"]
    assert server.requests == []
    # A higher cap lets the owner go on.
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_DAILY_USD", "50")
    body = follow(client, start(client, verb="ask", question="sleep")["run"])
    assert body["final"]["statement"] == SERIES and len(server.requests) == 1
    monkeypatch.setenv("THROUGHLINE_ASSISTANT_DAILY_USD", "x")
    r = client.post("/v1/web/assistant", json={"verb": "ask", "question": "q"})
    assert r.status_code == 400 and "DAILY_USD" in r.json()["detail"]


def test_registry_keeps_a_bounded_number_of_ended_runs(world, monkeypatch):
    client = world["client"]
    monkeypatch.setattr(runs, "MAX_ENDED_PER_TOKEN", 3)
    monkeypatch.setattr(runs, "MAX_ENDED", 4)
    mine = []
    for i in range(5):
        mine.append(start(client, verb="explain", statement=f"SELECT {i} AS a")["run"])
        follow(client, mine[-1])
        _settle()
    theirs = [start(client, verb="explain", statement="SELECT 1 AS a", headers=world["other"])["run"]]
    follow(client, theirs[0], headers=world["other"])
    _settle()
    runs._sweep()
    with runs._lock:
        kept = set(runs._runs)
    # The newest three of mine, and the total of four with the other token's.
    assert kept == set(mine[-3:]) | set(theirs)
    assert client.get(f"/v1/web/assistant/{mine[0]}").status_code == 404


def test_cancelled_run_holds_its_slot_until_its_worker_returns(world, fake):
    client = world["client"]
    gate = threading.Event()
    fake("anthropic", [anthropic([final_call(SERIES)])], gate=gate)
    started = [start(client, verb="ask", question=f"q{i}")["run"] for i in range(4)]
    time.sleep(0.3)
    assert ok(client.post(f"/v1/web/assistant/{started[0]}/cancel")) == {"status": "cancelled"}
    with runs._lock:
        cancelled = runs._runs[started[0]]
    # Cancel never writes the trace: only the worker does (L1).
    assert cancelled.trace_id is None and cancelled.on_finish is not None
    r = client.post("/v1/web/assistant", json={"verb": "ask", "question": "q5"})
    assert r.status_code == 429
    gate.set()
    _settle()
    assert cancelled.on_finish is None and cancelled.status == "cancelled"
    assert ok(client.get(f"/v1/web/assistant/{started[0]}"))["trace_id"]
    assert start(client, verb="ask", question="q6")["status"] == "running"


def test_process_wide_bound_on_active_runs(world, fake, monkeypatch):
    client = world["client"]
    monkeypatch.setattr(runs, "MAX_BUSY", 2)
    gate = threading.Event()
    fake("anthropic", [anthropic([final_call(SERIES)])], gate=gate)
    for i in range(2):
        start(client, verb="ask", question=f"q{i}")
    r = client.post(
        "/v1/web/assistant", json={"verb": "ask", "question": "q"}, headers=world["other"]
    )
    assert r.status_code == 429 and "busy" in r.json()["detail"]
    gate.set()
    _settle()
    assert start(client, verb="ask", question="q", headers=world["other"])["status"] == "running"


def test_describe_forwards_only_index_subjects_and_respects_the_tier(world, fake):
    client = world["client"]
    calls = [
        ("coverage", "t1"),
        ("egress", "t2"),
        ("functions", "t3"),
        ("result_sets", "t4"),
        ("anything_at_all", "t5"),
        ("operators", "t6"),
    ]
    script = [anthropic([tool_use("describe", {"subject": s}, id=i)]) for s, i in calls]
    server = fake("anthropic", script + [anthropic([final_call(SERIES)])])
    body = follow(client, start(client, verb="ask", question="x", tier="schema")["run"])
    assert body["status"] == "done", body
    steps = {e["input"]["subject"]: e for e in body["events"] if e.get("tool") == "describe"}
    assert steps["coverage"].get("is_error") and steps["egress"].get("is_error")
    assert steps["anything_at_all"].get("is_error")
    assert not steps["functions"].get("is_error") and not steps["result_sets"].get("is_error")
    assert not steps["operators"].get("is_error")
    results = _tool_results(server)
    assert "not available at the schema tier" in results[0]
    # At the samples tier the computed sections are forwarded.
    server = fake(
        "anthropic",
        [anthropic([tool_use("describe", {"subject": "coverage"})]), anthropic([final_call(SERIES)])],
    )
    body = follow(client, start(client, verb="ask", question="x", tier="samples")["run"])
    step = next(e for e in body["events"] if e.get("tool") == "describe")
    assert not step.get("is_error")


def test_helper_statements_reach_the_audit_log_and_the_reads_lane(world, fake):
    client = world["client"]
    cursor = ok(client.get("/v1/web/arrivals"))["cursor"]
    statement = "SELECT count(*)::bigint AS n FROM api.ch_synthetic_open"
    fake(
        "anthropic",
        [anthropic([tool_use("run_sql", {"statement": statement})]), anthropic([final_call(SERIES)])],
    )
    run = start(client, verb="ask", question="x")["run"]
    follow(client, run)
    with connect() as c:
        rows = c.execute(
            "SELECT statement,row_count,http_status,detail FROM tl.audit_log WHERE detail->>'client'='web-assistant'"
        ).fetchall()
    mine = [r for r in rows if r["statement"] == statement]
    assert mine and mine[0]["row_count"] == 1 and mine[0]["http_status"] == 200
    assert mine[0]["detail"]["run"] == run
    reads = ok(client.get("/v1/web/arrivals", params={"after": cursor}))["reads"]
    assert any(r["statement"] == statement for r in reads)
