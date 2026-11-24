"""The query helper of the web interface (docs/web/assistant.md).

A bounded, read-only tool loop that runs in the API process. Its product is
always a (statement, view spec) pair the owner can see, edit, run and save.

    assistant.describe()                 -> features.assistant of the bootstrap
    runs.start(token, request)           -> a run in the in-memory registry
    runs.lookup(token, run_id)           -> that run, for its owner only

The model is the provider kind 'assistant' (providers.get('assistant')):
local (deterministic, no model; the default), anthropic or openai. Every tool
reads under the request's own scope through the reader path that POST /v1/sql
uses; what the model may see of row values is decided by the disclosure tier.
"""

import os

from .. import providers
from ..providers import egress

TIERS = ("schema", "samples")
DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_USD = 0.50
DEFAULT_DAILY_USD = 5.00


def _env(name):
    return (os.getenv("THROUGHLINE_ASSISTANT_" + name) or "").strip() or None


def provider():
    """The configured assistant provider (ProviderUnavailable / ValueError)."""
    return providers.get("assistant")


def loopback(found):
    """Whether the helper's endpoint is on this machine (or there is none).

    A loopback endpoint may still forward what it receives (a local gateway
    in front of a vendor), so this alone never relaxes a disclosure rule; see
    model_is_local()."""
    return not providers.egress.leaves(found)


def model_is_local(found):
    """Whether the model itself runs on this machine: the local helper, or a
    loopback endpoint the owner declared local with
    THROUGHLINE_ASSISTANT_MODEL_IS_LOCAL=1. Only then may sensitivity labels
    be skipped and samples be the default."""
    if getattr(found, "local", False) or getattr(found, "scripted", False):
        return True
    declared = (_env("MODEL_IS_LOCAL") or "").lower() in ("1", "true", "yes", "on")
    return declared and loopback(found)


def configured_tier():
    value = (_env("TIER") or "").lower()
    if value and value not in TIERS:
        raise ValueError("THROUGHLINE_ASSISTANT_TIER must be schema or samples")
    return value or None


def tiers_for(found):
    """(allowed tiers, default tier) for a provider.

    The local helper (or a model declared local) defaults to samples. Every
    other model defaults to the schema tier, a loopback endpoint included (it
    may be a gateway to a vendor): a loopback endpoint may be asked for
    samples per run, a non-loopback one only once the owner set
    THROUGHLINE_ASSISTANT_TIER=samples.
    """
    wanted = configured_tier()
    if model_is_local(found):
        return list(TIERS), wanted or "samples"
    if wanted == "samples":
        return list(TIERS), "samples"
    if loopback(found):
        return list(TIERS), wanted or "schema"
    return ["schema"], "schema"


def max_steps():
    value = _env("MAX_STEPS")
    try:
        steps = int(value) if value else DEFAULT_MAX_STEPS
    except ValueError:
        raise ValueError("THROUGHLINE_ASSISTANT_MAX_STEPS must be an integer") from None
    if not 1 <= steps <= 32:
        raise ValueError("THROUGHLINE_ASSISTANT_MAX_STEPS must be 1..32")
    return steps


def max_usd():
    value = _env("MAX_USD")
    try:
        usd = float(value) if value else DEFAULT_MAX_USD
    except ValueError:
        raise ValueError("THROUGHLINE_ASSISTANT_MAX_USD must be a number") from None
    if not 0 <= usd <= 100:
        raise ValueError("THROUGHLINE_ASSISTANT_MAX_USD must be 0..100")
    return usd


def daily_usd():
    """Helper spend one token may reach in 24 hours (from the ledger)."""
    value = _env("DAILY_USD")
    try:
        usd = float(value) if value else DEFAULT_DAILY_USD
    except ValueError:
        raise ValueError("THROUGHLINE_ASSISTANT_DAILY_USD must be a number") from None
    if not 0 <= usd <= 10000:
        raise ValueError("THROUGHLINE_ASSISTANT_DAILY_USD must be 0..10000")
    return usd


def describe():
    """features.assistant of GET /v1/web/bootstrap: no host names, no keys."""
    try:
        found = provider()
    except ValueError:
        # Not configured (or disabled): the panel offers nothing.
        return dict(
            provider="none",
            model=None,
            loopback=True,
            default_tier=None,
            tiers=[],
            configured=False,
        )
    try:
        tiers, default = tiers_for(found)
    except ValueError:
        tiers, default = ["schema"], "schema"
    # The Messages API needs a key; an OpenAI-compatible server may not.
    configured = found.local or found.name != "anthropic" or bool(found.key)
    return dict(
        provider=found.name,
        model=found.model,
        loopback=loopback(found),
        default_tier=default,
        tiers=tiers,
        configured=bool(configured),
    )


__all__ = [
    "TIERS",
    "daily_usd",
    "describe",
    "egress",
    "max_steps",
    "max_usd",
    "model_is_local",
    "provider",
    "tiers_for",
]
