"""Deterministic local providers: pure Python, no network, no cost.

They are the offline defaults for rerank, semantic, controller and assistant,
and the
opt-in embedder (THROUGHLINE_EMBEDDING_PROVIDER=local). They are lexical: the
same input always gives the same output on every machine, which is what the
tests and the public benchmark rely on. They are not a substitute for a real
model's quality.
"""

import hashlib
import json
import math
import os
import re
import time

from .base import (
    MATCH_OUTPUTS,
    Assistant,
    Controller,
    Embedder,
    ProviderUnavailable,
    Reranker,
    SemanticModel,
    check_operation,
    get_operation,
    text_tokens,
)

WORD = re.compile(r"\w+")
# Words that carry no condition by themselves; a match never depends on them.
STOPWORDS = frozenset(
    "a an and are as at be been but by did do does for from had has have how i if in "
    "into is it its me my no not of on or our so than that the their them then there "
    "these they this those to was we were what when where which who whom why will with "
    "would you your about any all mention mentions mentioned mentioning say says said "
    "contain contains containing message messages text record records".split()
)

# The fixed plan of LocalPlanController. Data, not code: each entry is the
# action of one turn; the last entry repeats for every later turn. A string
# value "$name" is replaced by state[name] (missing names become None). The
# 'ops' shorthand {id, op, ...args} is read by the librarian as plan nodes
# {id, op, args}; recall always saves its result set.
PLAN = (
    {
        "plan": {
            "ops": [
                {"id": "found", "op": "recall", "query": "$goal"},
            ]
        }
    },
    {
        "deliver": {
            "selection": "$last_handle",
            "notes": [],
            "unresolved": [],
        }
    },
)


def _synonym_source():
    value = os.getenv("THROUGHLINE_LOCAL_SYNONYMS", "").strip()
    if not value:
        return None
    if value[0] in "{[":
        return json.loads(value)
    with open(value, encoding="utf-8") as handle:
        return json.load(handle)


_synonym_cache = {}


def synonyms():
    """Token -> canonical token, from THROUGHLINE_LOCAL_SYNONYMS.

    The value is a path to a JSON file or inline JSON, in any of three shapes:
    {"word": "canonical"}, {"canonical": ["word", ...]} or [["word", ...], ...]
    (the first word of a group is its canonical form).
    """
    key = os.getenv("THROUGHLINE_LOCAL_SYNONYMS", "").strip()
    if key not in _synonym_cache:
        mapping = {}
        try:
            source = _synonym_source()
        except (OSError, ValueError):
            raise ValueError(
                "THROUGHLINE_LOCAL_SYNONYMS is not a readable JSON synonym map"
            ) from None
        if isinstance(source, dict):
            for name, value in source.items():
                if isinstance(value, list):
                    for word in value:
                        mapping[str(word).lower()] = str(name).lower()
                elif isinstance(value, str):
                    mapping[str(name).lower()] = value.lower()
        elif isinstance(source, list):
            for group in source:
                if isinstance(group, list) and group:
                    for word in group[1:]:
                        mapping[str(word).lower()] = str(group[0]).lower()
        elif source is not None:
            raise ValueError("THROUGHLINE_LOCAL_SYNONYMS must be a JSON object or list")
        _synonym_cache.clear()
        _synonym_cache[key] = mapping
    return _synonym_cache[key]


def synonym_tag():
    """'' without synonyms, else '+syn-<digest>' of the active map.

    The local providers' output depends on the synonym map, so it is part of
    their version and model name: cache keys, and the embedding model under
    which conversation vectors are stored, change when the map changes.
    """
    mapping = synonyms()
    if not mapping:
        return ""
    digest = hashlib.sha256(
        json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "+syn-" + digest[:12]


class _Local:
    """version and model of a local provider: its base name plus synonym_tag()."""

    base_version = "local"
    local = True

    @property
    def version(self):
        return self.base_version + synonym_tag()

    @property
    def model(self):
        return self.version


def tokens(text):
    """Lower-cased \\w+ tokens in order, each mapped to its canonical synonym."""
    mapping = synonyms()
    return [mapping.get(t, t) for t in WORD.findall((text or "").lower())]


def spans(text):
    """(canonical token, start, end) for every token of the text."""
    mapping = synonyms()
    return [
        (mapping.get(m.group().lower(), m.group().lower()), m.start(), m.end())
        for m in WORD.finditer(text or "")
    ]


def bigrams(items):
    return [a + " " + b for a, b in zip(items, items[1:])]


class LocalHashEmbedder(_Local, Embedder):
    """Signed feature hashing of unigrams and bigrams, L2-normalised."""

    name = "local"
    base_version = "local-hash-v1"

    def __init__(self, dimensions=None):
        dimensions = int(
            dimensions or os.getenv("THROUGHLINE_EMBEDDING_DIMENSIONS") or 256
        )
        if not 1 <= dimensions <= 16000:
            raise ValueError("embedding dimensions must be 1..16000")
        self.dimensions = dimensions

    def _vector(self, text, dimensions):
        words = tokens(text)
        values = [0.0] * dimensions
        for feature in words + bigrams(words):
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:8], "big") % dimensions
            values[bucket] += 1.0 if digest[8] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in values))
        if not norm:
            # No tokens, or every feature cancelled: a fixed unit vector keeps
            # cosine distance defined.
            values[0] = norm = 1.0
        return [v / norm for v in values]

    def embed(self, texts, model=None, dimensions=None, input_type=None):
        started = time.perf_counter()
        dimensions = int(dimensions or self.dimensions)
        vectors = [self._vector(text, dimensions) for text in texts]
        return vectors, self._usage(
            text_tokens(*texts), ms=(time.perf_counter() - started) * 1000
        )


class LocalLexicalReranker(_Local, Reranker):
    """Share of distinct query tokens present, plus 0.1 per matched query bigram."""

    name = "local"
    base_version = "local-lexical-v1"

    def score(self, query, passages):
        started = time.perf_counter()
        words = tokens(query)
        wanted = list(dict.fromkeys(words))
        pairs = set(bigrams(words))
        scores = []
        for passage in passages:
            found = tokens(passage)
            present = set(found)
            share = (
                sum(1 for w in wanted if w in present) / len(wanted) if wanted else 0.0
            )
            scores.append(share + 0.1 * len(pairs & set(bigrams(found))))
        return scores, self._usage(
            text_tokens(*passages), ms=(time.perf_counter() - started) * 1000
        )


class LocalRuleModel(_Local, SemanticModel):
    """'match' by content tokens: all present, none present, or some.

    Any other registered operation runs its registered local implementation
    (register_operation(..., local=fn)); its outputs are checked against the
    registered values, so a faulty rule fails loudly instead of writing.
    """

    name = "local"
    base_version = "local-rule-v1"
    batch_size = 1000

    def apply(self, operation, instruction, items, output_schema=None):
        check_operation(operation)
        if operation != "match":
            return self._registered(operation, instruction, items, output_schema)
        started = time.perf_counter()
        items = list(items)
        wanted = list(
            dict.fromkeys(t for t in tokens(instruction) if t not in STOPWORDS)
        )
        results = []
        for item in items:
            first = {}
            for token, start, end in spans(item.get("text")):
                first.setdefault(token, [start, end])
            found = [t for t in wanted if t in first]
            if not wanted:
                output, note = "uncertain", "the condition has no content words"
            elif len(found) == len(wanted):
                output, note = "match", "every content word of the condition is present"
            elif not found:
                output, note = "nonmatch", "no content word of the condition is present"
            else:
                output = "uncertain"
                note = f"{len(found)} of {len(wanted)} content words are present"
            assert output in MATCH_OUTPUTS
            results.append(
                dict(
                    id=item.get("id"),
                    output=output,
                    evidence=[first[t] for t in found] if output == "match" else [],
                    note=note,
                )
            )
        return results, self._usage(
            text_tokens(*(i.get("text") for i in items)),
            ms=(time.perf_counter() - started) * 1000,
        )

    def _registered(self, operation, instruction, items, output_schema):
        spec = get_operation(operation)
        if spec.local is None:
            raise ProviderUnavailable(
                f"semantic operation '{operation}' has no local implementation"
            )
        started = time.perf_counter()
        items = list(items)
        rows = list(spec.local(instruction, items, output_schema) or [])
        by_id = {}
        for row in rows:
            if isinstance(row, dict):
                by_id.setdefault(json.dumps(row.get("id"), default=str), row)
        results = []
        for item in items:
            row = by_id.get(json.dumps(item.get("id"), default=str))
            if row is None or row.get("output") not in spec.outputs:
                raise ValueError(
                    f"local '{operation}' gave no valid output for an item"
                )
            results.append(dict(row, id=item.get("id")))
        return results, self._usage(
            text_tokens(*(i.get("text") for i in items)),
            ms=(time.perf_counter() - started) * 1000,
        )


def _fill(value, state):
    if isinstance(value, str) and value.startswith("$"):
        return state.get(value[1:])
    if isinstance(value, dict):
        return {k: _fill(v, state) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fill(v, state) for v in value]
    return value


class LocalPlanController(Controller):
    """Plays PLAN: turn n (state['turn'], from 0) returns entry n, filled from state."""

    name = "local"
    version = "local-plan-v1"
    model = "local-plan-v1"
    local = True

    def turn(self, brief, state):
        started = time.perf_counter()
        state = state or {}
        try:
            number = max(0, int(state.get("turn") or 0))
        except (TypeError, ValueError):
            number = 0
        action = _fill(PLAN[min(number, len(PLAN) - 1)], state)
        return action, self._usage(
            text_tokens(brief), ms=(time.perf_counter() - started) * 1000
        )


class LocalAssistant(Assistant):
    """The query helper without a model: nothing is sent anywhere. The helper
    (throughline.assistant.local) answers deterministically from the
    catalogue, the recipes and the rule table instead of calling turn()."""

    name = "local"
    version = "local-finder-v1"
    model = "local-finder-v1"
    local = True
    scripted = True

    def turn(self, system, tools, messages, max_tokens=None):
        raise ProviderUnavailable("the local assistant runs no model")
