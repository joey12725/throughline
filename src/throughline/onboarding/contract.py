"""The plugin onboarding contract: static, install-free plugin metadata.

A plugin repository describes its plugins in TOML so the web onboarding and
setup agents can show, configure, install and backfill them before any code
from the repository runs. Two layouts are accepted:

* a multi-plugin repository with `throughline-plugins.toml` at its root,
  holding `[[plugins]]` tables (each either complete, or `path = "dir"`
  pointing at a directory with its own `throughline-plugin.toml`);
* a single-plugin repository with `throughline-plugin.toml` at its root.

See docs/plugin-onboarding-contract.md for the full description. This
module only parses and validates; it never imports or executes plugin code.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

INDEX_FILE = "throughline-plugins.toml"
PLUGIN_FILE = "throughline-plugin.toml"
SCHEMA = 1

CATEGORIES = {
    "messages": "Messages & chat",
    "email": "Email",
    "calendar": "Calendar",
    "meetings": "Meetings & voice",
    "location": "Location",
    "health": "Health & fitness",
    "media": "Watching, listening & reading",
    "notes": "Notes & knowledge",
    "ai": "AI assistants & coding agents",
    "dev": "Code & work",
    "social": "Social",
    "finance": "Finance",
    "activity": "Computer activity",
    "photos": "Photos",
    "migration": "Migrate from another system",
    "other": "Other",
}

FieldKind = Literal[
    "text",
    "secret",
    "url",
    "email",
    "path",
    "directory",
    "file",
    "select",
    "boolean",
    "number",
    "list",
    "textarea",
]


class SetupField(BaseModel):
    """One value the owner provides. `key` is the environment variable the
    plugin reads (the same name as its Manifest declaration)."""

    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    label: str
    kind: FieldKind = "text"
    required: bool = False
    help: str = ""
    help_url: str | None = None
    placeholder: str | None = None
    default: str | None = None
    options: list[str] = Field(default_factory=list)
    advanced: bool = False
    # Only shown when another field has one of these values, e.g.
    # {"WAVE_SOURCE": ["api"]}.
    when: dict[str, list[str]] = Field(default_factory=dict)


class SetupStep(BaseModel):
    title: str
    body: str = ""
    url: str | None = None


class Setup(BaseModel):
    steps: list[SetupStep] = Field(default_factory=list)
    fields: list[SetupField] = Field(default_factory=list)


class Install(BaseModel):
    # python: `path` is a directory with a pyproject.toml; it is installed
    # into the plugin environment and run through its console script
    # `entry`. command: `run` is the argv (relative to the plugin dir).
    kind: Literal["python", "command"] = "python"
    entry: str | None = None
    run: list[str] = Field(default_factory=list)


class Backfill(BaseModel):
    supported: bool = True
    # full: the whole history the source still holds; window: only a recent
    # window (say how long in `note`); none: only new items from now on.
    history: Literal["full", "window", "none"] = "full"
    note: str = ""


class Schedule(BaseModel):
    interval_s: int = Field(default=3600, gt=0)
    mode: Literal["poll", "webhook", "stream", "manual"] = "poll"


class PluginSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    summary: str
    description: str = ""
    category: str = "other"
    icon: str = "plug"
    homepage: str | None = None
    tags: list[str] = Field(default_factory=list)
    # Channels this plugin writes. Detection of an already-connected source
    # (e.g. after restoring a backup) looks for data in these; a pattern
    # ending in ".*" matches a channel family.
    channels: list[str] = Field(default_factory=list)
    # Location-only plugins write a track source instead of channels.
    track_sources: list[str] = Field(default_factory=list)
    platforms: list[Literal["any", "linux", "macos", "windows"]] = Field(
        default_factory=lambda: ["any"]
    )
    # companion: needs files or apps on the owner's computer (runs through
    # the desktop companion); upload: the owner uploads an export file.
    requires: list[Literal["companion", "docker", "upload", "network"]] = Field(
        default_factory=list
    )
    # Sources that share data with this one (offered together).
    pairs_with: list[str] = Field(default_factory=list)
    install: Install = Field(default_factory=Install)
    setup: Setup = Field(default_factory=Setup)
    backfill: Backfill = Field(default_factory=Backfill)
    schedule: Schedule = Field(default_factory=Schedule)
    check: bool = False
    starter: bool = False
    # Directory of the plugin, relative to the repository root.
    path: str = "."

    @field_validator("category")
    @classmethod
    def _category(cls, value):
        if value not in CATEGORIES:
            raise ValueError(
                f"unknown category {value!r}; use one of: " + ", ".join(CATEGORIES)
            )
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value):
        parts = Path(value).parts
        if Path(value).is_absolute() or ".." in parts:
            raise ValueError("path must stay inside the repository")
        return value

    def required_fields(self):
        return [f for f in self.setup.fields if f.required]

    def secret_keys(self):
        return {f.key for f in self.setup.fields if f.kind == "secret"}


class RepoIndex(BaseModel):
    schema_: int = Field(default=SCHEMA, alias="schema")
    name: str = "Plugins"
    description: str = ""
    homepage: str | None = None
    plugins: list[PluginSpec]


class ContractError(ValueError):
    """A repository whose metadata cannot be used. `problems` lists each
    problem as {where, message} so the UI can show all of them at once."""

    def __init__(self, message, problems=()):
        super().__init__(message)
        self.problems = list(problems)


def _problems(error: ValidationError, where):
    return [
        {
            "where": where + ("." + ".".join(str(p) for p in e["loc"]) if e["loc"] else ""),
            "message": e["msg"],
        }
        for e in error.errors()
    ]


def _load(path: Path):
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ContractError(
            f"{path.name} is not valid TOML: {error}",
            [{"where": path.name, "message": str(error)}],
        ) from None


def _plugin(data, where, base="."):
    try:
        spec = PluginSpec.model_validate(data)
    except ValidationError as error:
        raise ContractError(f"{where} is not a valid plugin entry", _problems(error, where)) from None
    if base != ".":
        spec.path = str(Path(base) / spec.path) if spec.path != "." else base
    return spec


def load_repo(root: Path) -> RepoIndex:
    """Read the plugin metadata of a checked-out repository."""
    root = Path(root)
    index = root / INDEX_FILE
    single = root / PLUGIN_FILE
    if index.exists():
        data = _load(index)
        schema = data.get("schema", SCHEMA)
        if schema != SCHEMA:
            raise ContractError(
                f"{INDEX_FILE} uses schema {schema}; this Throughline reads schema {SCHEMA}"
            )
        specs, problems = [], []
        for i, entry in enumerate(data.get("plugins") or []):
            where = f"{INDEX_FILE} plugins[{i}]"
            try:
                if set(entry) == {"path"} or ("path" in entry and "id" not in entry):
                    sub = root / entry["path"]
                    if not (sub / PLUGIN_FILE).exists():
                        raise ContractError(
                            f"{where}: {entry['path']}/{PLUGIN_FILE} not found",
                            [{"where": where, "message": f"{entry['path']}/{PLUGIN_FILE} not found"}],
                        )
                    body = _load(sub / PLUGIN_FILE)
                    body.setdefault("path", ".")
                    specs.append(_plugin(body, f"{entry['path']}/{PLUGIN_FILE}", entry["path"]))
                else:
                    specs.append(_plugin(entry, where))
            except ContractError as error:
                problems.extend(error.problems or [{"where": where, "message": str(error)}])
        if problems:
            raise ContractError(
                f"{len(problems)} problem(s) in the plugin metadata", problems
            )
        seen = set()
        for spec in specs:
            if spec.id in seen:
                raise ContractError(
                    f"plugin id {spec.id!r} appears twice",
                    [{"where": INDEX_FILE, "message": f"duplicate id {spec.id}"}],
                )
            seen.add(spec.id)
        return RepoIndex(
            schema=SCHEMA,
            name=data.get("name") or root.name,
            description=data.get("description") or "",
            homepage=data.get("homepage"),
            plugins=specs,
        )
    if single.exists():
        spec = _plugin(_load(single), PLUGIN_FILE)
        return RepoIndex(schema=SCHEMA, name=spec.name, plugins=[spec])
    raise ContractError(
        f"no {INDEX_FILE} or {PLUGIN_FILE} at the repository root",
        [
            {
                "where": "/",
                "message": f"add {PLUGIN_FILE} (one plugin) or {INDEX_FILE} (several);"
                " see docs/plugin-onboarding-contract.md",
            }
        ],
    )


GITHUB = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?"
    r"(?:/tree/(?P<ref>[A-Za-z0-9._/-]{1,200}))?/?$"
)


def parse_github(url: str):
    """(owner, repo, ref or None) for a GitHub repository link, or raise a
    ContractError explaining what a valid link looks like."""
    text = (url or "").strip()
    short = re.fullmatch(r"(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})", text)
    match = short or GITHUB.match(text)
    if not match:
        raise ContractError(
            "That doesn't look like a GitHub repository link.",
            [
                {
                    "where": "url",
                    "message": "Paste a link like https://github.com/owner/repo"
                    " (or owner/repo, or …/tree/branch)",
                }
            ],
        )
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if repo in (".", "..") or not repo:
        raise ContractError("That repository name is not valid.")
    ref = match.groupdict().get("ref")
    if ref and (".." in ref or ref.startswith("/")):
        raise ContractError("That branch name is not valid.")
    return match.group("owner"), repo, ref
