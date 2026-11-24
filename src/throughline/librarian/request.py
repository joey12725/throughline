"""The librarian's request envelope (LIB-7).

The asker and the permissions come only from the authenticated token: an
'asker' given in the body is accepted and ignored, and nothing in the request
can widen the token's scope or raise its budgets.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..retrieval.types import Budgets, HardScope

HANDLE = re.compile(r"^rs_[0-9a-f]{32}$")
MAX_STARTING_SETS = 20
MAX_REFERENCES = 200
DEFAULT_CONTROLLER_TURNS = 2
# tl.investigations.request holds at most 64 KiB; long lists are stored as a
# count and a digest beyond STORED_REQUEST_BYTES.
STORED_REQUEST_BYTES = 60000
Reference = Annotated[str, Field(max_length=500)]
Seen = Annotated[str, Field(max_length=64)] | int


class CoverageRequirement(BaseModel):
    """What the delivered evidence must represent.

    must_include: record ids ('m:12') or terms that must appear among the
    delivered records. enumerate: the delivered set must be an enumeration
    (exhaustive), not a ranked sample. Unmet requirements are reported in the
    packet's unresolved_needs, never silently dropped.
    """

    model_config = ConfigDict(extra="forbid")
    must_include: list[Reference] = Field(default_factory=list, max_length=100)
    enumerate: bool = False


class LibrarianRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=4000)
    question: str | None = Field(default=None, max_length=4000)
    context: str | None = Field(default=None, max_length=8000)
    scope: HardScope = Field(default_factory=HardScope)
    starting_sets: list[str] = Field(default_factory=list, max_length=MAX_STARTING_SETS)
    known_references: list[Reference] = Field(
        default_factory=list, max_length=MAX_REFERENCES
    )
    coverage_requirement: CoverageRequirement = Field(
        default_factory=CoverageRequirement
    )
    budgets: Budgets = Field(default_factory=Budgets)
    as_of: datetime | None = None
    known_by: datetime | None = None
    exclude_seen: list[Seen] = Field(default_factory=list, max_length=10000)
    # Accepted and ignored: who asks is decided by the token, never the body.
    asker: Any = Field(default=None, exclude=True)

    @field_validator("starting_sets")
    @classmethod
    def handles(cls, value):
        for handle in value:
            if not HANDLE.fullmatch(str(handle)):
                raise ValueError("starting_sets are result set handles rs_<32 hex>")
        return value

    @field_validator("as_of", "known_by")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("times need a UTC offset")
        return value

    def stored(self):
        """The request as stored with the investigation (no asker). Fields that
        would not fit (longest lists first) are stored as a digest instead."""
        value = self.model_dump(mode="json", exclude={"asker"})
        for name in (
            "exclude_seen",
            "known_references",
            "context",
            "question",
            "coverage_requirement",
        ):
            if len(json.dumps(value, default=str)) <= STORED_REQUEST_BYTES:
                break
            items = value.get(name)
            value[name] = dict(
                count=len(items) if isinstance(items, list) else None,
                stored="digest",
                sha256=hashlib.sha256(
                    json.dumps(items, default=str).encode("utf-8")
                ).hexdigest(),
            )
        return value


def parse(request):
    """LibrarianRequest from a model, a dict or a JSON-like mapping."""
    if isinstance(request, LibrarianRequest):
        return request
    if isinstance(request, dict):
        return LibrarianRequest.model_validate(request)
    raise TypeError("a librarian request is an object with at least a goal")
