from typing import Any, Literal

from pydantic import BaseModel, Field


class Channel(BaseModel):
    name: str
    description: str
    fields: list[dict] = Field(default_factory=list)
    is_reference: bool = False
    is_outside: bool = False
    conversation_whole_thread: bool = False
    conversation_max_tokens: int = 1500
    conversation_gap_s: int = 1800
    location_tolerance_s: int = 600
    # Optional record model declarations. None leaves the decision to core:
    # 'reference' when is_reference, else 'life_event'.
    record_kind: (
        Literal["life_event", "reference", "page_revision", "derived"] | None
    ) = None
    # JSON pointers into the raw payload that hold page text; alternatives in
    # one entry are separated by "|", entries are joined by a blank line.
    passage_text_paths: list[str] | None = None
    # False: a new channel gets no vectors (no meaning search over it). None
    # leaves it embedded. The owner's later choice always wins.
    embed: bool | None = None
    # Link types whose targets are provenance sources of this channel's records.
    provenance_link_types: list[str] | None = None
    # Optional role of the channel, so core features find it by kind rather
    # than by name: chat, calendar, telemetry, transcript, voice_summary,
    # location, email (or another lowercase word). None declares nothing.
    role: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    # Ordering in POST /v1/orient, higher first. None declares nothing.
    orient_priority: int | None = Field(default=None, ge=-1000, le=1000)
    # Smallest thread window POST /v1/find reads around a hit of this
    # channel when the caller did not choose one. None declares nothing.
    context_window_min: int | None = Field(default=None, ge=0, le=40)


class Declaration(BaseModel):
    env: str
    description: str = ""
    required: bool = True
    default: str | None = None


class Manifest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str
    channels: list[Channel]
    settings: dict[str, Declaration] = Field(default_factory=dict)
    secrets: dict[str, Declaration] = Field(default_factory=dict)
    run_mode: Literal["poll", "webhook", "stream"] = "poll"
    default_interval_s: int = Field(default=3600, gt=0)
    link_types: list[str] = Field(default_factory=list)


class Checkpoint(BaseModel):
    cursor: dict[str, Any]
    key: str = "default"


class Record(BaseModel):
    kind: Literal["message", "link", "track", "identity", "provenance", "coverage"] = (
        "message"
    )
    data: dict[str, Any]
    # Identity records use an explicit HTTP path and require an owner/agent token.
    path: str | None = None


def provenance(derived, sources, relation="derived_from"):
    """A provenance record: `derived` cites `sources` (all {channel, external_id}).

    The runner delivers it to POST /v1/provenance after every message yielded
    before it, as it does for links. The derived record must be a message of
    one of the plugin's own page_revision or derived channels; sources may be
    in any channel. Unknown source keys are only counted by the core.
    """
    return Record(
        kind="provenance",
        data={
            "derived": dict(derived),
            "sources": [dict(s) for s in sources],
            "relation": relation,
        },
    )


def _when(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def coverage(
    channel=None,
    scope_key="",
    status="complete",
    reasons=(),
    window=None,
    high_water=None,
    rows_delivered=None,
    gaps=(),
    closes=(),
    verified=False,
    detail=None,
):
    """A coverage record: what this run delivered for a channel (or the whole
    plugin when channel is None) and scope key.

    status: complete, partial or best_effort (other words are kept raw and
    read as partial by the core). window: {from, to} or a (from, to) pair.
    gaps: [{from, to, reason}] windows the source did not deliver. closes:
    gap ids, or {channel, scope_key, from, to} windows of open gaps this run
    delivered. verified: the source confirmed the window, so an empty
    complete report may close a gap. The runner delivers it after every
    message yielded before it, like provenance.
    """
    data = {"scope_key": scope_key or "", "status": status}
    if channel is not None:
        data["channel"] = channel
    if reasons:
        data["reasons"] = list(reasons)
    if window is not None:
        if isinstance(window, (list, tuple)):
            window = {"from": window[0], "to": window[1]}
        data["window"] = {k: _when(v) for k, v in dict(window).items()}
    if high_water is not None:
        data["high_water"] = _when(high_water)
    if rows_delivered is not None:
        data["rows_delivered"] = rows_delivered
    if gaps:
        data["gaps"] = [{k: _when(v) for k, v in dict(g).items()} for g in gaps]
    if closes:
        data["closes"] = [
            {k: _when(v) for k, v in dict(c).items()} if isinstance(c, dict) else c
            for c in closes
        ]
    if verified:
        data["verified"] = True
    if detail is not None:
        data["detail"] = detail
    return Record(kind="coverage", data=data)
