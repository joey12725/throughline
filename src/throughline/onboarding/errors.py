"""Errors a person can act on.

Every onboarding failure carries a short title, one sentence saying what
happened, and a hint saying what to do next. Routes turn a SetupError into an
HTTP error whose `detail` is that object (plus `reason`, which the web
client's ApiError uses as its message).
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error

from fastapi import HTTPException


class SetupError(Exception):
    def __init__(self, title, message, hint=None, *, status=400, code=None, problems=None, docs_url=None):
        super().__init__(message)
        self.title = title
        self.message = message
        self.hint = hint
        self.status = status
        self.code = code
        self.problems = list(problems or [])
        self.docs_url = docs_url

    def body(self):
        out = dict(title=self.title, message=self.message, hint=self.hint, code=self.code, reason=self.message)
        if self.problems:
            out["problems"] = self.problems
        if self.docs_url:
            out["docs_url"] = self.docs_url
        return out

    def http(self):
        return HTTPException(self.status, self.body())


def explain_http(error, *, service, key_url=None, host=None):
    """A SetupError for a failed request to a service (urllib exceptions)."""
    where = host or service
    if isinstance(error, urllib.error.HTTPError):
        body = ""
        try:
            body = error.read(2000).decode("utf-8", "replace")
        except Exception:
            pass
        said = _vendor_message(body)
        suffix = f" {service} said: “{said}”" if said else ""
        if error.code in (401, 403):
            return SetupError(
                f"{service} refused the key",
                f"{service} answered {error.code} ({'unauthorized' if error.code == 401 else 'forbidden'}).{suffix}",
                "Check that you pasted the whole key with no spaces, that it hasn't been revoked, "
                + (f"or create a new one at {key_url}." if key_url else "or create a new one."),
                code=f"http_{error.code}",
            )
        if error.code == 402:
            return SetupError(
                f"{service} needs credits",
                f"The key works, but the account has no credit left.{suffix}",
                "Add credits or a payment method on the provider's billing page, then test again.",
                code="http_402",
            )
        if error.code == 404:
            return SetupError(
                f"{service} didn't recognise that address",
                f"{where} answered 404 Not Found.{suffix}",
                "Check the base URL: OpenAI-compatible servers usually end in /v1.",
                code="http_404",
            )
        if error.code == 429:
            return SetupError(
                f"{service} is rate limiting",
                f"Too many requests right now (429).{suffix}",
                "Wait a minute and test again. If it persists, check the plan's limits.",
                code="http_429",
            )
        return SetupError(
            f"{service} returned an error",
            f"HTTP {error.code}.{suffix}",
            "Try again in a moment; if it keeps failing, check the provider's status page.",
            code=f"http_{error.code}",
        )
    reason = getattr(error, "reason", error)
    if isinstance(reason, socket.gaierror):
        return SetupError(
            f"Couldn't find {where}",
            f"The name {where} did not resolve (DNS).",
            "Check the spelling of the URL, and that this server has internet access.",
            code="dns",
        )
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return SetupError(
            f"{service} didn't answer in time",
            f"No reply from {where} within the time limit.",
            "Check the URL and port, and any firewall or VPN between this server and the service.",
            code="timeout",
        )
    if isinstance(reason, ssl.SSLError):
        return SetupError(
            "Secure connection failed",
            f"TLS to {where} failed: {reason}.",
            "If this is a self-hosted service with its own certificate, use its http:// LAN address or install the certificate.",
            code="tls",
        )
    if isinstance(reason, ConnectionRefusedError):
        return SetupError(
            f"{where} refused the connection",
            "Nothing is listening at that address and port.",
            "Make sure the service is running and the port is right (a local server may need to listen on 0.0.0.0).",
            code="refused",
        )
    return SetupError(
        f"Couldn't reach {service}",
        f"{type(reason).__name__}: {reason}",
        "Check the URL and that this server can reach it.",
        code="network",
    )


def _vendor_message(body):
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        text = body.strip().splitlines()[0] if body.strip() else ""
        return text[:200] or None
    for path in (("error", "message"), ("error",), ("message",), ("detail",)):
        value = data
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, str) and value:
            return value[:200]
    return None
