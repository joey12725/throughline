"""Reciprocal rank fusion of leg results (QRY-17).

score(record) = sum over legs of 1 / (k + rank in that leg). Every leg's rank
and score stay in the candidate's discovery list; candidates are deduplicated
by record_id. Ties are broken by reference id, so a fusion of the lexical and
vector legs orders exactly like the legacy api.search_fused.
"""

import copy
import os

DEFAULT_K = 60


def rrf_k(request=None):
    if request is not None and getattr(request, "rrf_k", None):
        return int(request.rrf_k)
    value = os.getenv("THROUGHLINE_RRF_K")
    try:
        k = int(value) if value else DEFAULT_K
    except ValueError:
        k = DEFAULT_K
    return k if k > 0 else DEFAULT_K


def _merge(into, other):
    for entry in other.discovery:
        if entry not in into.discovery:
            into.discovery.append(dict(entry))
    for entry in other.support:
        if entry not in into.support:
            into.support.append(dict(entry))
    for name in (
        "kind",
        "message_id",
        "version",
        "thread_id",
        "thread_no",
        "span",
        "conversation",
    ):
        if getattr(into, name) is None and getattr(other, name) is not None:
            setattr(into, name, copy.deepcopy(getattr(other, name)))
    if other.role == "hit":
        into.role, into.depends_on = "hit", None
    for key, value in other.meta.items():
        into.meta.setdefault(key, copy.deepcopy(value))


def _ref(record_id):
    try:
        return int(record_id.split(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def rrf(results, k=DEFAULT_K):
    """results: {leg: [Candidate in rank order]} (discovery already holds the
    leg's rank). Returns new fused candidates, best first."""
    fused, order = {}, []
    for leg, candidates in results.items():
        for rank, candidate in enumerate(candidates, start=1):
            weight = 1.0 / (k + rank)
            current = fused.get(candidate.record_id)
            if current is None:
                current = copy.deepcopy(candidate)
                current.score = 0.0
                fused[candidate.record_id] = current
                order.append(candidate.record_id)
            else:
                _merge(current, candidate)
            current.score += weight
    ranked = [fused[r] for r in order]
    ranked.sort(key=lambda c: (-c.score, _ref(c.record_id), c.record_id))
    return ranked
