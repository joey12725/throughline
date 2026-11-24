"""The agent surface with the person, coverage, consumer and commitment
packages in place (INT-1, INT-7, INT-8, INT-10; the behaviour priorities
P1..P11 and the twelve behaviour scenarios in the docs index).

Like tests/test_v11_surface.py, everything is enumerated from the running
application, the database and the files, never from a fixed list of what
exists. Names that a sibling package registers (commitments) are
optional-feature: a mismatch limited to them skips, naming them.
"""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

# Imported at collection time, while the name 'conftest' still resolves to
# tests/conftest.py (a lazy import inside a test would get a plugin's).
from test_v11_recipes import build_corpus

from throughline import budget, recipes
from throughline.db import connect
from throughline.query import FUNCTIONS
from throughline.retrieval.types import ATTRIBUTION_KEYS, RECORD_KEYS
from throughline.semantic_ops import discover

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "throughline" / "SKILL.md"
# The reference index (docs/reference/ once renamed, docs/v11/ before).
INDEX = next(
    (p for p in (ROOT / "docs" / "reference" / "index.md", ROOT / "docs" / "v11" / "index.md") if p.exists()),
    ROOT / "docs" / "v11" / "index.md",
)
# The SQL functions (people 0047, coverage and consumers 0046):
# each must be registered, allow-listed and real.
WAVE5_SQL = {
    "person_brief",
    "person_timeline",
    "person_arc",
    "dms_with",
    "between_threads",
    "between",
    "last_contact",
    "open_loops",
    "interactions",
    "around",
    "day",
    "together",
    "mentions",
    "circle",
    "ties",
    "rhythm",
    "attribution",
    "suspected_attributions",
    "exchange_index_status",
    "coverage_for",
    "news",
    "pending",
}
# Registered by the commitments package (migration 0050 and its
# allow-list file): until both land, a mismatch limited to these names is
# optional-feature.
WAVE6_PENDING = re.compile(r"^(open_commitments|commitment\w*)$")
# Retrieval internals are never presented as tools (same list as
# tests/test_v11_surface.py).
INTERNAL = (
    "scope_filter",
    "search_records",
    "vector_records",
    "list_records",
    "entity_search",
    "time_overlap_many",
    "near_many",
    "follow_many",
    "rerank",
    "fusion",
    "rrf",
    "planner",
    "legs",
)
SCENARIOS = (
    "dyad_vs_author",
    "newest_relevant",
    "episode_count",
    "open_loops",
    "consumer_exactly_once",
    "coverage_honesty",
    "media_contaminated",
    "unenrolled_voice",
    "suspected_link",
    "write_replay",
    "link_vocabulary",
    "restricted_inference",
)


def ok(response):
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


def registry():
    with connect() as c:
        return {r["name"]: r for r in c.execute("SELECT * FROM tl.api_functions")}


def api_functions():
    with connect() as c:
        return {
            r["proname"]
            for r in c.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api'"
            )
        }


def defined_tests():
    names = set()
    for path in (
        list((ROOT / "tests").glob("test_*.py"))
        + list((ROOT / "plugins").glob("*/tests/test_*.py"))
        + list((ROOT / "sdk" / "tests").glob("test_*.py"))
    ):
        names |= set(
            re.findall(
                r"^\s*(?:async )?def (test_\w+)",
                path.read_text(encoding="utf-8", errors="ignore"),
                re.M,
            )
        )
    return names


def table_rows(text, heading):
    """{first cell: rest of the row} of the Markdown table under a heading."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    block = text[start : end if end > 0 else len(text)]
    rows = {}
    for line in block.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows[cells[0]] = cells[1:]
    return rows


def section_rows(text, wanted):
    """table_rows of the first '## ' section whose table rows satisfy
    wanted(rows): sections are found by what they hold, not by heading."""
    for match in re.finditer(r"^## .*$", text, re.M):
        rows = table_rows(text, match.group(0))
        if rows and wanted(rows):
            return rows
    raise AssertionError("no section with the wanted table")


# --- INT-8 -------------------------------------------------------------------


def test_int_8_registry_equals_allowlist_and_semantic_registry_incl_wave5_6(client):
    rows = registry()
    sql_rows = {n for n, r in rows.items() if r["kind"] == "sql"}
    semantic_rows = {n for n, r in rows.items() if r["kind"] == "semantic"}
    ops = set(discover())
    real = api_functions()
    gaps = {
        "registry rows not allow-listed": sql_rows - FUNCTIONS,
        "allow-listed names without a registry row": FUNCTIONS - sql_rows,
        "registry rows without an api function": sql_rows - real,
        "semantic rows without an operator": semantic_rows - ops,
        "operators without a semantic row": ops - semantic_rows,
    }
    unexpected = {
        k: sorted(n for n in v if not WAVE6_PENDING.match(n))
        for k, v in gaps.items()
        if any(not WAVE6_PENDING.match(n) for n in v)
    }
    assert not unexpected, unexpected
    # Every function is in all three: catalogue, allow-list, database.
    assert WAVE5_SQL <= sql_rows & FUNCTIONS & real, WAVE5_SQL - (
        sql_rows & FUNCTIONS & real
    )
    # A function that exists must be catalogued too (both ways once
    # its allow-list lands).
    wave6 = {n for n in real if WAVE6_PENDING.match(n)}
    for name in wave6 & FUNCTIONS:
        assert name in sql_rows, name
    # Examples of every catalogued function run on the fixture corpus (zero
    # rows is fine; a placeholder handle stands for one of the caller's sets).
    build_corpus(client)
    handle = ok(
        client.post("/v1/recall", json={"query": "orchard", "k": 3, "save": True})
    )["result_set"]
    failures = {}
    for name, row in sorted(rows.items()):
        assert re.search(rf"\bapi\.{name}\b", row["example"]), name
        if name not in FUNCTIONS and name not in ops:
            continue  # optional-feature: not callable through /v1/sql yet
        example = re.sub(r"rs_0{32}", handle, row["example"])
        r = client.post("/v1/sql", json={"statement": example})
        if r.status_code != 200:
            failures[name] = (r.status_code, r.text[:200])
    assert not failures, failures
    pending = {k: sorted(v) for k, v in gaps.items() if v}
    if pending:
        pytest.skip(f"optional-feature (commitments): {pending}")


def test_surface_and_ops_sections_name_waves_5_6_entry_points(client):
    surface = ok(client.get("/v1/describe", params={"subject": "surface"}))
    # The order of use is unchanged; the person brief is the first call for
    # any question about a person.
    assert [s["step"] for s in surface["order"]] == [
        "catalogue",
        "sql",
        "context",
        "recall",
        "librarian",
    ]
    assert "person_brief" in surface["first_call"]["person"]
    for key in ("attribution", "coverage", "incidents", "new_arrivals", "writes"):
        assert surface[key], key
    assert "Idempotency-Key" in surface["writes"]
    assert "is_me" in surface["attribution"]
    assert "not tools" in surface["techniques"]
    contract = ok(client.get("/v1/describe", params={"subject": "write_contract"}))
    rules = " ".join(contract["rules"])
    for route in ("/v1/plugins/{name}/coverage", "/v1/feeds", "/v1/consumers", "consumer_cursor"):
        assert route in rules, route
    ops = ok(client.get("/v1/describe", params={"subject": "ops"}))
    for command in ("backup", "verify", "alarm"):
        assert "throughline lib backup" in ops["commands"][command], command
    assert "THROUGHLINE_BACKUP_STALE_HOURS" in ops["commands"]["alarm"]
    index = ok(client.get("/v1/describe"))
    subjects = {s["subject"] for s in index["subjects"]}
    assert {"surface", "ops", "write_contract", "people", "transcripts"} <= subjects


def test_write_contract_names_every_receipt_kind_the_code_issues(client):
    """receipt_kinds in describe('write_contract') covers every kind a route
    can answer with: the tl.stored_hash kinds, every registered receipt kind
    (the app is loaded, so every route module has registered its own) and
    every row_receipt kind written in the source."""
    from throughline import writes

    issued = set(writes.KINDS) | set(writes._RECEIPTS)
    for path in (ROOT / "src" / "throughline").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        issued |= set(re.findall(r"row_receipt\(\s*\"(\w+)\"", text))
    contract = ok(client.get("/v1/describe", params={"subject": "write_contract"}))
    missing = sorted(issued - set(contract["receipt_kinds"]))
    assert not missing, missing
    assert {"consumer_cursor", "noise_run"} <= issued


# --- INT-7 -------------------------------------------------------------------


def test_int_7_skill_limits_and_brief_first():
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, "front matter"
    front = dict(
        line.split(": ", 1) for line in match.group(1).splitlines() if ": " in line
    )
    assert front["name"] == "throughline"
    assert len(front["description"]) < 240, len(front["description"])
    assert "brief" in front["description"]
    assert budget.estimate_tokens(text) < 3200, budget.estimate_tokens(text)
    body = text[match.end() :]
    lowered = body.lower()
    # People: the person brief comes right after search and context, before
    # exact SQL.
    assert (
        lowered.index("post /v1/find")
        < lowered.index("person_brief")
        < lowered.index("exact sql")
    )
    for term in (
        "attribution_status",
        "is_me",
        "evidence_class",
        "coverage before absence",
        "a gap is not absence",
        "episodes rather than hits",
        "feed",
        "then mark",
        "idempotency-key",
        "receipt",
        "do not read back",
        "409",
    ):
        assert term in lowered, term
    # The recipes it names are tested, among them the people and ops ones.
    tested = {n for n, r in recipes.load_files().items() if r["status"] == "tested"}
    named = set(re.findall(r"`([a-z_]+)`", text)) & tested
    assert {
        "person_brief_first",
        "open_loops_both_ways",
        "last_contact_pitfall",
        "write_with_receipt",
        "coverage_first",
        "feed_loop",
        "incidents_by_episode",
    } <= named, named
    # Internal techniques are still not tools; no host is named.
    for internal in INTERNAL:
        assert internal not in lowered, internal
    for host in ("claude", "hermes", "grokbot", "codex", "openai", "anthropic", "mcp"):
        assert not re.search(rf"\b{host}\b", lowered), host


# --- INT-10 ------------------------------------------------------------------


def test_int_10_record_keys_incl_attribution_across_recall_sets_packets_http_cli(
    client, monkeypatch
):
    build_corpus(client)
    assert "attribution" in RECORD_KEYS
    recall = ok(
        client.post(
            "/v1/recall",
            json={"query": "orchard map", "k": 10, "save": True},
        )
    )
    handle = recall["result_set"]
    shown = ok(client.get(f"/v1/sets/{handle}"))
    packet = ok(client.post("/v1/packets", json={"handle": handle}))
    bodies = {"recall": recall["records"], "set": shown["records"]}
    for name, records in bodies.items():
        assert records, name
        for record in records:
            assert set(RECORD_KEYS) <= set(record), (
                name,
                set(RECORD_KEYS) - set(record),
            )
            value = record["attribution"]
            if record["record_id"].startswith("m:"):
                assert set(ATTRIBUTION_KEYS) <= set(value), (name, value)
            else:
                assert value is None, (name, record["record_id"])
    for record in packet["records"]:
        assert "attribution" in record and "evidence_class" in record
    # The owner side by direction outgoing, the friend by its confirmed account.
    dm = [r for r in recall["records"] if r["channel"] == "synthetic.dm"]
    assert dm, [r["channel"] for r in recall["records"]]
    statuses = {r["attribution"]["attribution_status"] for r in dm}
    assert {r["attribution"]["is_me"] for r in dm} == {True, False}
    assert statuses <= {"confirmed", "self_direction"}, statuses
    # The same keys over the CLI (it calls the same routes).
    import throughline.cli as cli

    def request(method, path, data=None, params=None):
        response = client.request(method, "/v1/" + path, json=data, params=params)
        assert response.status_code < 400, response.text
        return response.json()

    monkeypatch.setattr(cli, "request", request)
    runner = CliRunner()
    recalled = runner.invoke(
        cli.app, ["lib", "recall", "orchard map", "--k", "10", "--json"]
    )
    assert recalled.exit_code == 0, recalled.output
    cli_records = json.loads(recalled.output)["records"]
    assert [r["record_id"] for r in cli_records] == [
        r["record_id"] for r in recall["records"]
    ]
    assert all(set(RECORD_KEYS) <= set(r) for r in cli_records)
    assert [r["attribution"] for r in cli_records] == [
        r["attribution"] for r in recall["records"]
    ]


# --- docs index -------------------------------------------------------------


def test_index_maps_p1_to_p11_and_twelve_scenarios():
    text = INDEX.read_text(encoding="utf-8")
    defined = defined_tests()
    priorities = section_rows(text, lambda rows: any(k.startswith("P1 ") for k in rows))
    for n in range(1, 12):
        row = next((v for k, v in priorities.items() if k.startswith(f"P{n} ")), None)
        assert row is not None, f"P{n}"
        named = re.findall(r"`(test_\w+)`( \*\(planned)?", row[0])
        assert named, f"P{n}"
        # At least one landed test accepts each priority.
        assert any(name in defined for name, _ in named), f"P{n}"
        for name, planned in named:
            assert planned or name in defined, (f"P{n}", name)
    scenarios = section_rows(text, lambda rows: bool(set(SCENARIOS) & set(rows)))
    assert set(SCENARIOS) <= set(scenarios), set(SCENARIOS) - set(scenarios)
    for name in SCENARIOS:
        check, feature = scenarios[name][0], scenarios[name][1]
        checks = re.findall(r"`(test_\w+)`", check)
        assert checks and all(c in defined for c in checks), (name, checks)
        assert re.search(r"`test_\w+`", feature), name
    try:
        from bench.behaviours import BEHAVIOURS
    except ImportError:
        BEHAVIOURS = None
    if BEHAVIOURS is not None:
        assert set(BEHAVIOURS) == set(SCENARIOS)
    # Deferred items are stated with their reason.
    deferred = text[text.index("## Deferred ") :]
    for item in (
        "identity_quality",
        "time_precision",
        "Identity state as of",
        "real model",
    ):
        assert item in deferred, item
    # Every named test in the whole index exists (or is planned by a sibling).
    for name, planned in re.findall(r"`(test_\w+)`( \*\(planned)?", text):
        assert planned or name in defined, name
    # The consolidated docs carry the documents.
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    for heading in (
        "Person read model",
        "Write contract",
        "Transcripts",
        "Disclosure",
        "Ops",
    ):
        assert re.search(rf"^#+ .*{heading}", architecture, re.M | re.I), heading
    scope = architecture[architecture.index("Scope and remaining work") :]
    for item in ("identity_quality", "time_precision", "as_of", "real model"):
        assert item in scope, item


# --- INT-1 -------------------------------------------------------------------


def test_int_1_help_budget_with_all_lib_commands():
    import throughline.cli as cli
    from throughline import cli_commands

    loaded = cli_commands.load(cli.lib)
    assert set(cli_commands.names()) <= set(loaded) | {"__init__"}
    runner = CliRunner()
    text = ""
    for name in ("", "token", "identity", "track"):
        result = runner.invoke(
            cli.app, ([name] if name else []) + ["--help"], color=False
        )
        assert result.exit_code == 0, result.output
        text += result.output
    assert len(text) / 4 < 1500, len(text) / 4
    root = {c.name for c in cli.app.registered_commands} | {
        g.name for g in cli.app.registered_groups
    }
    lib = runner.invoke(cli.app, ["lib", "--help"], color=False).output
    expected = {
        "person": ("brief", "last", "loops", "arc", "between", "circle", "around"),
        "feed": ("create", "news", "mark", "settings", "drop"),
        "backup": ("run", "verify-restore", "check"),
        "transcripts": ("noise", "speakers"),
        "coverage": (),
        "recipe": ("list", "show", "run"),
    }
    for group, commands in expected.items():
        assert group in lib, group
        assert group not in root, group
        if not commands:
            continue
        shown = runner.invoke(cli.app, ["lib", group, "--help"], color=False)
        assert shown.exit_code == 0, shown.output
        for command in commands:
            assert command in shown.output, (group, command)
    access = runner.invoke(cli.app, ["lib", "access", "--help"], color=False).output
    assert "label" in access
