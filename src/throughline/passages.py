"""Passage store for page revisions (BRN-6, EMB-2).

A page is one message per revision. Its full text is read from the stored raw
payload through the channel's passage_text_paths (the message content when the
channel declares none), so text beyond any cut of the content is still split.
split() turns that text into heading-aware passages whose content is always
text[char_start:char_end]; build() stores them for every page_revision version
that has none; embed_once() gives every distinct passage text one vector per
model through throughline.embeddings.embed, looked up at call time.
"""

import hashlib
import json
import logging
import os
import re

from .db import connect

log = logging.getLogger("throughline.passages")

# Session lock of the passage embedding batch (the conversation worker uses
# 71824009); it keeps two workers from buying the same texts twice.
EMBED_LOCK = 71824037
# First key of the transaction lock of one revision being split (the second
# is hashtext of its id); see tl.try_lock_passage_build.
BUILD_LOCK = 71824038

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*(?:[ \t]#+[ \t]*)?$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_PARAGRAPH = re.compile(r"\n[ \t]*\n")
_SENTENCE = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n")


# --- full text ----------------------------------------------------------------


def _pointer(document, pointer):
    """Resolve one JSON pointer (RFC 6901); None when it does not exist."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return None
    value = document
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if key not in value:
                return None
            value = value[key]
        elif isinstance(value, list):
            if not key.isdigit() or int(key) >= len(value):
                return None
            value = value[int(key)]
        else:
            return None
    return value


def _render(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(t for t in (_render(v) for v in value) if t.strip())
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def full_text(payload, paths, content):
    """The text passages are split from.

    paths is the channel's passage_text_paths: entries of '|'-separated JSON
    pointers; the first pointer of an entry with a non-blank value wins and
    the entries are joined by a blank line. Without paths, without a payload or
    when nothing is found, the message content is the text.
    """
    if not paths or payload is None:
        return content
    parts = []
    for entry in paths:
        for pointer in str(entry).split("|"):
            text = _render(_pointer(payload, pointer))
            if text.strip():
                parts.append(text)
                break
    text = "\n\n".join(parts)
    return text if text.strip() else content


# --- splitting ----------------------------------------------------------------


def tokens(text):
    """Token estimate used for passages (the providers' estimate)."""
    return (len(text) + 3) // 4


def _trim(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _sections(text):
    """(heading_path, start, end) of the preamble and every markdown section.

    A section runs from its heading line to the next heading; headings inside
    fenced code blocks are text."""
    sections = []
    stack = []
    path = []
    start = 0
    fenced = False
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if _FENCE.match(body):
            fenced = not fenced
        elif not fenced:
            match = _HEADING.match(body)
            if match:
                if offset > start:
                    sections.append((path, start, offset))
                level = len(match.group(1))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, match.group(2).strip() or match.group(1)))
                path = [title for _, title in stack]
                start = offset
        offset += len(line)
    if offset > start or not sections:
        sections.append((path, start, len(text)))
    return sections


def _pieces(text, start, end, level, max_chars):
    """Trimmed sub-spans of [start, end) at one split level: paragraphs (0),
    sentences and lines (1), hard limit (2)."""
    if level >= 2:
        spans = []
        position = start
        while position < end:
            stop = min(position + max_chars, end)
            if stop < end:
                # Prefer the last whitespace in the second half of the window.
                cut = max(
                    (text.rfind(ch, position + max_chars // 2, stop) for ch in " \n\t"),
                    default=-1,
                )
                if cut > position:
                    stop = cut
            s, e = _trim(text, position, stop)
            if e > s:
                spans.append((s, e))
            position = stop
        return spans
    pattern = _PARAGRAPH if level == 0 else _SENTENCE
    spans = []
    position = start
    for match in pattern.finditer(text, start, end):
        s, e = _trim(text, position, match.start() if level == 0 else match.end())
        if e > s:
            spans.append((s, e))
        position = match.end()
    s, e = _trim(text, position, end)
    if e > s:
        spans.append((s, e))
    return spans


def _pack(text, start, end, level, max_tokens):
    """Spans of at most max_tokens covering [start, end), split as coarsely
    as possible and packed greedily."""
    max_chars = max_tokens * 4
    result = []
    current = None
    for s, e in _pieces(text, start, end, level, max_chars):
        if tokens(text[s:e]) > max_tokens:
            if current:
                result.append(current)
                current = None
            result.extend(_pack(text, s, e, level + 1, max_tokens))
        elif current and tokens(text[current[0] : e]) <= max_tokens:
            current = (current[0], e)
        else:
            if current:
                result.append(current)
            current = (s, e)
    if current:
        result.append(current)
    return result


def _common(a, b):
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return list(a[:n])


def _related(a, b):
    """Same section, siblings, or parent and child."""
    return a == b or a[:-1] == b[:-1] or b[:-1] == a or a[:-1] == b


def split(text, max_tokens=400, min_tokens=40):
    """Split text into passages, deterministically.

    Markdown headings start sections and give each passage its heading_path.
    A section above max_tokens is split at paragraphs, then sentences and
    lines, then at the hard limit, and its pieces are packed greedily. Then
    adjacent passages of which one is small (below min_tokens) are merged
    while the result stays within max_tokens: pieces of one split section with
    each other, whole sections with a sibling, parent or child section,
    keeping the common heading path. Every passage has non-blank content ==
    text[char_start:char_end].
    """
    if max_tokens < 1 or min_tokens < 0:
        raise ValueError("max_tokens must be positive and min_tokens not negative")
    # (heading_path, start, end, section number or None for a whole section)
    spans = []
    for number, (path, start, end) in enumerate(_sections(text)):
        s, e = _trim(text, start, end)
        if e <= s:
            continue
        if tokens(text[s:e]) <= max_tokens:
            spans.append((list(path), s, e, None))
        else:
            spans.extend(
                (list(path), a, b, number) for a, b in _pack(text, s, e, 0, max_tokens)
            )
    merged = []
    for path, s, e, piece_of in spans:
        if merged:
            last_path, last_s, last_e, last_piece_of = merged[-1]
            small = (
                tokens(text[last_s:last_e]) < min_tokens
                or tokens(text[s:e]) < min_tokens
            )
            # Pieces of a split section merge only with each other; whole
            # sections merge with a related whole section.
            if piece_of is not None or last_piece_of is not None:
                allowed = piece_of == last_piece_of
            else:
                allowed = _related(last_path, path)
            if small and allowed and tokens(text[last_s:e]) <= max_tokens:
                merged[-1] = (_common(last_path, path), last_s, e, last_piece_of)
                continue
        merged.append((path, s, e, piece_of))
    result = []
    for ordinal, (path, s, e, _) in enumerate(merged):
        content = text[s:e]
        result.append(
            dict(
                ordinal=ordinal,
                heading_path=path,
                char_start=s,
                char_end=e,
                content=content,
                text_hash=text_hash(content),
                token_estimate=max(1, tokens(content)),
            )
        )
    return result


def text_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def limits():
    """(max_tokens, min_tokens) from THROUGHLINE_PASSAGE_MAX_TOKENS and
    THROUGHLINE_PASSAGE_MIN_TOKENS (defaults 400 and 40)."""

    def number(name, default):
        value = os.getenv(name)
        try:
            return int(value) if value else default
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None

    max_tokens = number("THROUGHLINE_PASSAGE_MAX_TOKENS", 400)
    min_tokens = number("THROUGHLINE_PASSAGE_MIN_TOKENS", 40)
    if max_tokens < 1 or not 0 <= min_tokens <= max_tokens:
        raise ValueError("passage token limits must satisfy 0 <= min <= max, max >= 1")
    return max_tokens, min_tokens


# --- store ----------------------------------------------------------------------

_UNBUILT = (
    "FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id AND c.record_kind='page_revision' "
    "WHERE NOT EXISTS(SELECT 1 FROM tl.passages p WHERE p.message_id=m.id)"
)


def build(conn, limit=200):
    """Store passages for up to `limit` page_revision versions that have none,
    current revisions first. Returns the number of revisions built here.

    Idempotent and safe to run concurrently: a revision is written by one run
    only (a transaction lock per revision; one already locked or already built
    is skipped), so its passages always come from a single split and a
    revision built by another run at the same time is counted only there.
    The caller owns the transaction."""
    max_tokens, min_tokens = limits()
    rows = conn.execute(
        "SELECT m.id,m.channel_id,m.external_id,m.version,m.content,c.passage_text_paths paths,"
        "(SELECT r.payload FROM tl.raw_payloads r WHERE r.message_id=m.id) payload "
        + _UNBUILT
        + " ORDER BY m.superseded_by IS NULL DESC,m.id LIMIT %s",
        (limit,),
    ).fetchall()
    built = 0
    for row in rows:
        text = full_text(row["payload"], row["paths"], row["content"])
        parts = split(text, max_tokens, min_tokens)
        if not parts:
            continue
        # One builder per revision: a revision another run is building right
        # now (lock held) or has built (rows committed) is skipped, so two
        # runs never interleave their ordinals (they could differ in limits)
        # and never wait on each other.
        if not conn.execute(
            "SELECT tl.try_lock_passage_build(%s) ok", (row["id"],)
        ).fetchone()["ok"]:
            continue
        cursor = conn.execute(
            "INSERT INTO tl.passages(message_id,channel_id,external_id,version,ordinal,heading_path,char_start,char_end,content,text_hash,token_estimate) "
            "SELECT %s,%s,%s,%s,x.ordinal,ARRAY(SELECT jsonb_array_elements_text(x.heading_path)),x.char_start,x.char_end,x.content,x.text_hash,x.token_estimate "
            "FROM jsonb_to_recordset(%s::jsonb) x(ordinal int,heading_path jsonb,char_start int,char_end int,content text,text_hash text,token_estimate int) "
            "WHERE NOT EXISTS(SELECT 1 FROM tl.passages p WHERE p.message_id=%s) "
            "ON CONFLICT (message_id,ordinal) DO NOTHING",
            (
                row["id"],
                row["channel_id"],
                row["external_id"],
                row["version"],
                json.dumps(parts),
                row["id"],
            ),
        )
        if cursor.rowcount:
            built += 1
    return built


def history():
    """Whether passages of superseded revisions are embedded too."""
    return os.getenv("THROUGHLINE_PASSAGE_EMBED_HISTORY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _unembedded(history_too, unreserved=False):
    """Passages of embedded channels whose text has no vector of the model
    (one %s). unreserved also leaves out texts held by an open batch job
    (a second %s for the model)."""
    from . import embeddings

    current = "" if history_too else "m.superseded_by IS NULL AND "
    return (
        "FROM tl.passages p JOIN tl.messages m ON m.id=p.message_id "
        "JOIN tl.channels ch ON ch.id=p.channel_id AND ch.embed WHERE "
        + current
        + "NOT EXISTS(SELECT 1 FROM tl.passage_embeddings e WHERE e.text_hash=p.text_hash AND e.model=%s)"
        + (
            " AND NOT EXISTS(SELECT 1 FROM tl.embedding_batch_items i JOIN tl.embedding_batches b ON b.id=i.batch_id WHERE b.status IN ('submitting','submitted') AND b.model=%s AND i.store AND i.conversation_id IS NULL AND i.text_hash=p.text_hash)"
            if unreserved
            else ""
        )
        # A text held by the egress rule is not sent to a remote provider.
        + embeddings.egress_clause("tl.message_egress_ok(p.message_id)")
    )


def pending_texts(conn, model, limit, by_page=False):
    """Up to `limit` distinct passage texts without a vector of `model` and
    not held by a batch job: text_hash, content, token_estimate and the
    passage they are read from (message_id, ordinal). by_page orders them by
    page and position, for contextual embedding."""
    rows = conn.execute(
        "SELECT DISTINCT ON (p.text_hash) p.text_hash,p.content,p.token_estimate,p.message_id,p.ordinal "
        + _unembedded(history(), unreserved=True)
        + " ORDER BY p.text_hash,m.superseded_by IS NULL DESC,p.message_id DESC,p.ordinal LIMIT %s",
        (model, model, limit),
    ).fetchall()
    if by_page:
        rows.sort(key=lambda r: (r["message_id"], r["ordinal"]))
    return rows


def passage_context(conn):
    """context(row, tokens): the passages of the row's revision just before
    it, in order, within a token budget."""

    def context(row, tokens):
        found = []
        for before in conn.execute(
            "SELECT p.text_hash,p.content,p.token_estimate FROM tl.passages p WHERE p.message_id=%s AND p.ordinal<%s ORDER BY p.ordinal DESC LIMIT 40",
            (row["message_id"], row["ordinal"]),
        ):
            tokens -= before["token_estimate"]
            if tokens < 0:
                break
            found.append(before)
        return found[::-1]

    return context


# A vector is stored only while an embedded channel still has the text.
STORE = (
    "INSERT INTO tl.passage_embeddings(text_hash,model,embedding) SELECT %s,%s,%s::vector "
    "WHERE EXISTS(SELECT 1 FROM tl.passages p JOIN tl.channels ch ON ch.id=p.channel_id AND ch.embed WHERE p.text_hash=%s) "
    "ON CONFLICT DO NOTHING"
)


def pending(conn, model=None):
    """What is not covered yet: revisions without passages and, for `model`
    (default: the configured embedding model, when there is one), distinct
    passage texts without a vector."""
    revisions = conn.execute("SELECT count(*) n " + _UNBUILT).fetchone()["n"]
    if model is None:
        from . import embeddings

        try:
            _, model, _ = embeddings.settings()
        except ValueError:
            model = None
    texts = None
    if model is not None:
        texts = conn.execute(
            "SELECT count(DISTINCT p.text_hash) n " + _unembedded(history()),
            (model,),
        ).fetchone()["n"]
    return dict(
        revisions_without_passages=revisions,
        texts_without_vector=texts,
        model=model,
        history=history(),
    )


def embed_once(batch=64):
    """Embed up to `batch` distinct passage texts lacking a vector of the
    configured model; returns how many were stored. Passages of current
    revisions only, unless THROUGHLINE_PASSAGE_EMBED_HISTORY=true. A
    configuration error (no provider, changed dimensions) returns 0."""
    from . import embeddings, providers

    try:
        _, model, dimensions = embeddings.settings()
    except ValueError as error:
        log.info("passage embedding skipped: %s", error)
        return 0
    with connect("app") as guard:
        guard.execute("SELECT pg_advisory_lock(%s)", (EMBED_LOCK,))
        guard.commit()
        try:
            return _embed_batch(embeddings, providers, model, dimensions, batch)
        finally:
            try:
                guard.rollback()
                guard.execute("SELECT pg_advisory_unlock(%s)", (EMBED_LOCK,))
            except Exception:
                guard.close()
                raise


def _embed_batch(embeddings, providers, model, dimensions, batch):
    from . import embedding_context

    contextual = embedding_context.active()
    with connect("app") as conn:
        conn.execute(
            "INSERT INTO tl.embedding_models(model,dimensions) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (model, dimensions),
        )
        stored = conn.execute(
            "SELECT dimensions FROM tl.embedding_models WHERE model=%s", (model,)
        ).fetchone()["dimensions"]
        if stored != dimensions:
            log.warning(
                "passage embedding skipped: model %s has other dimensions", model
            )
            return 0
        if embeddings.index_enabled():
            conn.execute("SELECT tl.ensure_embedding_index(%s)", (model,))
        rows = pending_texts(conn, model, batch, by_page=contextual)
        windows = (
            embedding_context.windows(
                rows, passage_context(conn), group=lambda r: r["message_id"]
            )
            if contextual and rows
            else None
        )
    if not rows:
        return 0
    # No transaction is open during the request. The module attribute is
    # looked up now, so a replacement assigned to embeddings.embed is used.
    if windows is not None:
        (vectors, used), cost = embeddings.spent(
            lambda r: r[1], lambda: embedding_context.embed(windows)
        )
    else:
        (vectors, used), cost = embeddings.spent(
            lambda r: r[1], lambda: embeddings.embed([r["content"] for r in rows])
        )
    with connect("app") as conn:
        with conn.cursor() as cur:
            cur.executemany(
                STORE,
                [
                    (row["text_hash"], model, embeddings.vector(v), row["text_hash"])
                    for row, v in zip(rows, vectors)
                ],
            )
        conn.execute(
            "INSERT INTO tl.embedding_ledger(model,tokens,cost) VALUES(%s,%s,%s)",
            (model, used, cost),
        )
    return len(rows)
