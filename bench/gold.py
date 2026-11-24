"""Gold evidence labels.

A question names its evidence by key, never by database id, so a corpus and
its labels are the same on every database: a key is (channel, external_id)
for the current version of a message or (channel, external_id, version) for
one revision. After a load, resolve() turns keys into message ids and bind()
returns the question with ids in place of keys; scorers work on either form.
"""

from datetime import datetime
from typing import Union

from pydantic import BaseModel, Field

Key = Union[tuple[str, str], tuple[str, str, int]]
Ref = Union[int, tuple[str, str], tuple[str, str, int]]


class Group(BaseModel):
    """One piece of evidence; any alternative covers it (BEN-6, BEN-10)."""

    id: str
    required: bool = True
    alternatives: list[Ref]


class Cluster(BaseModel):
    """One experience reported by several records (BEN-13).

    members certainly describe it; ambiguous ones may or may not.
    """

    id: str
    members: list[Ref]
    ambiguous: list[Ref] = Field(default_factory=list)


class Roles(BaseModel):
    """Populations of a pattern question (BEN-14)."""

    supporting: list[Ref] = Field(default_factory=list)
    counterexample: list[Ref] = Field(default_factory=list)
    comparison: list[Ref] = Field(default_factory=list)

    def items(self):
        return [
            (name, getattr(self, name))
            for name in ("supporting", "counterexample", "comparison")
            if getattr(self, name)
        ]


class Question(BaseModel):
    id: str
    text: str
    category: str
    # BEN-4 tag: canary (planted fact) or structure (generated from structure).
    source: str = "canary"
    # Sub-case inside a category, for example the kind of page mutation.
    variant: str | None = None
    # BEN-6: the system may only use data recorded before this instant.
    cutoff: datetime | None = None
    # Historical view asked for: record-time cutoff of the question itself.
    as_of: datetime | None = None
    # Ingest-time cutoff, as the load phase after which the question is asked.
    known_by_phase: int | None = None
    # Filled by bind() from the timestamp the runner recorded after that phase.
    known_by: datetime | None = None
    # Optional window the question is about (subject or record time).
    after: datetime | None = None
    before: datetime | None = None
    groups: list[Group] = Field(default_factory=list)
    helpful: list[Ref] = Field(default_factory=list)
    no_answer: bool = False
    clusters: list[Cluster] = Field(default_factory=list)
    roles: Roles = Field(default_factory=Roles)
    # The search cannot be complete and the system must say so (BEN-20).
    expects_incomplete: bool = False
    # Terms a correct result discloses, for example import_gap.
    expects_disclosure: list[str] = Field(default_factory=list)
    # Records that must not appear anywhere in the result (BEN-21).
    forbidden: list[Ref] = Field(default_factory=list)
    # Capabilities an arm must have for the question to be posed at all.
    needs: list[str] = Field(default_factory=list)
    # Behaviour labels.
    # How many incidents (episodes) the evidence forms, not how many records.
    expects_incidents: int | None = None
    # The record a newest-first answer must start with (newest relevant).
    expects_newest: Ref | None = None
    # The absence statement the system must make when nothing is found:
    # not_found_in_coverage or not_found_in_covered_sources.
    expects_absence_statement: str | None = None
    # Two person keys (handles) whose exchange the question is about.
    dyad: tuple[str, str] | None = None

    def refs(self):
        """Every reference in the labels, in a stable order, without repeats."""
        seen = {}
        for group in self.groups:
            for ref in group.alternatives:
                seen[ref] = None
        for cluster in self.clusters:
            for ref in cluster.members + cluster.ambiguous:
                seen[ref] = None
        for _, members in self.roles.items():
            for ref in members:
                seen[ref] = None
        for ref in self.helpful + self.forbidden:
            seen[ref] = None
        if self.expects_newest is not None:
            seen[self.expects_newest] = None
        return list(seen)

    def required_groups(self):
        return [g for g in self.groups if g.required]

    def gold_refs(self):
        """References that belong to an evidence group."""
        return {ref for g in self.groups for ref in g.alternatives}


def _rows(client, statement):
    response = client.post("/v1/sql", json={"statement": statement, "row_cap": 10000})
    if response.status_code != 200:
        raise RuntimeError(f"cannot resolve gold keys: {response.text}")
    data = response.json()
    return data["rows"], data["truncated"]


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def resolve(client, keys, page=5000):
    """Message id of every key that exists: {key: id}.

    A two-part key is the current version, a three-part key that version.
    Missing keys are left out so the caller decides whether that is an error.
    """
    keys = [tuple(k) for k in keys]
    wanted = {}
    for key in keys:
        wanted.setdefault(key[0], set()).add(key[1])
    found = {}
    for channel in sorted(wanted):
        last = 0
        while True:
            rows, truncated = _rows(
                client,
                "SELECT id,external_id,version,superseded_by FROM api.message_versions"
                f" WHERE channel={_literal(channel)} AND id>{last} ORDER BY id LIMIT {page}",
            )
            for row in rows:
                if row["external_id"] not in wanted[channel]:
                    continue
                found[(channel, row["external_id"], row["version"])] = row["id"]
                if row["superseded_by"] is None:
                    found[(channel, row["external_id"])] = row["id"]
            if not rows or (len(rows) < page and not truncated):
                break
            last = rows[-1]["id"]
    return {key: found[key] for key in keys if key in found}


def bind(question, ids, phase_times=None, strict=True):
    """The question with every key replaced by its message id."""
    missing = []

    def one(ref):
        if isinstance(ref, int):
            return ref
        ref = tuple(ref)
        if ref not in ids:
            missing.append(ref)
            return None
        return ids[ref]

    def many(refs):
        return [i for i in map(one, refs) if i is not None]

    update = dict(
        groups=[
            Group(id=g.id, required=g.required, alternatives=many(g.alternatives))
            for g in question.groups
        ],
        clusters=[
            Cluster(id=c.id, members=many(c.members), ambiguous=many(c.ambiguous))
            for c in question.clusters
        ],
        roles=Roles(**{name: many(refs) for name, refs in question.roles.items()}),
        helpful=many(question.helpful),
        forbidden=many(question.forbidden),
    )
    if question.expects_newest is not None:
        update["expects_newest"] = one(question.expects_newest)
    if question.known_by_phase is not None and phase_times:
        update["known_by"] = phase_times.get(question.known_by_phase)
    if missing and strict:
        raise KeyError(f"question {question.id}: unresolved gold keys {missing}")
    return question.model_copy(update=update)
