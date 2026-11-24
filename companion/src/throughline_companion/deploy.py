"""Install the companion on another computer over SSH, without its GUI.

    throughline-companion deploy user@laptop --pair - -- --detected --capture-screens 60 --service

1. Build wheels of the companion, the SDK and the desktop connectors from
   this checkout (or use --source: a wheel folder, a checkout or a
   requirement the target can install).
2. Copy them and the installer to ~/throughline-companion-install on the
   target (scp).
3. Run the installer there over ssh. It installs uv if needed (a pinned,
   checksum-verified download), installs the companion as a uv tool and runs
   `throughline-companion setup` with the arguments after `--`. The pairing
   code goes over the SSH connection's stdin, never on a command line.

Windows targets (--windows) need the OpenSSH server; the installer is the
PowerShell script and the service is a logon task in the user's desktop
session.
"""

import shlex
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

REMOTE_DIR = "throughline-companion-install"
DESKTOP_PACKAGES = [
    "throughline-sdk",
    "throughline-companion",
    "throughline-activitywatch",
    "throughline-aider",
    "throughline-apple-notes",
    "throughline-browser-history",
    "throughline-claude-code",
    "throughline-codex",
    "throughline-cursor",
    "throughline-imessage",
    "throughline-lmstudio",
    "throughline-markdown-notes",
    "throughline-t3code",
    "throughline-youtube",
    "throughline-capture",
]


def workspace_root():
    """The repository checkout this companion runs from, or None."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        project = parent / "pyproject.toml"
        if project.exists() and "[tool.uv.workspace]" in project.read_text(encoding="utf-8"):
            return parent
    return None


def build_wheels(out_dir):
    root = workspace_root()
    uv = shutil.which("uv")
    if root is None or uv is None:
        raise RuntimeError("deploy builds wheels from a Throughline checkout with uv; pass --source instead")
    for package in DESKTOP_PACKAGES:
        subprocess.run(
            [uv, "build", "--wheel", "--package", package, "--out-dir", str(out_dir)],
            cwd=root, check=True, capture_output=True,
        )
    return sorted(p.name for p in Path(out_dir).glob("*.whl"))


def _installer(name):
    return resources.files("throughline_companion").joinpath(f"install/{name}").read_bytes()


def deploy(host, *, pairing=None, setup_args=(), windows=False, source=None, extras="capture,cookies,desktop-plugins",
           ssh_options=(), runner=subprocess.run):
    ssh = ["ssh", *sum((["-o", o] for o in ssh_options), [])]
    scp = ["scp", "-q", "-r", *sum((["-o", o] for o in ssh_options), [])]
    setup = [a for a in setup_args if a != "--"]
    if pairing:
        setup = ["--pair", "-", *setup]
    with tempfile.TemporaryDirectory(prefix="tlc-deploy-") as tmp:
        stage = Path(tmp) / REMOTE_DIR
        (stage / "wheels").mkdir(parents=True)
        script = "install.ps1" if windows else "install.sh"
        (stage / script).write_bytes(_installer(script))
        remote_from = source
        if source is None or Path(source).is_dir():
            if source is None:
                names = build_wheels(stage / "wheels")
                print(f"built {len(names)} wheels", file=sys.stderr)
            else:
                for wheel in Path(source).glob("*.whl"):
                    shutil.copy2(wheel, stage / "wheels")
            remote_from = f"{REMOTE_DIR}/wheels" if not windows else f"{REMOTE_DIR}\\wheels"
        print(f"copying to {host}:{REMOTE_DIR}", file=sys.stderr)
        runner(ssh + [host, "rm -rf " + REMOTE_DIR] if not windows
               else ssh + [host, f'powershell -NoProfile -Command "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue {REMOTE_DIR}"'],
               check=False)
        runner(scp + [str(stage), f"{host}:"], check=True)
        if windows:
            remote = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                      f"{REMOTE_DIR}\\install.ps1", "-From", remote_from, "-Extras", extras, *setup]
            command = subprocess.list2cmdline(remote)
        else:
            remote = ["sh", f"{REMOTE_DIR}/install.sh", "--from", remote_from, "--extras", extras, "--", *setup]
            command = shlex.join(remote)
        print(f"installing on {host}", file=sys.stderr)
        result = runner(ssh + [host, command], input=(pairing + "\n") if pairing else "", text=True)
        return result.returncode
