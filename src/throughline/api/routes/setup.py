"""Onboarding over HTTP (throughline.onboarding, docs/onboarding.md).

Owner tokens only: every route here configures the install. Failures are
SetupErrors: `detail` is {title, message, hint, problems?}, so the web UI and
setup agents can say what went wrong and what to do next. The public
first-account pair lives in api/auth_routes.py (/v1/auth/setup).
"""

from __future__ import annotations

import functools
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ...onboarding import agent, catalog, jobs, people, providers, runs, state, store
from ...onboarding.contract import ContractError
from ...onboarding.errors import SetupError
from ...onboarding.owner import owner_ready, require_owner
from ..deps import Token, audit, require, require_unrestricted

router = APIRouter()

MAX_UPLOAD = 4 * 1024**3


def _owner(token):
    require_unrestricted(token)
    require(token, "owner")


def setup_route(function):
    """Owner check, SetupError → HTTP, and capture of the loopback API URL
    plugin processes use to reach this server."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        token = kwargs.get("token")
        _owner(token)
        request = kwargs.get("request")
        if request is not None:
            server = request.scope.get("server")
            if server and server[1]:
                jobs.set_api_url(f"http://127.0.0.1:{server[1]}")
        try:
            result = function(*args, **kwargs)
        except SetupError as error:
            raise error.http() from None
        except ContractError as error:
            first = (error.problems or [{}])[0]
            raise SetupError(str(error), first.get("message") or str(error), None, status=400,
                             problems=error.problems).http() from None
        audit(token, 1)
        return result

    return wrapper


def _body(data, key, default=None):
    return (data or {}).get(key, default) if isinstance(data, dict) else default


# ───────────── state ─────────────


@router.get("/v1/setup/state")
@setup_route
def setup_state(token: Token, request: Request):
    return state.get()


@router.put("/v1/setup/state")
@setup_route
def setup_state_put(token: Token, request: Request, data: dict | None = None):
    return state.save(data or {})


@router.post("/v1/setup/complete")
@setup_route
def setup_complete(token: Token, request: Request, data: dict | None = None):
    return state.complete()


@router.post("/v1/setup/reset")
@setup_route
def setup_reset(token: Token, request: Request, data: dict | None = None):
    return state.reset()


@router.get("/v1/setup/detect")
@setup_route
def setup_detect(token: Token, request: Request):
    return catalog.detect()


# ───────────── external sign-in (Authentik / OIDC) ─────────────


def _token_account_id(token: Token) -> int | None:
    acct = token.get("account") if isinstance(token, dict) else None
    return acct["id"] if isinstance(acct, dict) and acct.get("id") else None


@router.get("/v1/setup/auth")
@setup_route
def setup_auth_view(token: Token, request: Request):
    from ...onboarding import auth_setup

    return auth_setup.view(request, account_id=_token_account_id(token))


@router.put("/v1/setup/auth/provider")
@setup_route
def setup_auth_save_provider(token: Token, request: Request, data: dict | None = None):
    from ...onboarding import auth_setup

    verify = bool(_body(data, "verify", True))
    auth_setup.save_provider(data or {}, verify_discovery=verify)
    return auth_setup.view(request, account_id=_token_account_id(token))


@router.delete("/v1/setup/auth/provider/{provider_id}")
@setup_route
def setup_auth_remove_provider(provider_id: str, token: Token, request: Request):
    from ...onboarding import auth_setup

    auth_setup.remove_provider(provider_id)
    return auth_setup.view(request, account_id=_token_account_id(token))


@router.put("/v1/setup/auth/methods")
@setup_route
def setup_auth_methods(token: Token, request: Request, data: dict | None = None):
    from ...onboarding import auth_setup

    auth_setup.update_methods(
        password=_body(data, "password"),
        token=_body(data, "token"),
        public_url=_body(data, "public_url"),
    )
    return auth_setup.view(request, account_id=_token_account_id(token))


@router.post("/v1/setup/auth/account/email")
@setup_route
def setup_auth_account_email(token: Token, request: Request, data: dict | None = None):
    from ...onboarding import auth_setup

    account_id = _token_account_id(token)
    if not account_id:
        raise SetupError(
            "Sign in with a browser account first",
            "Updating an account's email requires signing in as that account (not a raw bearer token).",
            status=409,
        )
    auth_setup.update_account_email(account_id, _body(data, "email"))
    return auth_setup.view(request, account_id=account_id)


@router.post("/v1/setup/auth/oidc/{provider_id}/link")
@setup_route
def setup_auth_oidc_link(provider_id: str, token: Token, request: Request, data: dict | None = None):
    from ...auth import config as auth_config
    from ...auth import oidc, sessions

    account_id = _token_account_id(token)
    if not account_id:
        raise SetupError(
            "Sign in with a browser account first",
            "Linking an external identity requires signing in as that browser account.",
            status=409,
        )
    settings = auth_config.settings()
    provider = settings.providers.get(provider_id)
    if not provider or provider.protocol != "oidc":
        raise SetupError(
            "Unknown sign-in provider",
            f"No OpenID Connect provider named {provider_id!r} is configured.",
            status=404,
        )
    return_to = str(_body(data, "return_to") or f"/ui/welcome/account?linked={provider.id}")
    try:
        authorize_url, flow_state = oidc.begin(
            provider,
            request,
            settings,
            return_to,
            True,
            purpose="link_account",
            account_id=account_id,
        )
    except oidc.SignInError as err:
        raise SetupError(
            f"Couldn't reach {provider.label}",
            err.detail or "OpenID Connect discovery failed.",
            status=400,
        ) from None
    response = JSONResponse({"authorize_url": authorize_url, "provider": provider.public()})
    response.set_cookie(
        sessions.STATE_COOKIE,
        flow_state,
        max_age=oidc.FLOW_TTL_S,
        path="/v1/auth/",
        secure=sessions.secure(request, settings),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/v1/setup/restore")
@setup_route
def setup_restore_view(token: Token, request: Request):
    from ... import backups

    settings = backups.load()
    pub = backups.public(settings)
    ready, hint = owner_ready()
    return dict(
        backup_dir=settings["destination"]["dir"],
        passphrase_present=pub["encryption"]["passphrase_present"],
        encryption_kind=settings["encryption"]["kind"],
        owner_ready=ready,
        owner_hint=hint,
        scopes=list(backups.RESTORE_SCOPES),
        bundles=backups.list_bundles(settings),
        detected=catalog.detect(),
    )


@router.post("/v1/setup/restore")
@setup_route
def setup_restore_apply(token: Token, request: Request, data: dict | None = None):
    from ... import backups, ops

    bundle_raw = _body(data, "bundle")
    parts = _body(data, "parts")
    overwrite = bool(_body(data, "overwrite", False))
    clean = bool(_body(data, "clean", False))
    continue_onboarding = bool(_body(data, "continue_onboarding", True))

    if parts is not None and not isinstance(parts, list):
        raise SetupError("Bad request", "parts must be a list of restore scopes.", status=422)

    # Continuing onboarding with already-restored data needs no bundle.
    if not bundle_raw and (parts is None or len(parts) == 0):
        st = state.mark_restored(keep_steps=True) if continue_onboarding else state.get()
        return dict(
            ok=True,
            bundle=None,
            scopes=[],
            restored={},
            skipped=[],
            onboarding_open=st["status"] != "done",
            state=st,
            detected=catalog.detect(),
        )

    settings = backups.load()
    backup_root = Path(backups._expand(settings["destination"]["dir"])).resolve()
    if bundle_raw:
        candidate = Path(str(bundle_raw)).expanduser()
        if not candidate.is_absolute():
            if "/" in str(bundle_raw) or "\\" in str(bundle_raw) or ".." in candidate.parts:
                raise SetupError(
                    "Invalid bundle name",
                    "Pick a backup bundle from the list or give an absolute path on the server.",
                    status=422,
                )
            bundle_path = (backup_root / candidate.name).resolve()
            if not bundle_path.is_relative_to(backup_root):
                raise SetupError("Invalid bundle name", "Bundle must reside in the backup directory.", status=422)
        else:
            bundle_path = candidate.resolve()
    else:
        bundle_path = backups.latest(settings)
        if bundle_path is None:
            raise SetupError(
                "No backup bundle found",
                f"No backup bundles were found in {backup_root}.",
                "Copy a bundle to the server or enter its path, or continue with the data already here.",
                status=404,
            )

    requested = list(parts) if parts is not None else None
    if requested is None or "database" in requested:
        require_owner(
            f"throughline lib backup restore {bundle_path} --only database"
            + (" --clean" if clean else "")
            + (" --continue-onboarding" if continue_onboarding else " --no-continue-onboarding")
        )
    try:
        result = backups.restore_bundle(
            bundle_path,
            only=requested,
            settings=settings,
            merge=not overwrite,
            overwrite=overwrite,
            clean=clean,
            continue_onboarding=continue_onboarding,
        )
    except ops.OpsError as exc:
        raise SetupError("Could not restore backup", str(exc), status=400) from None
    return dict(result, state=state.get(), detected=catalog.detect())


# ───────────── providers ─────────────


@router.get("/v1/setup/providers")
@setup_route
def setup_providers(token: Token, request: Request):
    return providers.view()


@router.put("/v1/setup/providers/{provider_id}")
@setup_route
def setup_provider_put(provider_id: str, token: Token, request: Request, data: dict | None = None):
    values = _body(data, "values") or {}
    if not isinstance(values, dict):
        raise SetupError("Bad request", "values must be an object of settings.")
    return providers.save(provider_id, values)


@router.post("/v1/setup/providers/{provider_id}/test")
@setup_route
def setup_provider_test(provider_id: str, token: Token, request: Request, data: dict | None = None):
    return providers.test(provider_id, _body(data, "values"))


@router.put("/v1/setup/roles")
@setup_route
def setup_roles(token: Token, request: Request, data: dict | None = None):
    return providers.set_roles(data or {})


# ───────────── plugins ─────────────


@router.get("/v1/setup/catalog")
@setup_route
def setup_catalog(token: Token, request: Request):
    return catalog.catalog()


@router.post("/v1/setup/repos")
@setup_route
def setup_repo_add(token: Token, request: Request, data: dict | None = None):
    url = _body(data, "url")
    if not url:
        raise SetupError("Paste a GitHub link", "No repository link was given.", "For example https://github.com/owner/repo")
    repo_id = catalog.add_repo(url)
    view = catalog.catalog()
    return next(r for r in view["repos"] if r["id"] == repo_id)


@router.delete("/v1/setup/repos/{owner}/{repo}")
@setup_route
def setup_repo_remove(owner: str, repo: str, token: Token, request: Request):
    catalog.remove_repo(f"{owner}/{repo}".lower())
    return {"ok": True}


@router.post("/v1/setup/plugins/{plugin_id}/install")
@setup_route
def setup_plugin_install(plugin_id: str, token: Token, request: Request, data: dict | None = None):
    return jobs.get(runs.start_install(plugin_id))


@router.put("/v1/setup/plugins/{plugin_id}/config")
@setup_route
def setup_plugin_config(plugin_id: str, token: Token, request: Request, data: dict | None = None):
    values = _body(data, "values") or {}
    if not isinstance(values, dict):
        raise SetupError("Bad request", "values must be an object of settings.")
    return catalog.save_config(plugin_id, values, _body(data, "enabled"))


@router.post("/v1/setup/plugins/{plugin_id}/check")
@setup_route
def setup_plugin_check(plugin_id: str, token: Token, request: Request, data: dict | None = None):
    return runs.check(plugin_id, _body(data, "values"))


@router.post("/v1/setup/plugins/{plugin_id}/backfill")
@setup_route
def setup_plugin_backfill(plugin_id: str, token: Token, request: Request, data: dict | None = None):
    return jobs.get(runs.start_backfill(plugin_id))


@router.post("/v1/setup/uploads")
async def setup_upload(request: Request, token: Token, plugin: str = "", field: str = ""):
    """Raw-body upload of an export file (Google Timeline's Timeline.json,
    a Takeout file). Stored under the setup data directory, 0600."""
    _owner(token)
    try:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", plugin or ""):
            raise SetupError("Which source is this for?", "The upload needs ?plugin=<id>.", status=422)
        from urllib.parse import unquote

        name = unquote(request.headers.get("x-filename") or "upload")
        name = re.sub(r"[^\w.\-]+", "_", Path(name).name)[:120] or "upload"
        length = int(request.headers.get("content-length") or 0)
        if length > MAX_UPLOAD:
            raise SetupError("That file is too large", f"Uploads are limited to {MAX_UPLOAD // 1024**3} GB.",
                             "Copy it to the server and enter its path instead.", status=413)
        directory = store.data_dir() / "uploads" / plugin
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{secrets.token_hex(4)}-{name}"
        size = 0
        import os

        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    f.close()
                    path.unlink(missing_ok=True)
                    raise SetupError("That file is too large", "The upload went over the size limit.", status=413)
                f.write(chunk)
        if size == 0:
            path.unlink(missing_ok=True)
            raise SetupError("The file was empty", "Nothing was received.", "Pick the file again.", status=422)
        if name.lower().endswith(".json"):
            with open(path, "rb") as f:
                head = f.read(64).lstrip()
            if not head.startswith((b"{", b"[")):
                path.unlink(missing_ok=True)
                raise SetupError("That isn't a JSON export", f"{name} doesn't look like JSON.",
                                 "Pick the Timeline.json your phone exported (Settings → Location → Timeline → Export).", status=422)
    except SetupError as error:
        raise error.http() from None
    audit(token, 1)
    return dict(path=str(path), name=name, size=size, field=field)


# ───────────── jobs ─────────────


@router.get("/v1/setup/jobs")
@setup_route
def setup_jobs(token: Token, request: Request):
    return jobs.listing()


@router.get("/v1/setup/jobs/{job_id}")
@setup_route
def setup_job(job_id: int, token: Token, request: Request):
    return jobs.get(job_id)


@router.post("/v1/setup/jobs/{job_id}/cancel")
@setup_route
def setup_job_cancel(job_id: int, token: Token, request: Request, data: dict | None = None):
    jobs.get(job_id)
    return jobs.cancel(job_id)


@router.post("/v1/setup/jobs/{job_id}/retry")
@setup_route
def setup_job_retry(job_id: int, token: Token, request: Request, data: dict | None = None):
    return jobs.get(runs.retry(job_id))


# ───────────── people ─────────────


@router.get("/v1/setup/people")
@setup_route
def setup_people(token: Token, request: Request):
    return people.view(token["id"])


@router.post("/v1/setup/people/resolve")
@setup_route
def setup_people_resolve(token: Token, request: Request, data: dict | None = None):
    return jobs.get(people.start(token["id"]))


@router.post("/v1/setup/people/me")
@setup_route
def setup_people_me(token: Token, request: Request, data: dict | None = None):
    return people.set_me(token["id"], _body(data, "person_id"), _body(data, "identifier_ids"), _body(data, "display_name"))


@router.post("/v1/setup/people/questions/{question_id}")
@setup_route
def setup_people_answer(question_id: int, token: Token, request: Request, data: dict | None = None):
    return people.answer(token["id"], question_id, _body(data, "answer"), _body(data, "name"))


# ───────────── lenses, pages, first queries ─────────────


@router.get("/v1/setup/lenses")
@setup_route
def setup_lenses(token: Token, request: Request):
    from ...onboarding import starter

    return starter.lenses_view()


@router.put("/v1/setup/lenses")
@setup_route
def setup_lenses_put(token: Token, request: Request, data: dict | None = None):
    from ...onboarding import starter

    chosen = _body(data, "templates") or []
    if not isinstance(chosen, list):
        raise SetupError("Bad request", "templates must be a list of starter lens ids.")
    return starter.apply_lenses([str(t) for t in chosen], _body(data, "pace"), token=token)


@router.post("/v1/setup/lenses/{name}/run")
@setup_route
def setup_lens_run(name: str, token: Token, request: Request, data: dict | None = None):
    from ...onboarding import starter

    if not starter.model_status()["ready"]:
        raise SetupError("Choose a model first", starter.model_status()["hint"], status=409)
    pace = state.get()["choices"].get("pace") or starter.DEFAULT_PACE
    job_id = jobs.create("lens", f"Running the {name.removeprefix('starter_').replace('_', ' ')} lens", name)
    return jobs.get(jobs.submit(job_id, starter.run_lens_job, name, pace))


@router.get("/v1/setup/pages")
@setup_route
def setup_pages(token: Token, request: Request):
    from ...onboarding import starter

    return starter.pages_view()


@router.put("/v1/setup/pages")
@setup_route
def setup_pages_put(token: Token, request: Request, data: dict | None = None):
    from ...onboarding import starter

    chosen = _body(data, "families") or []
    if not isinstance(chosen, list):
        raise SetupError("Bad request", "families must be a list of page family ids.")
    return starter.apply_pages([str(f) for f in chosen], _body(data, "pace"), _body(data, "markdown_dir"))


@router.post("/v1/setup/pages/{family}/run")
@setup_route
def setup_pages_run(family: str, token: Token, request: Request, data: dict | None = None):
    from ...onboarding import starter

    if not starter.model_status()["ready"]:
        raise SetupError("Choose a model first", starter.model_status()["hint"], status=409)
    job_id = jobs.create("pages", f"Writing your first {family} pages", family)
    return jobs.get(jobs.submit(job_id, starter.run_pages_job, family))


@router.get("/v1/setup/queries")
@setup_route
def setup_queries(token: Token, request: Request):
    from ...onboarding import starter

    return starter.queries()


# ───────────── companion ─────────────

COMPANION_RELEASES = "https://github.com/joey12725/throughline/releases/latest/download"


def _downloads():
    import os

    base = (os.getenv("THROUGHLINE_COMPANION_DOWNLOADS") or COMPANION_RELEASES).rstrip("/")
    return [
        dict(os="macos", label="macOS (Apple silicon)", url=f"{base}/throughline-companion-macos-arm64.zip"),
        dict(os="macos", label="macOS (Intel)", url=f"{base}/throughline-companion-macos-x86_64.zip"),
        dict(os="windows", label="Windows", url=f"{base}/throughline-companion-windows-x86_64.zip"),
        dict(os="linux", label="Linux", url=f"{base}/throughline-companion-linux-x86_64.tar.gz",
             note="Or install with the command below."),
    ]


@router.get("/v1/setup/companion")
@setup_route
def setup_companion(token: Token, request: Request):
    import os

    require_unrestricted(token)
    from ...companion import devices
    from ...db import connect

    with connect("app") as conn:
        rows = devices(conn)
    ready, _ = owner_ready()
    return dict(
        devices=[dict(name=r["name"], last_seen_at=r["last_seen_at"], online=r["online"],
                      plugins=[p.get("name") for p in (r["plugins"] or []) if isinstance(p, dict)],
                      platform=r["platform"]) for r in rows if r["revoked_at"] is None],
        downloads=_downloads(),
        install_command=os.getenv("THROUGHLINE_COMPANION_INSTALL")
        or "uv tool install throughline-companion && throughline-companion",
        owner_ready=ready,
    )


@router.post("/v1/setup/companion/pair")
@setup_route
def setup_companion_pair(token: Token, request: Request, data: dict | None = None):
    from ... import companion

    name = re.sub(r"[^a-z0-9_]+", "_", str(_body(data, "name") or "laptop").lower()).strip("_") or "laptop"
    if not name[0].isalpha():
        name = "d_" + name
    name = name[:40]
    st = state.get()
    chosen = set(st["choices"].get("sources") or [])
    desktop = []
    location = []
    for plugin in catalog.catalog()["plugins"]:
        if plugin["id"] in chosen and "companion" in plugin["requires"]:
            desktop.append(plugin["id"])
            if plugin.get("track_sources"):
                location.append(plugin["id"])
    url = store.get_env("THROUGHLINE_PUBLIC_URL") or str(request.base_url).rstrip("/")
    require_owner(f"throughline lib companion enroll {name} --url {url}" + "".join(f" --plugin {p}" for p in desktop))
    try:
        enrolled = companion.enroll(name, url, desktop, location_plugins=location)
    except ValueError as error:
        raise SetupError("Couldn't pair that device", str(error).capitalize() + ".",
                         "Pick another name, or revoke the old device in Settings.", status=409) from None
    return dict(name=name, code=enrolled["pairing"], plugins=desktop, expires_at=None,
                command=f"throughline-companion pair {enrolled['pairing']}")


# ───────────── agents ─────────────


def _base(request):
    return store.get_env("THROUGHLINE_PUBLIC_URL") or str(request.base_url).rstrip("/")


@router.get("/v1/setup/agent")
@setup_route
def setup_agent(token: Token, request: Request):
    from ...db import connect

    require_unrestricted(token)
    base = _base(request)
    with connect("app") as conn:
        rows = conn.execute(
            "SELECT t.id,t.name,(SELECT min(a.at) FROM tl.audit_log a WHERE a.token_id=t.id) AS created_at,"
            "(SELECT max(a.at) FROM tl.audit_log a WHERE a.token_id=t.id) AS last_used_at"
            " FROM tl.tokens t WHERE t.kind='agent' AND t.revoked_at IS NULL ORDER BY t.id DESC LIMIT 20"
        ).fetchall()
    return dict(mcp_url=base + "/v1/mcp", api_url=base, skill_url=base + "/v1/setup/skill",
                clients=agent.clients(base), tokens=[dict(r) for r in rows])


@router.post("/v1/setup/agent/token")
@setup_route
def setup_agent_token(token: Token, request: Request, data: dict | None = None):
    from ...onboarding.owner import owner_create_token

    name = re.sub(r"[^\w .:-]+", "", str(_body(data, "name") or "agent")).strip()[:60] or "agent"
    client = str(_body(data, "client") or "other")
    minted = owner_create_token(f"agent:{name}", kind="agent")
    base = _base(request)
    return dict(id=minted["id"], name=name, token=minted["token"],
                clients=[c for c in agent.clients(base, minted["token"]) if client in (c["id"], "all")] or agent.clients(base, minted["token"]))


@router.get("/v1/setup/prompt")
@setup_route
def setup_prompt(token: Token, request: Request):
    return dict(markdown=agent.setup_prompt(_base(request)))


@router.get("/v1/setup/skill")
def setup_skill(token: Token):
    require_unrestricted(token)
    require(token, "owner", "agent")
    here = Path(__file__).resolve()
    for parent in here.parents:
        skill = parent / "skill" / "throughline" / "SKILL.md"
        if skill.exists():
            audit(token, 1)
            return JSONResponse({"markdown": skill.read_text(encoding="utf-8")})
    raise SetupError("Skill file not found", "SKILL.md isn't bundled with this install.", status=404).http()


# ───────────── MCP ─────────────


@router.post("/v1/mcp")
async def mcp(request: Request, token: Token):
    """Model Context Protocol (Streamable HTTP, JSON responses)."""
    require(token, "owner", "agent")
    server = request.scope.get("server")
    if server and server[1]:
        jobs.set_api_url(f"http://127.0.0.1:{server[1]}")
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse(agent._error(None, -32700, "Parse error"), status_code=400)
    bearer = (request.headers.get("authorization") or "").partition(" ")[2]
    asker = request.headers.get("x-throughline-asker")
    import asyncio

    if isinstance(message, list):
        answers = [a for a in [await asyncio.to_thread(agent.handle, m, bearer, asker) for m in message] if a]
        audit(token, len(message))
        return JSONResponse(answers) if answers else JSONResponse(None, status_code=202)
    answer = await asyncio.to_thread(agent.handle, message, bearer, asker)
    audit(token, 1)
    if answer is None:
        return JSONResponse(None, status_code=202)
    return JSONResponse(answer, headers={"Mcp-Session-Id": request.headers.get("mcp-session-id") or secrets.token_hex(8)})


@router.get("/v1/mcp")
def mcp_get(token: Token):
    """No server-initiated messages: the GET channel is not offered."""
    require(token, "owner", "agent")
    return JSONResponse({"detail": "this MCP server answers POST only"}, status_code=405, headers={"Allow": "POST"})
