"""Shared retrieval library (LIB-1): recall over every record kind.

    from throughline.retrieval import recall, RecallRequest, HardScope
    response = recall(scope, RecallRequest(query="orchard", k=10))

recall() runs planner-chosen legs (retrieval/legs) concurrently under the
caller's scope, fuses them, runs the stages (retrieval/stages) and hydrates
the result through the record registry (retrieval/hydrate), the only
hydrator in the project. HTTP: POST /v1/recall. CLI: throughline lib recall.
"""

from .recall import recall
from .types import (
    RECORD_CONTRACT,
    RECORD_KEYS,
    Candidate,
    HardScope,
    RecallRequest,
    RecallResponse,
)

__all__ = [
    "RECORD_CONTRACT",
    "RECORD_KEYS",
    "Candidate",
    "HardScope",
    "RecallRequest",
    "RecallResponse",
    "recall",
]
