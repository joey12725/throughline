"""The companion against a real core: enroll, pair, bridge calls, a connector
run with a companion-issued token."""

import json
import subprocess
import sys
import time

import httpx
import pytest

from throughline_companion.app import Companion
from throughline_companion.config import Config

core_server = pytest.importorskip("throughline_sdk._server").core_server


def enroll(env, url, *plugins):
    command = [sys.executable, "-m", "throughline", "lib", "companion", "enroll", "laptop", "--url", url, "--json"]
    for plugin in plugins:
        command += ["--plugin", plugin]
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["pairing"]


def wait(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_pair_bridge_and_run(tmp_path, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_COMPANION_HOME", str(tmp_path / "home"))
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "note.md").write_text("synthetic note")
    with core_server() as (owner, env):
        url = env["THROUGHLINE_URL"]
        code = enroll(env, url, "claude_code", "notes")
        config = Config()
        companion = Companion(config)
        companion.pair(code)
        companion.update_bridge(
            "file.read", {"enabled": True, "allowed_plugins": ["notes"], "roots": [str(shared)]}
        )
        # Synthetic Claude Code home so the real connector has something to read.
        project = tmp_path / "claude" / "projects" / "-synthetic"
        project.mkdir(parents=True)
        (project / "s1.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "s1",
                    "uuid": "u1",
                    "cwd": "/synthetic",
                    "timestamp": "2020-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "Synthetic orchard question"},
                }
            )
            + "\n"
        )
        companion.update_plugin(
            "claude_code", {"env": {"CLAUDE_CODE_HOMES": f"local={tmp_path / 'claude'}"}}
        )
        # Short polls: the core holds a poll open until its wait ends, and the
        # test server is stopped right after the test.
        companion.bridge.poll_wait = 1
        companion.bridge.start()
        try:
            devices = wait(
                lambda: [
                    d
                    for d in owner.get("companion/devices").json()["devices"]
                    if d["online"] and d["capabilities"]
                ]
            )
            assert devices[0]["name"] == "laptop"
            assert {"file.read", "file.list"} <= {c["name"] for c in devices[0]["capabilities"]}

            plugin_token = config.data["plugins"]["notes"]["token"]
            caller = httpx.Client(
                base_url=url + "/v1/", headers={"Authorization": "Bearer " + plugin_token}, timeout=60
            )
            answer = caller.post(
                "companion/call",
                json={"capability": "file.read", "args": {"path": str(shared / "note.md")}},
            )
            assert answer.status_code == 200, answer.text
            assert answer.json()["result"]["text"] == "synthetic note"
            # Outside the shared folder: the desktop refuses.
            refused = caller.post(
                "companion/call",
                json={"capability": "file.read", "args": {"path": str(tmp_path / "home" / "companion.json")}},
            )
            assert refused.status_code == 502
            # A plugin the desktop did not allow never reaches it.
            other = httpx.post(
                url + "/v1/companion/call",
                headers={"Authorization": "Bearer " + config.data["plugins"]["claude_code"]["token"]},
                json={"capability": "file.read", "args": {"path": str(shared / "note.md")}},
                timeout=30,
            )
            assert other.status_code == 403

            # The connector runs with its companion-issued token.
            job = companion.runner.jobs["claude_code"]
            assert companion.runner.run(job), job.last_error
            rows = owner.post(
                "sql",
                json={"statement": "SELECT count(*) AS n FROM api.messages WHERE channel LIKE 'claude_code.%'"},
            )
            assert rows.status_code == 200, rows.text
            assert rows.json()["rows"][0]["n"] >= 1
        finally:
            companion.stop()
