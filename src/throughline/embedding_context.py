"""Contextual embedding: a chunk is embedded together with its neighbours.

A conversation chunk or a page passage often means little alone ("yes, let's
do that Friday"). A contextual model (voyage-context-4) takes the chunks of
one document together and returns one vector per chunk that also carries
what the surrounding chunks say; search still returns the chunk itself.

Here a document is a window over one thread (conversation chunks in order)
or one page revision (passages in order): the texts to embed, preceded by
the chunks just before them as context. Context chunks are sent and paid for
but their vectors are not stored again. A window stays within WINDOW_TOKENS
estimated tokens. The estimate (characters / 4) undercounts: measured against
the voyage-context-4 tokenizer on the live corpus it is 1.5x short overall,
2.1x on email and 3.6x at worst, so 8,000 estimated tokens stay below the
model's 32,000-token document limit.
"""

import os

from . import providers
WINDOW_TOKENS = 8000
# Context is paid for again by every window it leads, so it is kept short: a
# new chunk of a busy thread is sent with about one chunk before it.
DEFAULT_CONTEXT_TOKENS = 2000


def active():
    """Whether the configured embedding model is contextual."""
    try:
        return bool(getattr(providers.get("embedding"), "contextual", False))
    except ValueError:
        return False


def context_tokens():
    """Estimated tokens of preceding text sent with a window
    (THROUGHLINE_EMBEDDING_CONTEXT_TOKENS, default 2000, at most 4000; 0
    sends none)."""
    value = os.getenv("THROUGHLINE_EMBEDDING_CONTEXT_TOKENS")
    try:
        tokens = int(value) if value else DEFAULT_CONTEXT_TOKENS
    except ValueError:
        raise ValueError(
            "THROUGHLINE_EMBEDDING_CONTEXT_TOKENS must be an integer"
        ) from None
    return max(0, min(tokens, WINDOW_TOKENS // 2))


def windows(rows, context, group, budget=WINDOW_TOKENS):
    """Group rows (ordered by group, then position) into windows.

    rows have ``content`` and ``token_estimate``. context(row, tokens) returns
    the rows just before `row` in its document, oldest first, within the
    token budget. A window is ``{context: [rows], rows: [(index, row)]}``:
    consecutive rows of one group within the budget, led by the context of
    its first row.
    """
    result, current = [], None
    lead_budget = context_tokens()
    for index, row in enumerate(rows):
        key, size = group(row), row["token_estimate"]
        if (
            current is None
            or current["group"] != key
            or current["tokens"] + size > budget
        ):
            lead = context(row, min(lead_budget, max(0, budget - size)))
            current = dict(
                group=key,
                context=lead,
                rows=[],
                tokens=sum(c["token_estimate"] for c in lead),
            )
            result.append(current)
        current["rows"].append((index, row))
        current["tokens"] += size
    return result


def texts(window):
    """The chunk texts of a window, context first."""
    return [c["content"] for c in window["context"]] + [
        row["content"] for _, row in window["rows"]
    ]


def embed(found):
    """Embed windows: (vectors in the order of the rows they were built
    from, token_count). embeddings.embed_documents is looked up at call time."""
    from . import embeddings

    documents, tokens = embeddings.embed_documents([texts(w) for w in found])
    vectors = [None] * sum(len(w["rows"]) for w in found)
    for window, document in zip(found, documents):
        own = document[len(window["context"]) :]
        for (index, _), vector in zip(window["rows"], own):
            vectors[index] = vector
    if any(v is None for v in vectors):
        raise providers.ProviderUnavailable("invalid embedding response")
    return vectors, tokens
