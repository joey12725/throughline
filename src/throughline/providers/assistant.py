"""Remote implementations of the assistant kind: the query helper's chat model.

    THROUGHLINE_ASSISTANT_PROVIDER=anthropic   Messages API (default model claude-opus-5-5)
    THROUGHLINE_ASSISTANT_PROVIDER=openai      any OpenAI-compatible /chat/completions
                                               endpoint with tools (http is an alias)

Both speak through providers.http.request: the egress allow-list is checked
before a socket is opened, proxies are ignored, redirects are not followed and
a response body is never echoed in an error. No SDK is used; the wire format
is plain JSON over httpx. One call is one model turn; the loop, the tools and
the disclosure tiers live in throughline.assistant.
"""

import json
import os
import time

from . import egress, http
from .base import Assistant, ProviderUnavailable, Usage, text_tokens

ANTHROPIC_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2026-10-21"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept the server-side refusal fallback ("fallbacks": "default").
FALLBACK_MODELS = (
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-fable-5-1",
    "claude-fable-5-2",
    "claude-sonnet-5-5",
)
# USD per million tokens: input, output, cache read. A cache write costs 1.25x
# the input price (five-minute entries). Unknown models are priced like the
# most expensive one listed, so the run ceiling errs on the safe side;
# THROUGHLINE_ASSISTANT_PRICE_IN / _PRICE_OUT override both.
ANTHROPIC_PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
UNKNOWN_PRICE = (10.0, 50.0, 1.00)
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def setting(name, *fallbacks):
    """THROUGHLINE_ASSISTANT_<name>, else the first fallback variable set."""
    value = os.getenv("THROUGHLINE_ASSISTANT_" + name)
    for other in fallbacks:
        if value:
            break
        value = os.getenv(other)
    return (value or "").strip() or None


def _number(name, default, low, high):
    value = setting(name)
    if value is None:
        return default
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"THROUGHLINE_ASSISTANT_{name} must be a number") from None
    if not low <= number <= high:
        raise ValueError(f"THROUGHLINE_ASSISTANT_{name} must be {low}..{high}")
    return number


def _usage_int(data, key):
    value = (data or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class _Remote(Assistant):
    local = False

    def __init__(self, base, model, key=None, timeout=120):
        self.base = base.rstrip("/")
        self.model = model
        self.key = key or None
        self.timeout = timeout
        self.excerpts_leave_host = egress.leaves_host(self.base)
        price_in = setting("PRICE_IN")
        price_out = setting("PRICE_OUT")
        if price_in is not None:
            self.price_in = _number("PRICE_IN", 0.0, 0.0, 10000.0)
        if price_out is not None:
            self.price_out = _number("PRICE_OUT", 0.0, 0.0, 10000.0)
        self.max_tokens = int(_number("MAX_TOKENS", 16000, 256, 64000))

    def _cost(self, tokens_in, tokens_out, cache_write=0, cache_read=0, model=None):
        price_in, price_out = self.price_in, self.price_out
        price_read = price_in
        return (
            tokens_in * price_in
            + cache_write * price_in * 1.25
            + cache_read * price_read
            + tokens_out * price_out
        ) / 1000000


class AnthropicAssistant(_Remote):
    """POST {base}/v1/messages with client tools, adaptive thinking, prompt
    caching of the tools and system prompt, and the server-side refusal
    fallback on the models that accept it."""

    name = "anthropic"
    version = "anthropic-messages-v1"

    def __init__(self, base, model, key=None, timeout=120):
        prices = ANTHROPIC_PRICES.get(model, UNKNOWN_PRICE)
        self.price_in, self.price_out, self.price_read = prices
        super().__init__(base, model, key=key, timeout=timeout)
        if setting("PRICE_IN") is not None:
            self.price_read = self.price_in / 10
        effort = (setting("EFFORT") or "medium").lower()
        if effort not in EFFORTS:
            raise ValueError(
                "THROUGHLINE_ASSISTANT_EFFORT must be one of " + ", ".join(EFFORTS)
            )
        self.effort = effort
        self.fallbacks = model in FALLBACK_MODELS and setting("FALLBACKS") != "0"

    def url(self):
        return self.base + (
            "/messages" if self.base.endswith("/v1") else "/v1/messages"
        )

    def _cost(self, tokens_in, tokens_out, cache_write=0, cache_read=0, model=None):
        price_in, price_out, price_read = (
            (self.price_in, self.price_out, self.price_read)
            if model in (None, self.model) or model not in ANTHROPIC_PRICES
            else ANTHROPIC_PRICES[model]
        )
        return (
            tokens_in * price_in
            + cache_write * price_in * 1.25
            + cache_read * price_read
            + tokens_out * price_out
        ) / 1000000

    def body(self, system, tools, messages, max_tokens=None):
        parts = [
            p for p in (system if isinstance(system, (list, tuple)) else [system]) if p
        ]
        system_blocks = [{"type": "text", "text": p} for p in parts]
        if system_blocks:
            # Tools and system render before the messages: one breakpoint
            # after the system prompt caches both for every later turn.
            system_blocks[-1]["cache_control"] = {"type": "ephemeral"}
        tool_list = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]
        wire = []
        for message in messages:
            role = message.get("role")
            if role == "user":
                wire.append({"role": "user", "content": message.get("text") or ""})
            elif role == "assistant":
                # The content exactly as it came back (thinking blocks
                # included): it must be passed back unchanged.
                wire.append({"role": "assistant", "content": message["native"]})
            elif role == "tool":
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": r["id"],
                                "content": r["content"],
                                "is_error": bool(r.get("is_error")),
                            }
                            for r in message["results"]
                        ],
                    }
                )
        body = {
            "model": self.model,
            "max_tokens": min(self.max_tokens, max_tokens or self.max_tokens),
            "system": system_blocks,
            "tools": tool_list,
            "tool_choice": {"type": "auto"},
            "messages": wire,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            # The conversation prefix is cached too, so each tool turn reads
            # the previous one from the cache.
            "cache_control": {"type": "ephemeral"},
        }
        if self.fallbacks:
            body["fallbacks"] = "default"
        return body

    def turn(self, system, tools, messages, max_tokens=None):
        started = time.perf_counter()
        headers = {
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if self.key:
            headers["x-api-key"] = self.key
        if self.fallbacks:
            headers["anthropic-beta"] = FALLBACK_BETA
        data = http.request(
            "POST",
            self.url(),
            headers=headers,
            payload=self.body(system, tools, messages, max_tokens),
            timeout=self.timeout,
            what="assistant",
            retries=2,
        )
        content = data.get("content")
        if not isinstance(content, list):
            raise ProviderUnavailable("invalid assistant response")
        text, calls = [], []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text.append(block["text"])
            elif block.get("type") == "tool_use":
                calls.append(
                    dict(
                        id=str(block.get("id") or ""),
                        name=str(block.get("name") or ""),
                        input=block.get("input")
                        if isinstance(block.get("input"), dict)
                        else {},
                    )
                )
        reason = data.get("stop_reason")
        stop = {
            "tool_use": "tool_use",
            "end_turn": "end",
            "stop_sequence": "end",
            "max_tokens": "max_tokens",
            "model_context_window_exceeded": "max_tokens",
            "refusal": "refusal",
            "pause_turn": "pause",
        }.get(reason, "other")
        detail = None
        if stop == "refusal":
            details = data.get("stop_details") or {}
            detail = details.get("category") if isinstance(details, dict) else None
        usage = data.get("usage") or {}
        tokens_in = _usage_int(usage, "input_tokens")
        cache_write = _usage_int(usage, "cache_creation_input_tokens")
        cache_read = _usage_int(usage, "cache_read_input_tokens")
        tokens_out = _usage_int(usage, "output_tokens")
        served = data.get("model") if isinstance(data.get("model"), str) else self.model
        cost = self._cost(tokens_in, tokens_out, cache_write, cache_read, served)
        reply = dict(
            text="\n\n".join(t for t in text if t.strip()),
            calls=calls,
            stop=stop,
            detail=detail,
            native=content,
            tokens_in=tokens_in + cache_write + cache_read,
            tokens_out=tokens_out,
        )
        return reply, Usage(
            model=served,
            tokens=tokens_in + cache_write + cache_read + tokens_out,
            cost=cost,
            calls=1,
            ms=(time.perf_counter() - started) * 1000,
        )


class OpenAIAssistant(_Remote):
    """POST {base}/chat/completions with function tools: OpenAI, OpenRouter
    or a local server (llama.cpp, vLLM, Ollama's OpenAI endpoint)."""

    name = "openai"
    version = "openai-chat-tools-v1"

    def body(self, system, tools, messages, max_tokens=None):
        parts = [
            p for p in (system if isinstance(system, (list, tuple)) else [system]) if p
        ]
        wire = [{"role": "system", "content": "\n\n".join(parts)}]
        for message in messages:
            role = message.get("role")
            if role == "user":
                wire.append({"role": "user", "content": message.get("text") or ""})
            elif role == "assistant":
                native = message["native"] or {}
                entry = {"role": "assistant", "content": native.get("content")}
                if native.get("tool_calls"):
                    entry["tool_calls"] = native["tool_calls"]
                wire.append(entry)
            elif role == "tool":
                for r in message["results"]:
                    wire.append(
                        {
                            "role": "tool",
                            "tool_call_id": r["id"],
                            "content": r["content"],
                        }
                    )
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": wire,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ],
            "tool_choice": "auto",
        }
        # Sent when configured, or when the run's remaining budget lowers it.
        if setting("MAX_TOKENS") or (max_tokens and max_tokens < self.max_tokens):
            body["max_tokens"] = min(self.max_tokens, max_tokens or self.max_tokens)
        return body

    def turn(self, system, tools, messages, max_tokens=None):
        started = time.perf_counter()
        payload = self.body(system, tools, messages, max_tokens)
        data = http.request(
            "POST",
            self.base + "/chat/completions",
            key=self.key,
            payload=payload,
            timeout=self.timeout,
            what="assistant",
            retries=2,
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderUnavailable("invalid assistant response") from None
        if not isinstance(message, dict):
            raise ProviderUnavailable("invalid assistant response")
        calls = []
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            raw = function.get("arguments")
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except ValueError:
                arguments = None
            calls.append(
                dict(
                    id=str(call.get("id") or ""),
                    name=str(function.get("name") or ""),
                    input=arguments
                    if isinstance(arguments, dict)
                    else {"_invalid": True},
                )
            )
        finish = choice.get("finish_reason")
        refusal = message.get("refusal")
        if refusal or finish == "content_filter":
            stop = "refusal"
        elif calls:
            stop = "tool_use"
        elif finish == "length":
            stop = "max_tokens"
        elif finish in ("stop", None):
            stop = "end"
        else:
            stop = "other"
        usage = data.get("usage") or {}
        tokens_in = _usage_int(usage, "prompt_tokens") or text_tokens(
            json.dumps(payload["messages"], default=str)
        )
        tokens_out = _usage_int(usage, "completion_tokens")
        text = message.get("content") if isinstance(message.get("content"), str) else ""
        native = {"content": message.get("content")}
        if message.get("tool_calls"):
            native["tool_calls"] = message["tool_calls"]
        reply = dict(
            text=text or "",
            calls=calls,
            stop=stop,
            detail=None,
            native=native,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        return reply, Usage(
            model=data.get("model")
            if isinstance(data.get("model"), str)
            else self.model,
            tokens=tokens_in + tokens_out,
            cost=self._cost(tokens_in, tokens_out),
            calls=1,
            ms=(time.perf_counter() - started) * 1000,
        )


def _key(base, vendor_host, vendor_variable):
    """THROUGHLINE_ASSISTANT_API_KEY; the vendor's conventional variable only
    when the endpoint is that vendor's own host over https, so a key is never
    sent to an endpoint it was not issued for, nor in the clear."""
    own = setting("API_KEY", "THROUGHLINE_ASSISTANT_KEY")
    if own:
        return own
    try:
        host = egress.target(base)[0]
    except ProviderUnavailable:
        return None
    # Only over https: a plain-http URL naming the vendor's host would send
    # the key in the clear.
    if host != vendor_host or not base.strip().lower().startswith("https://"):
        return None
    return (os.getenv(vendor_variable) or "").strip() or None


def build(name):
    """The configured remote assistant (ValueError / ProviderUnavailable when
    it is not usable)."""
    timeout = _number("TIMEOUT_S", 120, 5, 600)
    if name == "anthropic":
        base = setting("BASE_URL") or ANTHROPIC_BASE
        return AnthropicAssistant(
            base,
            setting("MODEL") or ANTHROPIC_DEFAULT_MODEL,
            key=_key(base, "api.anthropic.com", "ANTHROPIC_API_KEY"),
            timeout=timeout,
        )
    if name in ("openai", "http"):
        base, model = setting("BASE_URL"), setting("MODEL")
        if not (base and model):
            raise ProviderUnavailable(
                "assistant provider unavailable: configure THROUGHLINE_ASSISTANT_BASE_URL and _MODEL"
            )
        return OpenAIAssistant(
            base,
            model,
            key=_key(base, "api.openai.com", "OPENAI_API_KEY"),
            timeout=timeout,
        )
    raise ValueError(
        "THROUGHLINE_ASSISTANT_PROVIDER must be local, anthropic, openai or none"
    )
