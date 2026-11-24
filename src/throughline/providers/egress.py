"""Egress allow-list: decided from the URL alone, before any socket is opened.

Loopback is always allowed. Any other host must be listed in
THROUGHLINE_EGRESS_ALLOW (comma or space separated ``host`` or ``host:port``
entries). Names are compared as written: nothing is resolved, so a host name
that merely points at loopback is still treated as remote.
"""

import ipaddress
import os
from urllib.parse import urlsplit

import httpx

from .base import EgressDenied

_PORTS = {"http": 80, "https": 443}


def target(url):
    """(host, port) of an http(s) URL; raises EgressDenied for anything else."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port or _PORTS.get(parts.scheme)
    except ValueError:
        raise EgressDenied("model endpoint is not a valid URL") from None
    if parts.scheme not in _PORTS or not host or parts.username or parts.password:
        raise EgressDenied("model endpoint must be a plain http or https URL")
    # The request is sent by httpx, which parses the URL itself. Both parsers
    # must name the same scheme, host and port, or a URL that one of them reads
    # differently (stray backslashes, control characters, odd authority
    # syntax) could reach a host other than the one that was checked.
    try:
        sent = httpx.URL(url)
        sent_host = (sent.host or "").lower().rstrip(".")
        sent_port = sent.port or _PORTS.get(sent.scheme)
        agree = (
            sent.scheme == parts.scheme
            and sent_host == host
            and sent_port == port
            and not sent.userinfo
        )
    except (httpx.InvalidURL, ValueError, TypeError):
        agree = False
    if not agree:
        raise EgressDenied("model endpoint is not an unambiguous http or https URL")
    return host, port


def is_loopback(host):
    host = host.lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allowed():
    """The configured entries as a set of 'host' and 'host:port' strings."""
    value = os.getenv("THROUGHLINE_EGRESS_ALLOW", "")
    return {
        entry.strip().lower().rstrip(".")
        for entry in value.replace(",", " ").split()
        if entry.strip()
    }


def leaves_host(url):
    """True when a request to this URL would carry data off this machine."""
    try:
        return not is_loopback(target(url)[0])
    except EgressDenied:
        return True


def check(url):
    """Return the URL when a request to it is permitted; raise EgressDenied."""
    host, port = target(url)
    if is_loopback(host):
        return url
    entries = allowed()
    bracketed = f"[{host}]" if ":" in host else host
    if host in entries or bracketed in entries or f"{bracketed}:{port}" in entries:
        return url
    # The host is configuration, not data, and naming it is what lets the
    # owner fix the setting.
    raise EgressDenied(
        f"egress to {bracketed}:{port} is not allowed: add it to THROUGHLINE_EGRESS_ALLOW"
    )


# --- label-aware egress (migration 0055) ----------------------------------------


def leaves(provider):
    """True when text handed to this provider leaves the machine: it is not a
    local implementation and its endpoint is not loopback. A remote provider
    without a known endpoint counts as leaving."""
    if getattr(provider, "local", False):
        return False
    base = getattr(provider, "base", None)
    return True if not base else leaves_host(base)


def held(message_ids):
    """The ids among message_ids whose content may not be sent off this
    machine: a label of the message, its thread, its channel or one of its
    sources is a kind the owner has not allowed to leave (tl.labels.egress).
    Ids that name no stored message are held too."""
    ids = sorted({int(i) for i in message_ids or () if i is not None})
    if not ids:
        return set()
    from ..db import connect

    with connect("app") as conn:
        return {
            int(row["id"])
            for row in conn.execute(
                "SELECT h AS id FROM tl.messages_egress_held(%s::bigint[]) h", (ids,)
            )
        }


def held_for(provider, message_ids):
    """held(message_ids) when the provider leaves the host, else nothing."""
    return held(message_ids) if leaves(provider) else set()
