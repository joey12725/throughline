"""Companion devices and the desktop bridge (docs/companion.md)."""

import threading
import time

from fastapi.testclient import TestClient

from throughline import companion
from throughline.api import create_app
from throughline.db import connect, create_token


def headers(token):
    return {"Authorization": "Bearer " + token}


def enrolled(name="laptop", plugins=("synthetic",)):
    result = companion.enroll(name, "http://127.0.0.1:8000", plugins)
    bundle = companion.decode_pairing(result["pairing"])
    return result, bundle


def test_pairing_code_round_trip_and_scoped_tokens(client):
    result, bundle = enrolled(plugins=("synthetic", "browser_history"))
    assert bundle["device"] == "laptop"
    assert set(bundle["plugin_tokens"]) == {"browser_history", "synthetic"}
    with connect() as conn:
        rows = conn.execute(
            "SELECT kind,plugin FROM tl.tokens WHERE name LIKE 'companion laptop%%' ORDER BY plugin"
        ).fetchall()
    assert [(r["kind"], r["plugin"]) for r in rows] == [
        ("plugin", "browser_history"),
        ("plugin", "companion_laptop"),
        ("plugin", "synthetic"),
    ]
    # A second enrollment under the same name is refused.
    try:
        companion.enroll("laptop", "http://x", ())
    except ValueError as error:
        assert "already enrolled" in str(error)
    else:
        raise AssertionError("duplicate device enrolled")


def test_device_routes_refuse_other_tokens(client):
    enrolled()
    plugin = create_token("p", "plugin", "synthetic")["token"]
    r = client.post("/v1/companion/hello", json={}, headers=headers(plugin))
    assert r.status_code == 403
    r = client.get("/v1/companion/poll?wait=0", headers=headers(plugin))
    assert r.status_code == 403


def test_call_round_trip_and_answer_is_cleared(client):
    _, bundle = enrolled()
    device = headers(bundle["device_token"])
    plugin = headers(bundle["plugin_tokens"]["synthetic"])
    r = client.post(
        "/v1/companion/hello",
        headers=device,
        json=dict(
            platform="linux",
            app_version="0.1.0",
            capabilities=[
                dict(name="cookies", description="Browser cookies", allowed_plugins=["synthetic"])
            ],
            plugins=[dict(name="browser_history", state="ok")],
        ),
    )
    assert r.status_code == 200, r.text

    answer = {}

    def call():
        caller = TestClient(create_app())
        answer["response"] = caller.post(
            "/v1/companion/call",
            headers=plugin,
            json=dict(capability="cookies", args={"domain": "example.com"}, timeout_s=20),
        )

    thread = threading.Thread(target=call)
    thread.start()
    claimed = []
    deadline = time.monotonic() + 15
    while not claimed and time.monotonic() < deadline:
        claimed = client.get("/v1/companion/poll?wait=1", headers=device).json()["requests"]
    assert len(claimed) == 1
    assert claimed[0]["capability"] == "cookies"
    assert claimed[0]["args"] == {"domain": "example.com"}
    assert claimed[0]["plugin"] == "synthetic"
    r = client.post(
        f"/v1/companion/requests/{claimed[0]['id']}",
        headers=device,
        json=dict(ok=True, result={"cookie_header": "SID=synthetic"}),
    )
    assert r.status_code == 200, r.text
    thread.join(20)
    response = answer["response"]
    assert response.status_code == 200, response.text
    assert response.json() == {"device": "laptop", "result": {"cookie_header": "SID=synthetic"}}
    with connect() as conn:
        row = conn.execute("SELECT status,response,args FROM tl.companion_requests").fetchone()
    assert row["status"] == "collected"
    assert row["response"] is None and row["args"] == {}
    # The owner sees the device online with what it offers.
    devices = client.get("/v1/companion/devices").json()["devices"]
    assert devices[0]["online"] is True
    assert devices[0]["capabilities"][0]["allowed_plugins"] == ["synthetic"]


def test_call_refused_for_a_plugin_the_device_does_not_allow(client):
    _, bundle = enrolled()
    client.post(
        "/v1/companion/hello",
        headers=headers(bundle["device_token"]),
        json=dict(capabilities=[dict(name="cookies", allowed_plugins=["youtube"])]),
    )
    other = create_token("p", "plugin", "synthetic")["token"]
    r = client.post(
        "/v1/companion/call", headers=headers(other), json=dict(capability="cookies")
    )
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "not_offered"
    # The device itself cannot call the bridge.
    r = client.post(
        "/v1/companion/call",
        headers=headers(bundle["device_token"]),
        json=dict(capability="cookies"),
    )
    assert r.status_code == 403


def test_offline_device_and_timeout(client):
    _, bundle = enrolled()
    device = headers(bundle["device_token"])
    client.post(
        "/v1/companion/hello",
        headers=device,
        json=dict(capabilities=[dict(name="file.read", allowed_plugins=["*"])]),
    )
    plugin = headers(bundle["plugin_tokens"]["synthetic"])
    r = client.post(
        "/v1/companion/call", headers=plugin, json=dict(capability="file.read", timeout_s=1)
    )
    assert r.status_code == 504
    with connect() as conn:
        assert conn.execute("SELECT status FROM tl.companion_requests").fetchone()[
            "status"
        ] == "expired"
        conn.execute("UPDATE tl.companion_devices SET last_seen_at=now()-interval '1 hour'")
    r = client.post("/v1/companion/call", headers=plugin, json=dict(capability="file.read"))
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "offline"


def test_revoke_revokes_every_device_token(client):
    _, bundle = enrolled(plugins=("synthetic", "aa"))
    result = companion.revoke("laptop")
    assert result["tokens_revoked"] == 3
    r = client.post("/v1/companion/hello", headers=headers(bundle["device_token"]), json={})
    assert r.status_code == 401


def test_a_revoked_device_name_can_be_enrolled_again(client):
    enrolled()
    companion.revoke("laptop")
    _, bundle = enrolled()
    r = client.post("/v1/companion/hello", headers=headers(bundle["device_token"]), json={})
    assert r.status_code == 200


def test_provision_keeps_the_docker_device_enrolled(client, tmp_path):
    path = tmp_path / "pairing" / "connectors"
    url = "http://throughline:8000"
    # Nothing enabled and no device: nothing is enrolled.
    assert companion.provision("docker", url, [], path)["status"] == "none"
    assert not path.exists()

    first = companion.provision("docker", url, ["github", "caldav"], path)
    assert first == dict(status="enrolled", device="docker", plugins=["caldav", "github"])
    assert path.stat().st_mode & 0o777 == 0o600
    bundle = companion.decode_pairing(path.read_text())
    r = client.post("/v1/companion/hello", headers=headers(bundle["device_token"]), json={})
    assert r.status_code == 200

    # Run again unchanged: same tokens, nothing rewritten.
    assert companion.provision("docker", url, ["caldav", "github"], path)["status"] == "unchanged"
    assert companion.decode_pairing(path.read_text()) == bundle

    # A plugin added and one removed: the removed token is revoked, the
    # device and the kept token stay the same.
    changed = companion.provision("docker", url, ["github", "youtube"], path)
    assert changed["status"] == "updated" and changed["plugins"] == ["github", "youtube"]
    after = companion.decode_pairing(path.read_text())
    assert after["device_token"] == bundle["device_token"]
    assert after["plugin_tokens"]["github"] == bundle["plugin_tokens"]["github"]
    caldav = bundle["plugin_tokens"]["caldav"]
    assert client.get("/v1/channels", headers=headers(caldav)).status_code == 401
    youtube = after["plugin_tokens"]["youtube"]
    assert client.get("/v1/channels", headers=headers(youtube)).status_code == 200

    # The file lost: the old device is revoked and a new one enrolled.
    path.unlink()
    again = companion.provision("docker", url, ["github"], path)
    assert again["status"] == "enrolled"
    assert client.post(
        "/v1/companion/hello", headers=headers(after["device_token"]), json={}
    ).status_code == 401
    fresh = companion.decode_pairing(path.read_text())
    assert client.post(
        "/v1/companion/hello", headers=headers(fresh["device_token"]), json={}
    ).status_code == 200

    # The owner revoked the device by hand: provisioning enrolls it again.
    companion.revoke("docker")
    assert companion.provision("docker", url, ["github"], path)["status"] == "enrolled"


def test_provision_follows_the_location_source_setting(client, tmp_path):
    path = tmp_path / "pairing"
    companion.provision("docker", "http://t", ["gps"], path)

    def flag():
        with connect() as conn:
            return conn.execute(
                "SELECT location_source FROM tl.tokens WHERE name='companion docker: gps' AND revoked_at IS NULL"
            ).fetchone()["location_source"]

    assert flag() is False
    assert companion.provision("docker", "http://t", ["gps"], path, location_plugins=["gps"])["status"] == "updated"
    assert flag() is True


def test_provision_with_a_bad_plugin_name_changes_nothing(client, tmp_path):
    path = tmp_path / "pairing"
    companion.provision("docker", "http://t", ["github"], path)
    before = path.read_text()
    path.unlink()  # would re-enroll (and revoke the device) if names were fine
    try:
        companion.provision("docker", "http://t", ["Bad-Name"], path)
    except ValueError as error:
        assert "invalid plugin name" in str(error)
    else:
        raise AssertionError("bad plugin name accepted")
    device_token = companion.decode_pairing(before)["device_token"]
    r = client.post("/v1/companion/hello", headers=headers(device_token), json={})
    assert r.status_code == 200


def test_provision_command_skips_a_pasted_pairing_code(client, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from throughline.cli import app

    settings = tmp_path / "connectors.json"
    settings.write_text('{"plugins": {"github": {"enabled": true}}}')
    out = tmp_path / "pairing"
    args = ["lib", "companion", "provision", "docker", "--url", "http://t",
            "--config", str(settings), "--out", str(out)]
    monkeypatch.setenv("THROUGHLINE_COMPANION_PAIRING", "tlc1.x")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0 and "skipped" in result.output and not out.exists()
    monkeypatch.delenv("THROUGHLINE_COMPANION_PAIRING")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0 and "enrolled" in result.output and out.exists()
    settings.write_text('{"plugins": {"github": true}}')
    assert CliRunner().invoke(app, args).exit_code != 0
