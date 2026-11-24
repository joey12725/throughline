"""Real-process contract helpers. Core/psycopg are optional test-time tools only."""

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile


from ._server import core_server
from ._capture import capture
from .models import Manifest
from ._checks import auxiliary_state, message_checks


def command_for(entry):
    """Resolve module:PluginClass to the standard SDK runner process."""
    if isinstance(entry, str):
        module, symbol = entry.split(":")
        return [
            sys.executable,
            "-c",
            f"from {module} import {symbol}; from throughline_sdk import run; run({symbol})",
        ]
    return list(entry)


def check_plugin(entry, source_env, *, secrets=(), crash_seed=17):
    """Run against recorded inputs only. Returns evidence; assertions fail CI.

    Each crash run gets a fresh database. Three seeded write barriers kill the
    plugin after the core commits and before the plugin receives its ACK.
    Supply a small historical fixture (at most 10,000 messages), covering every
    declared channel and enough writes for three distinct crash points.
    """
    command = command_for(entry)
    logs = []
    manifest = json.loads(
        subprocess.check_output(
            command + ["manifest"], env=dict(os.environ, **source_env)
        )
    )
    Manifest.model_validate(manifest)
    secrets = tuple(
        set(secrets)
        | {
            source_env.get(d["env"], os.getenv(d["env"]))
            for d in manifest.get("secrets", {}).values()
        }
        - {None, ""}
    )
    channels = {c["name"] for c in manifest["channels"]}

    def invoke(args, env):
        result = subprocess.run(
            command + args,
            env=dict(env, **source_env),
            capture_output=True,
            text=True,
            timeout=120,
        )
        logs.append(result.stdout + result.stderr)
        for secret in secrets:
            assert secret not in result.stdout + result.stderr, (
                "secret leaked in plugin output"
            )
        assert result.returncode == 0, (
            "plugin failed; inspect synthetic fixture and sanitized logs"
        )
        return json.loads(result.stdout.splitlines()[-1])

    def state(client):
        response = client.post(
            "sql",
            json={
                "statement": "SELECT channel,external_id,version,thread_no,happened_at,ended_at,content,fields,images,happened_offset_min FROM api.messages ORDER BY channel,external_id",
                "row_cap": 10000,
                "raw": True,
            },
        )
        response.raise_for_status()
        assert not response.json()["truncated"], (
            "use a recorded fixture of at most 10,000 messages"
        )
        return response.json()["rows"]

    with core_server(channels) as (client, env):
        dry = invoke(["backfill", "--dry-run"], env)
        assert not state(client), "dry-run wrote messages"
        with capture(env["THROUGHLINE_URL"]) as (relay, evidence, _):
            invoke(["backfill"], dict(env, THROUGHLINE_URL=relay))
        import_requests = list(evidence)
        delivered = [
            m
            for e in evidence
            if e["path"] == "/v1/ingest/messages"
            for m in e["body"]["messages"]
        ]
        baseline = state(client)
        auxiliary = auxiliary_state(env)
        assert baseline, "fixture must contain messages"
        assert {r["channel"] for r in baseline} <= channels
        from collections import Counter
        from datetime import datetime

        counts = Counter(row["channel"] for row in baseline)
        for channel, count in counts.items():
            assert abs(dry["counts"].get(channel, 0) - count) <= count * 0.01
        dates = [datetime.fromisoformat(m["happened_at"]) for m in delivered]
        assert datetime.fromisoformat(dry["start"]) == min(dates)
        assert datetime.fromisoformat(dry["end"]) == max(dates)
        invoke(["sync", "--once"], env)
        assert state(client) == baseline
        # Reset only the cursor, then replay the historical import in full.
        client.put(
            f"plugins/{manifest['name']}/cursor/default", json={}
        ).raise_for_status()
        invoke(["backfill"], env)
        assert state(client) == baseline, "duplicate delivery changed current state"
        assert auxiliary_state(env) == auxiliary, "duplicate auxiliary delivery"
        # Inspect only this kit-created database; compare raw source payloads,
        # original offsets and the independent ingest clock against wire data.
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(
            env["THROUGHLINE_DATABASE_URL"], row_factory=dict_row
        ) as conn:
            raw = conn.execute(
                "SELECT c.name channel,m.external_id,m.happened_at,m.ingested_at,m.happened_offset_min,p.payload FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id JOIN tl.raw_payloads p ON p.message_id=m.id WHERE m.superseded_by IS NULL"
            ).fetchall()
        expected = {(m["channel"], m["external_id"]): m for m in delivered}
        for row in raw:
            original = expected[(row["channel"], row["external_id"])]
            assert row["payload"] == original["payload"]
            assert row["happened_at"] == datetime.fromisoformat(original["happened_at"])
            assert row["happened_offset_min"] == int(
                datetime.fromisoformat(original["happened_at"])
                .utcoffset()
                .total_seconds()
                / 60
            )
            assert row["ingested_at"] > row["happened_at"], (
                "use historical recorded fixtures"
            )
        # Check numbering and 10/5 context against timestamp-sorted wire input.
        numbered = client.post(
            "sql",
            json={
                "statement": "SELECT id,channel,thread_id,thread_no,happened_at FROM api.messages ORDER BY channel,thread_id,thread_no",
                "row_cap": 10000,
            },
        ).json()["rows"]
        groups = {}
        for row in numbered:
            groups.setdefault((row["channel"], row["thread_id"]), []).append(row)
        for group in groups.values():
            assert [r["thread_no"] for r in group] == list(range(1, len(group) + 1))
            assert [r["happened_at"] for r in group] == sorted(
                r["happened_at"] for r in group
            )
            anchor = min(10, len(group) - 1)
            context = client.get(
                "context",
                params={"message_id": group[anchor]["id"], "before": 10, "after": 5},
            ).json()["rows"]
            assert [r["id"] for r in context] == [
                r["id"] for r in group[max(0, anchor - 10) : anchor + 6]
            ]
        for row in baseline:
            assert row["content"].strip() and row["thread_no"] > 0
        status = client.get("status").json()
        assert any(
            p["name"] == manifest["name"] and p["last_success_at"]
            for p in status["plugins"]
        )
        with psycopg.connect(env["THROUGHLINE_DATABASE_URL"]) as conn:
            conn.execute(
                "UPDATE tl.plugins SET last_success_at=now()-make_interval(secs=>interval_s*2+1) WHERE name=%s",
                (manifest["name"],),
            )
        assert next(
            p
            for p in client.get("status").json()["plugins"]
            if p["name"] == manifest["name"]
        )["stale"]
        serialized = json.dumps([baseline, raw, evidence], default=str)
        for secret in secrets:
            assert secret not in serialized
            assert secret not in auxiliary
        message_checks(client, manifest, delivered)
        for channel in channels:
            response = client.post("reparse", json={"channel": channel})
            response.raise_for_status()
        for row in state(client):
            assert row["content"] == json.dumps(
                expected[(row["channel"], row["external_id"])]["payload"],
                sort_keys=True,
            )
    # Sync must import the same history into an empty store, not merely exit
    # successfully when the backfill cursor is already at EOF.
    with core_server() as (client, env):
        invoke(["sync", "--once"], env)
        assert state(client) == baseline, "sync has a different normalized shape"
        assert auxiliary_state(env) == auxiliary
    # Replay the emitted normalized messages in reverse order into an empty
    # store, so numbering is exercised on insertion rather than on duplicates.
    with core_server() as (client, env):
        for channel in manifest["channels"]:
            response = client.post(
                "channels", json=dict(channel, plugin=manifest["name"])
            )
            response.raise_for_status()
        reversed_messages = list(reversed(delivered))
        for start in range(0, len(reversed_messages), 5000):
            response = client.post(
                "ingest/messages",
                json={
                    "plugin": manifest["name"],
                    "messages": reversed_messages[start : start + 5000],
                },
            )
            response.raise_for_status()
            assert not response.json()["rejected"]
        assert state(client) == baseline, "out-of-order delivery changed messages"
    rng = random.Random(crash_seed)
    candidates = [
        i + 1
        for i, request in enumerate(import_requests)
        if request["path"] == "/v1/ingest/messages"
    ]
    assert candidates, "plugin sent no ingest requests"
    for stop_at in (
        rng.sample(candidates, 3)
        if len(candidates) >= 3
        else rng.choices(candidates, k=3)
    ):
        with core_server() as (client, env):
            with capture(env["THROUGHLINE_URL"], stop_at=stop_at) as (
                relay,
                crash_evidence,
                blocked,
            ):
                with tempfile.TemporaryFile(mode="w+") as output:
                    process = subprocess.Popen(
                        command + ["backfill"],
                        env=dict(env, **source_env, THROUGHLINE_URL=relay),
                        stdout=output,
                        stderr=output,
                    )
                    try:
                        assert blocked.wait(30), "import did not reach crash barrier"
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=15)
                    output.seek(0)
                    logs.append(output.read())
                for secret in secrets:
                    assert secret not in json.dumps(crash_evidence)
            invoke(["backfill"], env)
            assert state(client) == baseline, "crash/resume differs"
            assert auxiliary_state(env) == auxiliary, (
                "crash/resume auxiliary state differs"
            )
    for secret in secrets:
        assert all(secret not in log for log in logs), "secret leaked"
    return dict(messages=len(baseline), crashes=3, manifest=manifest)


def main():
    parser = argparse.ArgumentParser(
        description="Test a plugin with recorded source inputs from environment"
    )
    parser.add_argument("--entry", help="module:PluginClass")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    entry = args.entry or args.command
    if not entry:
        parser.error("provide --entry or a command after --")
    if isinstance(entry, list) and entry[0] == "--":
        entry = entry[1:]
    print(json.dumps(check_plugin(entry, {})))
