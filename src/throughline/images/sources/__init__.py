"""Where image bytes come from, one module per source.

Each module exposes:
    discover(conn, settings, *, limit=None) -> iterator of Candidate
        read only; one Candidate per image attachment (non-images and
        filtered items come back with `skip` set, so dry runs can count them)
    Resolver(settings, ...).fetch(candidate) -> Fetched
        the bytes, or a status saying why not (never raises for a missing file)

A candidate names the message it belongs to by (channel, external_id); links
are keyed that way, so an image may be linked before its message is ingested.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Candidate:
    origin: str  # beeper | t3code | email | hermes | capture | immich
    channel: str
    external_id: str
    ord: int = 0
    role: str = "attachment"
    name: str | None = None
    mime: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    happened_at: datetime | None = None
    source_ref: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)  # label, thread, author, body (for the Immich description)
    route: str | None = None  # how the resolver would fetch it
    skip: str | None = None  # why it is not taken (filtered or unfetchable here)
    message_known: bool | None = None  # its message is in tl.messages (None = not checked)

    @property
    def key(self):
        return (self.channel, self.external_id, self.ord)


@dataclass
class Fetched:
    status: str  # ok | expired | needs_token | not_found | pending_export | truncated | failed | filtered
    data: bytes | None = None
    mime: str | None = None
    detail: str | None = None


SOURCES = ("beeper", "t3code", "email", "hermes", "capture")
_REGISTERED: dict[str, object] = {}


def register_source(name: str, adapter) -> None:
    """Register an external image source adapter (exposing discover and Resolver)."""
    _REGISTERED[name] = adapter


def available_sources() -> tuple[str, ...]:
    """Built-in, registered, and entry-point (`throughline.image_sources`) source names."""
    from importlib.metadata import entry_points

    names = list(SOURCES)
    for name in _REGISTERED:
        if name not in names:
            names.append(name)
    for ep in entry_points(group="throughline.image_sources"):
        if ep.name not in names:
            names.append(ep.name)
    return tuple(names)


def module(name):
    import importlib
    from importlib.metadata import entry_points

    if name in _REGISTERED:
        return _REGISTERED[name]
    for ep in entry_points(group="throughline.image_sources"):
        if ep.name == name:
            return ep.load()
    if name not in SOURCES:
        raise ValueError(
            f"unknown source {name!r}; one of {', '.join(available_sources())} (or immich)"
        )
    return importlib.import_module(f"{__name__}.{name}")
