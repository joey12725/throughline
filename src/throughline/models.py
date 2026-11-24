"""Validated public ingest shapes. Payloads remain untouched JSON values."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


RecordKind = Literal["life_event", "reference", "page_revision", "derived"]
Precision = Literal["exact", "day", "month", "year", "approximate", "unknown"]


class Channel(BaseModel):
    name: str = Field(max_length=60, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
    plugin: str
    description: str = ""
    fields: list[dict] = Field(default_factory=list)
    is_reference: bool = False
    is_outside: bool = False
    conversation_whole_thread: bool = False
    conversation_max_tokens: int = Field(default=1500, ge=32)
    conversation_gap_s: int = Field(default=1800, ge=0)
    location_tolerance_s: int = Field(default=600, ge=0)
    # None means "not declared": registration keeps the stored kind, or derives
    # it from is_reference for a new channel.
    record_kind: RecordKind | None = None
    # Where the passage text of a page lives in the raw payload. Each entry is
    # one or more JSON pointers separated by "|" (the first non-empty value
    # wins); the entries are joined by a blank line.
    passage_text_paths: list[str] | None = None
    # Whether the channel's conversations and passages get vectors. None means
    # "not declared": a new channel is embedded, an existing one keeps the
    # owner's choice (throughline embed channels). A declared value applies
    # only when the channel is first registered.
    embed: bool | None = None
    # Link types whose targets are provenance sources of this channel's records.
    provenance_link_types: list[str] | None = None
    # What the channel holds, for features that work by kind rather than by
    # name (throughline.channel_roles): chat, calendar, telemetry, transcript,
    # voice_summary, location, email, or any other lowercase word. None means
    # "not declared": a new channel has no role, an existing one keeps the
    # stored value (so an owner may set it by hand for a plugin that does not
    # declare one).
    role: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    # Ordering in POST /v1/orient (higher first); None keeps the stored value.
    orient_priority: int | None = Field(default=None, ge=-1000, le=1000)
    # Smallest thread window POST /v1/find reads around a hit when the caller
    # did not choose a window; None keeps the stored value.
    context_window_min: int | None = Field(default=None, ge=0, le=40)

    @field_validator("passage_text_paths")
    @classmethod
    def pointers_valid(cls, value):
        for entry in value or []:
            if not entry or any(
                not pointer.startswith("/") for pointer in entry.split("|")
            ):
                raise ValueError(
                    "passage_text_paths entries are JSON pointers separated by '|'"
                )
        return value

    @field_validator("provenance_link_types")
    @classmethod
    def link_types_valid(cls, value):
        if any(not item for item in value or []):
            raise ValueError("provenance_link_types must not contain empty names")
        return value

    @model_validator(mode="after")
    def kind_consistent(self):
        if self.record_kind == "reference" and not self.is_reference:
            raise ValueError("record_kind 'reference' requires is_reference")
        return self

    @field_validator("fields")
    @classmethod
    def fields_valid(cls, value):
        reserved = {
            "id",
            "channel",
            "channel_id",
            "external_id",
            "version",
            "thread_id",
            "thread_no",
            "happened_at",
            "content",
            "fields",
            "lat",
            "lon",
            "person_id",
            "superseded_by",
            "happened_offset_min",
            "ended_at",
            "ingested_at",
            "author_handle",
            "author_identifier_id",
            "images",
            "loc_accuracy_m",
            "loc_method",
            "loc_reason",
            "access_label",
            "payload_hash",
            "tsv",
            "score",
            "conversation_id",
            "first_thread_no",
            "last_thread_no",
            "is_reference",
            "is_outside",
            "thread_external_id",
            "thread_title",
            "person_handle",
            "display_name",
            "kind",
            "record_kind",
            "record_id",
            "ref_id",
            "record_time",
            "is_current",
            "subject_from",
            "subject_to",
            "subject_from_precision",
            "subject_to_precision",
            "provenance",
            "support",
            "expansion",
            "generated",
            "unsupported",
            "claim",
            "heading_path",
            "char_start",
            "char_end",
        }
        seen = set()
        for f in value:
            import re

            name = f.get("name", "")
            if (
                not re.fullmatch("[a-z][a-z0-9_]*", name)
                or name in seen
                or name in reserved
            ):
                raise ValueError(
                    "invalid or duplicate field name; reserved names: "
                    + ", ".join(sorted(reserved))
                )
            if f.get("type") not in (
                "text",
                "integer",
                "number",
                "boolean",
                "timestamp",
                "json",
            ):
                raise ValueError("unsupported field type")
            seen.add(name)
        return value


class Location(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    accuracy_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Author(BaseModel):
    system: str
    kind: str
    value: str = Field(min_length=1)
    display_name: str | None = None


class Thread(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)
    external_id: str = Field(min_length=1)
    title: str | None = None


class Subject(BaseModel):
    """The period a record is about, when that differs from when it happened.

    Plain field names without aliases: a dump validates back unchanged, which
    the repeated-key ingest path relies on.
    """

    start: datetime | None = None
    end: datetime | None = None
    start_precision: Precision = "exact"
    end_precision: Precision = "exact"

    @field_validator("start", "end")
    @classmethod
    def offset_required(cls, v):
        if v is not None and v.utcoffset() is None:
            raise ValueError("timestamp must include UTC offset")
        return v

    @model_validator(mode="after")
    def interval_valid(self):
        if self.start is None and self.end is None:
            raise ValueError("subject needs a start or an end")
        if self.start and self.end and self.end < self.start:
            raise ValueError("subject end precedes subject start")
        return self


class Message(BaseModel):
    channel: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    thread: Thread
    happened_at: datetime
    ended_at: datetime | None = None
    author: Author | None = None
    content: str = Field(min_length=1)
    fields: dict[str, Any] = Field(default_factory=dict)
    images: list[Any] = Field(default_factory=list)
    location: Location | None = None
    access_label: str | None = None
    subject: Subject | None = None
    payload: Any
    # Earlier or provisional external ids of the same record in the same
    # channel (fingerprint or bridge-id forms). Outside the payload
    # hash, so MSG-2 replay equality is unchanged. Both delivery controls
    # are left out of dumps: a dump is the record itself.
    aliases: list[str] = Field(default_factory=list, max_length=10, exclude=True)
    # Compare-and-set: the current version the writer expects;
    # 0 means the record must not exist yet. Outside the payload hash.
    expected_version: int | None = Field(default=None, ge=0, exclude=True)

    @field_validator("aliases")
    @classmethod
    def aliases_valid(cls, value):
        seen = []
        for alias in value:
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError("aliases must be non-empty strings")
            if len(alias) > 500:
                raise ValueError("an alias is at most 500 characters")
            if alias not in seen:
                seen.append(alias)
        return seen

    @field_validator("happened_at", "ended_at")
    @classmethod
    def offset_required(cls, v):
        if v is not None and v.utcoffset() is None:
            raise ValueError("timestamp must include UTC offset")
        return v

    @field_validator("content")
    @classmethod
    def content_required(cls, v):
        if not v.strip():
            raise ValueError("content must not be empty")
        return v

    @model_validator(mode="after")
    def time_order(self):
        if self.ended_at and self.ended_at < self.happened_at:
            raise ValueError("ended_at precedes happened_at")
        # The record's own id is not an alias of itself.
        self.aliases = [a for a in self.aliases if a != self.external_id]
        return self


def inline_schema(model):
    """Document valid items while retaining per-item ingest rejection semantics."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(node):
        if isinstance(node, list):
            return [expand(item) for item in node]
        if isinstance(node, dict):
            if "$ref" in node:
                return expand(definitions[node["$ref"].rsplit("/", 1)[1]])
            return {key: expand(value) for key, value in node.items()}
        return node

    return expand(schema)


class RejectedMessage(BaseModel):
    index: int
    external_id: str | None = None
    # None only in a replayed response (stored responses keep no text).
    error: str | None = None
    # 'version_conflict' for a failed expected_version, with the current
    # version (0 when the record does not exist).
    reason: str | None = None
    current_version: int | None = None


class AliasedMessage(BaseModel):
    """A message item resolved through aliases."""

    index: int
    status: Literal["aliased", "pair_linked"]
    external_id: str | None = None
    canonical: str | None = None
    link_id: int | None = None


class LinkResult(BaseModel):
    """The outcome of one link of a batch (POST /v1/links/batch and the links
    of POST /v1/ingest/messages)."""

    index: int
    status: Literal["created", "existing", "folded", "candidate", "refused", "error"]
    reason: str | None = None
    link_id: int | None = None
    receipt: dict | None = None


class IngestResult(BaseModel):
    received: int
    inserted: int
    new_versions: int
    unchanged: int
    rejected: list[RejectedMessage]
    aliased: list[AliasedMessage] = Field(default_factory=list)
    link_results: list[LinkResult] = Field(default_factory=list)
    # Per-message receipts, only when the request asked for them.
    receipts: list[dict] | None = None


class Ingest(BaseModel):
    plugin: str
    dry_run: bool = False
    messages: list[dict] = Field(
        max_length=5000, json_schema_extra={"items": inline_schema(Message)}
    )
    links: list[dict] = Field(default_factory=list)
    # Per-message readback receipts in the response (at most 1,000 messages).
    receipts: bool = False


class TrackPoint(Location):
    at: datetime
    source: str

    @field_validator("at")
    @classmethod
    def aware(cls, v):
        if v.utcoffset() is None:
            raise ValueError("timestamp must include UTC offset")
        return v


class TrackBatch(BaseModel):
    points: list[TrackPoint] = Field(max_length=10000)
