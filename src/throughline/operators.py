"""Operator contract and registry (CST-8, LIB-4).

Every leg, stage, ranker, semantic operation and transform describes itself with
one OperatorSpec. Several specs may implement the same logical operation
(`implements`); the planner picks among them with equivalents() and choose(),
using measured profiles from tl.operator_profiles.
"""

import threading
from dataclasses import asdict, dataclass
from typing import Callable

KINDS = ("leg", "stage", "ranker", "semantic", "transform")
COST_CLASSES = ("free", "embedding", "model")


@dataclass(frozen=True)
class OperatorSpec:
    name: str  # unique id of this implementation, e.g. "lexical.fts"
    implements: str  # logical operation, e.g. "lexical_search"
    implementation: str  # how, e.g. "postgres_fts"
    kind: str
    inputs: tuple = ()
    outputs: tuple = ()
    exact: bool = True
    access: str = "reader"  # which path reads data: reader | none | app
    provenance: str = "preserves"  # preserves | derives | none
    cost_class: str = "free"
    cancellable: bool = True
    cache_key: str | None = None  # what a cached result is keyed by
    version: str = "1"
    estimate: Callable | None = None  # estimate(request) -> cost estimate

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError("operator kind must be one of " + ", ".join(KINDS))
        if self.cost_class not in COST_CLASSES:
            raise ValueError("cost_class must be free, embedding or model")
        if not (self.name and self.implements and self.implementation):
            raise ValueError("operator needs name, implements and implementation")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))

    def to_dict(self):
        result = asdict(self)
        result["inputs"] = list(self.inputs)
        result["outputs"] = list(self.outputs)
        result["estimate"] = self.estimate is not None
        return result


_registry = {}  # name -> spec, in registration order
_lock = threading.Lock()


def register(spec, replace=False):
    """Add a spec. A second spec under the same name is rejected."""
    if not isinstance(spec, OperatorSpec):
        raise TypeError("an OperatorSpec is required")
    with _lock:
        if spec.name in _registry and not replace:
            raise ValueError(f"operator {spec.name} is already registered")
        _registry[spec.name] = spec
    return spec


def unregister(name):
    with _lock:
        return _registry.pop(name, None)


def get(name):
    with _lock:
        return _registry.get(name)


def registered():
    with _lock:
        return list(_registry.values())


def equivalents(implements, exact_only=True):
    """Implementations of one logical operation, in registration order.

    The first registered one is the reference implementation.
    """
    return [
        spec
        for spec in registered()
        if spec.implements == implements and (spec.exact or not exact_only)
    ]


def _profile(profiles, spec):
    if not profiles:
        return None
    if isinstance(profiles, dict):
        return profiles.get(spec.name)
    latest = None
    for row in profiles:
        if row.get("operator") != spec.name:
            continue
        if row.get("version") not in (None, spec.version):
            continue
        old, new = (latest or {}).get("measured_at"), row.get("measured_at")
        if latest is None or old is None or (new is not None and new >= old):
            latest = row
    return latest


def _not_worse(quality, reference):
    """Quality is a number or a {metric: number} mapping, higher is better."""
    if quality is None or reference is None:
        return False
    if isinstance(reference, dict):
        if not isinstance(quality, dict) or not reference:
            return False
        return all(
            isinstance(quality.get(metric), (int, float))
            and isinstance(value, (int, float))
            and quality[metric] >= value
            for metric, value in reference.items()
        )
    if isinstance(quality, dict):
        return False
    return quality >= reference


def choose(implements, profiles=None, allow_approximate=False):
    """The implementation to run for a logical operation.

    profiles: {operator name: {p95_ms, quality}} or rows of tl.operator_profiles.
    The reference (first registered, exact preferred) is returned unless
    another implementation has a recorded quality not worse than the
    reference's recorded quality and a lower measured p95. Approximate
    implementations are considered only with allow_approximate.
    """
    exact = equivalents(implements, exact_only=True)
    candidates = (
        equivalents(implements, exact_only=False) if allow_approximate else exact
    )
    if not candidates:
        return None
    reference = exact[0] if exact else candidates[0]
    measured = _profile(profiles, reference)
    best, best_p95 = reference, (measured or {}).get("p95_ms")
    if not measured or measured.get("quality") is None:
        return reference
    for spec in candidates:
        if spec is reference:
            continue
        profile = _profile(profiles, spec)
        if not profile or profile.get("p95_ms") is None:
            continue
        if not _not_worse(profile.get("quality"), measured["quality"]):
            continue
        if best_p95 is None or profile["p95_ms"] < best_p95:
            best, best_p95 = spec, profile["p95_ms"]
    return best


def describe():
    return [spec.to_dict() for spec in registered()]


def load_profiles(conn):
    """Latest profile per (operator, version) from tl.operator_profiles."""
    return conn.execute(
        "SELECT DISTINCT ON (operator,version) operator,implementation,version,measured_at,p50_ms,p95_ms,cost_per_call,quality,source FROM tl.operator_profiles ORDER BY operator,version,measured_at DESC"
    ).fetchall()


def record_profile(
    conn, spec, p50_ms, p95_ms, quality=None, cost_per_call=None, source="bench"
):
    from psycopg.types.json import Jsonb

    conn.execute(
        "INSERT INTO tl.operator_profiles(operator,implementation,version,p50_ms,p95_ms,cost_per_call,quality,source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            spec.name,
            spec.implementation,
            spec.version,
            p50_ms,
            p95_ms,
            cost_per_call,
            Jsonb(quality) if quality is not None else None,
            source,
        ),
    )


def sync(conn):
    """Upsert the registry into tl.operators (called when the API starts)."""
    specs = registered()
    for spec in specs:
        conn.execute(
            "INSERT INTO tl.operators(name,implements,implementation,kind,exact,cost_class,access,provenance,version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(name) DO UPDATE SET implements=EXCLUDED.implements,implementation=EXCLUDED.implementation,kind=EXCLUDED.kind,exact=EXCLUDED.exact,cost_class=EXCLUDED.cost_class,access=EXCLUDED.access,provenance=EXCLUDED.provenance,version=EXCLUDED.version",
            (
                spec.name,
                spec.implements,
                spec.implementation,
                spec.kind,
                spec.exact,
                spec.cost_class,
                spec.access,
                spec.provenance,
                spec.version,
            ),
        )
    return len(specs)
