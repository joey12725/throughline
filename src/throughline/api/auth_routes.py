"""Sign-in routes under /v1/auth (docs/web/auth.md).

They are public by nature (nobody is signed in yet), so they live outside
api/routes/, whose routers all depend on authentication. They hold no data:
the configuration screen, a password check, the redirects of an external
provider, and the caller's own session. Everything they touch goes through
throughline.auth.
"""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .. import auth
from ..auth import config, oidc, sessions
from .deps import same_origin_request

log = logging.getLogger("throughline.auth")

router = APIRouter(include_in_schema=False)

NO_STORE = {"Cache-Control": "no-store"}


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    remember: bool = False


def _session_body(token):
    if not token:
        return {"account": None}
    session = dict(token["session"])
    return {
        "account": token["account"],
        "session": {
            "method": session["method"],
            "persistent": session["persistent"],
            "created_at": session["created_at"].isoformat(),
            "expires_at": session["expires_at"].isoformat(),
        },
        # What the account may do is its principal's; the interface reads the
        # details from /v1/web/bootstrap like any token.
        "principal": {"kind": token["kind"], "restricted": bool(token["askers"])},
    }


def _provider(provider_id):
    provider = config.settings().providers.get(provider_id)
    if not provider or provider.protocol != "oidc":
        raise HTTPException(404, "unknown sign-in provider")
    return provider


def _failed(provider_id, error):
    if error.detail:
        log.warning("sign-in with %s failed (%s): %s", provider_id, error.code, error.detail)
    fragment = {"auth_error": error.code, "provider": provider_id}
    if error.subject:
        fragment["subject"] = error.subject
    base = oidc.safe_return(error.return_to) if getattr(error, "return_to", None) else "/ui/"
    response = RedirectResponse(base + "#" + urlencode(fragment), status_code=303)
    return response


@router.get("/v1/auth/config")
def auth_config():
    return JSONResponse(auth.describe(), headers=NO_STORE)


class FirstAccount(BaseModel):
    code: str = Field(default="", max_length=64)
    handle: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    remember: bool = True


class FirstAccountOidc(BaseModel):
    code: str = Field(default="", max_length=64)
    provider_id: str = Field(default="authentik", max_length=40)
    kind: str | None = Field(default=None, max_length=20)
    label: str | None = Field(default=None, max_length=80)
    issuer: str | None = Field(default=None, max_length=500)
    client_id: str | None = Field(default=None, max_length=300)
    client_secret: str | None = Field(default=None, max_length=1024)
    allowed_groups: list[str] | str | None = None
    link_by_email: bool = True
    handle: str | None = Field(default=None, max_length=64)
    remember: bool = True


@router.get("/v1/auth/setup")
def auth_setup(request: Request):
    """Public: does this install still need its first account?"""
    from ..onboarding import auth_setup as onboarding_auth
    from ..onboarding import owner, store

    try:
        needs = not owner.accounts_exist()
    except Exception:
        needs = False
    settings = config.settings()
    ready, _hint = owner.owner_ready()
    return JSONResponse(
        {
            "needs_account": needs,
            "code_required": needs,
            "code_hint": "It was printed by `throughline serve` when it started, and is saved in "
            f"{owner._code_file()} (or run `throughline lib setup code`).",
            "instance_name": "Throughline",
            "providers": [p.public() for p in settings.providers.values()],
            "redirect_uri": onboarding_auth.public_callback_url(request, "authentik"),
            "can_configure_oidc": not store.locked("THROUGHLINE_AUTH_PROVIDERS"),
            "owner_ready": ready,
        },
        headers=NO_STORE,
    )


@router.post("/v1/auth/setup")
def auth_setup_create(data: FirstAccount, request: Request):
    """Public, once: create the first owner account with the setup code and
    sign it in."""
    same_origin_request(request, always=True)
    from ..onboarding import owner
    from ..onboarding.errors import SetupError

    try:
        account = owner.create_first_account(
            data.code, data.handle, data.display_name, data.password, email=data.email
        )
    except SetupError as error:
        return JSONResponse({"detail": error.body()}, status_code=error.status, headers=NO_STORE)
    settings = config.settings()
    from ..db import connect

    with connect() as conn:
        secret = sessions.start(conn, account["id"], "password", data.remember,
                                request.headers.get("user-agent"), settings)
    response = JSONResponse(_session_body(sessions.resolve(secret)), status_code=201, headers=NO_STORE)
    sessions.set_cookie(response, request, secret, data.remember, settings)
    return response


# Public and once only: it creates the first account (guarded by the setup
# code) and opens the caller's own browser session; afterwards it refuses.
auth_setup_create.own_working_state = True


@router.post("/v1/auth/setup/oidc")
def auth_setup_oidc(data: FirstAccountOidc, request: Request):
    """Public, once: configure (if supplied) and start an OIDC/Authentik sign-in
    flow that creates and links the first owner account on callback."""
    same_origin_request(request, always=True)
    from ..onboarding import auth_setup as onboarding_auth
    from ..onboarding import owner
    from ..onboarding.errors import SetupError

    try:
        if owner.accounts_exist():
            raise SetupError(
                "An account already exists",
                "Throughline already has an owner account.",
                "Sign in with it instead.",
                status=409,
            )
        owner.check_code(data.code)
        owner.require_owner("throughline lib account create <handle> --email <your-email>")
        pid = (data.provider_id or "authentik").strip().lower()
        if data.issuer or data.client_id:
            provider = onboarding_auth.save_provider(
                {
                    "id": pid,
                    "kind": data.kind,
                    "label": data.label,
                    "issuer": data.issuer,
                    "client_id": data.client_id,
                    "client_secret": data.client_secret,
                    "allowed_groups": data.allowed_groups,
                    "link_by_email": data.link_by_email,
                },
                verify_discovery=True,
            )
        else:
            provider = config.settings().providers.get(pid)
            if not provider or provider.protocol != "oidc":
                raise SetupError(
                    "Sign-in provider isn't configured yet",
                    f"No OpenID Connect provider named {pid!r} is configured.",
                    "Enter your Authentik Issuer URL and Client ID first.",
                    status=404,
                )
        settings = config.settings()
        try:
            authorize_url, state = oidc.begin(
                provider,
                request,
                settings,
                "/ui/welcome",
                data.remember,
                purpose="claim_owner",
                handle_hint=data.handle,
            )
        except oidc.SignInError as err:
            raise SetupError(
                f"Couldn't start sign-in with {provider.label}",
                err.detail or "OpenID Connect discovery failed.",
                "Verify the Issuer URL is reachable from the Throughline server.",
                status=400,
            ) from None
    except SetupError as error:
        return JSONResponse({"detail": error.body()}, status_code=error.status, headers=NO_STORE)

    response = JSONResponse(
        {"authorize_url": authorize_url, "provider": provider.public()},
        headers=NO_STORE,
    )
    response.set_cookie(
        sessions.STATE_COOKIE,
        state,
        max_age=oidc.FLOW_TTL_S,
        path="/v1/auth/",
        secure=sessions.secure(request, settings),
        httponly=True,
        samesite="lax",
    )
    return response


auth_setup_oidc.own_working_state = True


@router.get("/v1/auth/session")
def auth_session(request: Request):
    same_origin_request(request)
    token = sessions.resolve(sessions.cookie_secret(request))
    return JSONResponse(_session_body(token), headers=NO_STORE)


@router.post("/v1/auth/login")
def auth_login(data: Login, request: Request):
    same_origin_request(request, always=True)
    settings = config.settings()
    if not settings.password:
        raise HTTPException(404, "password sign-in is not enabled")
    address = request.client.host if request.client else "unknown"
    try:
        _row, secret = sessions.password_login(
            data.username,
            data.password,
            data.remember,
            request.headers.get("user-agent"),
            address,
            settings,
        )
    except PermissionError as exc:
        reason = str(exc)
        if reason.startswith("throttled:"):
            wait = reason.split(":", 1)[1]
            return JSONResponse(
                {"detail": "too many attempts; try again later", "retry_after_s": int(wait)},
                status_code=429,
                headers={"Retry-After": wait, **NO_STORE},
            )
        return JSONResponse(
            {"detail": "incorrect username or password"}, status_code=401, headers=NO_STORE
        )
    response = JSONResponse(_session_body(sessions.resolve(secret)), headers=NO_STORE)
    sessions.set_cookie(response, request, secret, data.remember, settings)
    return response


@router.post("/v1/auth/logout")
def auth_logout(request: Request):
    same_origin_request(request, always=True)
    sessions.end(sessions.cookie_secret(request))
    response = JSONResponse({"signed_out": True}, headers=NO_STORE)
    sessions.clear_cookie(response, request)
    return response


@router.get("/v1/auth/oidc/{provider_id}/start")
def oidc_start(provider_id: str, request: Request, return_to: str = "/ui/", remember: bool = False):
    provider = _provider(provider_id)
    settings = config.settings()
    try:
        url, state = oidc.begin(provider, request, settings, return_to, remember)
    except oidc.SignInError as error:
        return _failed(provider_id, error)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        sessions.STATE_COOKIE,
        state,
        max_age=oidc.FLOW_TTL_S,
        path="/v1/auth/",
        secure=sessions.secure(request, settings),
        httponly=True,
        samesite="lax",
    )
    response.headers.update(NO_STORE)
    return response


@router.get("/v1/auth/oidc/{provider_id}/return")
def oidc_callback(
    provider_id: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    provider = _provider(provider_id)
    settings = config.settings()
    try:
        if error:
            # The provider refused or the person cancelled; its own message is
            # not shown (it is the provider's text, not ours).
            raise oidc.SignInError("denied", f"provider answered error={error[:100]}")
        _account, secret, return_to, persistent = oidc.finish(
            provider,
            request,
            settings,
            code,
            state,
            request.cookies.get(sessions.STATE_COOKIE),
            request.headers.get("user-agent"),
        )
    except oidc.SignInError as failure:
        response = _failed(provider_id, failure)
    else:
        response = RedirectResponse(return_to, status_code=303)
        sessions.set_cookie(response, request, secret, persistent, settings)
    response.delete_cookie(sessions.STATE_COOKIE, path="/v1/auth/")
    response.headers.update(NO_STORE)
    return response
