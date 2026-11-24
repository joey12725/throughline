"""Assemble the public starter-plugins repository from this checkout.

    uv run python scripts/export_starter_plugins.py OUT_DIR

Copies every plugin listed in plugins/throughline-plugins.toml (and nothing
else) into OUT_DIR, with the index at its root, so OUT_DIR can be pushed as
the repository Throughline's onboarding points at by default
(THROUGHLINE_STARTER_REPO; the default is DEFAULT_STARTER_REPO in
throughline.onboarding.catalog).
Plugins not in the index (private ones) are never copied. The output is
validated against the onboarding contract before the script exits.

It never runs git: review OUT_DIR, then initialise and push it yourself.
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from throughline.onboarding.contract import INDEX_FILE, load_repo  # noqa: E402

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".venv", "node_modules", ".git", "*.egg-info")

README = """# Throughline starter plugins

The connectors Throughline's onboarding offers by default: messages, email,
calendars, meetings, location, health, media, notes, AI assistants and more.

Each directory is one plugin (a Python package built on `throughline-sdk`).
`throughline-plugins.toml` describes them for onboarding: what each imports,
the credentials it needs, how to back fill history. See the plugin
onboarding contract, `docs/plugin-onboarding-contract.md` in the Throughline
repository.

Add your own repository of plugins the same way: put a
`throughline-plugins.toml` (several plugins) or `throughline-plugin.toml` (one)
at its root, and paste its GitHub link into Throughline's onboarding or
Settings → Plugins.
"""


def main(out):
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    index = load_repo(ROOT / "plugins")
    out.mkdir(parents=True, exist_ok=True)
    for spec in index.plugins:
        shutil.copytree(ROOT / "plugins" / spec.path, out / spec.path, ignore=IGNORE)
    shutil.copy2(ROOT / "plugins" / INDEX_FILE, out / INDEX_FILE)
    (out / "README.md").write_text(README)
    if (ROOT / "LICENSE").exists():
        shutil.copy2(ROOT / "LICENSE", out / "LICENSE")
    exported = load_repo(out)
    leaked = [p.name for p in out.iterdir() if p.is_dir() and p.name not in {s.path for s in exported.plugins}]
    if leaked:
        raise SystemExit("unexpected directories: " + ", ".join(leaked))
    print(f"{len(exported.plugins)} plugins written to {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
