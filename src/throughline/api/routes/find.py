"""POST /v1/find: recall shaped for an agent's context window.

recall returns one record per hit with the full record contract, which is
the right shape for programs and the wrong one for a reader: a chat hit is
one short line without the messages around it, long records (email, pages,
transcripts) arrive whole, and execution metadata outweighs the evidence.
find runs the same recall (same legs, fusion, ranker and hard scope, under
the token's scope) and returns compact episodes:

- chat and transcript hits come with the messages around them in their
  thread (window before/after), hits close together in one thread are
  merged into one episode, and each episode notes `later_in_thread` when
  newer messages exist beyond the returned window;
- when unscoped across channels, a soft per-channel cap keeps one high-volume
  channel from crowding out other matching sources while preserving top hits;
- when the query mentions known people (unrestricted scope, no explicit
  persons filter), find reports `matched_people` with their `person_id`s so
  the reader can follow up directly with `api.person_brief` or `persons` scope;
- long records are cut to the passage(s) that best match the query, with a
  note on how to read the rest;
- every message keeps its channel and external_id for citation, its time,
  its author (marked when it is the owner) and its message_id for
  api.context.

The response stops at max_chars and says so. Nothing here is new evidence:
it is a view over recall plus reads of api.messages / api.people.
"""

import math
import re

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ... import channel_roles
from ...agent_view import local_time, snippet
from ...query import query_ex
from ...retrieval import images
from ...retrieval import recall as recall_library
from ...retrieval.scope_sql import lit
from ...retrieval.types import HardScope, RecallRequest, move_scope_aliases
from ..deps import Token, audit, require, scope_of

router = APIRouter()

MESSAGE_CUT = 600
HIT_CUT = 2400

_NAME_SKIP = frozenset(
    """the and for that this with from have been were will would could should
    about after before when where what which while whose into onto over under
    between through during since until also just only very more most some any
    all each every both few many much other another such than then there here
    they them their theirs she her hers his him its our ours your yours who
    whom why how did does done had has was are not yes nope yeah okay sure
    really still never always ever maybe think know want need like love hate
    said says told tell ask asked talk talked chat text call message email
    note page event plan plans trip work home house room food sleep time day
    week month year today yesterday tomorrow morning night evening afternoon
    monday tuesday wednesday thursday friday saturday sunday january february
    march april may june july august september october november december
    last next first second third recent latest current newest oldest best worst
    help check find look list count show give make take keep stop start change
    group friend friends family dad mom brother sister guy girl people person
    one two three four five six seven eight nine ten""".split()
)


class FindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    k: int = Field(default=15, ge=1, le=100)
    scope: HardScope = Field(default_factory=HardScope)
    strategy: str = "auto"
    ranker: str | None = None
    sort: str = Field(default="relevance", pattern="^(relevance|recent)$")
    window: int = Field(default=4, ge=0, le=40)
    max_chars: int = Field(default=42000, ge=2000, le=400000)
    # Related pages over the page graph (docs/v11/pages.md); 0 turns it off.
    pages: int = Field(default=5, ge=0, le=20)
    include_nsfw: bool = False

    @model_validator(mode="before")
    @classmethod
    def top_level_scope(cls, data):
        # Same top-level scope aliases as /v1/recall, plus common query/k aliases.
        # Unlike recall, find searches reference channels (curated pages, places,
        # projects) too unless the scope says otherwise: a reader wants leads.
        if isinstance(data, dict):
            data = dict(data)
            for alias in ("q", "text", "search"):
                if alias in data and "query" not in data:
                    data["query"] = data.pop(alias)
                elif alias in data:
                    data.pop(alias)
            for alias in ("limit", "top_k"):
                if alias in data and "k" not in data:
                    data["k"] = data.pop(alias)
                elif alias in data:
                    data.pop(alias)
            if data.get("sort") in ("newest", "latest", "date", "time", "desc"):
                data["sort"] = "recent"
            elif data.get("sort") in ("score", "default"):
                data["sort"] = "relevance"
        data = move_scope_aliases(data)
        if isinstance(data, dict):
            scope = dict(data.get("scope") or {})
            if "include_reference" in data:
                scope["include_reference"] = data.pop("include_reference")
            scope.setdefault("include_reference", True)
            data = dict(data, scope=scope)
        return data


def _time(value):
    return local_time(value)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _match_people(query, scope, token):
    """Detect unambiguous/known non-owner people named in the query (unrestricted only)."""
    if scope.asker is not None:
        return []
    tokens = []
    seen_t = set()
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_'-]{2,30}", query or ""):
        low = word.lower().strip("'-")
        if len(low) < 3 or low in _NAME_SKIP or low in seen_t:
            continue
        seen_t.add(low)
        tokens.append(low)
    if not tokens:
        return []
    tokens = tokens[:12]
    arr = lit(tokens, "text[]")
    stmt = (
        "SELECT id, display_name, handle FROM api.people"
        " WHERE merged_into IS NULL AND NOT coalesce(is_owner, false)"
        f" AND (lower(coalesce(display_name, '')) = ANY({arr})"
        f"  OR lower(split_part(coalesce(display_name, ''), ' ', 1)) = ANY({arr})"
        f"  OR lower(coalesce(handle, '')) = ANY({arr}))"
        " ORDER BY id LIMIT 8"
    )
    try:
        rows = query_ex(stmt, 10, scope=scope, token_row=token, materialize=False)["rows"]
    except Exception:
        return []
    q_low = (query or "").lower()
    exact = [r for r in rows if r.get("display_name") and r["display_name"].lower() in q_low]
    picked = exact if exact else rows
    return picked[:4]


def _windows(records, window, minimums=None):
    """Episodes: [{thread_id, lo, hi, hits:[record]}] in best-hit order.

    minimums: {channel: context_window_min} raising the window of a hit in
    that channel (a transcript turn says little without its neighbours).
    Pass None when the caller chose the window explicitly: it is honoured.
    """
    minimums = minimums or {}
    episodes = []
    for record in records:
        thread, number = _int(record.get("thread_id")), _int(record.get("thread_no"))
        is_message = record.get("kind") in ("life_event", "derived")
        if thread is None or number is None or not is_message or window == 0:
            episodes.append(dict(thread_id=None, hits=[record]))
            continue
        w = max(window, minimums.get(record.get("channel")) or 0)
        lo, hi = number - w, number + w
        for episode in episodes:
            if episode["thread_id"] == thread and lo <= episode["hi"] + 1 and hi >= episode["lo"] - 1:
                episode["lo"], episode["hi"] = min(lo, episode["lo"]), max(hi, episode["hi"])
                episode["hits"].append(record)
                break
        else:
            episodes.append(dict(thread_id=thread, lo=lo, hi=hi, hits=[record]))
    return episodes


def _read_windows(episodes, scope, token):
    ranges = [e for e in episodes if e["thread_id"] is not None]
    if not ranges:
        return {}, {}
    where = " OR ".join(
        f"(m.thread_id={int(e['thread_id'])} AND m.thread_no BETWEEN {int(e['lo'])} AND {int(e['hi'])})"
        for e in ranges
    )
    statement = (
        "SELECT m.id, m.channel, m.external_id, m.thread_id, m.thread_no, m.happened_at,"
        " coalesce(m.display_name, m.author_handle) AS author, p.is_owner,"
        f" left(m.content, {HIT_CUT + 200}) AS content, length(m.content) AS chars, m.thread_title"
        " FROM api.messages m LEFT JOIN api.people p ON p.id = m.person_id"
        f" WHERE m.superseded_by IS NULL AND ({where}) ORDER BY m.thread_id, m.thread_no, m.id"
    )
    result = query_ex(statement, 4000, scope=scope, token_row=token)
    by_thread = {}
    for row in result["rows"]:
        by_thread.setdefault(row["thread_id"], []).append(row)

    # Lightweight tail check per returned thread so agents immediately see when
    # newer messages exist after the returned episode window.
    tids = sorted({int(e["thread_id"]) for e in ranges})
    tails = {}
    try:
        tail_stmt = (
            "SELECT thread_id, max(thread_no) AS max_no, max(happened_at) AS max_at"
            f" FROM api.messages WHERE superseded_by IS NULL AND thread_id = ANY({lit(tids, 'bigint[]')})"
            " GROUP BY thread_id"
        )
        for row in query_ex(tail_stmt, len(tids) + 5, scope=scope, token_row=token, materialize=False)["rows"]:
            tails[int(row["thread_id"])] = row
    except Exception:
        tails = {}
    return by_thread, tails


def _line(row, hit, query):
    text = row.get("content") or ""
    limit = HIT_CUT if hit else MESSAGE_CUT
    text, _ = snippet(text, query, limit)
    if (row.get("chars") or 0) > len(text) and not text.endswith("]"):
        text += f" [... {row['chars']} chars]"
    author = row.get("author")
    if row.get("is_owner"):
        author = f"{author} (owner)"
    out = dict(
        time=_time(row.get("happened_at")),
        author=author,
        text=text,
        channel=row.get("channel"),
        external_id=row.get("external_id"),
        message_id=row.get("id"),
    )
    if hit:
        out["hit"] = True
    return out


def _dedup_hits(records):
    """Deduplicate hits by (channel, external_id) and long identical text."""
    out, seen = [], set()
    for record in records:
        if record.get("role", "hit") != "hit":
            continue
        key = (record.get("channel"), record.get("external_id"))
        text = (record.get("content") or "").strip()[:300]
        text = text if len(text) >= 40 else None  # short lines repeat legitimately
        if key in seen or (text and text in seen):
            continue
        seen.update({key, text} if text else {key})
        out.append(record)
    return out


# Subtle hierarchical diversity decay (family -> channel -> thread).
# Applied additively in log-odds (logit) space for cross-encoder probabilities
# (and very mildly multiplicatively for RRF fallback). Each level is subtle on
# its own; their sum (0.04 + 0.06 + 0.18 = 0.28 logits ~= 1.3x odds per
# same-thread repeat) gently breaks ties and long single-thread pileups without
# displacing genuinely stronger matches.
LOGIT_FAMILY_DECAY = 0.04
LOGIT_CHANNEL_DECAY = 0.06
LOGIT_THREAD_DECAY = 0.18
SCOPED_LOGIT_THREAD_DECAY = 0.15

RRF_FAMILY_DECAY = 0.98
RRF_CHANNEL_DECAY = 0.97
RRF_THREAD_DECAY = 0.92
SCOPED_RRF_THREAD_DECAY = 0.94


def _rerank_logit(record):
    """Return log-odds score when cross-encoder rerank ran, else None."""
    for d in record.get("discovery") or []:
        if d.get("leg") == "rerank" and d.get("score") is not None:
            s = float(d["score"])
            if 0.0 < s < 1.0:
                p = max(1e-6, min(1.0 - 1e-6, s))
                return math.log(p / (1.0 - p))
            # Already a raw logit (or boundary 0/1)
            if s <= 0.0:
                return max(-14.0, s) if s < 0.0 else -13.8
            return min(14.0, s) if s > 1.0 else 13.8
    return None


def _channel_family(channel, roles=None):
    """The diversity family of a channel: its declared role, else the first
    segment of its name."""
    declared = (roles or {}).get(channel)
    if declared is not None and declared.declared and declared.role:
        return "role:" + declared.role
    return (channel or "").split(".", 1)[0]


def _diversify(records, k, scoped_channels, roles=None):
    """Greedy hierarchical score-decay re-ranking (family, channel, thread)."""
    if len(records) <= 1:
        return records[:k], {}

    use_logit = any(_rerank_logit(r) is not None for r in records)
    pool = []
    for idx, rec in enumerate(records):
        ch = rec.get("channel") or ""
        fam = _channel_family(ch, roles)
        th = (ch, rec.get("thread_id")) if rec.get("thread_id") is not None else (ch, rec.get("external_id"))
        if use_logit:
            lg = _rerank_logit(rec)
            base = lg if lg is not None else -14.0 - idx * 0.01
        else:
            sc = rec.get("score")
            base = float(sc) if (sc is not None and float(sc) > 0) else 1.0 / (idx + 10.0)
        pool.append((rec, base, fam, ch, th))

    picked = []
    fam_counts, ch_counts, th_counts = {}, {}, {}
    remaining = list(pool)
    while remaining and len(picked) < k:
        best_i = 0
        best_eff = -1e18
        for i, (rec, base, fam, ch, th) in enumerate(remaining):
            fc = fam_counts.get(fam, 0)
            cc = ch_counts.get(ch, 0)
            tc = th_counts.get(th, 0)
            if use_logit:
                if scoped_channels:
                    eff = base - SCOPED_LOGIT_THREAD_DECAY * tc
                else:
                    eff = base - LOGIT_FAMILY_DECAY * fc - LOGIT_CHANNEL_DECAY * cc - LOGIT_THREAD_DECAY * tc
            else:
                if scoped_channels:
                    eff = base * (SCOPED_RRF_THREAD_DECAY ** tc)
                else:
                    eff = base * (RRF_FAMILY_DECAY ** fc) * (RRF_CHANNEL_DECAY ** cc) * (RRF_THREAD_DECAY ** tc)
            if eff > best_eff:
                best_eff = eff
                best_i = i
        rec, _base, fam, ch, th = remaining.pop(best_i)
        picked.append(rec)
        if fam:
            fam_counts[fam] = fam_counts.get(fam, 0) + 1
        if ch:
            ch_counts[ch] = ch_counts.get(ch, 0) + 1
        th_counts[th] = th_counts.get(th, 0) + 1

    other_channels = {}
    for rec, _base, _fam, ch, _th in remaining:
        if ch:
            other_channels[ch] = other_channels.get(ch, 0) + 1
    return picked, other_channels


def _page_map(data, pool, matched_people, scope, token):
    """Pages related to the search, ranked over the page graph (docs/v11/pages.md).
    Unrestricted askers only; a failure never fails the search."""
    if scope.asker is not None or not data.pages:
        return [], {}
    from ...pages.graph import related_pages_for_scope

    return related_pages_for_scope(scope, data.query, pool, matched_people, limit=data.pages)


@router.post("/v1/find")
def find(data: FindRequest, token: Token):
    """Recall as compact episodes with thread context; see api.describe('recall')."""
    require(token, "owner", "agent")
    scope = scope_of(token)
    strategy = data.strategy if data.strategy in ("auto", "keyword", "meaning", "fused") else "auto"
    unscoped_channels = not data.scope.channels
    k_pool = min(40, max(data.k * 2, 25)) if unscoped_channels else min(40, max(data.k + 8, 20))
    request = RecallRequest(
        query=data.query,
        scope=data.scope,
        k=k_pool,
        strategy=strategy,
        ranker=data.ranker,
        excerpt_chars=HIT_CUT,
        include_nsfw=data.include_nsfw,
        # images[] come below for every shown message, in one statement.
        include_images=False,
    )
    response = recall_library(scope, request, token_row=token)

    matched_people = []
    if data.scope.persons is None and scope.asker is None:
        matched_people = _match_people(data.query, scope, token)

    deduped = _dedup_hits(response.records)
    roles = channel_roles.load(
        {r.get("channel") for r in deduped}, scope=scope, token=token
    )
    if data.sort == "recent":
        # Keep the strongest relevant candidates, then order newest-first before windowing.
        pool_n = min(len(deduped), max(data.k * 2, 25))
        cand = [r for r in deduped[:pool_n] if (_rerank_logit(r) is None or _rerank_logit(r) > -2.5)]
        if len(cand) < min(data.k, len(deduped)):
            cand = deduped[:pool_n]
        cand.sort(
            key=lambda r: (_time(r.get("record_time")) or "", _int(r.get("message_id")) or 0),
            reverse=True,
        )
        records, other_channels = [], {}
        th_seen = {}
        for rec in cand:
            th = (rec.get("channel"), rec.get("thread_id")) if rec.get("thread_id") is not None else (rec.get("channel"), rec.get("external_id"))
            if len(records) < data.k and th_seen.get(th, 0) < 3:
                records.append(rec)
                th_seen[th] = th_seen.get(th, 0) + 1
            else:
                ch = rec.get("channel")
                if ch:
                    other_channels[ch] = other_channels.get(ch, 0) + 1
    else:
        records, other_channels = _diversify(deduped, data.k, not unscoped_channels, roles)

    # Surface up to 3 newer matching candidates from the already-fetched pool when
    # they sit outside the returned top-k (helps catch later policy/status revisions).
    picked_keys = {(r.get("channel"), r.get("external_id")) for r in records}
    top_times = [t for r in records[:3] if (t := _time(r.get("record_time")))]
    ref_top_time = max(top_times) if top_times else ""
    newer_candidates = []
    if ref_top_time and data.sort == "relevance":
        for rec in deduped:
            key = (rec.get("channel"), rec.get("external_id"))
            if key in picked_keys:
                continue
            rt = _time(rec.get("record_time")) or ""
            if rt > ref_top_time:
                lg = _rerank_logit(rec)
                # Keep strong matches (positive rerank logit, or top 25 when RRF-only)
                if lg is None or lg > -2.5:
                    newer_candidates.append((rt, rec))
        newer_candidates.sort(key=lambda pair: pair[0], reverse=True)
    newer_in_pool = [
        dict(
            time=rt,
            channel=rec.get("channel"),
            external_id=rec.get("external_id"),
            message_id=_int(rec.get("message_id")),
            preview=" ".join((rec.get("content") or "").split())[:140],
        )
        for rt, rec in newer_candidates[:3]
    ]

    minimums = None
    if "window" not in data.model_fields_set:
        minimums = {
            name: r.context_window_min
            for name, r in roles.items()
            if r.context_window_min is not None
        }
    episodes = _windows(records, data.window, minimums)
    threads, tails = _read_windows(episodes, scope, token)
    out, size, left_out = [], 0, 0
    channels_hit = {}
    times_seen = []
    for rank, episode in enumerate(episodes, 1):
        hit_ids = {_int(h.get("message_id")) for h in episode["hits"]}
        if episode["thread_id"] is not None:
            rows = [
                r
                for r in threads.get(episode["thread_id"], [])
                if episode["lo"] <= r["thread_no"] <= episode["hi"]
            ]
        else:
            rows = []
        if rows:
            messages = [_line(r, r["id"] in hit_ids, data.query) for r in rows]
            ch = rows[0].get("channel")
            item = dict(
                rank=rank,
                channel=ch,
                thread=rows[0].get("thread_title"),
                thread_id=episode["thread_id"],
                time_from=messages[0]["time"],
                time_to=messages[-1]["time"],
                messages=messages,
            )
            tail = tails.get(int(episode["thread_id"]))
            if tail and tail.get("max_no") is not None:
                shown_max = max(int(r["thread_no"]) for r in rows)
                newer = int(tail["max_no"]) - shown_max
                if newer > 0:
                    item["later_in_thread"] = f"{newer} newer messages through {_time(tail.get('max_at'))}"
            t_for_range = messages[-1]["time"] or messages[0]["time"]
        else:
            hit = episode["hits"][0]
            text, _ = snippet(hit.get("content") or "", data.query, HIT_CUT)
            ch = hit.get("channel")
            t_for_range = _time(hit.get("record_time"))
            item = dict(
                rank=rank,
                channel=ch,
                kind=hit.get("kind"),
                time=t_for_range,
                author=hit.get("author_handle"),
                heading=hit.get("heading_path"),
                text=text,
                external_id=hit.get("external_id"),
                message_id=_int(hit.get("message_id")),
                evidence_class=hit.get("evidence_class"),
            )
        encoded = len(str(item))
        if out and size + encoded > data.max_chars:
            left_out = len(episodes) - rank + 1
            break
        out.append(item)
        size += encoded
        if ch:
            channels_hit[ch] = channels_hit.get(ch, 0) + 1
        if t_for_range:
            times_seen.append(t_for_range)

    images.attach(
        images.reader(scope, token),
        [m for item in out for m in (item.get("messages") or [item])],
        include_nsfw=data.include_nsfw,
    )

    execution = response.execution or {}
    counts = execution.get("counts") or {}
    coverage = execution.get("coverage") or {}
    searched = dict(
        legs=counts.get("per_leg"),
        retrieved=counts.get("retrieved"),
        ranker=(execution.get("ranker") or {}).get("name"),
        status=execution.get("status"),
        coverage=coverage.get("status"),
        channels_hit=channels_hit,
    )
    if other_channels:
        searched["other_matching_channels"] = other_channels
    if newer_in_pool:
        searched["newer_in_pool"] = newer_in_pool
    if times_seen:
        searched["date_range"] = dict(oldest=min(times_seen), newest=max(times_seen))

    result = dict(
        query=data.query,
        episodes=out,
        note=(
            "Ranked sample of the best-matching records with their thread context,"
            " not an exhaustive list: counts here are not totals. Search again with"
            " other words, a scope (channels, after, before, persons), or read more"
            " of a thread with api.context(message_id, before, after)."
        ),
        searched=searched,
    )
    page_map, person_pages = _page_map(data, deduped, matched_people, scope, token)
    if matched_people:
        result["matched_people"] = [
            dict(person_id=p["id"], display_name=p.get("display_name"), handle=p.get("handle"),
                 **({"page": person_pages[p["id"]]} if p["id"] in person_pages else {}))
            for p in matched_people
        ]
    if page_map:
        result["pages"] = page_map
    warnings = [w for w in execution.get("warnings") or [] if isinstance(w, str)]
    if left_out:
        warnings.append(
            f"{left_out} more episodes left out at max_chars={data.max_chars}; lower k or window"
        )
    if warnings:
        result["warnings"] = warnings[:8]
    audit(token, len(out), operation="find", retrieved=counts.get("retrieved"))
    return result
