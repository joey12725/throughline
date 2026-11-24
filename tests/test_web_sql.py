"""SQL surface additions for the web interface (docs/web/backend.md):
allow-listed builtins, output columns and validate_only."""

import time

import pytest
from conftest import ingest, message

from throughline.db import connect
from throughline.query import BUILTINS, query, validate_sql

ADDED = {
    "floor",
    "ceil",
    "ceiling",
    "width_bucket",
    "date_bin",
    "percentile_cont",
    "percentile_disc",
    "mode",
    "stddev_samp",
    "stddev_pop",
    "var_samp",
    "corr",
    "string_agg",
    "bool_or",
    "bool_and",
    "jsonb_typeof",
    "jsonb_array_length",
    "lag",
    "lead",
    "first_value",
    "last_value",
    "nth_value",
    "ntile",
    "percent_rank",
    "cume_dist",
    "ln",
    "log",
    "sqrt",
    "power",
    "exp",
    "sign",
    "mod",
    "split_part",
    "concat",
    "char_length",
    "cardinality",
    "array_length",
    "to_timestamp",
    "age",
    "justify_interval",
    "make_interval",
    "isfinite",
    "jsonb_array_elements",
    "jsonb_array_elements_text",
    "jsonb_object_keys",
    "unnest",
}

# One realistic statement per group; together they call every added name.
ACCEPTED = {
    "binning": """
        SELECT width_bucket((fields->>'rating')::numeric, 0, 10, 5) AS bucket,
               date_bin('15 minutes', happened_at, '2020-01-01'::timestamptz) AS slot,
               floor(avg(length(content))) AS lo, ceil(avg(length(content))) AS hi,
               ceiling(max((fields->>'rating')::numeric) / 3) AS third, count(*) AS n
        FROM api.messages GROUP BY 1, 2 ORDER BY 2, 1""",
    "quantiles": """
        SELECT channel,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY length(content)) AS median,
               percentile_disc(0.9) WITHIN GROUP (ORDER BY (fields->>'rating')::int) AS p90,
               mode() WITHIN GROUP (ORDER BY author_handle) AS usual_author,
               stddev_samp(length(content)) AS sd, stddev_pop(length(content)) AS sdp,
               var_samp(length(content)) AS var,
               corr(length(content), (fields->>'rating')::float8) AS r,
               string_agg(DISTINCT author_handle, ', ' ORDER BY author_handle) AS authors,
               bool_or((fields->>'rating')::int > 3) AS any_high,
               bool_and(content <> '') AS all_text
        FROM api.messages GROUP BY channel""",
    "windows": """
        SELECT id,
               lag(happened_at) OVER w AS previous_at,
               lead(happened_at, 1) OVER w AS next_at,
               first_value(id) OVER w AS first_id,
               last_value(id) OVER (PARTITION BY thread_id ORDER BY happened_at
                                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS last_id,
               nth_value(id, 2) OVER w AS second_id,
               ntile(4) OVER w AS quartile,
               percent_rank() OVER w AS pr,
               cume_dist() OVER w AS cd,
               age(happened_at, lag(happened_at) OVER w) AS gap
        FROM api.messages WINDOW w AS (PARTITION BY thread_id ORDER BY happened_at)
        ORDER BY happened_at""",
    "arithmetic": """
        SELECT ln(count(*) + 1) AS l, log(10, count(*) + 1) AS l10, log(count(*) + 1) AS lg,
               sqrt(sum(length(content))) AS root, power(count(*), 2) AS squared,
               exp(1) AS e, sign(min((fields->>'rating')::int) - 3) AS s,
               mod(count(*), 7) AS weekday_slot
        FROM api.messages""",
    "text and json": """
        SELECT split_part(channel, '.', 1) AS family,
               concat(author_handle, ' @ ', channel) AS label,
               char_length(content) AS chars,
               jsonb_typeof(fields->'rating') AS rating_type,
               jsonb_array_length(images) AS n_images,
               cardinality(array_agg(id) OVER ()) AS total,
               array_length(array_agg(id) OVER (), 1) AS total_again
        FROM api.messages ORDER BY id LIMIT 20""",
    "time": """
        SELECT to_timestamp(1577836800) AS epoch_start,
               to_timestamp('2020-01-01', 'YYYY-MM-DD') AS parsed,
               age(max(happened_at), min(happened_at)) AS span,
               justify_interval(max(happened_at) - min(happened_at)) AS tidy,
               make_interval(days => 7) AS week,
               make_interval(secs => count(*)) AS seconds,
               isfinite(max(happened_at)) AS finite
        FROM api.messages""",
    "set returning in FROM": """
        SELECT m.id, k AS field, e.value AS element, t.value AS element_text, u AS tag
        FROM api.messages m
        CROSS JOIN LATERAL jsonb_object_keys(m.fields) AS k
        CROSS JOIN LATERAL jsonb_array_elements('[1, 2]'::jsonb) AS e
        CROSS JOIN LATERAL jsonb_array_elements_text('["a"]'::jsonb) AS t
        CROSS JOIN LATERAL unnest(ARRAY['x', 'y']) AS u
        ORDER BY m.id LIMIT 30""",
    "set returning in the select list": """
        SELECT id, jsonb_object_keys(fields) AS field, unnest(ARRAY[1, 2]) AS n,
               jsonb_array_elements(jsonb_build_object('a', 1)->'b') AS none,
               jsonb_array_elements_text('["a", "b"]'::jsonb) AS letter
        FROM api.messages ORDER BY id LIMIT 10""",
    "set returning with ordinality and column aliases": """
        SELECT x.item, x.n, y.value, y.ordinality, z.value AS z, k.k, u.u
        FROM jsonb_array_elements('[1, 2, 3]'::jsonb) WITH ORDINALITY AS x(item, n)
        CROSS JOIN jsonb_array_elements('[true]'::jsonb) WITH ORDINALITY AS y
        CROSS JOIN jsonb_array_elements_text('["q"]'::jsonb) AS z
        CROSS JOIN jsonb_object_keys('{"a": 1}'::jsonb) AS k
        CROSS JOIN unnest(ARRAY[1]) AS u""",
}

REJECTED = [
    # Files, settings, catalogues, sleeping, network.
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_ls_dir('.')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT set_config('throughline.scope_mode', 'owner', false)",
    "SELECT current_setting('throughline.asker')",
    "SELECT pg_sleep(10)",
    "SELECT * FROM dblink('dbname=postgres', 'SELECT 1') AS t(x int)",
    "SELECT query_to_xml('SELECT * FROM tl.messages', true, true, '')",
    "SELECT regexp_replace(content, '(a+)+$', '') FROM api.messages",
    "SELECT regexp_matches(content, 'a') FROM api.messages",
    "SELECT version()",
    "SELECT pg_backend_pid()",
    "SELECT repeat('x', 1000000000)",
    "SELECT txid_current()",
    "SELECT nextval('tl.messages_id_seq')",
    # An allow-listed aggregate or window function cannot carry another call.
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY pg_sleep(1)) FROM api.messages",
    "SELECT percentile_cont(pg_backend_pid()) WITHIN GROUP (ORDER BY id) FROM api.messages",
    "SELECT mode() WITHIN GROUP (ORDER BY current_setting('throughline.asker')) FROM api.messages",
    "SELECT lag(pg_backend_pid()) OVER () FROM api.messages",
    "SELECT lag(id) OVER (ORDER BY pg_sleep(1)) FROM api.messages",
    "SELECT first_value(id) OVER (PARTITION BY set_config('a.b', 'c', false)) FROM api.messages",
    "SELECT ntile(4) OVER w FROM api.messages WINDOW w AS (ORDER BY pg_sleep(1))",
    "SELECT count(*) FILTER (WHERE pg_sleep(1) IS NULL) FROM api.messages",
    "SELECT string_agg(pg_read_file('/etc/passwd'), ',') FROM api.messages",
    "SELECT concat(current_setting('server_version'))",
    "SELECT make_interval(secs => pg_backend_pid())",
    "SELECT unnest(ARRAY[pg_backend_pid()])",
    "SELECT * FROM jsonb_array_elements(to_jsonb(pg_ls_dir('.'))) e",
    "SELECT * FROM unnest(ARRAY[1]) u, LATERAL pg_sleep(u)",
    # A FROM function's alias cannot be used to call a function by attribute
    # notation (alias.name resolves to name(alias) when no such column exists).
    "SELECT e.pg_column_size FROM jsonb_array_elements('[1]'::jsonb) AS e",
    "SELECT e.jsonb_pretty FROM jsonb_array_elements('[1]'::jsonb) AS e",
    "SELECT e.value FROM jsonb_array_elements('[1]'::jsonb) AS e(item)",
    "SELECT k.value FROM jsonb_object_keys('{}'::jsonb) AS k",
    "SELECT k.length FROM jsonb_object_keys('{\"a\": 1}'::jsonb) AS k",
    "SELECT u.md5 FROM unnest(ARRAY['x']) AS u",
    "SELECT (e).value FROM jsonb_array_elements('[1]'::jsonb) AS e",
    "SELECT jsonb_array_elements.jsonb_pretty FROM jsonb_array_elements('[1]'::jsonb)",
    # Schema-qualified spellings of the added names are not accepted.
    "SELECT pg_catalog.floor(1.5)",
    "SELECT pg_catalog.jsonb_array_elements('[1]'::jsonb)",
    # age(xid) reads the transaction counter.
    "SELECT age('5'::xid)",
    "SELECT age(CAST('5' AS xid8))",
    # Relations stay in schema api.
    "SELECT * FROM unnest(ARRAY(SELECT content FROM tl.messages)) u",
    "SELECT string_agg(rolname, ',') FROM pg_roles",
]


@pytest.fixture()
def corpus(client, channel):
    items = [
        message(i * 400, external_id=f"web-{i}", fields={"rating": i % 6})
        for i in range(12)
    ] + [
        message(
            i * 400 + 7,
            external_id=f"web-b-{i}",
            thread={"external_id": "other-thread"},
            author=dict(system="fake", kind="account", value="fake-other"),
            fields={"rating": 5 - i % 5},
        )
        for i in range(8)
    ]
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    return client


def sql(client, statement, **extra):
    return client.post("/v1/sql", json=dict(statement=statement, **extra))


def test_every_added_builtin_is_allow_listed_and_exercised():
    import re

    assert ADDED <= BUILTINS
    called = set(re.findall(r"\b([a-z_0-9]+)\s*\(", " ".join(ACCEPTED.values()).lower()))
    assert ADDED <= called, sorted(ADDED - called)
    # Nothing that matches text against a pattern, reads a setting or a
    # catalogue was added.
    assert not {n for n in BUILTINS if n.startswith(("regexp", "pg_", "current_"))}
    assert not BUILTINS & {"set_config", "current_setting", "repeat", "lpad", "rpad"}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_added_builtins_run_on_a_corpus(corpus, name):
    statement = ACCEPTED[name]
    assert validate_sql(statement)
    r = sql(corpus, statement)
    assert r.status_code == 200, (name, r.text)
    body = r.json()
    assert body["columns"] and len(body["columns"]) >= len(body["rows"][0])
    if name != "set returning in the select list":
        assert body["row_count"] >= 1


def test_added_builtins_give_the_expected_values(corpus):
    row = sql(
        corpus,
        """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY (fields->>'rating')::int) AS median,
                  mode() WITHIN GROUP (ORDER BY thread_id) = min(thread_id) AS first_thread_is_usual,
                  count(*) AS n, floor(2.7) AS f, ceil(2.1) AS c, width_bucket(5, 0, 10, 5) AS b,
                  mod(17, 5) AS m, split_part('a.b.c', '.', 2) AS part
           FROM api.messages""",
    ).json()["rows"][0]
    assert row["n"] == 20 and row["first_thread_is_usual"] is True
    assert (row["f"], row["c"], row["b"], row["m"], row["part"]) == ("2", "3", 3, 2, "b")
    rows = sql(
        corpus,
        "SELECT e.value AS v, n FROM jsonb_array_elements('[10, 20, 30]'::jsonb) WITH ORDINALITY AS e(value, n) ORDER BY n",
    ).json()["rows"]
    assert [r["v"] for r in rows] == [10, 20, 30]
    rows = sql(
        corpus,
        "SELECT id, lag(id) OVER (ORDER BY id) AS previous FROM api.messages ORDER BY id LIMIT 3",
    ).json()["rows"]
    assert rows[0]["previous"] is None and rows[1]["previous"] == rows[0]["id"]


# An alias over a function with a named output column is a relation name, not
# a column: `x.x` would otherwise resolve as a call of the function x.
ALIAS_CALLS = [
    "SELECT current_setting.current_setting FROM jsonb_array_elements_text('[\"server_version\"]') AS current_setting",
    "SELECT pg_get_viewdef.pg_get_viewdef FROM jsonb_array_elements_text('[\"api.messages\"]') AS pg_get_viewdef",
    "SELECT x.x FROM jsonb_array_elements('[1]'::jsonb) AS x",
    "SELECT x.x FROM jsonb_array_elements_text('[\"a\"]') WITH ORDINALITY AS x",
]


@pytest.mark.parametrize("statement", ALIAS_CALLS)
def test_an_alias_is_not_a_column_of_a_named_output_function(statement):
    with pytest.raises(ValueError):
        validate_sql(statement)


def test_named_output_column_still_reachable_through_the_alias():
    assert validate_sql("SELECT x.value FROM jsonb_array_elements_text('[\"a\"]') AS x")
    assert validate_sql("SELECT x.value, x.ordinality FROM jsonb_array_elements('[1]'::jsonb) WITH ORDINALITY AS x")


@pytest.mark.parametrize("statement", REJECTED)
def test_dangerous_statements_stay_rejected(statement):
    with pytest.raises(ValueError):
        validate_sql(statement)


def test_rejected_over_http_without_reaching_the_database(corpus):
    for statement in REJECTED[:8]:
        r = sql(corpus, statement)
        assert r.status_code == 400, (statement, r.text)
        assert "code" not in r.json(), statement  # refused by the validator


def test_generate_series_and_unnest_abuse_is_bounded_by_the_deadline(corpus):
    for statement in (
        "SELECT count(*) AS n FROM generate_series(1, 100000000000) AS g",
        "SELECT sum(power(g, 2)) AS s FROM generate_series(1, 100000000000) AS g, unnest(ARRAY[1, 2, 3]) AS u",
        "SELECT string_agg(concat(g, 'x'), '') AS s FROM generate_series(1, 100000000000) AS g",
    ):
        started = time.monotonic()
        r = sql(corpus, statement, deadline_ms=400)
        assert time.monotonic() - started < 10
        assert r.status_code == 400 and r.json()["code"] == "57014", r.text
    # And by the row cap: the statement stops one row past it.
    r = sql(
        corpus,
        "SELECT unnest(array_agg(g)) AS n FROM generate_series(1, 5000) AS g",
        row_cap=10,
    )
    assert r.status_code == 200 and r.json()["truncated"] and r.json()["row_count"] == 10


# --- columns ------------------------------------------------------------------


def test_columns_are_reported_with_type_names(corpus):
    body = sql(
        corpus,
        "SELECT id, content, happened_at, fields, lat, is_reference, array_agg(id) OVER () AS ids,"
        " count(*) OVER () AS n, 1.5 AS ratio, happened_at::date AS day FROM api.messages ORDER BY id LIMIT 2",
    ).json()
    assert body["columns"] == [
        {"name": "id", "type": "int8"},
        {"name": "content", "type": "text"},
        {"name": "happened_at", "type": "timestamptz"},
        {"name": "fields", "type": "jsonb"},
        {"name": "lat", "type": "float8"},
        {"name": "is_reference", "type": "bool"},
        {"name": "ids", "type": "_int8"},
        {"name": "n", "type": "int8"},
        {"name": "ratio", "type": "numeric"},
        {"name": "day", "type": "date"},
    ]
    assert [c["name"] for c in body["columns"]] == list(body["rows"][0])
    assert set(body) == {"rows", "truncated", "row_count", "execution", "columns"}


def test_columns_present_without_rows_and_duplicates_kept(corpus):
    body = sql(
        corpus, "SELECT id, content AS id, happened_at FROM api.messages WHERE false"
    ).json()
    assert body["rows"] == [] and body["row_count"] == 0
    assert body["columns"] == [
        {"name": "id", "type": "int8"},
        {"name": "id", "type": "text"},
        {"name": "happened_at", "type": "timestamptz"},
    ]
    # A column called like the wrapper's own marker is still the caller's.
    body = sql(corpus, "SELECT 1 AS ok, 2 AS q, 'x' AS d").json()
    assert [c["name"] for c in body["columns"]] == ["ok", "q", "d"]
    assert body["rows"] == [{"ok": 1, "q": 2, "d": "x"}]
    # Truncated results describe their columns too.
    body = sql(corpus, "SELECT id FROM api.messages", row_cap=3).json()
    assert body["truncated"] and body["columns"] == [{"name": "id", "type": "int8"}]


def test_in_process_query_keeps_exactly_three_keys(corpus):
    assert set(query("SELECT 1 AS one")) == {"rows", "truncated", "row_count"}


# --- validate_only ------------------------------------------------------------


def test_validate_only_answers_without_running(corpus):
    started = time.monotonic()
    r = sql(
        corpus,
        "SELECT count(*) AS n, max(g) AS top FROM generate_series(1, 100000000000) AS g",
        validate_only=True,
    )
    assert time.monotonic() - started < 5
    assert r.status_code == 200 and r.json() == {
        "valid": True,
        "error": None,
        "code": None,
        "columns": [{"name": "n", "type": "int8"}, {"name": "top", "type": "int8"}],
    }


def test_validate_only_reports_validator_and_database_errors(corpus):
    cases = {
        "SELECT pg_sleep(1)": ("function is outside the query allow-list", None),
        "SELEC 1": ("invalid SQL syntax", None),
        "SELECT 1; SELECT 2": ("only one SELECT statement is allowed", None),
        "SELECT * FROM tl.messages": ("relations must be explicitly in schema api", None),
        "SELECT nope FROM api.messages": ("database operation rejected", "42703"),
        "SELECT * FROM api.no_such_view": ("database operation rejected", "42P01"),
        "SELECT content + 1 FROM api.messages": ("database operation rejected", "42883"),
    }
    for statement, (error, code) in cases.items():
        r = sql(corpus, statement, validate_only=True)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["valid"] is False, statement
        assert body["code"] == code, statement
        assert body["columns"] == [], statement
        assert (body["error"] or "").startswith(error), (statement, body["error"])


def test_validate_only_is_audited_with_the_statement(corpus):
    statement = "SELECT id FROM api.messages"
    sql(corpus, statement, validate_only=True)
    with connect() as c:
        row = c.execute("SELECT * FROM tl.audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["statement"] == statement and row["row_count"] == 0
    assert row["detail"] == {"route": "POST /v1/sql", "validate_only": True}


def test_validate_only_respects_the_asker_gate_and_token_kinds(corpus):
    from throughline.db import create_token

    plugin = create_token("plugin", kind="plugin", plugin="synthetic")["token"]
    r = corpus.post(
        "/v1/sql",
        json={"statement": "SELECT 1", "validate_only": True},
        headers={"Authorization": "Bearer " + plugin},
    )
    assert r.status_code == 403
    assert corpus.post("/v1/sql", json={"validate_only": True}).status_code == 422
