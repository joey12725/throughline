#!/usr/bin/env python3
"""Generate plugins/catalog.json from plugins/throughline-plugins.toml.

    python3 plugins/build_catalog.py          write catalog.json
    python3 plugins/build_catalog.py --check  exit 1 when catalog.json is stale

The TOML file is the source of truth (the web onboarding reads it; format in
docs/plugin-onboarding-contract.md). The JSON is a flat summary for other
installers: one entry per shipped plugin with its channels and their roles,
the credentials and settings it needs, its license and an install command.
Standard library only.
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "throughline-plugins.toml"
OUT = HERE / "catalog.json"
ROLES = (
    "chat",
    "email",
    "calendar",
    "telemetry",
    "transcript",
    "voice_summary",
    "location",
    "media",
    "notes",
    "other",
)

# Shipped packages that are not onboarding entries: the Companion runs
# `capture` itself, and `media` is a library other plugins import.
EXTRA = [
    {
        "id": "capture",
        "path": "capture",
        "name": "Screen and audio capture",
        "summary": "Uploads what the desktop Companion's optional screen and audio capture records.",
        "category": "activity",
        "kind": "plugin",
        "onboarding": False,
        "channels": [
            {"name": "capture.screens", "role": "media"},
            {"name": "capture.transcripts", "role": "transcript"},
        ],
        "track_sources": [],
        "requires": ["companion"],
        "credentials": [{"key": "IMMICH_API_KEY", "label": "Immich API key", "required": False}],
        "settings": [
            {"key": "CAPTURE_SPOOL_DIR", "label": "Capture spool folder (set by the Companion)", "required": True},
            {"key": "IMMICH_URL", "label": "Immich server", "required": False},
        ],
        "entry": "throughline-capture",
        "note": "Configured and run by the Companion; see docs/capture.md.",
    },
    {
        "id": "media",
        "path": "media",
        "name": "Media enrichment library",
        "summary": "Shared transcription, ffmpeg, yt-dlp and asset-store helpers used by other plugins.",
        "category": "other",
        "kind": "library",
        "onboarding": False,
        "channels": [],
        "track_sources": [],
        "requires": [],
        "credentials": [],
        "settings": [],
        "entry": None,
        "note": "Not a plugin: installed as a dependency of beeper and youtube.",
    },
]


def project(path):
    with open(HERE / path / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]


def field(f):
    return {
        "key": f["key"],
        "label": f.get("label", f["key"]),
        "required": bool(f.get("required", False)),
        **({"when": f["when"]} if f.get("when") else {}),
    }


def entry(spec):
    roles = spec.get("channel_roles") or {}
    channels = []
    for name in spec.get("channels") or []:
        role = roles.get(name)
        if role not in ROLES:
            raise SystemExit(f"{spec['id']}: channel {name} needs a channel_roles entry from {ROLES}")
        channels.append({"name": name, "role": role})
    extra = set(roles) - set(spec.get("channels") or [])
    if extra:
        raise SystemExit(f"{spec['id']}: channel_roles names unknown channels {sorted(extra)}")
    fields = (spec.get("setup") or {}).get("fields") or []
    install = spec.get("install") or {}
    return {
        "id": spec["id"],
        "path": spec["path"],
        "name": spec["name"],
        "summary": spec["summary"],
        "category": spec.get("category", "other"),
        "kind": "plugin",
        "onboarding": True,
        "channels": channels,
        "track_sources": [{"name": t, "role": "location"} for t in spec.get("track_sources") or []],
        "requires": spec.get("requires") or [],
        "credentials": [field(f) for f in fields if f.get("kind") == "secret"],
        "settings": [field(f) for f in fields if f.get("kind") != "secret"],
        "entry": install.get("entry"),
        **({"homepage": spec["homepage"]} if spec.get("homepage") else {}),
    }


def build():
    with open(INDEX, "rb") as handle:
        index = tomllib.load(handle)
    plugins = [entry(p) for p in index["plugins"]] + [dict(e) for e in EXTRA]
    for plugin in plugins:
        meta = project(plugin["path"])
        plugin["package"] = meta["name"]
        plugin["license"] = meta.get("license")
        plugin["install"] = f"uv pip install ./plugins/{plugin['path']}"
    plugins.sort(key=lambda p: p["id"])
    ids = [p["id"] for p in plugins]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate plugin ids")
    return {
        "schema": 1,
        "generated_from": "plugins/throughline-plugins.toml",
        "roles": list(ROLES),
        "plugins": plugins,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when catalog.json is stale")
    args = parser.parse_args(argv)
    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print("plugins/catalog.json is stale: run python3 plugins/build_catalog.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
