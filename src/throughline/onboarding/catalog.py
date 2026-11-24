"""Plugin repositories, the catalog, installs and detection.

* The **starter** repository is the bundled `plugins/` directory of a source
  checkout (or THROUGHLINE_STARTER_PLUGINS), else the public starter
  repository, cloned on first use (THROUGHLINE_STARTER_REPO).
* **Third-party** repositories are added by pasting a GitHub link; they are
  cloned shallowly into the setup data directory and validated against the
  onboarding contract before anything from them can be installed.
* An **install** records where a plugin's code is and how to start it, its
  plugin token, and whether it is scheduled (tl.plugin_installs). Settings and
  secrets live in onboarding.store.
* **Detection** looks at what the database already holds, so a restored or
  migrated install shows its sources as already connected.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from ..db import connect
from . import store
from .contract import CATEGORIES, ContractError, PluginSpec, load_repo, parse_github
from .errors import SetupError

STARTER_ID = "starter"
DEFAULT_STARTER_REPO = "https://github.com/joey12725/throughline-plugins"
TOKEN_KEY = "THROUGHLINE_TOKEN"
GIT_TIMEOUT_S = 120

# Plugin names that only ever belong to the owner's own private builds; a
# third-party repository may not claim them either.
RESERVED = {"throughline", "setup", "companion"}


def _conn():
    return connect("app")


# ───────────── repositories ─────────────


def starter_root() -> Path | None:
    configured = os.getenv("THROUGHLINE_STARTER_PLUGINS")
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "plugins" / "throughline-plugins.toml"
        if candidate.exists():
            return candidate.parent
    cloned = store.data_dir() / "repos" / "starter"
    if (cloned / "throughline-plugins.toml").exists():
        return cloned
    return None


def _git(*args, cwd=None):
    git = shutil.which("git")
    if not git:
        raise SetupError(
            "git is not installed",
            "Adding plugins from GitHub needs git on the Throughline server.",
            "Install git (e.g. `apt install git`) and try again.",
            status=500,
        )
    try:
        result = subprocess.run(
            [git, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false"),
        )
    except subprocess.TimeoutExpired:
        raise SetupError(
            "GitHub took too long",
            f"git {args[0]} did not finish within {GIT_TIMEOUT_S} seconds.",
            "Check this server's internet connection and try again.",
            status=504,
        ) from None
    return result


def _clone(url, ref, dest: Path):
    tmp = dest.with_name(dest.name + ".incoming")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "--depth", "1", "--quiet"]
    if ref:
        args += ["--branch", ref]
    result = _git(*args, url, str(tmp))
    if result.returncode != 0:
        said = (result.stderr or "").strip().splitlines()
        said = said[-1] if said else ""
        if "not found" in said.lower() or "could not read username" in said.lower() or "terminal prompts disabled" in said.lower():
            raise SetupError(
                "Repository not found",
                f"GitHub has no public repository at {url}.",
                "Check the link. Private repositories aren't supported here: clone it on the server and set THROUGHLINE_STARTER_PLUGINS or use the CLI.",
                status=404,
                code="repo_not_found",
            )
        if ref and ("remote branch" in said.lower() or "not found in upstream" in said.lower()):
            raise SetupError(
                "Branch not found",
                f"The repository has no branch or tag called {ref!r}.",
                "Remove /tree/… from the link to use the default branch.",
                status=404,
            )
        raise SetupError(
            "Couldn't download the repository",
            f"git clone failed: {said or 'unknown error'}",
            "Check the link and this server's internet access.",
            status=502,
        )
    head = _git("rev-parse", "HEAD", cwd=tmp).stdout.strip() or None
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return head


def ensure_starter():
    """Path of the starter repository, cloning the public one if needed."""
    root = starter_root()
    if root is not None:
        return root
    url = os.getenv("THROUGHLINE_STARTER_REPO") or DEFAULT_STARTER_REPO
    owner, repo, ref = parse_github(url)
    dest = store.data_dir() / "repos" / "starter"
    _clone(f"https://github.com/{owner}/{repo}.git", ref, dest)
    return dest


def _repo_rows(conn):
    return conn.execute("SELECT * FROM tl.plugin_repos ORDER BY added_at").fetchall()


def add_repo(url):
    owner, repo, ref = parse_github(url)
    repo_id = f"{owner}/{repo}".lower()
    dest = store.data_dir() / "repos" / re.sub(r"[^a-z0-9._-]", "_", repo_id.replace("/", "__"))
    commit = _clone(f"https://github.com/{owner}/{repo}.git", ref, dest)
    try:
        index = load_repo(dest)
    except ContractError as error:
        shutil.rmtree(dest, ignore_errors=True)
        raise SetupError(
            "This repository isn't a Throughline plugin repository",
            str(error),
            "Its root needs throughline-plugin.toml (one plugin) or throughline-plugins.toml (several). See docs/plugin-onboarding-contract.md.",
            status=422,
            problems=error.problems,
            code="contract",
        ) from None
    if not index.plugins:
        shutil.rmtree(dest, ignore_errors=True)
        raise SetupError("No plugins found", "The repository's plugin list is empty.", status=422)
    taken = {p.id: r for r, p in _all_specs(include=[STARTER_ID]) if r != repo_id}
    clashes = [p.id for p in index.plugins if p.id in taken or p.id in RESERVED]
    if clashes:
        shutil.rmtree(dest, ignore_errors=True)
        raise SetupError(
            "Plugin names already taken",
            "These plugin ids already exist: " + ", ".join(clashes) + ".",
            "A third-party plugin needs its own id (Manifest name). Ask the author to rename it.",
            status=409,
            problems=[{"where": f"plugins.{c}", "message": f"id {c} is already used"} for c in clashes],
        )
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tl.plugin_repos(id,name,url,ref,commit,path,description) VALUES (%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name,url=EXCLUDED.url,ref=EXCLUDED.ref,"
            "commit=EXCLUDED.commit,path=EXCLUDED.path,description=EXCLUDED.description,updated_at=now()",
            (repo_id, index.name, f"https://github.com/{owner}/{repo}", ref, commit, str(dest), index.description),
        )
    return repo_id


def remove_repo(repo_id):
    if repo_id == STARTER_ID:
        raise SetupError("The starter plugins can't be removed", "They ship with Throughline.", "Disable individual plugins instead.", status=409)
    with _conn() as conn:
        row = conn.execute("SELECT path FROM tl.plugin_repos WHERE id=%s", (repo_id,)).fetchone()
        if not row:
            raise SetupError("Repository not found", f"No repository {repo_id!r} was added.", status=404)
        used = conn.execute(
            "SELECT plugin_id FROM tl.plugin_installs WHERE repo_id=%s", (repo_id,)
        ).fetchall()
        if used:
            raise SetupError(
                "Plugins from it are installed",
                "Uninstall these first: " + ", ".join(r["plugin_id"] for r in used) + ".",
                status=409,
            )
        conn.execute("DELETE FROM tl.plugin_repos WHERE id=%s", (repo_id,))
    shutil.rmtree(row["path"], ignore_errors=True)


def _repos():
    """[(repo_id, root, index, row)] for the starter set and every added repo."""
    out = []
    try:
        root = ensure_starter()
        out.append((STARTER_ID, root, load_repo(root), None))
    except (SetupError, ContractError) as error:
        out.append((STARTER_ID, None, error, None))
    with _conn() as conn:
        rows = _repo_rows(conn)
    for row in rows:
        root = Path(row["path"])
        try:
            out.append((row["id"], root, load_repo(root), row))
        except (ContractError, OSError) as error:
            out.append((row["id"], root, error, row))
    return out


def _all_specs(include=None):
    for repo_id, root, index, _ in _repos():
        if include is not None and repo_id not in include:
            continue
        if isinstance(index, Exception):
            continue
        for spec in index.plugins:
            yield repo_id, spec


def find(plugin_id):
    """(repo_id, repo root, spec) for a plugin id, or a 404 SetupError."""
    for repo_id, root, index, _ in _repos():
        if isinstance(index, Exception):
            continue
        for spec in index.plugins:
            if spec.id == plugin_id:
                return repo_id, root, spec
    raise SetupError("Unknown plugin", f"No plugin called {plugin_id!r} in any added repository.", status=404)


# ───────────── detection ─────────────


def data_by_channel(conn):
    rows = conn.execute(
        "SELECT c.name,c.plugin,count(m.id) AS n,min(m.happened_at) AS first_at,max(m.happened_at) AS last_at"
        " FROM tl.channels c LEFT JOIN tl.messages m ON m.channel_id=c.id AND m.superseded_by IS NULL"
        " GROUP BY c.name,c.plugin"
    ).fetchall()
    return {r["name"]: r for r in rows}


def data_by_track(conn):
    rows = conn.execute(
        "SELECT source,count(*) AS n,min(at) AS first_at,max(at) AS last_at FROM tl.location_track GROUP BY source"
    ).fetchall()
    return {r["source"]: r for r in rows}


def _match(pattern, name):
    if pattern.endswith(".*"):
        return name.startswith(pattern[:-1])
    return name == pattern


def presence(spec: PluginSpec, channels, tracks, plugins):
    records, first, last = 0, None, None
    for name, row in channels.items():
        if any(_match(p, name) for p in spec.channels) or (row["plugin"] == spec.id):
            records += row["n"] or 0
            first = min(filter(None, [first, row["first_at"]]), default=None)
            last = max(filter(None, [last, row["last_at"]]), default=None)
    for source in spec.track_sources:
        row = tracks.get(source)
        if row:
            records += row["n"] or 0
            first = min(filter(None, [first, row["first_at"]]), default=None)
            last = max(filter(None, [last, row["last_at"]]), default=None)
    seen = plugins.get(spec.id)
    connected = records > 0 or bool(seen and seen.get("last_success_at"))
    return dict(connected=connected, records=records, first_at=first, last_at=last)


def _plugins_table(conn):
    rows = conn.execute(
        "SELECT name,last_success_at,last_error,consecutive_failures,paused_at FROM tl.plugins"
    ).fetchall()
    return {r["name"]: r for r in rows}


def _installs(conn):
    return {r["plugin_id"]: r for r in conn.execute("SELECT * FROM tl.plugin_installs").fetchall()}


# ───────────── install state ─────────────


def server_bin():
    return Path(sys.executable).parent


def env_dir():
    return store.data_dir() / "env"


def resolve_command(spec: PluginSpec, root: Path | None):
    """argv to run the plugin, or None when it isn't installed."""
    plugin_dir = (root / spec.path) if root else None
    if spec.install.kind == "command":
        if not spec.install.run or plugin_dir is None or not plugin_dir.exists():
            return None
        first = spec.install.run[0]
        local = plugin_dir / first
        argv = [str(local) if local.exists() else first, *spec.install.run[1:]]
        return argv if shutil.which(argv[0]) or Path(argv[0]).exists() else None
    entry = spec.install.entry
    if not entry:
        return None
    for directory in (env_dir() / "bin", server_bin()):
        candidate = directory / entry
        if candidate.exists():
            return [str(candidate)]
    return None


def installable(spec: PluginSpec, root):
    if root is None:
        return False, "The repository isn't available on this server."
    if spec.install.kind == "python" and not spec.install.entry:
        return False, "The plugin entry doesn't name its console script (install.entry)."
    return True, None


def install_hint(spec):
    if "companion" in spec.requires:
        return (
            "Reads apps or files on a computer. Install it here if Throughline runs on that computer;"
            " otherwise add it to the desktop companion when you pair it."
        )
    if "network" in spec.requires:
        return "Receives data pushed from your phone or another device, so that device must be able to reach this server."
    return None


def plugin_view(repo_id, root, spec, *, presence_row, install_row, plugins_row):
    values = store.public_plugin_values(spec.id, spec.secret_keys() | {TOKEN_KEY})
    values.pop(TOKEN_KEY, None)
    effective = store.plugin_values(spec.id)
    missing = [
        f.key
        for f in spec.setup.fields
        if f.required and not effective.get(f.key) and not f.default and _visible(f, effective, spec)
    ]
    command = resolve_command(spec, root)
    can, why = installable(spec, root)
    body = spec.model_dump(exclude={"install", "path"})
    body["setup"]["fields"] = [f.model_dump() for f in spec.setup.fields]
    body["repo_id"] = repo_id
    return dict(
        body,
        status=dict(
            installed=command is not None,
            installable=can,
            install_hint=why or install_hint(spec),
            configured=not missing,
            enabled=bool(install_row and install_row["enabled"]),
            connected=presence_row["connected"],
            records=presence_row["records"],
            last_at=presence_row["last_at"],
            last_error=(install_row or {}).get("last_error") or (plugins_row or {}).get("last_error"),
            values=values,
            missing=missing,
        ),
    )


def _visible(field, values, spec):
    for key, allowed in (field.when or {}).items():
        current = values.get(key)
        if current is None:
            default = next((f.default for f in spec.setup.fields if f.key == key), None)
            current = default
        if current not in allowed:
            return False
    return True


def catalog():
    repos, plugins = [], []
    with _conn() as conn:
        channels, tracks = data_by_channel(conn), data_by_track(conn)
        seen, installs = _plugins_table(conn), _installs(conn)
    problems = []
    for repo_id, root, index, row in _repos():
        if isinstance(index, Exception):
            problems.append(dict(repo_id=repo_id, message=str(index)))
            if repo_id != STARTER_ID:
                repos.append(
                    dict(id=repo_id, name=row["name"], url=row["url"], description=str(index), builtin=False,
                         commit=row["commit"], added_at=row["added_at"], plugins=[], error=str(index))
                )
            continue
        repos.append(
            dict(
                id=repo_id,
                name=index.name if repo_id != STARTER_ID else "Starter plugins",
                url=(row or {}).get("url") if row else None,
                description=index.description,
                builtin=repo_id == STARTER_ID,
                commit=(row or {}).get("commit") if row else None,
                added_at=(row or {}).get("added_at") if row else None,
                plugins=[p.id for p in index.plugins],
            )
        )
        for spec in index.plugins:
            plugins.append(
                plugin_view(
                    repo_id,
                    root,
                    spec,
                    presence_row=presence(spec, channels, tracks, seen),
                    install_row=installs.get(spec.id),
                    plugins_row=seen.get(spec.id),
                )
            )
    return dict(
        categories=[{"id": k, "label": v} for k, v in CATEGORIES.items()],
        repos=repos,
        plugins=plugins,
        problems=problems,
    )


def one(plugin_id):
    repo_id, root, spec = find(plugin_id)
    with _conn() as conn:
        channels, tracks = data_by_channel(conn), data_by_track(conn)
        seen, installs = _plugins_table(conn), _installs(conn)
    return plugin_view(
        repo_id, root, spec,
        presence_row=presence(spec, channels, tracks, seen),
        install_row=installs.get(spec.id),
        plugins_row=seen.get(spec.id),
    )


def detect():
    """What the install already holds (restore / migration / external runners)."""
    sources = []
    with _conn() as conn:
        channels, tracks = data_by_channel(conn), data_by_track(conn)
        seen, installs = _plugins_table(conn), _installs(conn)
        totals = conn.execute(
            "SELECT (SELECT count(*) FROM tl.messages WHERE superseded_by IS NULL) AS records,"
            "(SELECT count(*) FROM tl.people WHERE merged_into IS NULL) AS people,"
            "(SELECT count(*) FROM tl.channels) AS channels,"
            "(SELECT count(*) FROM tl.plugins) AS plugins,"
            "(SELECT count(*) FROM tl.location_track) AS track_points,"
            "(SELECT count(*) FROM tl.accounts) AS accounts,"
            "(SELECT count(*) FROM tl.companion_devices WHERE revoked_at IS NULL) AS devices"
        ).fetchone()
    for repo_id, root, index, _ in _repos():
        if isinstance(index, Exception):
            continue
        for spec in index.plugins:
            row = presence(spec, channels, tracks, seen)
            if not row["connected"] and spec.id not in installs:
                continue
            health = None
            p = seen.get(spec.id)
            if p:
                health = "paused" if p["paused_at"] else ("failing" if p["consecutive_failures"] else ("ok" if p["last_success_at"] else "never"))
            sources.append(
                dict(plugin_id=spec.id, name=spec.name, connected=row["connected"], managed=spec.id in installs,
                     records=row["records"], first_at=row["first_at"], last_at=row["last_at"], health=health)
            )
    from .providers import current_role

    existing = bool(totals["records"] or totals["track_points"])
    return dict(
        existing=existing,
        totals={k: totals[k] for k in ("records", "people", "channels", "plugins", "track_points")},
        sources=sources,
        providers=dict(
            embedding=current_role("embedding")["provider"],
            assistant=current_role("assistant")["provider"],
        ),
        companion_devices=totals["devices"],
        accounts=totals["accounts"],
        restored_hint=os.getenv("THROUGHLINE_SETUP_MODE", "").lower() in ("restore", "resume", "continue")
        or (store.setup_dir() / "restored").exists(),
    )


# ───────────── configuration ─────────────


def save_config(plugin_id, values, enabled=None):
    repo_id, root, spec = find(plugin_id)
    known = {f.key for f in spec.setup.fields}
    manifest_keys = set(manifest_env(spec, root) or [])
    unknown = set(values or {}) - known - manifest_keys
    if unknown:
        raise SetupError(
            "Unknown settings",
            f"{spec.name} doesn't read: " + ", ".join(sorted(unknown)) + ".",
            "Only send fields from the plugin's setup form.",
        )
    problems = []
    for field in spec.setup.fields:
        value = (values or {}).get(field.key)
        if value in (None, "") or (isinstance(value, str) and value.startswith("••••")):
            continue
        value = str(value).strip()
        if field.kind == "number":
            try:
                float(value)
            except ValueError:
                problems.append({"where": field.key, "message": f"{field.label} must be a number"})
        if field.kind == "url" and not re.match(r"^[a-z][a-z0-9+.-]*://", value):
            problems.append({"where": field.key, "message": f"{field.label} must start with http:// or https://"})
        if field.kind == "select" and field.options and value not in field.options:
            problems.append({"where": field.key, "message": f"{field.label} must be one of: {', '.join(field.options)}"})
        if field.kind == "email" and "@" not in value:
            problems.append({"where": field.key, "message": f"{field.label} must be an email address"})
        if field.kind in ("path", "directory", "file") and not Path(value).expanduser().exists():
            problems.append({"where": field.key, "message": f"{value} doesn't exist on the Throughline server"})
    if problems:
        raise SetupError(
            "Some settings need fixing",
            f"{len(problems)} field(s) for {spec.name} aren't valid.",
            "Fix the highlighted fields and save again.",
            status=422,
            problems=problems,
        )
    cleaned = {k: (v.strip() if isinstance(v, str) else v) for k, v in (values or {}).items()}
    store.set_plugin_values(plugin_id, cleaned, secret_keys=spec.secret_keys() | _manifest_secrets(spec, root))
    if enabled is not None:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO tl.plugin_installs(plugin_id,repo_id,enabled,interval_s) VALUES (%s,%s,%s,%s)"
                " ON CONFLICT (plugin_id) DO UPDATE SET enabled=EXCLUDED.enabled,updated_at=now()",
                (plugin_id, repo_id, bool(enabled), spec.schedule.interval_s),
            )
    return one(plugin_id)


def manifest_env(spec, root):
    """Every env var the plugin's Manifest declares (installed plugins only)."""
    manifest = read_manifest(spec, root)
    if not manifest:
        return []
    out = []
    for group in ("settings", "secrets"):
        for declaration in (manifest.get(group) or {}).values():
            if isinstance(declaration, dict) and declaration.get("env"):
                out.append(declaration["env"])
    return out


def _manifest_secrets(spec, root):
    manifest = read_manifest(spec, root) or {}
    return {
        d.get("env")
        for d in (manifest.get("secrets") or {}).values()
        if isinstance(d, dict) and d.get("env")
    }


_manifest_cache: dict = {}


def read_manifest(spec, root):
    command = resolve_command(spec, root)
    if not command:
        return None
    key = (spec.id, tuple(command))
    if key in _manifest_cache:
        return _manifest_cache[key]
    try:
        result = subprocess.run(
            [*command, "manifest"], capture_output=True, text=True, timeout=30,
            env=plugin_base_env(), cwd=str(root / spec.path) if root else None,
        )
        import json

        manifest = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else None
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        manifest = None
    _manifest_cache[key] = manifest
    return manifest


SENSITIVE = re.compile(r"(DATABASE_URL|PASSWORD|SECRET|TOKEN|_KEY$|_API_KEY|DSN)", re.I)
KEEP = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "DISPLAY", "WAYLAND_DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS", "THROUGHLINE_TIMEZONE", "THROUGHLINE_OWNER_TZ", "UV_CACHE_DIR"}


def plugin_base_env():
    """The environment every plugin process starts from: only harmless
    variables of the server. Database URLs, role passwords and the server's
    own model keys never reach a plugin; it gets its own settings on top."""
    return {k: v for k, v in os.environ.items() if k in KEEP and not SENSITIVE.search(k)}


def ensure_token(plugin_id, spec):
    """The plugin's own scoped token (created once, stored as a secret)."""
    values = store.plugin_values(plugin_id)
    if values.get(TOKEN_KEY):
        return values[TOKEN_KEY]
    from .owner import owner_create_token

    token = owner_create_token(
        f"setup:{plugin_id}", kind="plugin", plugin=plugin_id, location_source=bool(spec.track_sources)
    )
    store.set_plugin_values(plugin_id, {TOKEN_KEY: token["token"]}, secret_keys={TOKEN_KEY})
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tl.plugin_installs(plugin_id,repo_id,token_id,interval_s) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (plugin_id) DO UPDATE SET token_id=EXCLUDED.token_id,updated_at=now()",
            (plugin_id, find(plugin_id)[0], token["id"], spec.schedule.interval_s),
        )
    return token["token"]


def record_install(plugin_id, repo_id, root, spec, command):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tl.plugin_installs(plugin_id,repo_id,path,command,interval_s,installed_at)"
            " VALUES (%s,%s,%s,%s,%s,now())"
            " ON CONFLICT (plugin_id) DO UPDATE SET repo_id=EXCLUDED.repo_id,path=EXCLUDED.path,"
            "command=EXCLUDED.command,installed_at=now(),updated_at=now()",
            (plugin_id, repo_id, str(root / spec.path) if root else None, Jsonb(command), spec.schedule.interval_s),
        )
    _manifest_cache.clear()


def now():
    return datetime.now(timezone.utc)
