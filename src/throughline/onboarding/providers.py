"""Model providers and other bring-your-own services for onboarding.

Each provider lists the variables it needs (the same names the core already
reads: OPENROUTER_API_KEY, VOYAGE_API_KEY, ...), the roles it can fill and a
live connection test. Choosing a provider for a role writes the core's own
THROUGHLINE_<KIND>_* variables through onboarding.store, and adds the
provider's host to THROUGHLINE_EGRESS_ALLOW so the core may reach it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import store
from .errors import SetupError, explain_http

TIMEOUT_S = 12

ANTHROPIC_MODELS = [
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "note": "Best balance", "default": True},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "note": "Most capable"},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5", "note": "Fastest, cheapest"},
]
OPENAI_CHAT = [
    {"id": "gpt-5.6", "label": "GPT-5.6", "default": True},
    {"id": "gpt-5.6-mini", "label": "GPT-5.6 mini", "note": "Cheaper"},
]
OPENROUTER_CHAT = [
    {"id": "anthropic/claude-sonnet-5.5", "label": "Claude Sonnet 5.5", "default": True},
    {"id": "openai/gpt-5.6", "label": "GPT-5.6"},
    {"id": "google/gemini-3-flash", "label": "Gemini 3 Flash", "note": "Cheap and fast"},
]


def _embedding_models(table, default):
    out = []
    for model, (dims, price) in table.items():
        out.append(
            {
                "id": model,
                "label": model,
                "note": f"{dims} dims · ${price:.2f}/M tokens",
                "default": model == default,
            }
        )
    return out


def _vendor_tables():
    from ..providers.vendors import GEMINI_MODELS, OPENROUTER_MODELS, VOYAGE_MODELS

    return VOYAGE_MODELS, GEMINI_MODELS, OPENROUTER_MODELS


def _secret(key, label, placeholder=None, help_url=None, help=None):
    return dict(key=key, label=label, kind="secret", required=True, placeholder=placeholder, help_url=help_url, help=help)


def _url(key, label, default=None, help=None, required=True):
    return dict(key=key, label=label, kind="url", required=required, default=default, placeholder=default, help=help)


def catalog():
    voyage, gemini, openrouter = _vendor_tables()
    return [
        dict(
            id="openrouter",
            name="OpenRouter",
            tagline="One key for hundreds of models: embeddings, assistant and Librarian.",
            roles=["embedding", "assistant", "semantic"],
            fields=[_secret("OPENROUTER_API_KEY", "API key", "sk-or-v1-…", "https://openrouter.ai/settings/keys")],
            hosts=["openrouter.ai"],
            docs_url="https://openrouter.ai/settings/keys",
            cost_note="Pay per use. Embedding a year of messages typically costs well under $5.",
            privacy_note="Throughline asks OpenRouter to route only to providers that don't keep or train on your data.",
            models=dict(
                embedding=_embedding_models(openrouter, "openai/text-embedding-3-small"),
                assistant=OPENROUTER_CHAT,
                semantic=OPENROUTER_CHAT,
            ),
        ),
        dict(
            id="voyage",
            name="Voyage AI",
            tagline="Top-ranked embeddings and reranking for meaning search.",
            roles=["embedding"],
            fields=[_secret("VOYAGE_API_KEY", "API key", "pa-…", "https://dashboard.voyageai.com/api-keys")],
            hosts=["api.voyageai.com"],
            docs_url="https://dashboard.voyageai.com/api-keys",
            cost_note="The first 200M tokens of most models are free; then a few cents per million.",
            privacy_note="Voyage does not train on API data when you opt out in the dashboard.",
            models=dict(embedding=_embedding_models(voyage, "voyage-4-large")),
        ),
        dict(
            id="openai",
            name="OpenAI",
            tagline="Embeddings and GPT models with your own OpenAI key.",
            roles=["embedding", "assistant", "semantic"],
            fields=[_secret("OPENAI_API_KEY", "API key", "sk-…", "https://platform.openai.com/api-keys")],
            hosts=["api.openai.com"],
            docs_url="https://platform.openai.com/api-keys",
            cost_note="text-embedding-3-small costs $0.02 per million tokens.",
            privacy_note="OpenAI does not train on API traffic by default.",
            models=dict(
                embedding=[
                    {"id": "text-embedding-3-small", "label": "text-embedding-3-small", "note": "1536 dims · $0.02/M", "default": True},
                    {"id": "text-embedding-3-large", "label": "text-embedding-3-large", "note": "3072 dims · $0.13/M"},
                ],
                assistant=OPENAI_CHAT,
                semantic=OPENAI_CHAT,
            ),
        ),
        dict(
            id="anthropic",
            name="Anthropic",
            tagline="Claude as the built-in assistant.",
            roles=["assistant"],
            fields=[_secret("ANTHROPIC_API_KEY", "API key", "sk-ant-…", "https://console.anthropic.com/settings/keys")],
            hosts=["api.anthropic.com"],
            docs_url="https://console.anthropic.com/settings/keys",
            cost_note="Billed per token by Anthropic; the assistant has its own daily spend cap.",
            privacy_note="Anthropic does not train on API data.",
            models=dict(assistant=ANTHROPIC_MODELS),
        ),
        dict(
            id="gemini",
            name="Google Gemini",
            tagline="Gemini embeddings and models with a Google AI Studio key.",
            roles=["embedding", "semantic"],
            fields=[_secret("GEMINI_API_KEY", "API key", "AIza…", "https://aistudio.google.com/apikey")],
            hosts=["generativelanguage.googleapis.com"],
            docs_url="https://aistudio.google.com/apikey",
            cost_note="Generous free tier; paid tier is per token.",
            privacy_note="On the free tier Google may use prompts to improve its products.",
            models=dict(
                embedding=_embedding_models(gemini, "gemini-embedding-2"),
                semantic=[{"id": "gemini-3-flash", "label": "Gemini 3 Flash", "default": True}],
            ),
        ),
        dict(
            id="cli_proxy",
            name="CLI proxy / OpenAI-compatible",
            tagline="Any OpenAI-compatible endpoint: a CLI proxy for your Claude or ChatGPT subscription, LiteLLM, vLLM…",
            roles=["assistant", "semantic", "embedding"],
            fields=[
                _url("THROUGHLINE_PROXY_BASE_URL", "Base URL", "http://127.0.0.1:8317/v1", "Usually ends in /v1."),
                dict(key="THROUGHLINE_PROXY_API_KEY", label="API key", kind="secret", required=False, help="Leave empty if the proxy has no key."),
            ],
            hosts=[],
            docs_url=None,
            cost_note="Uses whatever the proxy is connected to.",
            privacy_note="Traffic goes wherever the proxy sends it.",
            models={},
        ),
        dict(
            id="local",
            name="Local models",
            tagline="Ollama, LM Studio or llama.cpp on this machine. Nothing leaves the box.",
            roles=["embedding", "assistant", "semantic"],
            fields=[
                _url("THROUGHLINE_LOCAL_BASE_URL", "Server URL", "http://127.0.0.1:11434/v1", "Ollama: http://127.0.0.1:11434/v1 · LM Studio: http://127.0.0.1:1234/v1"),
            ],
            hosts=[],
            docs_url="https://ollama.com",
            cost_note="Free; needs a capable GPU or Apple Silicon for good speed.",
            privacy_note="Fully local.",
            models={},
        ),
        dict(
            id="immich",
            name="Immich",
            tagline="Bring your existing Immich photo library: photos join your timeline, faces join your people.",
            roles=["images"],
            fields=[
                _url("IMMICH_URL", "Immich URL", "http://127.0.0.1:2283", "The address you open Immich at."),
                _secret(
                    "IMMICH_API_KEY",
                    "API key",
                    None,
                    None,
                    "Immich → Account settings → API keys → New API key (read access to assets, people and albums).",
                ),
            ],
            hosts=[],
            docs_url="https://immich.app/docs/features/command-line-interface#obtain-the-api-key",
            cost_note=None,
            privacy_note="Photos stay in Immich; Throughline stores references and descriptions.",
            models={},
        ),
    ]


def _find(provider_id):
    for provider in catalog():
        if provider["id"] == provider_id:
            return provider
    raise SetupError(
        "Unknown provider",
        f"There is no provider called {provider_id!r}.",
        "Choose one of: " + ", ".join(p["id"] for p in catalog()),
        status=404,
    )


def _keys(provider):
    return [f["key"] for f in provider["fields"]]


def _secret_keys(provider):
    return {f["key"] for f in provider["fields"] if f["kind"] == "secret"}


def status(provider):
    secret = _secret_keys(provider)
    values, locked = {}, []
    for key in _keys(provider):
        value = store.get_env(key)
        if store.locked(key):
            locked.append(key)
        values[key] = store.mask(value) if key in secret else value
    required = [f["key"] for f in provider["fields"] if f["required"]]
    configured = all(store.get_env(k) for k in required)
    out = {k: v for k, v in provider.items() if k != "hosts"}
    return dict(out, configured=configured, values=values, locked=locked)


def view():
    roles = {kind: current_role(kind) for kind in ("embedding", "assistant", "semantic")}
    from ..providers import egress

    return dict(
        providers=[status(p) for p in catalog()],
        roles=roles,
        egress_allow=sorted(egress.allowed()),
    )


def save(provider_id, values):
    provider = _find(provider_id)
    allowed = set(_keys(provider))
    unknown = set(values) - allowed
    if unknown:
        raise SetupError(
            "Unexpected settings",
            f"{provider['name']} doesn't use: " + ", ".join(sorted(unknown)),
            "Only send the fields listed for this provider.",
        )
    cleaned = {}
    for key, value in values.items():
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("••••"):
                continue
        cleaned[key] = value
    try:
        store.set_env(cleaned, secret_keys=_secret_keys(provider))
    except PermissionError as error:
        raise SetupError(
            "Set by the server",
            str(error).capitalize() + ".",
            "These values come from the server's environment (for example its .env file). Change them there and restart.",
            status=409,
        ) from None
    _allow_hosts(_hosts(provider))
    return status(provider)


def _hosts(provider):
    hosts = list(provider.get("hosts") or [])
    for field in provider["fields"]:
        if field["kind"] == "url":
            value = store.get_env(field["key"])
            if value:
                host = urllib.parse.urlsplit(value).hostname
                if host:
                    hosts.append(host)
    return hosts


def _allow_hosts(hosts):
    from ..providers import egress

    hosts = [h for h in hosts if h and not egress.is_loopback(h)]
    if not hosts:
        return
    current = egress.allowed()
    missing = [h for h in hosts if h.lower() not in current]
    if not missing:
        return
    if store.locked("THROUGHLINE_EGRESS_ALLOW"):
        # The owner manages egress in the environment; report, don't change.
        return
    value = " ".join(sorted(current | {h.lower() for h in missing}))
    store.set_env({"THROUGHLINE_EGRESS_ALLOW": value})


# ───────────── live tests ─────────────


def _request(url, headers=None, data=None, method=None):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        body = response.read(2_000_000)
    try:
        return json.loads(body or b"null")
    except ValueError:
        return None


def _value(values, key):
    value = (values or {}).get(key)
    if isinstance(value, str) and value.startswith("••••"):
        value = None
    return (value or "").strip() or store.get_env(key)


def test(provider_id, values=None):
    """Try the provider with the given (unsaved) values, else the saved ones.
    Returns a CheckResult dict; never raises for a failed check."""
    provider = _find(provider_id)
    missing = [
        f["label"] for f in provider["fields"] if f["required"] and not _value(values, f["key"])
    ]
    if missing:
        return dict(
            ok=False,
            title="Missing details",
            message="Fill in: " + ", ".join(missing) + ".",
            hint=None,
        )
    started = time.monotonic()
    try:
        result = _TESTS[provider_id](provider, values)
    except SetupError as error:
        return dict(ok=False, title=error.title, message=error.message, hint=error.hint, duration_ms=_ms(started))
    except (urllib.error.URLError, OSError) as error:
        e = explain_http(
            error,
            service=provider["name"],
            key_url=provider.get("docs_url"),
            host=_host_of(provider, values),
        )
        return dict(ok=False, title=e.title, message=store.redact(e.message), hint=e.hint, duration_ms=_ms(started))
    result.setdefault("ok", True)
    result.setdefault("title", "Connected")
    result["duration_ms"] = _ms(started)
    return result


def _ms(started):
    return int((time.monotonic() - started) * 1000)


def _host_of(provider, values):
    for field in provider["fields"]:
        if field["kind"] == "url":
            value = _value(values, field["key"])
            if value:
                return urllib.parse.urlsplit(value).netloc
    hosts = provider.get("hosts") or []
    return hosts[0] if hosts else None


def _test_openrouter(provider, values):
    key = _value(values, "OPENROUTER_API_KEY")
    data = _request("https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"}) or {}
    info = data.get("data") or {}
    limit = info.get("limit_remaining")
    usage = info.get("usage")
    message = "The key works."
    if limit is not None:
        message += f" ${limit:,.2f} of credit left."
    elif usage is not None:
        message += f" ${usage:,.2f} used so far."
    return dict(message=message, detail={"label": info.get("label")})


def _test_voyage(provider, values):
    key = _value(values, "VOYAGE_API_KEY")
    body = json.dumps({"input": ["hello"], "model": "voyage-4-lite"}).encode()
    data = _request(
        "https://api.voyageai.com/v1/embeddings",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        body,
        "POST",
    ) or {}
    dims = len(((data.get("data") or [{}])[0]).get("embedding") or [])
    return dict(message=f"The key works (a test embedding returned {dims} numbers).")


def _test_openai(provider, values):
    key = _value(values, "OPENAI_API_KEY")
    data = _request("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}) or {}
    count = len(data.get("data") or [])
    return dict(message=f"The key works ({count} models available).")


def _test_anthropic(provider, values):
    key = _value(values, "ANTHROPIC_API_KEY")
    data = _request(
        "https://api.anthropic.com/v1/models",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    ) or {}
    names = [m.get("display_name") or m.get("id") for m in data.get("data") or []][:3]
    return dict(message="The key works." + (f" Models include {', '.join(names)}." if names else ""))


def _test_gemini(provider, values):
    key = _value(values, "GEMINI_API_KEY")
    data = _request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=50",
        {"x-goog-api-key": key},
    ) or {}
    count = len(data.get("models") or [])
    return dict(message=f"The key works ({count} models available).")


def _openai_compatible(base, key, service):
    if not base.startswith(("http://", "https://")):
        raise SetupError(
            "That URL needs a scheme",
            f"{base!r} doesn't start with http:// or https://.",
            "For example http://127.0.0.1:11434/v1",
        )
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = _request(base.rstrip("/") + "/models", headers)
    models = []
    if isinstance(data, dict):
        models = [m.get("id") for m in data.get("data") or data.get("models") or [] if isinstance(m, dict)]
    models = [m for m in models if m]
    return models


def _test_proxy(provider, values):
    base = _value(values, "THROUGHLINE_PROXY_BASE_URL")
    models = _openai_compatible(base, _value(values, "THROUGHLINE_PROXY_API_KEY"), provider["name"])
    shown = ", ".join(models[:4]) + ("…" if len(models) > 4 else "")
    return dict(
        message=f"Connected. {len(models)} models offered" + (f": {shown}." if models else "."),
        detail={"models": models[:200]},
    )


def _test_local(provider, values):
    base = _value(values, "THROUGHLINE_LOCAL_BASE_URL")
    models = _openai_compatible(base, None, provider["name"])
    if not models:
        return dict(
            ok=True,
            title="Connected, but no models",
            message="The server answered but lists no models.",
            hint="Pull one first, e.g. `ollama pull nomic-embed-text` and `ollama pull llama3.2`.",
            detail={"models": []},
        )
    return dict(message=f"Connected. Models: {', '.join(models[:5])}.", detail={"models": models[:200]})


def _test_immich(provider, values):
    base = _value(values, "IMMICH_URL").rstrip("/")
    key = _value(values, "IMMICH_API_KEY")
    if base.endswith("/api"):
        base = base[:-4]
    ping = _request(base + "/api/server/ping") or {}
    if ping.get("res") != "pong":
        raise SetupError(
            "That isn't an Immich server",
            f"{base}/api/server/ping did not answer like Immich does.",
            "Use the address you open Immich at in the browser, e.g. http://192.168.1.10:2283",
        )
    me = _request(base + "/api/users/me", {"x-api-key": key, "Accept": "application/json"}) or {}
    stats = {}
    try:
        stats = _request(base + "/api/assets/statistics", {"x-api-key": key}) or {}
    except urllib.error.HTTPError:
        pass
    total = stats.get("total")
    who = me.get("name") or me.get("email") or "your account"
    return dict(
        message=f"Signed in as {who}."
        + (f" {total:,} photos and videos in the library." if isinstance(total, int) else ""),
    )


_TESTS = {
    "openrouter": _test_openrouter,
    "voyage": _test_voyage,
    "openai": _test_openai,
    "anthropic": _test_anthropic,
    "gemini": _test_gemini,
    "cli_proxy": _test_proxy,
    "local": _test_local,
    "immich": _test_immich,
}


# ───────────── roles ─────────────

ROLE_VARS = {
    "embedding": [
        "THROUGHLINE_EMBEDDING_PROVIDER",
        "THROUGHLINE_EMBEDDING_MODEL",
        "THROUGHLINE_EMBEDDING_BASE_URL",
        "THROUGHLINE_EMBEDDING_DIMENSIONS",
        "THROUGHLINE_EMBEDDING_KEY",
    ],
    "assistant": [
        "THROUGHLINE_ASSISTANT_PROVIDER",
        "THROUGHLINE_ASSISTANT_MODEL",
        "THROUGHLINE_ASSISTANT_BASE_URL",
        "THROUGHLINE_ASSISTANT_API_KEY",
    ],
    "semantic": [
        "THROUGHLINE_SEMANTIC_PROVIDER",
        "THROUGHLINE_SEMANTIC_MODEL",
        "THROUGHLINE_SEMANTIC_BASE_URL",
        "THROUGHLINE_SEMANTIC_KEY",
        "THROUGHLINE_CONTROLLER_PROVIDER",
        "THROUGHLINE_CONTROLLER_MODEL",
        "THROUGHLINE_CONTROLLER_BASE_URL",
        "THROUGHLINE_CONTROLLER_KEY",
        "THROUGHLINE_TABLE_LLM_BASE_URL",
        "THROUGHLINE_TABLE_LLM_MODEL",
        "THROUGHLINE_TABLE_LLM_KEY",
        "THROUGHLINE_TABLE_LLM_PRICE_PER_MILLION",
    ],
}
ROLE_KEY = "THROUGHLINE_SETUP_ROLE_"  # remembers which onboarding provider fills a role


def current_role(kind):
    chosen = store.get_env(ROLE_KEY + kind.upper())
    model = store.get_env(f"THROUGHLINE_{kind.upper()}_MODEL")
    if chosen:
        return dict(provider=None if chosen == "none" else chosen, model=model)
    provider = store.get_env(f"THROUGHLINE_{kind.upper()}_PROVIDER")
    return dict(provider=provider or None, model=model)


def _openai_dims(model):
    return {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}.get(model)


def _probe_dimensions(base, key, model):
    body = json.dumps({"input": ["hello"], "model": model}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        data = _request(base.rstrip("/") + "/embeddings", headers, body, "POST") or {}
    except (urllib.error.URLError, OSError) as error:
        raise explain_http(error, service="The embedding server", host=urllib.parse.urlsplit(base).netloc) from None
    vector = ((data.get("data") or [{}])[0]).get("embedding") or []
    if not vector:
        raise SetupError(
            "That model didn't return an embedding",
            f"{model} answered without a vector.",
            "Pick an embedding model (for Ollama, e.g. nomic-embed-text).",
        )
    return len(vector)


def _role_env(kind, provider_id, model):
    """The core variables that make `provider_id` fill `kind`."""
    if provider_id in (None, "", "none"):
        return {"embedding": {"THROUGHLINE_EMBEDDING_PROVIDER": "none"},
                "assistant": {"THROUGHLINE_ASSISTANT_PROVIDER": "none"},
                "semantic": {"THROUGHLINE_SEMANTIC_PROVIDER": "none", "THROUGHLINE_CONTROLLER_PROVIDER": "none"}}[kind]
    provider = _find(provider_id)
    if kind not in provider["roles"]:
        raise SetupError(
            f"{provider['name']} can't do that",
            f"{provider['name']} doesn't offer {kind} models.",
            "Pick another provider for this role.",
        )
    if not status(provider)["configured"]:
        raise SetupError(
            f"Add your {provider['name']} details first",
            f"{provider['name']} has no saved key yet.",
            "Save the provider's key above, then choose it here.",
        )
    defaults = {m["id"]: m for m in provider["models"].get(kind, [])}
    if not model:
        model = next((m["id"] for m in defaults.values() if m.get("default")), None)
    if not model:
        raise SetupError("Choose a model", f"Pick which {kind} model to use from {provider['name']}.")
    proxy_base = store.get_env("THROUGHLINE_PROXY_BASE_URL")
    proxy_key = store.get_env("THROUGHLINE_PROXY_API_KEY")
    local_base = store.get_env("THROUGHLINE_LOCAL_BASE_URL")
    openai_key = store.get_env("OPENAI_API_KEY")
    if kind == "embedding":
        if provider_id in ("voyage", "openrouter", "gemini"):
            return {"THROUGHLINE_EMBEDDING_PROVIDER": provider_id, "THROUGHLINE_EMBEDDING_MODEL": model}
        if provider_id == "openai":
            return {
                "THROUGHLINE_EMBEDDING_PROVIDER": "http",
                "THROUGHLINE_EMBEDDING_BASE_URL": "https://api.openai.com/v1",
                "THROUGHLINE_EMBEDDING_MODEL": model,
                "THROUGHLINE_EMBEDDING_DIMENSIONS": str(_openai_dims(model) or _probe_dimensions("https://api.openai.com/v1", openai_key, model)),
                "THROUGHLINE_EMBEDDING_KEY": openai_key,
            }
        base, key = (proxy_base, proxy_key) if provider_id == "cli_proxy" else (local_base, None)
        return {
            "THROUGHLINE_EMBEDDING_PROVIDER": "http",
            "THROUGHLINE_EMBEDDING_BASE_URL": base,
            "THROUGHLINE_EMBEDDING_MODEL": model,
            "THROUGHLINE_EMBEDDING_DIMENSIONS": str(_probe_dimensions(base, key, model)),
            "THROUGHLINE_EMBEDDING_KEY": key,
        }
    if kind == "assistant":
        if provider_id == "anthropic":
            return {"THROUGHLINE_ASSISTANT_PROVIDER": "anthropic", "THROUGHLINE_ASSISTANT_MODEL": model}
        if provider_id == "openai":
            return {"THROUGHLINE_ASSISTANT_PROVIDER": "openai", "THROUGHLINE_ASSISTANT_MODEL": model}
        base, key = {
            "openrouter": ("https://openrouter.ai/api/v1", store.get_env("OPENROUTER_API_KEY")),
            "cli_proxy": (proxy_base, proxy_key),
            "local": (local_base, None),
        }[provider_id]
        return {
            "THROUGHLINE_ASSISTANT_PROVIDER": "openai",
            "THROUGHLINE_ASSISTANT_BASE_URL": base,
            "THROUGHLINE_ASSISTANT_MODEL": model,
            "THROUGHLINE_ASSISTANT_API_KEY": key,
        }
    # semantic (Librarian, Lenses, classifiers) and its controller
    if provider_id in ("openrouter", "gemini"):
        env = {"THROUGHLINE_SEMANTIC_PROVIDER": provider_id, "THROUGHLINE_SEMANTIC_MODEL": model}
    else:
        base, key = {
            "openai": ("https://api.openai.com/v1", openai_key),
            "cli_proxy": (proxy_base, proxy_key),
            "local": (local_base, None),
        }[provider_id]
        env = {
            "THROUGHLINE_SEMANTIC_PROVIDER": "http",
            "THROUGHLINE_SEMANTIC_BASE_URL": base,
            "THROUGHLINE_SEMANTIC_MODEL": model,
            "THROUGHLINE_SEMANTIC_KEY": key,
        }
    env.update({k.replace("SEMANTIC", "CONTROLLER"): v for k, v in env.items()})
    # Lenses and pages call an OpenAI-compatible chat endpoint
    # (THROUGHLINE_TABLE_LLM_*): the same model, through that interface.
    table = {
        "openrouter": ("https://openrouter.ai/api/v1", store.get_env("OPENROUTER_API_KEY"), None),
        "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", store.get_env("GEMINI_API_KEY"), None),
        "openai": ("https://api.openai.com/v1", openai_key, None),
        "cli_proxy": (proxy_base, proxy_key, "0"),
        "local": (local_base, None, "0"),
    }[provider_id]
    env.update({
        "THROUGHLINE_TABLE_LLM_BASE_URL": table[0],
        "THROUGHLINE_TABLE_LLM_MODEL": model,
        "THROUGHLINE_TABLE_LLM_KEY": table[1],
        "THROUGHLINE_TABLE_LLM_PRICE_PER_MILLION": table[2],
    })
    return env


def set_roles(choices):
    for kind, choice in (choices or {}).items():
        if kind not in ROLE_VARS:
            raise SetupError("Unknown role", f"{kind!r} is not a model role.", "Use embedding, assistant or semantic.")
        choice = choice or {}
        provider_id = choice.get("provider")
        env = _role_env(kind, provider_id, choice.get("model"))
        values = {name: None for name in ROLE_VARS[kind] if not store.locked(name)}
        values.update(env)
        values[ROLE_KEY + kind.upper()] = provider_id or "none"
        blocked = [k for k in env if store.locked(k)]
        if blocked:
            raise SetupError(
                "Set by the server",
                "These are set in the server's environment: " + ", ".join(blocked) + ".",
                "Remove them from the server's environment (e.g. .env) to choose here, or keep the server's choice.",
                status=409,
            )
        secret = {k for k in values if k.endswith(("_KEY", "_API_KEY"))}
        store.set_env(values, secret_keys=secret)
        if provider_id and provider_id != "none":
            _allow_hosts(_hosts(_find(provider_id)))
    return view()
