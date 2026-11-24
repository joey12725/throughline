"""The librarian: a bounded controller over the shared library that returns
an evidence packet on demand.

    from throughline import librarian
    packet = librarian.investigate(scope, token_row, {"goal": "..."})

Everything it does is a call any agent can make (GET /v1/describe, POST
/v1/plans, POST /v1/sets/{handle}/inspect, POST /v1/packets); see tools.py.
Investigate is never required and never runs by default: callers invoke it through
POST /v1/librarian, api.librarian(...) as the only call of a statement, the
CLI, or an explicit and authorized escalation of POST /v1/recall.
"""

from .brief import BRIEF_TOKEN_LIMIT, BRIEF_VERSION
from .controller import (
    STATUSES,
    TRIGGERS,
    RecursionRefused,
    coverage_needs,
    investigate,
)
from .request import CoverageRequirement, LibrarianRequest
from .tools import NAMES as TOOLS

__all__ = [
    "BRIEF_TOKEN_LIMIT",
    "BRIEF_VERSION",
    "STATUSES",
    "TOOLS",
    "TRIGGERS",
    "CoverageRequirement",
    "LibrarianRequest",
    "RecursionRefused",
    "coverage_needs",
    "investigate",
]
