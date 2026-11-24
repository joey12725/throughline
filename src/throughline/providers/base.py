"""Provider interfaces: what a model implementation must offer, local or remote.

Five kinds exist: embedding, rerank, semantic, controller and assistant (the
query helper of the web interface, a tool-calling chat model). Every provider
has ``name`` (which implementation), ``version`` (changes whenever its output
for the same input may change; part of every cache key), ``local`` (true when
nothing leaves the host and nothing is charged), ``model`` and ``estimate``.
"""

import math
import re
import threading
from dataclasses import asdict, dataclass

# Operations of SemanticModel.apply. Only registered operations run. OPERATIONS
# names the operations built into every implementation (the interface's own
# tuple, unchanged since 'match' shipped); register_operation() adds more at
# import time of the package that owns them, without editing provider files.
# The reserved names are part of the interface already, so adding one later
# changes no signature, cache key shape or ledger column; they stay refused.
OPERATIONS = ("match",)
RESERVED_OPERATIONS = ("extract", "compare", "relate", "summarize")
MATCH_OUTPUTS = ("match", "nonmatch", "uncertain")
MATCH_RULE = (
    "Operation 'match': output is 'match' when the item satisfies the condition, "
    "'nonmatch' when it does not, and 'uncertain' when the text does not decide it."
)
KINDS = ("embedding", "rerank", "semantic", "controller", "assistant")


class ProviderUnavailable(ValueError):
    """No usable provider: not configured, unreachable, invalid or over budget."""


class EgressDenied(ProviderUnavailable):
    """The endpoint is not loopback and not in THROUGHLINE_EGRESS_ALLOW."""


@dataclass(frozen=True)
class Usage:
    """What one provider call consumed."""

    model: str
    tokens: int = 0
    cost: float = 0.0
    calls: int = 1
    ms: float = 0.0

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Estimate:
    """What a provider call is expected to consume, known before it runs."""

    purpose: str
    provider: str
    model: str
    calls: int = 1
    tokens: int = 0
    cost: float = 0.0
    local: bool = True

    def to_dict(self):
        return asdict(self)


def text_tokens(*texts):
    """The token estimate used everywhere a provider reports none."""
    return sum((len(t or "") + 3) // 4 for t in texts)


OPERATION_NAME = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
# Types of the extra result fields an operation may declare (besides id,
# output, evidence and note).
FIELD_TYPES = ("number", "unit", "integer", "string")


@dataclass(frozen=True)
class Operation:
    """A registered semantic operation.

    outputs: the allowed values of each result's ``output``.
    local: ``fn(instruction, items, output_schema) -> results`` used by the
    deterministic local model (None: no local implementation).
    prompt: the operation's rule, appended to the semantic system prompt of
    remote models. fields: extra result fields {name: type} (FIELD_TYPES;
    'unit' is a number from 0 to 1) that remote answers may carry.
    strict: a remote answer whose output (or a declared field) is invalid
    fails the whole call (ProviderUnavailable) instead of becoming
    'uncertain'. 'match' keeps its original lenient rule.
    """

    name: str
    outputs: tuple
    local: object = None
    prompt: str | None = None
    fields: tuple = ()
    strict: bool = True

    def field_types(self):
        return dict(self.fields)


_registry = {}
_registry_lock = threading.Lock()


def register_operation(
    name, outputs, *, local=None, prompt=None, fields=None, strict=True
):
    """Register a semantic operation; returns its Operation.

    Registering a name again with the same outputs, prompt and fields replaces
    its local implementation (a module reload); anything else is a conflict.
    Reserved names (extract, compare, relate, summarize) are refused.
    """
    if not isinstance(name, str) or not OPERATION_NAME.fullmatch(name):
        raise ValueError("operation name must be lower-case letters, digits, _")
    if name in RESERVED_OPERATIONS:
        raise ValueError(f"semantic operation '{name}' is reserved")
    outputs = tuple(outputs or ())
    if not outputs or not all(isinstance(o, str) and o for o in outputs):
        raise ValueError("an operation needs at least one output value")
    if len(set(outputs)) != len(outputs):
        raise ValueError("operation outputs must be distinct")
    if local is not None and not callable(local):
        raise ValueError("local must be callable")
    if prompt is not None and not isinstance(prompt, str):
        raise ValueError("prompt must be text")
    declared = []
    for key, kind in sorted((fields or {}).items()):
        if key in ("id", "output", "evidence", "note") or kind not in FIELD_TYPES:
            raise ValueError("fields map extra names to " + ", ".join(FIELD_TYPES))
        declared.append((key, kind))
    operation = Operation(name, outputs, local, prompt, tuple(declared), bool(strict))
    with _registry_lock:
        old = _registry.get(name)
        if old is not None and (old.outputs, old.prompt, old.fields, old.strict) != (
            operation.outputs,
            operation.prompt,
            operation.fields,
            operation.strict,
        ):
            raise ValueError(f"semantic operation '{name}' is already registered")
        _registry[name] = operation
    return operation


def registered_operations():
    """Names of every registered operation, in registration order."""
    with _registry_lock:
        return tuple(_registry)


def check_operation(operation):
    if operation in RESERVED_OPERATIONS:
        raise ValueError(
            f"semantic operation '{operation}' is reserved and not registered"
        )
    with _registry_lock:
        known = operation in _registry
    if not known:
        raise ValueError(f"semantic operation '{operation}' is not registered")


def get_operation(operation):
    """The registered Operation of a name (ValueError when not registered)."""
    check_operation(operation)
    with _registry_lock:
        return _registry[operation]


def valid_field(kind, value):
    """Whether a declared extra field value has its type."""
    if isinstance(value, bool):
        return False
    if kind == "string":
        return isinstance(value, str) and len(value) <= 500
    if kind == "integer":
        return isinstance(value, int)
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    return kind == "number" or 0 <= value <= 1


# 'match' is built in: the local rule model and the HTTP model implement it
# themselves (lenient: an invalid remote answer becomes 'uncertain').
register_operation("match", MATCH_OUTPUTS, prompt=MATCH_RULE, strict=False)


class Provider:
    name = "none"
    version = "0"
    local = True
    model = None
    # Price of one million tokens; zero for local implementations.
    price_per_million = 0.0
    # Set by remote providers: true when the endpoint is not loopback.
    excerpts_leave_host = False

    def _estimate(self, purpose, tokens, calls=1):
        tokens = int(tokens)
        return Estimate(
            purpose=purpose,
            provider=self.name,
            model=self.model,
            calls=calls,
            tokens=tokens,
            cost=0.0 if self.local else tokens * self.price_per_million / 1000000,
            local=self.local,
        )

    def _usage(self, tokens, calls=1, ms=0.0, model=None):
        tokens = int(tokens)
        return Usage(
            model=model or self.model,
            tokens=tokens,
            cost=0.0 if self.local else tokens * self.price_per_million / 1000000,
            calls=calls,
            ms=ms,
        )


class Embedder(Provider):
    """embed(texts, model=None, dimensions=None) -> (vectors, Usage).

    asymmetric: the model embeds a query and a document differently, so
    embed() takes ``input_type`` ('document' or 'query') and callers say which.
    contextual: the model embeds a chunk together with the other chunks of
    its document: embed_documents(documents) takes lists of chunks and returns
    one list of vectors per document. batch: a throughline.providers.batch
    client for the vendor's asynchronous (cheaper, slower) batch interface,
    or None. batch_discount: the share of the price a batch request costs.
    """

    dimensions = None
    asymmetric = False
    contextual = False
    batch_discount = 1.0

    def embed(self, texts, model=None, dimensions=None):
        raise NotImplementedError

    def embed_documents(self, documents, model=None, dimensions=None):
        raise NotImplementedError

    def batch_client(self):
        return None

    def estimate(self, texts, purpose="embedding"):
        return self._estimate(purpose, text_tokens(*texts))


class Reranker(Provider):
    """score(query, passages) -> (scores, Usage): one float per passage, in order."""

    def score(self, query, passages):
        raise NotImplementedError

    def rank(self, query, passages):
        """Passage indexes, best first; equal scores keep their input order."""
        scores, usage = self.score(query, passages)
        return sorted(range(len(scores)), key=lambda i: -scores[i]), usage

    def estimate(self, query, passages, purpose="rerank"):
        return self._estimate(
            purpose, text_tokens(*passages) + len(passages) * text_tokens(query)
        )


class SemanticModel(Provider):
    """apply(operation, instruction, items, output_schema) -> (results, Usage).

    items are ``{id, text}``. results are ``{id, output, evidence, note}`` in
    the order of the items, where evidence is a list of ``[start, end]``
    character offsets into that item's text. For operation 'match' the output
    is one of match, nonmatch, uncertain; a registered operation states its
    own outputs (register_operation) and may add declared fields.
    ``operations`` names the built-in operations; registered_operations()
    lists every runnable one.
    """

    operations = OPERATIONS
    # Items sent in one request; estimates count one call per batch.
    batch_size = 20

    def apply(self, operation, instruction, items, output_schema=None):
        raise NotImplementedError

    def estimate(self, operation, instruction, items, purpose=None):
        check_operation(operation)
        items = list(items)
        calls = max(1, -(-len(items) // self.batch_size))
        return self._estimate(
            purpose or operation,
            text_tokens(*(i.get("text") for i in items))
            + calls * text_tokens(instruction),
            calls,
        )


class Controller(Provider):
    """turn(brief, state) -> (action, Usage); action is {plan} or {deliver}."""

    def turn(self, brief, state):
        raise NotImplementedError

    def estimate(self, brief, state=None, turns=1, purpose="controller"):
        import json

        return self._estimate(
            purpose,
            turns * text_tokens(brief, json.dumps(state or {}, default=str)),
            turns,
        )


class Assistant(Provider):
    """turn(system, tools, messages) -> (reply, Usage): one model turn of the
    query helper's bounded tool loop (throughline.assistant).

    system: a list of text parts (the stable ones first, so a provider can
    cache the prefix). tools: [{name, description, parameters}] with a JSON
    schema. messages, in a provider-neutral form:
      {"role": "user", "text": str}
      {"role": "assistant", "native": <the provider's own message, passed
       back unchanged>, "text": str, "calls": [...]}
      {"role": "tool", "results": [{"id", "name", "content", "is_error"}]}
    reply: {text, calls: [{id, name, input}], stop, detail, native,
    tokens_in, tokens_out}; stop is tool_use | end | max_tokens | refusal |
    pause | other. scripted: true for the local implementation, which runs no
    model (the helper answers deterministically instead).
    """

    scripted = False
    # USD per million input and output tokens.
    price_in = 0.0
    price_out = 0.0
    # Output tokens one turn is expected to produce (for estimates).
    expected_output = 2000

    def turn(self, system, tools, messages, max_tokens=None):
        """max_tokens: this turn's output cap (None: the provider's own)."""
        raise NotImplementedError

    def estimate(
        self, system, tools, messages, purpose="assistant", output_tokens=None
    ):
        """The turn's cost for its input and `output_tokens` output tokens
        (default: the provider's max_tokens when it has one, else
        expected_output), so the estimate is the turn's worst case."""
        import json

        tokens = text_tokens(
            *(system if isinstance(system, (list, tuple)) else [system]),
            json.dumps(tools, default=str),
            json.dumps(messages, default=str),
        )
        if output_tokens is None:
            output_tokens = getattr(self, "max_tokens", None) or self.expected_output
        cost = 0.0
        if not self.local:
            cost = (tokens * self.price_in + output_tokens * self.price_out) / 1000000
        return Estimate(
            purpose=purpose,
            provider=self.name,
            model=self.model,
            calls=1,
            tokens=tokens + output_tokens,
            cost=cost,
            local=self.local,
        )
