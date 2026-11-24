"""Request, candidate and response shapes of the shared retrieval library.

HardScope is binding (QRY-18): every leg and every statement that creates a
hit applies it before ranking. hints are soft and never narrow the scope.
A Record is a dict with the RECORD CONTRACT keys plus the retrieval keys
(RECALL_KEYS); every kind (message, link, entity, passage, claim) has exactly
the same keys.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Kinds of message records (tl.channels.record_kind) and of other records.
MESSAGE_KINDS = ("life_event", "reference", "page_revision", "derived")
RECORD_KINDS = MESSAGE_KINDS + ("link", "entity", "passage")

RECORD_CONTRACT = (
    "record_id",
    "kind",
    "ref_id",
    "message_id",
    "version",
    "is_current",
    "channel",
    "external_id",
    "thread_id",
    "thread_no",
    "content",
    "record_time",
    "ended_at",
    "ingested_at",
    "subject_from",
    "subject_to",
    "subject_from_precision",
    "subject_to_precision",
    "person_id",
    "author_handle",
    "lat",
    "lon",
    "loc_reason",
    "is_outside",
    "heading_path",
    "char_start",
    "char_end",
    "claim",
    "generated",
    "unsupported",
    "provenance",
    "support",
    "expansion",
)
RECALL_KEYS = (
    "span",
    "conversation",
    "score",
    "discovery",
    "role",
    "depends_on",
    "groups",
    "tokens",
    # Computed by retrieval.hydrate.evidence_class from contract fields only;
    # never part of the SQL record contract.
    "evidence_class",
    # {is_me, direction, attribution_status, identity_quality,
    # time_precision} of message records from api.attribution (one statement
    # per hydrate call); None for other kinds.
    "attribution",
    # [{asset_id, kind, tags, people, place, taken_at, ocr, caption, width,
    # height, thumb_url, content_url}] of message records with images, from
    # api.assets_of (one statement per recall); None otherwise (docs/images.md).
    "images",
)
ATTRIBUTION_KEYS = (
    "is_me",
    "direction",
    "attribution_status",
    "identity_quality",
    "time_precision",
)
EVIDENCE_CLASSES = ("primary", "normalized", "derived", "inference")
RECORD_KEYS = RECORD_CONTRACT + RECALL_KEYS
ROLES = ("hit", "dependency", "context")
COMPLETENESS = ("exhaustive", "truncated", "ranked_sample")


class Near(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    meters: float = Field(default=100, gt=0, le=20_000_000)


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=500)
    meters: float = Field(default=100, gt=0, le=20_000_000)


class HardScope(BaseModel):
    """Binding constraints. Empty means unconstrained (the asker gate always
    applies). kinds may name message kinds and link, entity, passage, claim;
    'ignored' is not a public kind (use include_ignored)."""

    model_config = ConfigDict(extra="forbid")
    persons: list[int] | None = None
    # between: messages by any listed person (at least two) in the threads
    # where every listed person has a message (api.between); resolved by
    # scope_sql.resolve into a thread and message restriction.
    person_role: Literal["author", "involved", "between"] = "author"
    channels: list[str] | None = None
    kinds: list[str] | None = None
    after: datetime | None = None
    before: datetime | None = None
    # effective: the overlay's effective interval (migration 0042); passage
    # functions, which have no overlay, read it as record.
    time_basis: Literal["record", "subject", "either", "effective"] = "record"
    near: Near | None = None
    place: Place | None = None
    thread_ids: list[int] | None = None
    message_ids: list[int] | None = None
    as_of: datetime | None = None
    known_by: datetime | None = None
    include_reference: bool = False
    include_outside: bool = True
    # Also return records whose effective overlay state is ignored. Rendered
    # into the SQL kinds list only (scope_sql); kinds itself is never rewritten.
    include_ignored: bool = False
    must_terms: list[str] | None = None
    min_score: float | None = None

    @field_validator("kinds")
    @classmethod
    def known_kinds(cls, value):
        for kind in value or []:
            if kind not in RECORD_KINDS:
                raise ValueError("kinds must be among " + ", ".join(RECORD_KINDS))
        return value

    @field_validator("after", "before", "as_of", "known_by", mode="before")
    @classmethod
    def date_only(cls, value):
        # A bare date (2024-03-01) means midnight UTC on that day.
        if isinstance(value, str) and len(value.strip()) == 10:
            return value.strip() + "T00:00:00+00:00"
        return value

    @field_validator("after", "before", "as_of", "known_by")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("times need a UTC offset")
        return value

    @model_validator(mode="after")
    def one_place(self):
        if self.near is not None and self.place is not None:
            raise ValueError("give scope.near or scope.place, not both")
        return self


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deadline_ms: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    model_cost: float | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    controller_turns: int | None = Field(default=None, ge=0)


# Scope keys agents commonly put at the top level of a recall request; they
# mean the same as inside scope (an explicit scope value wins).
SCOPE_ALIASES = {
    "channels": "channels",
    "channel": "channels",
    "persons": "persons",
    "people": "persons",
    "person": "persons",
    "person_ids": "persons",
    "person_role": "person_role",
    "after": "after",
    "since": "after",
    "from": "after",
    "start": "after",
    "before": "before",
    "until": "before",
    "to": "before",
    "end": "before",
    "as_of": "as_of",
    "known_by": "known_by",
    "time_basis": "time_basis",
    "include_outside": "include_outside",
    "kinds": "kinds",
    "thread_ids": "thread_ids",
    "must_terms": "must_terms",
}


def move_scope_aliases(data):
    """Move top-level scope keys (SCOPE_ALIASES) into data["scope"]."""
    if not isinstance(data, dict):
        return data
    moved = {}
    for key in list(data):
        target = SCOPE_ALIASES.get(key)
        if target is None:
            continue
        value = data[key]
        if target in ("channels", "persons", "kinds", "thread_ids", "must_terms"):
            value = value if isinstance(value, list) else [value]
            if target == "persons":
                value = [int(v) if isinstance(v, str) and v.isdigit() else v
                         for v in value]
        moved.setdefault(target, value)
    if not moved:
        return data
    data = {k: v for k, v in data.items() if k not in SCOPE_ALIASES}
    scope = dict(data.get("scope") or {})
    for key, value in moved.items():
        scope.setdefault(key, value)
    data["scope"] = scope
    return data


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def top_level_scope(cls, data):
        return move_scope_aliases(data)
    query: str = Field(default="", max_length=4000)
    scope: HardScope = Field(default_factory=HardScope)
    hints: dict[str, Any] = Field(default_factory=dict)
    k: int = Field(default=20, ge=1, le=1000)
    strategy: Literal["auto", "keyword", "meaning", "fused"] = "auto"
    legs: list[str] | None = None
    ranker: str | None = None
    rrf_k: int | None = Field(default=None, ge=1, le=10000)
    expand: bool = True
    raw_only: bool = False
    budgets: Budgets = Field(default_factory=Budgets)
    save: bool = False
    session: str | None = None
    starting_sets: list[str] | None = None
    exclude_seen: list[str | int] | None = None
    group_by: str | None = None
    exhaustive: bool = False
    trace: bool = False
    # HTTP response size: "auto" keeps a response under about max_chars by
    # excerpting long contents (agent_view.shrink_recall); "full" never does.
    view: Literal["auto", "full"] = "auto"
    # Cut each record's content to the part that best matches the query
    # (agent_view.snippet) before the token budget is applied, so one long
    # page cannot use up the budget of the whole response. None = whole.
    # The HTTP route sets 4000 for view=auto.
    excerpt_chars: int | None = Field(default=None, ge=200, le=1_000_000)
    max_chars: int = Field(default=40000, ge=2000, le=10_000_000)
    # Images: nsfw images (nsfw >= 0.6 or tag nsfw) are left out of image
    # legs and images[] unless include_nsfw. include_images=false leaves
    # images[] None (a caller that attaches images itself, such as find).
    include_nsfw: bool = False
    include_images: bool = True


@dataclass
class Candidate:
    """One record found by a leg or stage, before hydration. Mutable: stages
    rescore, annotate and re-role candidates."""

    record_id: str
    kind: str | None = None
    message_id: int | None = None
    version: int | None = None
    thread_id: int | None = None
    thread_no: int | None = None
    span: dict | None = None  # {first_no, last_no}
    conversation: dict | None = None  # {id, first_no, last_no}
    score: float | None = None
    discovery: list = field(default_factory=list)  # [{leg, rank, score, detail}]
    support: list = field(default_factory=list)  # [{message_id, version, relation}]
    generated: bool | None = None
    unsupported: bool | None = None
    role: str = "hit"
    depends_on: str | None = None
    groups: list = field(default_factory=list)
    tokens: int | None = None
    meta: dict = field(default_factory=dict)

    @property
    def prefix(self):
        return self.record_id.split(":", 1)[0]

    @property
    def ref_id(self):
        return int(self.record_id.split(":", 1)[1])

    def leg_scores(self):
        return [d["score"] for d in self.discovery if d.get("score") is not None]


@dataclass
class RecallResponse:
    records: list
    execution: dict
    result_set: str | None = None
    manifest: Any = None
    dropped: list = field(default_factory=list)
    extras: dict = field(default_factory=dict)

    def to_dict(self):
        return dict(
            records=self.records,
            execution=self.execution,
            result_set=self.result_set,
            manifest=self.manifest,
            dropped=self.dropped,
            extras=self.extras,
        )
