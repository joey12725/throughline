"""Secret reference resolution for Throughline core (Infisical).

Any credential environment variable read by Throughline core (model/embedding
API keys, database passwords, CLI tokens, lens/classifier LLM keys) may be set
to an Infisical reference instead of the raw secret:

    OPENROUTER_API_KEY=infisical:OPENROUTER_API_KEY
    VOYAGE_API_KEY=infisical:/throughline/VOYAGE_API_KEY
    THROUGHLINE_APP_PASSWORD=infisical:THROUGHLINE_APP_PASSWORD?env=prod

When no setting uses an `infisical:` reference, this module makes zero network
calls. Resolved secrets are kept only in memory (with an optional short TTL
cache controlled by INFISICAL_CACHE_TTL_S, default 300 s) and are never written
to disk or echoed in errors.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

PREFIX = "infisical:"
DEFAULT_SITE_URL = "https://app.infisical.com"
DEFAULT_ENVIRONMENT = "prod"
DEFAULT_SECRET_PATH = "/"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_CACHE_TTL_S = 300.0

_ALLOWED_REF_KEYS = frozenset(
    {
        "infisical",
        "path",
        "secret_path",
        "environment",
        "env",
        "project",
        "project_id",
        "workspace_id",
        "project_slug",
        "workspace_slug",
        "as",
    }
)


class InfisicalError(RuntimeError):
    """Raised when an Infisical request or secret reference fails."""


@dataclass(frozen=True)
class SecretRef:
    name: str
    secret_path: str | None = None
    environment: str | None = None
    project_id: str | None = None
    project_slug: str | None = None
    mode: str = "value"

    def label(self) -> str:
        if self.secret_path and self.secret_path != "/":
            return f"{PREFIX}{self.secret_path.rstrip('/')}/{self.name}"
        return f"{PREFIX}{self.name}"


def normalize_secret_path(path: str | None, *, default: str | None = None) -> str | None:
    if path is None:
        return default
    cleaned = str(path).strip()
    if not cleaned:
        return default
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    if len(cleaned) > 1:
        cleaned = cleaned.rstrip("/")
    return cleaned


def normalize_site_url(url: str | None) -> str:
    raw = (url or "").strip() or DEFAULT_SITE_URL
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InfisicalError("Infisical site_url must be a valid http or https URL")
    if parts.username or parts.password:
        raise InfisicalError("Infisical site_url must not contain embedded credentials")
    cleaned = raw.rstrip("/")
    if cleaned.endswith("/api"):
        cleaned = cleaned[:-4]
    return cleaned


def is_ref(value: Any) -> bool:
    if isinstance(value, SecretRef):
        return True
    if isinstance(value, str):
        return value.startswith(PREFIX)
    return isinstance(value, dict) and "infisical" in value


def _validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Infisical reference is missing a secret name")
    if any(ch.isspace() or ord(ch) < 32 for ch in cleaned) or "/" in cleaned:
        raise ValueError(f"invalid Infisical secret name: {cleaned!r}")
    return cleaned


def _parse_target(target: str) -> tuple[str, str | None]:
    target = (target or "").strip()
    if not target:
        raise ValueError("Infisical reference is missing a secret name")
    if "/" in target:
        folder, _, name = target.rpartition("/")
        secret_path = normalize_secret_path(folder or "/", default="/")
        return _validate_name(name), secret_path
    return _validate_name(target), None


def parse_ref(value: Any) -> SecretRef:
    if isinstance(value, SecretRef):
        return value
    if isinstance(value, str):
        if not value.startswith(PREFIX):
            raise ValueError("not an Infisical reference")
        body = value[len(PREFIX) :]
        target, sep, query = body.partition("?")
        name, secret_path = _parse_target(target)
        params = parse_qs(query, keep_blank_values=False) if sep else {}

        def first(*keys: str) -> str | None:
            for k in keys:
                vals = params.get(k)
                if vals and vals[0].strip():
                    return vals[0].strip()
            return None

        explicit_path = first("path", "secret_path")
        if explicit_path is not None:
            secret_path = normalize_secret_path(explicit_path, default="/")
        mode = (first("as") or "value").lower()
        if mode not in ("value", "file"):
            raise ValueError(f"Infisical reference mode must be 'value' or 'file', got {mode!r}")
        return SecretRef(
            name=name,
            secret_path=secret_path,
            environment=first("env", "environment"),
            project_id=first("project_id", "workspace_id", "project"),
            project_slug=first("project_slug", "workspace_slug"),
            mode=mode,
        )

    if isinstance(value, dict) and "infisical" in value:
        unknown = set(value) - _ALLOWED_REF_KEYS
        if unknown:
            raise ValueError("unknown Infisical reference fields: " + ", ".join(sorted(unknown)))
        raw = value.get("infisical")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("infisical field must be a non-empty secret name")
        base = parse_ref(raw if raw.startswith(PREFIX) else PREFIX + raw)

        def pick(*keys: str) -> str | None:
            for k in keys:
                v = value.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
            return None

        explicit_path = pick("path", "secret_path")
        secret_path = (
            normalize_secret_path(explicit_path, default="/")
            if explicit_path is not None
            else base.secret_path
        )
        mode = (pick("as") or base.mode or "value").lower()
        if mode not in ("value", "file"):
            raise ValueError(f"Infisical reference mode must be 'value' or 'file', got {mode!r}")
        return SecretRef(
            name=base.name,
            secret_path=secret_path,
            environment=pick("environment", "env") or base.environment,
            project_id=pick("project_id", "workspace_id", "project") or base.project_id,
            project_slug=pick("project_slug", "workspace_slug") or base.project_slug,
            mode=mode,
        )

    raise ValueError("expected an 'infisical:...' string or {'infisical': ...} object")


def format_ref(name: str, secret_path: str | None = None) -> str:
    clean_name = _validate_name(name)
    norm_path = normalize_secret_path(secret_path)
    if norm_path and norm_path != "/":
        return f"{PREFIX}{norm_path}/{clean_name}"
    return f"{PREFIX}{clean_name}"


def _first_env(env: dict[str, str], *names: str) -> str:
    for name in names:
        val = (env.get(name) or "").strip()
        if val:
            return val
    return ""


@dataclass(frozen=True)
class InfisicalConfig:
    site_url: str = DEFAULT_SITE_URL
    client_id: str = ""
    client_secret: str = ""
    token: str = ""
    project_id: str = ""
    project_slug: str = ""
    environment: str = DEFAULT_ENVIRONMENT
    secret_path: str = DEFAULT_SECRET_PATH
    timeout_s: float = DEFAULT_TIMEOUT_S
    cache_ttl_s: float = DEFAULT_CACHE_TTL_S

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> InfisicalConfig:
        environ = dict(os.environ) if env is None else env
        site_url = _first_env(environ, "INFISICAL_SITE_URL", "INFISICAL_API_URL") or DEFAULT_SITE_URL
        client_id = _first_env(environ, "INFISICAL_CLIENT_ID", "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID")
        client_secret = _first_env(
            environ, "INFISICAL_CLIENT_SECRET", "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"
        )
        token = _first_env(environ, "INFISICAL_TOKEN")
        project_id = _first_env(environ, "INFISICAL_PROJECT_ID", "INFISICAL_WORKSPACE_ID")
        project_slug = _first_env(environ, "INFISICAL_PROJECT_SLUG", "INFISICAL_WORKSPACE_SLUG")
        environment = _first_env(environ, "INFISICAL_ENVIRONMENT") or DEFAULT_ENVIRONMENT
        secret_path = (
            normalize_secret_path(_first_env(environ, "INFISICAL_SECRET_PATH"), default=DEFAULT_SECRET_PATH)
            or DEFAULT_SECRET_PATH
        )
        raw_timeout = _first_env(environ, "INFISICAL_TIMEOUT_S")
        try:
            timeout_s = max(1.0, float(raw_timeout)) if raw_timeout else DEFAULT_TIMEOUT_S
        except ValueError:
            timeout_s = DEFAULT_TIMEOUT_S
        raw_ttl = _first_env(environ, "INFISICAL_CACHE_TTL_S")
        try:
            cache_ttl_s = max(0.0, float(raw_ttl)) if raw_ttl != "" else DEFAULT_CACHE_TTL_S
        except ValueError:
            cache_ttl_s = DEFAULT_CACHE_TTL_S

        return cls(
            site_url=normalize_site_url(site_url),
            client_id=client_id,
            client_secret=client_secret,
            token=token,
            project_id=project_id,
            project_slug=project_slug,
            environment=environment,
            secret_path=secret_path,
            timeout_s=timeout_s,
            cache_ttl_s=cache_ttl_s,
        )

    @property
    def auth_mode(self) -> str:
        if self.client_id and self.client_secret:
            return "universal_auth"
        if self.token:
            return "token"
        return "none"

    @property
    def configured(self) -> bool:
        return self.auth_mode != "none"

    def cache_key(self) -> tuple[Any, ...]:
        return (
            self.site_url,
            self.client_id,
            self.client_secret,
            self.token,
            self.project_id,
            self.project_slug,
            self.environment,
            self.secret_path,
            self.timeout_s,
            self.cache_ttl_s,
        )


class InfisicalClient:
    """Thread-safe HTTP client for Infisical's REST API."""

    def __init__(self, config: InfisicalConfig, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._transport = transport
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._secret_cache: dict[tuple[str, str, str, str, str], tuple[str, float]] = {}

    def _http(self) -> httpx.Client:
        kwargs: dict[str, Any] = dict(
            timeout=self.config.timeout_s,
            follow_redirects=False,
        )
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def clear_cache(self) -> None:
        with self._lock:
            self._secret_cache.clear()
            self._access_token = None
            self._token_expires_at = 0.0

    def _authenticate(self, *, force: bool = False) -> str:
        mode = self.config.auth_mode
        if mode == "token":
            return self.config.token
        if mode != "universal_auth":
            raise InfisicalError(
                "Infisical is not configured: set INFISICAL_CLIENT_ID and INFISICAL_CLIENT_SECRET "
                "(Universal Auth) or INFISICAL_TOKEN"
            )
        with self._lock:
            if not force and self._access_token and time.monotonic() < self._token_expires_at:
                return self._access_token
            url = self.config.site_url + "/api/v1/auth/universal-auth/login"
            try:
                with self._http() as client:
                    response = client.post(
                        url,
                        json={
                            "clientId": self.config.client_id,
                            "clientSecret": self.config.client_secret,
                        },
                    )
            except httpx.HTTPError as exc:
                raise InfisicalError(f"Infisical authentication failed: {type(exc).__name__}") from None
            if response.status_code != 200:
                raise InfisicalError(
                    f"Infisical Universal Auth failed (HTTP {response.status_code})"
                )
            try:
                data = response.json()
            except ValueError:
                raise InfisicalError("Infisical Universal Auth returned invalid JSON") from None
            token = data.get("accessToken")
            if not isinstance(token, str) or not token:
                raise InfisicalError("Infisical Universal Auth returned no accessToken")
            raw_ttl = data.get("expiresIn")
            ttl = float(raw_ttl) if isinstance(raw_ttl, (int, float)) and raw_ttl > 0 else 7200.0
            self._access_token = token
            self._token_expires_at = time.monotonic() + max(30.0, ttl - 60.0)
            return token

    def _request_json(self, method: str, path: str, *, params: dict[str, str], what: str) -> dict[str, Any]:
        url = self.config.site_url + path
        for attempt in range(2):
            token = self._authenticate(force=(attempt > 0))
            try:
                with self._http() as client:
                    response = client.request(
                        method,
                        url,
                        params=params,
                        headers={"Authorization": "Bearer " + token},
                    )
            except httpx.HTTPError as exc:
                raise InfisicalError(f"Infisical request failed ({what}): {type(exc).__name__}") from None
            if response.status_code == 401 and attempt == 0 and self.config.auth_mode == "universal_auth":
                with self._lock:
                    self._access_token = None
                    self._token_expires_at = 0.0
                continue
            if response.status_code == 404:
                raise InfisicalError(f"Infisical {what} not found (HTTP 404)")
            if response.status_code in (401, 403):
                raise InfisicalError(
                    f"Infisical denied access to {what} (HTTP {response.status_code})"
                )
            if response.status_code != 200:
                raise InfisicalError(
                    f"Infisical failed for {what} (HTTP {response.status_code})"
                )
            try:
                data = response.json()
            except ValueError:
                raise InfisicalError(f"Infisical returned invalid JSON for {what}") from None
            if not isinstance(data, dict):
                raise InfisicalError(f"Infisical returned unexpected response for {what}")
            return data
        raise InfisicalError(f"Infisical authentication failed for {what}")

    def _scope_params(
        self,
        *,
        project_id: str | None = None,
        project_slug: str | None = None,
        environment: str | None = None,
        secret_path: str | None = None,
    ) -> tuple[dict[str, str], tuple[str, str, str, str]]:
        pid = (project_id or "").strip() or self.config.project_id
        pslug = (project_slug or "").strip() or self.config.project_slug
        if not pid and not pslug:
            raise InfisicalError(
                "Infisical project is not configured: set INFISICAL_PROJECT_ID "
                "(or INFISICAL_PROJECT_SLUG)"
            )
        env_slug = (environment or "").strip() or self.config.environment or DEFAULT_ENVIRONMENT
        path = normalize_secret_path(secret_path, default=self.config.secret_path) or DEFAULT_SECRET_PATH
        params: dict[str, str] = {
            "environment": env_slug,
            "secretPath": path,
        }
        if pid:
            params["workspaceId"] = pid
        else:
            params["workspaceSlug"] = pslug
        return params, (pid, pslug, env_slug, path)

    def get_secret(self, ref: SecretRef | str | dict[str, Any]) -> str:
        parsed = parse_ref(ref)
        params, scope = self._scope_params(
            project_id=parsed.project_id,
            project_slug=parsed.project_slug,
            environment=parsed.environment,
            secret_path=parsed.secret_path,
        )
        cache_key = (*scope, parsed.name)
        if self.config.cache_ttl_s > 0:
            with self._lock:
                cached = self._secret_cache.get(cache_key)
                if cached and time.monotonic() < cached[1]:
                    return cached[0]

        params["expandSecretReferences"] = "true"
        params["include_imports"] = "true"
        data = self._request_json(
            "GET",
            f"/api/v3/secrets/raw/{quote(parsed.name, safe='')}",
            params=params,
            what=f"secret '{parsed.name}'",
        )
        secret = data.get("secret")
        if not isinstance(secret, dict) or "secretValue" not in secret:
            raise InfisicalError(f"Infisical response missing secretValue for '{parsed.name}'")
        value = "" if secret["secretValue"] is None else str(secret["secretValue"])
        if self.config.cache_ttl_s > 0:
            with self._lock:
                self._secret_cache[cache_key] = (value, time.monotonic() + self.config.cache_ttl_s)
        return value

    def list_secret_names(
        self,
        *,
        secret_path: str | None = None,
        environment: str | None = None,
        project_id: str | None = None,
        project_slug: str | None = None,
        recursive: bool = False,
    ) -> list[dict[str, Any]]:
        params, (_, _, env_slug, path) = self._scope_params(
            project_id=project_id,
            project_slug=project_slug,
            environment=environment,
            secret_path=secret_path,
        )
        params["include_imports"] = "true"
        params["expandSecretReferences"] = "false"
        if recursive:
            params["recursive"] = "true"
        data = self._request_json(
            "GET",
            "/api/v3/secrets/raw",
            params=params,
            what=f"secrets at '{path}'",
        )
        seen: dict[tuple[str, str], dict[str, Any]] = {}

        def record(item: dict[str, Any], *, imported: bool, fallback_path: str) -> None:
            key = item.get("secretKey")
            if not isinstance(key, str) or not key:
                return
            item_path = normalize_secret_path(item.get("secretPath"), default=fallback_path) or fallback_path
            item_env = str(item.get("environment") or env_slug)
            seen.setdefault(
                (item_path, key),
                dict(
                    key=key,
                    path=item_path,
                    environment=item_env,
                    imported=imported,
                    ref=format_ref(key, item_path if item_path != self.config.secret_path else None),
                ),
            )

        for item in data.get("secrets") or []:
            if isinstance(item, dict):
                record(item, imported=False, fallback_path=path)
        for imp in data.get("imports") or []:
            if not isinstance(imp, dict):
                continue
            imp_path = normalize_secret_path(imp.get("secretPath"), default=path) or path
            for item in imp.get("secrets") or []:
                if isinstance(item, dict):
                    record(item, imported=True, fallback_path=imp_path)

        return [seen[k] for k in sorted(seen)]

    def test(self) -> dict[str, Any]:
        items = self.list_secret_names()
        return dict(
            ok=True,
            site_url=self.config.site_url,
            auth_mode=self.config.auth_mode,
            project_id=self.config.project_id or None,
            project_slug=self.config.project_slug or None,
            environment=self.config.environment,
            secret_path=self.config.secret_path,
            secret_count=len(items),
        )


# --- process-wide client cache (recreated automatically if env changes) -------

_client_lock = threading.Lock()
_cached_key: tuple[Any, ...] | None = None
_cached_client: InfisicalClient | None = None
_client_override: InfisicalClient | None = None


def set_client_override(client: InfisicalClient | None) -> None:
    """Test hook: inject a custom InfisicalClient (None restores env-based client)."""
    global _client_override
    with _client_lock:
        _client_override = client


def clear_cache() -> None:
    global _cached_key, _cached_client
    with _client_lock:
        if _cached_client is not None:
            _cached_client.clear_cache()
        _cached_key = None
        _cached_client = None


def _client() -> InfisicalClient:
    with _client_lock:
        if _client_override is not None:
            return _client_override
        cfg = InfisicalConfig.from_env()
        key = cfg.cache_key()
        global _cached_key, _cached_client
        if _cached_client is None or _cached_key != key:
            _cached_client = InfisicalClient(cfg)
            _cached_key = key
        return _cached_client


def resolve(value: str | None) -> str | None:
    """Resolve an `infisical:...` reference, or return plain values unchanged."""
    if value is None or not is_ref(value):
        return value
    return _client().get_secret(value)


def getenv(name: str, default: str | None = None) -> str | None:
    """os.getenv(name, default) with transparent `infisical:...` resolution."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return resolve(raw)


def require(name: str) -> str:
    """os.environ[name] with transparent `infisical:...` resolution."""
    return resolve(os.environ[name]) or ""


def status() -> dict[str, Any]:
    """Non-sensitive summary of the current Infisical configuration."""
    cfg = InfisicalConfig.from_env()
    return dict(
        configured=cfg.configured,
        auth_mode=cfg.auth_mode,
        site_url=cfg.site_url,
        project_id=cfg.project_id or None,
        project_slug=cfg.project_slug or None,
        environment=cfg.environment,
        secret_path=cfg.secret_path,
        cache_ttl_s=cfg.cache_ttl_s,
    )


def test() -> dict[str, Any]:
    return _client().test()


def list_secret_names(
    *,
    secret_path: str | None = None,
    environment: str | None = None,
    project_id: str | None = None,
    project_slug: str | None = None,
    recursive: bool = False,
) -> list[dict[str, Any]]:
    return _client().list_secret_names(
        secret_path=secret_path,
        environment=environment,
        project_id=project_id,
        project_slug=project_slug,
        recursive=recursive,
    )
