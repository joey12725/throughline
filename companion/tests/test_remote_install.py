"""Unattended and remote install: service definitions, setup, deploy."""

import base64
import json
import plistlib
import subprocess

import pytest

from throughline_companion import cli, deploy, service
from throughline_companion.config import Config


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_COMPANION_HOME", str(tmp_path / "companion"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def code(**extra):
    bundle = dict(v=1, url="http://example.invalid:8000", device="sshbox", device_token="dev-synthetic",
                  plugin_tokens={"claude_code": "tok-synthetic"}, **extra)
    return "tlc1." + base64.urlsafe_b64encode(json.dumps(bundle).encode()).decode().rstrip("=")


def test_service_runs_headless_with_absolute_paths():
    cmd = service.command()
    assert cmd[-2:] == ["run", "--headless"]
    assert cmd[0].startswith("/")


def test_systemd_unit_and_install(home, monkeypatch):
    calls = []
    monkeypatch.setattr(service.paths, "platform", lambda: "linux")
    monkeypatch.setattr(service, "_systemd_running", lambda: True)
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/" + name)

    def run(cmd, check=False):
        calls.append(cmd)
        out = "WAYLAND_DISPLAY=wayland-1\n" if cmd[-1] == "show-environment" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(service, "_run", run)
    notes = service.install(linger=True)
    text = service._unit_path().read_text()
    assert "ExecStart=" in text and "run --headless" in text and "WantedBy=default.target" in text
    assert "Restart=on-failure" in text
    # enable + restart (not start): an update must replace a running old version.
    assert ["systemctl", "--user", "enable", service.UNIT] in calls
    assert ["systemctl", "--user", "restart", service.UNIT] in calls
    assert any(c[:2] == ["loginctl", "enable-linger"] for c in calls)
    assert notes == ["linger on"]


def test_no_systemd_is_a_clear_error(home, monkeypatch):
    monkeypatch.setattr(service.paths, "platform", lambda: "linux")
    monkeypatch.setattr(service, "_systemd_running", lambda: False)
    with pytest.raises(RuntimeError, match="no systemd"):
        service.install()
    assert service.status()["state"] == "no systemd"


def test_launch_agent_goes_to_the_gui_or_background_domain(home, monkeypatch):
    calls = []
    monkeypatch.setattr(service.paths, "platform", lambda: "macos")
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    console = {"user": "someone-else"}

    def run(cmd, check=False):
        calls.append(cmd)
        if cmd[0] == "stat":
            return subprocess.CompletedProcess(cmd, 0, console["user"] + "\n", "")
        if cmd[0] == "id":
            return subprocess.CompletedProcess(cmd, 0, "tester\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(service, "_run", run)
    notes = service.install()
    assert ["launchctl", "bootstrap", "user/501", str(service._plist_path())] in calls
    assert any("screen capture waits" in n for n in notes)
    plist = plistlib.loads(service._plist_path().read_bytes())
    assert plist["ProgramArguments"][-2:] == ["run", "--headless"] and plist["RunAtLoad"] is True
    console["user"] = "tester"
    calls.clear()
    service.install()
    assert ["launchctl", "bootstrap", "gui/501", str(service._plist_path())] in calls


def test_windows_logon_task_is_interactive_and_least_privilege(monkeypatch):
    calls = []
    monkeypatch.setattr(service.paths, "platform", lambda: "windows")
    monkeypatch.setenv("USERDOMAIN", "BOX")
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setattr(service, "_run", lambda cmd, check=False: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    service.install()
    create = calls[0]
    assert create[:2] == ["schtasks", "/Create"] and "ONLOGON" in create and "/IT" in create
    assert create[create.index("/RL") + 1] == "LIMITED" and create[create.index("/RU") + 1] == "BOX\\tester"
    assert "run --headless" in create[create.index("/TR") + 1]
    assert calls[1:3] == [["schtasks", "/End", "/TN", service.TASK], ["schtasks", "/Run", "/TN", service.TASK]]


def test_setup_pairs_from_stdin_and_enables_detected(home, monkeypatch, capsys):
    (home / "claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude"))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(code() + "\n"))
    assert cli.main(["setup", "--pair", "-", "--detected", "--capture-screens", "60",
                     "--set", "claude_code:CLAUDE_CODE_OWNER_NAME=Synthetic"]) == 0
    config = Config()
    assert config.server["device"] == "sshbox"
    assert config.data["plugins"]["claude_code"]["enabled"] is True
    assert config.data["plugins"]["claude_code"]["env"]["CLAUDE_CODE_OWNER_NAME"] == "Synthetic"
    assert config.data["capture"]["screens"]["interval_s"] == 60 and config.data["capture"]["enabled"] is True
    assert "tok-synthetic" not in capsys.readouterr().out


def test_deploy_copies_and_installs_with_the_code_on_stdin(tmp_path, monkeypatch):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "throughline_companion-0.1.0-py3-none-any.whl").write_bytes(b"synthetic")
    calls = []

    def runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    rc = deploy.deploy("tester@box", pairing=code(), setup_args=["--detected", "--service"], source=str(wheels),
                       ssh_options=["Port=2222"], runner=runner)
    assert rc == 0
    scp = [c for c, _ in calls if c[0] == "scp"][0]
    assert scp[-1] == "tester@box:" and "Port=2222" in scp
    install, kwargs = calls[-1]
    assert install[:3] == ["ssh", "-o", "Port=2222"]
    assert "sh throughline-companion-install/install.sh --from throughline-companion-install/wheels" in install[-1]
    assert install[-1].endswith("-- --pair - --detected --service")
    assert "tlc1." not in " ".join(install) and kwargs["input"].startswith("tlc1.")


def test_deploy_windows_uses_powershell(tmp_path):
    calls = []
    deploy.deploy("tester@winbox", windows=True, source="git+https://example.invalid/throughline#subdirectory=companion",
                  setup_args=["--service"], runner=lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    assert "powershell" in calls[-1][-1] and "install.ps1" in calls[-1][-1] and "--service" in calls[-1][-1]


def test_deploy_cli_splits_setup_arguments(monkeypatch):
    seen = {}
    monkeypatch.setattr(deploy, "deploy", lambda host, **kw: seen.update(host=host, **kw) or 0)
    assert cli.main(["deploy", "tester@box", "--ssh-option", "Port=2222", "--", "--detected", "--service"]) == 0
    assert seen["host"] == "tester@box" and seen["ssh_options"] == ["Port=2222"]
    assert seen["setup_args"] == ["--detected", "--service"]


def test_install_scripts_are_packaged():
    from importlib import resources

    root = resources.files("throughline_companion").joinpath("install")
    assert b"tool install --force" in root.joinpath("install.sh").read_bytes()
    assert b"schtasks" not in root.joinpath("install.sh").read_bytes()
    assert b"Get-FileHash" in root.joinpath("install.ps1").read_bytes()


def test_no_display_means_no_gui(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert cli.has_display() is False
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    assert cli.has_display() is True
