"""Source coverage (migration 0046, throughline.coverage): degrade-to-partial
reporting, gaps that close only on covering reports, coverage in recall and
packets, staleness in status, the gate on coverage rows. Synthetic data only."""

import json
from contextlib import contextmanager

import typer
from conftest import ingest, message, truncate_tl
from psycopg import sql
from typer.testing import CliRunner

from throughline import access, packets, resultsets
from throughline.db import connect, create_token
from throughline.scope import Scope

SECRET = "synthetic.secret"
NOTES = "synthetic.notes"
JAN = {"from": "2020-01-01T00:00:00+00:00", "to": "2020-02-01T00:00:00+00:00"}


def headers(kind, plugin=None, askers=None, asker=None):
    token = create_token("synthetic-" + kind, kind, plugin, askers)["token"]
    out = {"Authorization": "Bearer " + token}
    if asker:
        out["X-Throughline-Asker"] = asker
    return out


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def report(client, reports, h=None, plugin="synthetic"):
    return client.post(
        f"/v1/plugins/{plugin}/coverage", json={"reports": reports}, headers=h or {}
    )


def owner_rows(statement, params=()):
    with connect() as c:
        return c.execute(statement, params).fetchall()


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


def add_channel(client, name, plugin="synthetic", **extra):
    ok(client.post("/v1/channels", json=dict(name=name, plugin=plugin, **extra)))


def recall(client, h=None, **body):
    return ok(client.post("/v1/recall", json=body, headers=h or {}))


def coverage_rows(channel="synthetic.chat"):
    return owner_rows(
        "SELECT c.* FROM tl.coverage c JOIN tl.channels ch ON ch.id=c.channel_id WHERE ch.name=%s ORDER BY c.scope_key",
        (channel,),
    )


# --- P1 reporting ---------------------------------------------------------------


def test_p1_unknown_status_degrades_to_partial_with_raw_kept(client, channel):
    plugin = headers("plugin", "synthetic")
    body = ok(
        report(
            client,
            [
                {"channel": channel, "scope_key": "a", "status": "empty_thread"},
                {"channel": channel, "scope_key": "b", "status": "Glorious-Success"},
                {
                    "channel": channel,
                    "scope_key": "c",
                    "status": "empty_thread",
                    "verified": True,
                },
                {"channel": channel, "scope_key": "d", "status": "ok"},
            ],
            plugin,
        )
    )
    assert body["stored"] == 4 and body["refused"] == 0
    degraded = {d["index"]: d["reasons"] for d in body["degraded"]}
    assert degraded == {0: ["unrecognised_status"], 1: ["unrecognised_status"]}
    rows = {r["scope_key"]: r for r in coverage_rows()}
    assert (
        rows["a"]["status"] == "partial" and rows["a"]["raw_status"] == "empty_thread"
    )
    assert "unrecognised_status" in rows["a"]["reasons"]
    assert rows["b"]["status"] == "partial"
    assert rows["b"]["raw_status"] == "Glorious-Success"
    assert rows["c"]["status"] == "complete"
    assert rows["c"]["reasons"] == ["empty_verified"]
    assert rows["d"]["status"] == "complete" and rows["d"]["raw_status"] == "ok"
    logged = owner_rows(
        "SELECT received,normalized FROM tl.coverage_reports ORDER BY id"
    )
    assert logged[1]["received"]["status"] == "Glorious-Success"
    assert logged[1]["normalized"]["status"] == "partial"


def test_p1_never_hard_stop_on_shape(client, channel):
    plugin = headers("plugin", "synthetic")
    body = ok(
        report(
            client,
            [
                {"channel": channel, "status": "complete", "detail": ["a", "b"]},
                {
                    "channel": channel,
                    "scope_key": "t",
                    "status": "partial",
                    "window": {"from": "not a time", "to": "2020-01-02"},
                    "high_water": 12,
                },
                {
                    "channel": channel,
                    "scope_key": "r",
                    "status": "partial",
                    "reasons": ["rate_limited", "invented_code"],
                    "detail": {"n": 1},
                },
                {
                    "channel": channel,
                    "scope_key": "big",
                    "status": "complete",
                    "detail": {"blob": "x" * 70000},
                },
            ],
            plugin,
        )
    )
    assert body["stored"] == 4
    degraded = {d["index"]: d["reasons"] for d in body["degraded"]}
    assert degraded[0] == ["unknown_shape"]
    assert degraded[1] == ["unknown_shape"]
    assert degraded[2] == ["unrecognised_reason"]
    assert "unknown_shape" in degraded[3]
    rows = {r["scope_key"]: r for r in coverage_rows()}
    assert rows[""]["raw_detail"] == {"value": ["a", "b"]}
    # A report whose shape was not understood is not proof of completeness.
    assert rows[""]["status"] == "partial"
    assert rows["t"]["covered_from"] is None and rows["t"]["high_water_at"] is None
    assert rows["r"]["reasons"] == ["rate_limited", "unrecognised_reason"]
    assert rows["r"]["raw_detail"] == {
        "n": 1,
        "unrecognised_reasons": ["invented_code"],
    }
    assert rows["big"]["raw_detail"]["truncated"] is True
    received = owner_rows(
        "SELECT received FROM tl.coverage_reports WHERE scope_key='big'"
    )[0]["received"]
    assert received["truncated"] is True
    assert len(json.dumps(received)) <= 64 * 1024
    # Not a list of reports is the only shape refused (and only as a body).
    assert report(client, "nope", plugin).status_code in (400, 422)


def test_p1_gap_closes_only_on_covering_report(client, channel):
    plugin = headers("plugin", "synthetic")
    opened = ok(
        report(
            client,
            [
                {
                    "channel": channel,
                    "status": "partial",
                    "reasons": ["source_error"],
                    "window": JAN,
                }
            ],
            plugin,
        )
    )["gaps_opened"]
    assert len(opened) == 1 and opened[0]["index"] == 0
    gap_id = opened[0]["id"]

    def open_ids():
        return [
            r["id"]
            for r in owner_rows(
                "SELECT id FROM tl.coverage_gaps WHERE closed_at IS NULL ORDER BY id"
            )
        ]

    # A healthy empty without verified never closes the failed interval.
    for entry in (
        {"status": "complete", "rows_delivered": 0},
        {"status": "complete"},
        {"status": "empty", "rows_delivered": 0},
        {"status": "best_effort", "rows_delivered": 5},
    ):
        body = ok(report(client, [dict(entry, channel=channel, window=JAN)], plugin))
        assert body["gaps_closed"] == []
        assert open_ids() == [gap_id]
    # A complete report over a larger window with delivered rows closes it.
    wide = {"from": "2019-12-01T00:00:00Z", "to": "2020-03-01T00:00:00Z"}
    body = ok(
        report(
            client,
            [
                {
                    "channel": channel,
                    "status": "complete",
                    "rows_delivered": 3,
                    "window": wide,
                }
            ],
            plugin,
        )
    )
    assert body["gaps_closed"] == [gap_id] and open_ids() == []
    # verified closes too (empty window confirmed by the source).
    second = ok(
        report(
            client, [{"channel": channel, "status": "partial", "window": JAN}], plugin
        )
    )["gaps_opened"][0]["id"]
    body = ok(
        report(
            client,
            [{"channel": channel, "status": "empty", "verified": True, "window": JAN}],
            plugin,
        )
    )
    assert body["gaps_closed"] == [second]
    # closes by id and by window.
    gaps = ok(
        report(
            client,
            [
                {
                    "channel": channel,
                    "status": "partial",
                    "gaps": [
                        {
                            "from": "2020-04-01Z",
                            "to": "2020-04-02Z",
                            "reason": "rate_limited",
                        },
                        {"from": "2020-05-01Z", "to": "2020-05-02Z"},
                    ],
                }
            ],
            plugin,
        )
    )["gaps_opened"]
    assert len(gaps) == 2
    body = ok(
        report(
            client,
            [{"channel": channel, "status": "best_effort", "closes": [gaps[0]["id"]]}],
            plugin,
        )
    )
    assert body["gaps_closed"] == [gaps[0]["id"]]
    body = ok(
        report(
            client,
            [
                {
                    "channel": channel,
                    "status": "best_effort",
                    "closes": [
                        {
                            "channel": channel,
                            "scope_key": "",
                            "from": "2020-05-01T00:00:00+00:00",
                            "to": "2020-05-02T00:00:00+00:00",
                        }
                    ],
                }
            ],
            plugin,
        )
    )
    assert body["gaps_closed"] == [gaps[1]["id"]] and open_ids() == []
    closed = owner_rows("SELECT closed_by_report FROM tl.coverage_gaps")
    assert all(r["closed_by_report"] for r in closed)


def test_p1_post_returns_opened_gap_ids_and_plugin_reads_own_gaps(client, channel):
    add_channel(client, "other.feed", plugin="other")
    plugin = headers("plugin", "synthetic")
    other = headers("plugin", "other")
    body = ok(
        report(
            client,
            [
                {"channel": channel, "status": "complete", "rows_delivered": 1},
                {
                    "channel": channel,
                    "scope_key": "chat-1",
                    "status": "partial",
                    "gaps": [
                        {
                            "from": "2020-01-01Z",
                            "to": "2020-01-03Z",
                            "reason": "source_cap",
                        }
                    ],
                },
            ],
            plugin,
        )
    )
    (gap,) = body["gaps_opened"]
    assert gap["index"] == 1 and gap["id"]
    assert gap["from"].startswith("2020-01-01") and gap["to"].startswith("2020-01-03")
    listed = ok(client.get("/v1/plugins/synthetic/coverage/gaps", headers=plugin))
    assert [g["id"] for g in listed["gaps"]] == [gap["id"]]
    assert listed["gaps"][0]["channel"] == channel
    assert listed["gaps"][0]["scope_key"] == "chat-1"
    assert listed["gaps"][0]["reason"] == "source_cap"
    filtered = ok(
        client.get(
            "/v1/plugins/synthetic/coverage/gaps",
            params={"channel": channel, "scope_key": "nope"},
            headers=plugin,
        )
    )
    assert filtered["gaps"] == []
    # Another plugin reads and writes nothing of this plugin.
    assert (
        client.get("/v1/plugins/synthetic/coverage/gaps", headers=other).status_code
        == 403
    )
    assert report(client, [{"status": "complete"}], other).status_code == 403
    assert (
        ok(client.get("/v1/plugins/other/coverage/gaps", headers=other))["gaps"] == []
    )
    # The owner may read any plugin's gaps; an agent may not.
    assert ok(client.get("/v1/plugins/synthetic/coverage/gaps"))["gaps"]
    agent = headers("agent")
    assert (
        client.get("/v1/plugins/synthetic/coverage/gaps", headers=agent).status_code
        == 403
    )
    # attempt notes last_attempt_at without a report.
    attempted = ok(
        client.post(
            "/v1/plugins/synthetic/coverage/attempt",
            json={"channels": [channel, "other.feed"]},
            headers=plugin,
        )
    )
    assert attempted == {"attempted": [channel], "refused": 1}
    assert all(r["last_attempt_at"] for r in coverage_rows())


def test_p1_best_effort_report_opens_no_window_gap(client, channel):
    plugin = headers("plugin", "synthetic")
    body = ok(
        report(
            client,
            [
                {
                    "channel": channel,
                    "status": "best_effort",
                    "reasons": ["partial_history"],
                    "window": JAN,
                    "rows_delivered": 4,
                }
            ],
            plugin,
        )
    )
    assert body["gaps_opened"] == []
    assert owner_rows("SELECT count(*) AS n FROM tl.coverage_gaps")[0]["n"] == 0
    (row,) = coverage_rows()
    assert row["status"] == "best_effort" and row["reasons"] == ["partial_history"]
    assert row["rows_delivered"] == 4


def test_p1_default_rows_not_reported_and_stale_degrades(client, channel):
    add_channel(client, NOTES)
    with reader() as c:
        rows = c.execute(
            "SELECT * FROM api.coverage WHERE channel_id IS NOT NULL ORDER BY channel"
        ).fetchall()
    assert [r["channel"] for r in rows] == [channel, NOTES]
    for r in rows:
        assert r["status"] == "best_effort" and r["reasons"] == ["not_reported"]
        assert r["reported"] is False
    plugin = headers("plugin", "synthetic")
    ok(report(client, [{"channel": channel, "status": "complete"}], plugin))
    ok(
        client.post(
            "/v1/plugins/synthetic/heartbeat", json={"interval_s": 60}, headers=plugin
        )
    )
    with reader() as c:
        row = c.execute(
            "SELECT * FROM api.coverage_for(ARRAY['synthetic.chat'])"
        ).fetchone()
    assert row["status"] == "complete" and row["stale"] is False
    assert row["window_status"] == "covered"
    with connect() as c:
        c.execute(
            "UPDATE tl.plugins SET last_success_at=now()-interval '1 hour' WHERE name='synthetic'"
        )
        c.execute("UPDATE tl.coverage SET last_success_at=now()-interval '1 hour'")
    with reader() as c:
        cov = c.execute(
            "SELECT * FROM api.coverage WHERE channel='synthetic.chat'"
        ).fetchone()
        row = c.execute(
            "SELECT * FROM api.coverage_for(ARRAY['synthetic.chat'])"
        ).fetchone()
    assert cov["status"] == "partial" and cov["reported_status"] == "complete"
    assert "stale" in cov["reasons"] and cov["stale"] is True
    assert row["status"] == "partial" and row["window_status"] == "partly_covered"


def test_p1_plugin_cannot_report_foreign_channel(client, channel):
    add_channel(client, "other.feed", plugin="other")
    plugin = headers("plugin", "synthetic")
    body = ok(
        report(
            client,
            [
                {"channel": "other.feed", "status": "partial", "window": JAN},
                {"channel": "synthetic.absent", "status": "complete"},
                {"channel": channel, "status": "complete"},
            ],
            plugin,
        )
    )
    assert body["refused"] == 2 and body["stored"] == 1
    assert body["gaps_opened"] == []
    rows = owner_rows("SELECT plugin,channel_id FROM tl.coverage")
    assert len(rows) == 1 and rows[0]["plugin"] == "synthetic"
    assert owner_rows("SELECT count(*) AS n FROM tl.coverage_reports")[0]["n"] == 1
    # Even the owner cannot report one plugin's channel under another plugin.
    body = ok(report(client, [{"channel": "other.feed", "status": "complete"}]))
    assert body["refused"] == 1


# --- coverage in recall and packets -------------------------------------------------


def test_p1_recall_execution_reports_coverage_alongside_completeness(client, channel):
    ingest(client, [message(i) for i in range(3)])
    plugin = headers("plugin", "synthetic")
    ok(report(client, [{"channel": channel, "status": "complete"}], plugin))
    body = recall(client, query="orchard", scope={"channels": [channel]})
    execution = body["execution"]
    assert execution["completeness"] == "ranked_sample"
    coverage = execution["coverage"]
    assert coverage["status"] == "complete" and coverage["absence"] is None
    assert [c["channel"] for c in coverage["channels"]] == [channel]
    assert coverage["channels"][0]["window_status"] == "covered"
    assert "absence_is_not_evidence" not in execution["warnings"]
    assert body["records"]


def test_coverage_honesty_absence_statements(client, channel):
    add_channel(client, NOTES)
    ingest(client, [message(i) for i in range(3)])
    plugin = headers("plugin", "synthetic")
    gap = ok(
        report(
            client,
            [
                {"channel": channel, "status": "partial", "window": JAN},
                {"channel": NOTES, "status": "complete"},
            ],
            plugin,
        )
    )["gaps_opened"][0]
    window = {"after": "2020-01-05T00:00:00Z", "before": "2020-01-06T00:00:00Z"}
    body = recall(client, query="zeppelin", scope=dict(window, channels=[channel]))
    assert body["records"] == []
    coverage = body["execution"]["coverage"]
    assert coverage["absence"] == "not_found_in_coverage"
    assert coverage["partial_channels"] == [channel]
    assert [g["id"] for g in coverage["gaps"]] == [gap["id"]]
    assert coverage["channels"][0]["window_status"] == "not_covered"
    assert "absence_is_not_evidence" in body["execution"]["warnings"]
    assert "did_not_happen" not in json.dumps(body)
    # A complete, covering channel: absence in covered sources (still never a
    # statement that something did not happen).
    body = recall(client, query="zeppelin", scope=dict(window, channels=[NOTES]))
    coverage = body["execution"]["coverage"]
    assert coverage["absence"] == "not_found_in_covered_sources"
    assert coverage["status"] == "complete" and coverage["gaps"] == []
    assert "absence_is_not_evidence" not in body["execution"]["warnings"]
    assert "did_not_happen" not in json.dumps(body)
    # Both channels: the worst status wins.
    body = recall(client, query="zeppelin", scope=window)
    coverage = body["execution"]["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["absence"] == "not_found_in_coverage"
    # as_of: coverage describes today, not then.
    body = recall(
        client,
        query="zeppelin",
        scope=dict(window, channels=[NOTES], as_of="2021-01-01T00:00:00Z"),
    )
    assert "coverage_current_only" in body["execution"]["warnings"]


def test_p1_coverage_on_unresolvable_scope_and_empty_results(client, channel):
    ingest(client, [message(i) for i in range(2)])
    body = recall(
        client, query="orchard", scope={"place": {"name": "Synthetic Nowhere"}}
    )
    assert body["records"] == []
    assert any(
        w.startswith("unresolvable_scope") for w in body["execution"]["warnings"]
    )
    coverage = body["execution"]["coverage"]
    assert coverage["absence"] == "not_found_in_coverage"
    assert coverage["status"] == "best_effort"
    assert "absence_is_not_evidence" in body["execution"]["warnings"]
    # An empty candidate list.
    body = recall(client, query="zeppelin")
    assert body["records"] == []
    assert body["execution"]["coverage"]["absence"] == "not_found_in_coverage"
    # A coverage failure degrades, never fails, the recall.
    from throughline import coverage as coverage_module

    original = coverage_module.statement
    coverage_module.statement = lambda *a, **k: "SELECT * FROM tl.coverage"
    try:
        body = recall(client, query="orchard")
    finally:
        coverage_module.statement = original
    assert body["records"]
    assert body["execution"]["status"] == "degraded"
    assert any(i["stage"] == "coverage" for i in body["execution"]["incomplete"])
    assert body["execution"]["coverage"]["status"] == "unknown"


def test_p1_coverage_absence_when_token_budget_cuts_every_record(client, channel):
    ingest(client, [message(i) for i in range(3)])
    plugin = headers("plugin", "synthetic")
    ok(report(client, [{"channel": channel, "status": "complete"}], plugin))
    body = recall(client, query="orchard", budgets={"tokens": 1})
    assert body["records"] == []
    assert body["execution"]["truncation"]["reason"] == "token_budget"
    assert body["execution"]["coverage"]["absence"] == "not_found_in_covered_sources"


def test_p1_coverage_absence_when_a_must_term_is_not_found(client, channel):
    ingest(client, [message(i) for i in range(3)])
    plugin = headers("plugin", "synthetic")
    ok(report(client, [{"channel": channel, "status": "complete"}], plugin))
    body = recall(
        client,
        query="orchard",
        scope={"channels": [channel]},
        hints={"must_terms": ["orchard", "zeppelin"]},
    )
    assert body["records"]
    coverage = body["execution"]["coverage"]
    assert coverage["missing_terms"] == ["zeppelin"]
    assert coverage["absence"] == "not_found_in_covered_sources"
    assert "did_not_happen" not in json.dumps(body)
    # Every must_term found: no absence statement.
    body = recall(
        client,
        query="orchard",
        scope={"channels": [channel]},
        hints={"must_terms": ["orchard"]},
    )
    coverage = body["execution"]["coverage"]
    assert coverage["absence"] is None and "missing_terms" not in coverage


def test_p1_attempt_placeholder_and_run_failed_do_not_stick(client, channel):
    plugin = headers("plugin", "synthetic")
    ok(client.post("/v1/plugins/synthetic/coverage/attempt", json={}, headers=plugin))

    def status():
        with reader() as c:
            return c.execute(
                "SELECT status,window_status FROM api.coverage_for(ARRAY['synthetic.chat'])"
            ).fetchone()

    assert status()["status"] == "best_effort"
    # A plugin that reports per scope key: the attempt placeholder at scope
    # key '' no longer holds the channel at best_effort.
    ok(
        report(
            client,
            [{"channel": channel, "scope_key": "chat-1", "status": "complete"}],
            plugin,
        )
    )
    assert status() == {"status": "complete", "window_status": "covered"}
    # A failed run (what the SDK posts) makes the channel partial ...
    ok(
        report(
            client,
            [{"channel": channel, "status": "partial", "reasons": ["run_failed"]}],
            plugin,
        )
    )
    assert status()["status"] == "partial"
    # ... until the next successful report for a scope key of the channel.
    ok(
        report(
            client,
            [{"channel": channel, "scope_key": "chat-1", "status": "complete"}],
            plugin,
        )
    )
    assert status()["status"] == "complete"
    # Another reason at scope key '' is a real report and stays.
    ok(
        report(
            client,
            [{"channel": channel, "status": "partial", "reasons": ["rate_limited"]}],
            plugin,
        )
    )
    ok(
        report(
            client,
            [{"channel": channel, "scope_key": "chat-1", "status": "complete"}],
            plugin,
        )
    )
    assert status()["status"] == "partial"


def test_p1_packet_known_source_gaps(client, channel):
    add_channel(client, NOTES)
    ingest(client, [message(i) for i in range(2)])
    ingest(
        client,
        [message(9, channel=NOTES, external_id="note-9", fields={})],
    )
    plugin = headers("plugin", "synthetic")
    gap = ok(
        report(
            client,
            [
                {"channel": channel, "status": "partial", "window": JAN},
                {"channel": NOTES, "status": "complete"},
            ],
            plugin,
        )
    )["gaps_opened"][0]
    ids = [r["id"] for r in owner_rows("SELECT id FROM tl.messages ORDER BY id")]
    token = owner_rows(
        "SELECT * FROM tl.tokens WHERE kind='owner' ORDER BY id LIMIT 1"
    )[0]
    scope = Scope.from_token(dict(token, asker=None))
    handle = resultsets.create(
        scope, [dict(record_id=f"m:{i}") for i in ids], completeness="exhaustive"
    )
    packet = packets.build(scope, handle)
    gaps = packet["coverage"]["known_source_gaps"]
    assert {"kind": "partial_channel", "channel": channel}.items() <= gaps[0].items()
    assert any(g.get("kind") == "gap" and g["id"] == gap["id"] for g in gaps)
    assert all(g.get("channel") != NOTES for g in gaps)
    status = packet["coverage"]["source_status"]
    assert status[channel]["status"] == "partial"
    assert status[NOTES]["status"] == "complete"


def test_p1_catalog_section_and_describe_coverage(client, channel):
    with reader() as c:
        described = c.execute("SELECT api.describe('coverage') AS d").fetchone()["d"]
        subjects = c.execute("SELECT api.describe() AS d").fetchone()["d"]["subjects"]
        consumers = c.execute("SELECT api.describe('consumers') AS d").fetchone()["d"]
        functions = {
            r["name"]: r
            for r in c.execute(
                "SELECT f->>'name' AS name,f->>'exactness' AS exactness,f->>'description' AS description FROM jsonb_array_elements(api.describe('functions')) f"
            ).fetchall()
        }
    text = json.dumps(described)
    assert "unknown degrades to partial" in text
    assert "A gap is not absence" in text
    assert "not found in covered sources versus not found in coverage" in text
    assert set(described["statuses"]) == {"complete", "partial", "best_effort"}
    assert "empty_verified" in described["reason_codes"]
    assert [c["channel"] for c in described["channels"]] == [channel]
    assert {"coverage", "feeds", "consumers"} <= {s["subject"] for s in subjects}
    assert "mark after work" in json.dumps(consumers).lower()
    assert consumers["renamed_to"] == "feeds"
    for name in ("coverage_for", "news", "pending"):
        assert functions[name]["exactness"] == "exact"
    assert "not a population" in functions["news"]["description"]
    from throughline.query import validate_sql

    for statement in (
        "SELECT * FROM api.coverage_for()",
        "SELECT * FROM api.news('x',10)",
        "SELECT * FROM api.pending('x')",
    ):
        validate_sql(statement)


def test_acl_8_coverage_rows_only_for_granted_channels_no_count_leak(client, channel):
    add_channel(client, SECRET)
    plugin = headers("plugin", "synthetic")
    ok(
        report(
            client,
            [
                {"channel": channel, "status": "complete"},
                {"channel": SECRET, "status": "partial", "window": JAN},
                {"status": "partial", "scope_key": "account"},
            ],
            plugin,
        )
    )
    access.register_asker("guest")
    access.grant("guest", channel=channel)
    guest = {"X-Throughline-Asker": "guest"}
    body = ok(client.get("/v1/coverage", headers=guest))
    assert [r["channel"] for r in body["channels"]] == [channel]
    assert body["plugins"] == []
    owner = ok(client.get("/v1/coverage"))
    assert [r["channel"] for r in owner["channels"]] == [channel, SECRET]
    assert [r["scope_key"] for r in owner["plugins"]] == ["account"]

    def sql_rows(statement):
        return ok(client.post("/v1/sql", json={"statement": statement}, headers=guest))[
            "rows"
        ]

    assert sql_rows("SELECT count(*) AS n FROM api.coverage") == [{"n": 1}]
    assert sql_rows("SELECT count(*) AS n FROM api.coverage_gaps") == [{"n": 0}]
    assert [r["channel"] for r in sql_rows("SELECT * FROM api.coverage_for()")] == [
        channel
    ]
    assert sql_rows("SELECT * FROM api.coverage_for(ARRAY['synthetic.secret'])") == []
    # Error oracle: a failing expression never runs on a hidden row.
    for statement in (
        "SELECT 1/(CASE WHEN channel='synthetic.secret' THEN 0 ELSE 1 END) AS x FROM api.coverage",
        "SELECT 1/(CASE WHEN channel='synthetic.secret' THEN 0 ELSE 1 END) AS x FROM api.coverage_gaps",
        "SELECT ('x'||channel)::int AS x FROM api.coverage WHERE channel='synthetic.secret'",
    ):
        r = client.post("/v1/sql", json={"statement": statement}, headers=guest)
        assert r.status_code == 200, (statement, r.text)
    # A restricted asker cannot write or read plugin coverage state.
    assert report(client, [{"status": "complete"}], guest).status_code == 403
    assert (
        client.get("/v1/plugins/synthetic/coverage/gaps", headers=guest).status_code
        == 403
    )
    with reader("asker", "guest", "deny") as c:
        assert (
            c.execute("SELECT count(*) AS n FROM api.coverage_gaps").fetchone()["n"]
            == 0
        )


def test_plg_7_status_staleness_plugins_coverage_backup_age(client, channel):
    ingest(client, [message(0)])
    plugin = headers("plugin", "synthetic")
    ok(
        client.post(
            "/v1/plugins/synthetic/heartbeat", json={"interval_s": 60}, headers=plugin
        )
    )
    ok(
        report(
            client,
            [{"channel": channel, "status": "partial", "window": JAN}],
            plugin,
        )
    )
    body = ok(client.get("/v1/status"))
    assert {"embeddings", "plugins", "channels"} <= set(body)
    s = body["staleness"]
    assert s["newest_ingest_at"] and 0 <= s["newest_ingest_age_s"] < 600
    (p,) = s["plugins"]
    assert p["name"] == "synthetic" and p["stale"] is False and p["age_s"] < 600
    assert s["coverage"] == {"partial": [channel], "best_effort": [], "open_gaps": 1}
    assert s["backup"]["stale"] is True and s["backup"]["last_dump_at"] is None
    with connect() as c:
        c.execute(
            "INSERT INTO tl.backup_runs(kind,started_at,finished_at,status,file_sha256,bytes) VALUES ('dump',now()-interval '1 hour',now()-interval '1 hour','ok','fake',10),('verify',now(),now(),'ok',NULL,NULL)"
        )
    backup = ok(client.get("/v1/status"))["staleness"]["backup"]
    assert backup["stale"] is False and 0.9 < backup["age_hours"] < 1.1
    assert backup["last_verified_at"]
    with connect() as c:
        c.execute(
            "UPDATE tl.backup_runs SET finished_at=now()-interval '40 hours' WHERE kind='dump'"
        )
    assert ok(client.get("/v1/status"))["staleness"]["backup"]["stale"] is True


def test_p1_cli_lib_coverage(client, channel, monkeypatch):
    from throughline import cli

    plugin = headers("plugin", "synthetic")
    ok(
        report(
            client, [{"channel": channel, "status": "partial", "window": JAN}], plugin
        )
    )

    def fake_request(method, path, data=None, params=None):
        return ok(client.request(method, "/v1/" + path, json=data, params=params))

    monkeypatch.setattr(cli, "request", fake_request)
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["lib", "coverage", "--channel", channel, "--json"], color=False
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["channels"][0]["channel"] == channel
    assert body["channels"][0]["status"] == "partial"
    result = runner.invoke(cli.app, ["lib", "coverage"], color=False)
    assert result.exit_code == 0, result.output
    assert "status=partial" in result.output and "gaps=1" in result.output


def test_int_1_help_budget():
    from throughline import cli

    runner = CliRunner()
    text = ""
    for group in ("", "token", "identity", "track"):
        result = runner.invoke(
            cli.app, ([group] if group else []) + ["--help"], color=False
        )
        assert result.exit_code == 0, result.output
        text += result.output
    assert len(text) / 4 < 1500, len(text) / 4
    root = {c.name for c in cli.app.registered_commands}
    assert not root & {"coverage", "feed", "consumer"}
    lib = runner.invoke(cli.app, ["lib", "--help"], color=False).output
    assert "coverage" in lib and "feed" in lib
    for group in ("feed", "consumer"):  # consumer: hidden deprecated alias
        shown = runner.invoke(cli.app, ["lib", group, "--help"], color=False)
        for name in ("create", "list", "news", "pending", "mark", "drop", "settings"):
            assert name in shown.output, (group, name)
    assert isinstance(cli.lib, typer.Typer)


def test_seeds_survive_truncation(client):
    with connect() as c:
        truncate_tl(c)
        reasons = c.execute("SELECT count(*) AS n FROM tl.coverage_reasons").fetchone()
        settings = c.execute("SELECT * FROM tl.consumer_settings").fetchall()
    assert reasons["n"] == 16
    assert [(s["settle_s"], s["margin_s"]) for s in settings] == [(120, 15)]
