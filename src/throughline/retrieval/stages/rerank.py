"""Rerank stage (order 20, QRY-23, QRY-22, CST-5).

Enabled when the request's ranker is not 'fused'. The top
THROUGHLINE_RERANK_TOP_N (50) candidates are handed to the ranker; a ranker
that needs text gets the text of records the asker may see, hydrated through
the gated record views (a record that does not hydrate sends nothing and is
placed after the reranked ones). Each reranked candidate gets a discovery
entry {leg: 'rerank', rank, score, detail: {ranker, version}}.

When the ranker cannot answer (provider unavailable, deadline, approval
required for a non-local provider) the fused order is kept and the stage is
reported failed with that reason: the recall degrades, it never fails.
"""

import os

from ...agent_view import local_time, snippet
from .. import hydrate, ranking
from ..executor import StageFailed
from . import Stage

DEFAULT_TOP_N = 50


def top_n():
    try:
        value = int(os.getenv("THROUGHLINE_RERANK_TOP_N") or DEFAULT_TOP_N)
    except ValueError:
        value = DEFAULT_TOP_N
    return max(1, min(value, 1000))


def _message_ids(record):
    ids = []
    if record.get("kind") in ("life_event", "reference", "page_revision", "derived"):
        if record.get("message_id") is not None:
            ids.append(record["message_id"])
    if record.get("kind") == "passage" and record.get("message_id") is not None:
        ids.append(record["message_id"])
    for entry in record.get("support") or []:
        if isinstance(entry, dict) and isinstance(entry.get("message_id"), int):
            ids.append(entry["message_id"])
    return sorted(set(ids))


# What a cross-encoder reads per record: every record carries a compact header
# (date, channel, thread title, author) so the reranker sees WHO, WHEN and WHERE;
# long records are cut to their query-matched passage(s) and short chat/transcript
# lines include neighbouring turns with speaker names.
EXCERPT_CHARS = 1600
SHORT_CHARS = 350
NEIGHBOURS = 2


def _header(meta, fallback_record, include_author=True):
    when = local_time(meta.get("happened_at") if meta else fallback_record.get("record_time"))
    channel = (meta.get("channel") if meta else None) or fallback_record.get("channel")
    title = meta.get("thread_title") if meta else None
    parts = [p for p in (when, channel, title) if p]
    if include_author:
        author = (meta.get("author") if meta else None) or fallback_record.get("author_handle")
        if meta and meta.get("is_owner") and author:
            author = f"{author} (owner)"
        if author:
            parts.append(author)
    return "[" + " | ".join(parts) + "]" if parts else ""


def rerank_texts(ctx, query, records):
    """{record_id: text} for the ranker; missing ids keep their content."""
    msg_ids = sorted(
        {
            int(r["message_id"])
            for r in records
            if r.get("message_id") is not None
            and r.get("kind") in ("life_event", "reference", "derived", "page_revision")
        }
    )
    meta_by_id = {}
    if msg_ids:
        try:
            id_list = ",".join(str(i) for i in msg_ids)
            rows = ctx.query(
                "SELECT m.id, m.channel, m.happened_at, m.thread_title,"
                " coalesce(m.display_name, m.author_handle) AS author, p.is_owner"
                " FROM api.messages m LEFT JOIN api.people p ON p.id = m.person_id"
                f" WHERE m.id IN ({id_list})",
                cap=len(msg_ids) + 5,
                leg="rerank_meta",
            )["rows"]
            meta_by_id = {int(r["id"]): r for r in rows}
        except Exception:
            meta_by_id = {}

    texts, short = {}, []
    for record in records:
        content = record.get("content") or ""
        mid = record.get("message_id")
        meta = meta_by_id.get(int(mid)) if mid is not None else None
        head = _header(meta, record)
        if len(content) > EXCERPT_CHARS:
            body = snippet(content, query, EXCERPT_CHARS)[0]
            texts[record["record_id"]] = f"{head}\n{body}" if head else body
        elif (
            len(content) < SHORT_CHARS
            and record.get("kind") in ("life_event", "derived")
            and record.get("thread_id") is not None
            and record.get("thread_no") is not None
        ):
            short.append(record)
            if head:
                texts[record["record_id"]] = f"{head}\n{content}"
        elif head:
            texts[record["record_id"]] = f"{head}\n{content}"

    if not short:
        return texts
    where = " OR ".join(
        f"(m.thread_id={int(r['thread_id'])} AND m.thread_no BETWEEN"
        f" {int(r['thread_no']) - NEIGHBOURS} AND {int(r['thread_no']) + NEIGHBOURS})"
        for r in short
    )
    try:
        rows = ctx.query(
            "SELECT m.thread_id, m.thread_no, m.channel, m.happened_at, m.thread_title,"
            " coalesce(m.display_name, m.author_handle) AS author, p.is_owner,"
            " left(m.content, 240) AS content"
            " FROM api.messages m LEFT JOIN api.people p ON p.id = m.person_id"
            f" WHERE m.superseded_by IS NULL AND ({where})",
            cap=len(short) * (2 * NEIGHBOURS + 1) * 2,
            leg="rerank_context",
        )["rows"]
    except Exception:
        return texts
    by_thread = {}
    for row in rows:
        by_thread.setdefault(int(row["thread_id"]), []).append(row)
    for record in short:
        thread, number = int(record["thread_id"]), int(record["thread_no"])
        near = sorted(
            (r for r in by_thread.get(thread, []) if abs(int(r["thread_no"]) - number) <= NEIGHBOURS),
            key=lambda r: int(r["thread_no"]),
        )
        if len(near) <= 1:
            continue
        anchor = next((r for r in near if int(r["thread_no"]) == number), near[0])
        head = _header(anchor, record, include_author=False)
        lines = [head] if head else []
        for r in near:
            mark = ">> " if int(r["thread_no"]) == number else ""
            who = r.get("author") or ""
            if r.get("is_owner") and who:
                who = f"{who} (owner)"
            prefix = f"{who}: " if who else ""
            lines.append(mark + prefix + (r.get("content") or ""))
        texts[record["record_id"]] = "\n".join(lines)
    return texts


class Rerank(Stage):
    name = "rerank"
    order = 20
    exact = True

    def _ranker(self, ctx):
        ranker = getattr(ctx, "ranker", None)
        if ranker is None:
            ranker, _ = ranking.choose(ctx.request)
        return ranker

    def exact_for(self, ctx):
        return self._ranker(ctx).exact

    def enabled(self, ctx):
        return self._ranker(ctx).name != ranking.DEFAULT

    def run(self, ctx, candidates):
        ranker = self._ranker(ctx)
        head, tail = candidates[: top_n()], candidates[top_n() :]
        if not head:
            return candidates
        query = ctx.request.query or ""
        if ranker.needs_text:
            records = {r["record_id"]: r for r in hydrate.records(ctx, head)}
            texts = rerank_texts(ctx, query, [records[c.record_id] for c in head if c.record_id in records])
            items = [
                ranking.RankItem(
                    candidate=c,
                    text=texts.get(c.record_id, records[c.record_id]["content"] or ""),
                    message_ids=_message_ids(records[c.record_id]),
                )
                for c in head
                if c.record_id in records
            ]
            hidden = [c for c in head if c.record_id not in records]
        else:
            items = [ranking.RankItem(candidate=c) for c in head]
            hidden = []
        if ctx.tracker is not None and ctx.tracker.expired():
            raise StageFailed("timeout", ranker=ranker.name)
        try:
            ranked = ranker.rank(ctx, query, items)
        except Exception as exc:
            failure = ranking.stage_failure(exc, ranker=ranker.name)
            if failure is None:
                raise
            raise failure from None
        version = ranker.version
        result = []
        for rank, (item, score) in enumerate(ranked, start=1):
            candidate = item.candidate
            candidate.discovery = [
                d for d in candidate.discovery if d.get("leg") != "rerank"
            ]
            candidate.discovery.append(
                dict(
                    leg="rerank",
                    rank=rank,
                    score=score,
                    detail=dict(ranker=ranker.name, version=version),
                )
            )
            result.append(candidate)
        return result + hidden + tail


STAGE = Rerank()
