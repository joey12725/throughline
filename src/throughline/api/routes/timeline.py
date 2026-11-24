"""POST /v1/timeline: exhaustive keyword + optional semantic matches as episodes in time order.

Counting and "every time" questions need the whole population, not a ranked
sample, and they need it at episode grain (one conversation about a thing is
one occurrence however many messages it has). timeline runs an exhaustive
full-text match over the gated api.messages view (accepting either one query
string or a `queries` list of alternative wordings/synonyms, each in websearch
syntax: words are ANDed, "or" between alternatives, quotes for phrases, -word
to exclude), optionally unions strong semantic matches (when `semantic=true`,
the default) so paraphrased episodes that omit exact keywords are still surfaced
and tagged (`match: "keyword" | "semantic" | "both"`), groups all matches with
api.episodes_of, and returns one compact line per episode plus per-channel
coverage status.
"""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...agent_view import local_time, snippet
from ...query import query_ex
from ...retrieval import images
from ...retrieval import recall as recall_library
from ...retrieval.scope_sql import lit
from ...retrieval.types import HardScope, RecallRequest, move_scope_aliases
from ..deps import Token, audit, is_unrestricted, require, scope_of

router = APIRouter()

MAX_MATCHES = 20000
SEMANTIC_POOL = 80
SEMANTIC_MAX_KEEP = 35
SEMANTIC_MIN_ABS = 0.28
SEMANTIC_REL_RATIO = 0.80
# Image-space similarities (query text against image vectors) sit on another
# scale than text-to-text ones: their own floor per model and their own
# relative cut. Uncalibrated starting points; see docs/images.md.
VISUAL_MIN_ABS = {"gemini-embedding-2": 0.30, "siglip2-so400m-384": 0.10}
VISUAL_REL_RATIO = 0.85
VISUAL_MAX_KEEP = 15


class TimelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = Field(default=None, min_length=1, max_length=1000)
    queries: list[str] | None = Field(default=None, min_length=1, max_length=12)
    scope: HardScope = Field(default_factory=HardScope)
    semantic: bool = True
    max_episodes: int = Field(default=150, ge=1, le=2000)
    order: str = Field(default="asc", pattern="^(asc|desc)$")
    sample: str = Field(default="head", pattern="^(head|spread)$")
    text_chars: int = Field(default=240, ge=40, le=2000)
    include_nsfw: bool = False

    @model_validator(mode="before")
    @classmethod
    def top_level_scope(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            for alias in ("q", "text", "search"):
                if alias in data and "query" not in data and "queries" not in data:
                    data["query"] = data.pop(alias)
                elif alias in data:
                    data.pop(alias)
            if "limit" in data and "max_episodes" not in data:
                data["max_episodes"] = data.pop("limit")
        return move_scope_aliases(data)

    @model_validator(mode="after")
    def require_query(self):
        if not self.query and not self.queries:
            raise ValueError("query or queries is required")
        return self


def _time(value):
    return local_time(value)


def _coverage_summary(channels, after, before, scope, token):
    if not channels:
        return None
    try:
        rows = query_ex(
            "SELECT channel, status, covered_from, covered_to, stale, window_status, reasons"
            f" FROM api.coverage_for({lit(sorted(channels), 'text[]')},"
            f" {lit(after.isoformat()) + '::timestamptz' if after else 'NULL'},"
            f" {lit(before.isoformat()) + '::timestamptz' if before else 'NULL'})",
            100,
            scope=scope,
            token_row=token,
            materialize=False,
        )["rows"]
        if not rows:
            return None
        return [
            dict(
                channel=r.get("channel"),
                status=r.get("status"),
                covered_from=_time(r.get("covered_from")),
                covered_to=_time(r.get("covered_to")),
                stale=r.get("stale"),
                window_status=r.get("window_status"),
                reasons=r.get("reasons"),
            )
            for r in rows
        ]
    except Exception:
        return None


def _keep(scored, floor, ratio, limit):
    if not scored:
        return []
    scored = sorted(scored, key=lambda x: -x[1])
    cutoff = max(floor, scored[0][1] * ratio)
    kept = []
    for mid, sim in scored:
        if sim < cutoff:
            break
        if mid not in kept:
            kept.append(mid)
            if len(kept) >= limit:
                break
    return kept


def _semantic_ids(q_list, hard_scope, scope, token, include_nsfw=False):
    """Return high-similarity message_ids from the meaning legs (text vector,
    and the visual leg for messages with images), or empty list on degrade."""
    query_text = " ; ".join(q_list[:4])
    if not query_text.strip():
        return []
    req = RecallRequest(
        query=query_text,
        scope=hard_scope.model_copy(update=dict(include_reference=True)),
        k=SEMANTIC_POOL,
        legs=["vector", "visual"],
        ranker="fused",
        expand=False,
        include_nsfw=include_nsfw,
        include_images=False,
    )
    try:
        resp = recall_library(scope, req, token_row=token)
    except Exception:
        return []
    scored, visual = [], {}
    for rec in resp.records:
        mid = rec.get("message_id")
        if mid is None:
            continue
        vec_sim, seen_visual = None, False
        for d in rec.get("discovery") or []:
            if d.get("score") is None:
                continue
            if d.get("leg") == "vector" and vec_sim is None:
                vec_sim = float(d["score"])
            elif d.get("leg") == "visual":
                seen_visual = True
                model = (d.get("detail") or {}).get("model")
                visual.setdefault(model, []).append((int(mid), float(d["score"])))
        if vec_sim is None and not seen_visual and rec.get("score") is not None:
            vec_sim = float(rec["score"])
        if vec_sim is not None:
            scored.append((int(mid), vec_sim))
    kept = _keep(scored, SEMANTIC_MIN_ABS, SEMANTIC_REL_RATIO, SEMANTIC_MAX_KEEP)
    for model, hits in sorted(visual.items(), key=lambda x: str(x[0])):
        for mid in _keep(
            hits, VISUAL_MIN_ABS.get(model, 0.30), VISUAL_REL_RATIO, VISUAL_MAX_KEEP
        ):
            if mid not in kept:
                kept.append(mid)
    return kept


@router.post("/v1/timeline")
def timeline(data: TimelineRequest, token: Token):
    """Every match of one or more keyword queries (plus optional semantic matches) as episodes in time order."""
    require(token, "owner", "agent")
    scope = scope_of(token)
    s = data.scope
    q_list = []
    if data.query:
        q_list.append(data.query)
    if data.queries:
        for q in data.queries:
            if q and q.strip() and q not in q_list:
                q_list.append(q.strip())

    q_expr = " OR ".join(
        f"m.tsv @@ websearch_to_tsquery('english', {lit(q)})" for q in q_list
    )
    scope_where = ["m.superseded_by IS NULL"]
    if s.channels:
        scope_where.append(f"m.channel = ANY({lit(list(s.channels))})")
    if s.persons:
        if s.person_role == "involved":
            p_lit = lit([int(p) for p in s.persons])
            # The thread's other authors, or a person the message's images
            # show (depicts links, written from confirmed faces; face
            # recognition is for unrestricted askers only, as in api.links).
            depicts = (
                f" OR EXISTS(SELECT 1 FROM api.links l WHERE l.type='depicts' AND l.status='active' AND l.from_kind='message'"
                f" AND l.from_id=m.id AND l.to_kind='person' AND l.to_id = ANY({p_lit}))"
                if is_unrestricted(token)
                else ""
            )
            scope_where.append(
                f"(m.person_id = ANY({p_lit}) OR EXISTS(SELECT 1 FROM api.messages o WHERE o.thread_id=m.thread_id AND o.person_id = ANY({p_lit}))"
                f"{depicts})"
            )
        else:
            scope_where.append(f"m.person_id = ANY({lit([int(p) for p in s.persons])})")
    if s.after:
        scope_where.append(f"m.happened_at >= {lit(s.after.isoformat())}::timestamptz")
    if s.before:
        scope_where.append(f"m.happened_at < {lit(s.before.isoformat())}::timestamptz")
    if s.thread_ids:
        scope_where.append(f"m.thread_id = ANY({lit([int(t) for t in s.thread_ids])})")

    sem_ids = (
        _semantic_ids(q_list, s, scope, token, data.include_nsfw)
        if data.semantic
        else []
    )
    sem_set = set(sem_ids)
    if sem_ids:
        sem_lit = lit(sem_ids, "bigint[]")
        match_clause = f"(({q_expr}) OR m.id = ANY({sem_lit}))"
        kw_flag = f"({q_expr}) AS kw_match"
    else:
        match_clause = f"({q_expr})"
        kw_flag = "true AS kw_match"

    where = scope_where + [match_clause]
    joined_query = " ".join(q_list)
    statement = (
        "WITH hits AS MATERIALIZED (SELECT m.id, m.channel, m.external_id, m.happened_at,"
        f" m.thread_title, coalesce(m.display_name, m.author_handle) AS author,"
        f" left(m.content, 4000) AS content, {kw_flag}"
        f" FROM api.messages m WHERE {' AND '.join(where)} LIMIT {MAX_MATCHES + 1}),"
        " eps AS (SELECT * FROM api.episodes_of(ARRAY(SELECT id FROM hits)))"
        " SELECT e.episode_id, e.started_at, e.ended_at, h.id, h.channel, h.external_id,"
        " h.happened_at, h.thread_title, h.author, h.content, h.kw_match"
        " FROM hits h LEFT JOIN eps e ON e.message_id = h.id"
        " ORDER BY coalesce(e.started_at, h.happened_at), h.happened_at, h.id"
    )
    result = query_ex(statement, MAX_MATCHES + 1, scope=scope, token_row=token)
    rows = result["rows"]
    capped = len(rows) > MAX_MATCHES
    rows = rows[:MAX_MATCHES]
    episodes, order = {}, []
    for row in rows:
        key = row.get("episode_id") or f"m{row['id']}"
        episode = episodes.get(key)
        text, _ = snippet(row.get("content") or "", joined_query, data.text_chars)
        entry = dict(
            author=row.get("author"),
            text=text,
            channel=row.get("channel"),
            external_id=row.get("external_id"),
            message_id=row.get("id"),
        )
        is_kw = bool(row.get("kw_match"))
        is_sem = int(row["id"]) in sem_set
        if episode is None:
            episode = episodes[key] = dict(
                time=_time(row.get("started_at") or row.get("happened_at")),
                channel=row.get("channel"),
                thread=row.get("thread_title"),
                matches=0,
                _kw=False,
                _sem=False,
                first=entry,
                last=None,
                ids=[],
            )
            order.append(key)
        else:
            episode["last"] = entry
        if is_kw:
            episode["_kw"] = True
        if is_sem:
            episode["_sem"] = True
        episode["matches"] += 1
        if len(episode["ids"]) < 5:
            episode["ids"].append(
                dict(
                    channel=row.get("channel"),
                    external_id=row.get("external_id"),
                    message_id=row.get("id"),
                )
            )
    listed = []
    kw_ep_count = 0
    sem_only_count = 0
    for k in order:
        ep = episodes[k]
        kw_m = ep.pop("_kw")
        sem_m = ep.pop("_sem")
        if kw_m and sem_m:
            ep["match"] = "both"
            kw_ep_count += 1
        elif kw_m:
            ep["match"] = "keyword"
            kw_ep_count += 1
        else:
            ep["match"] = "semantic"
            sem_only_count += 1
        listed.append(ep)
    if data.order == "desc":
        listed.reverse()
    by_year, by_channel = {}, {}
    for episode in listed:
        year = (episode["time"] or "")[:4]
        by_year[year] = by_year.get(year, 0) + 1
        by_channel[episode["channel"]] = by_channel.get(episode["channel"], 0) + 1
    if len(listed) > data.max_episodes and data.sample == "spread":
        if data.max_episodes == 1:
            shown = [listed[0]]
        else:
            step = (len(listed) - 1) / (data.max_episodes - 1)
            idxs = sorted({round(i * step) for i in range(data.max_episodes)})
            shown = [listed[i] for i in idxs]
    else:
        shown = listed[: data.max_episodes]
    for episode in shown:
        if episode["matches"] == 1:
            episode.pop("ids", None)
            episode.pop("last", None)
    images.attach(
        images.reader(scope, token),
        [e for ep in shown for e in (ep.get("first"), ep.get("last")) if e],
        include_nsfw=data.include_nsfw,
    )
    cov_channels = list(s.channels) if s.channels else list(by_channel.keys())
    cov = _coverage_summary(cov_channels, s.after, s.before, scope, token)
    ep_times = [ep["time"] for ep in listed if ep.get("time")]
    out = dict(
        query=data.query if len(q_list) == 1 else q_list,
        total_matches=len(rows),
        total_episodes=len(listed),
        keyword_episodes=kw_ep_count,
        semantic_only_episodes=sem_only_count,
        episodes_by_year=dict(sorted(by_year.items())),
        episodes_by_channel=by_channel,
        episodes=shown,
        exact=not capped,
        note=(
            "Exhaustive keyword matches (websearch syntax: words ANDed, 'or' between"
            " alternatives, quotes for phrases, -word excludes; or pass a `queries` list)"
            " plus high-similarity semantic episodes when semantic=true (tagged"
            " match='keyword'|'semantic'|'both'), grouped into episodes in time order."
            " Set semantic=false for strict keyword-only counts; use sample='spread' to"
            " sample evenly across all eras when total_episodes > max_episodes; open key"
            " episodes with api.context before counting."
        ),
    )
    if ep_times:
        out["date_range"] = dict(first=min(ep_times), last=max(ep_times))
    if cov:
        out["coverage"] = cov
    if len(listed) > len(shown):
        out["warnings"] = [
            f"{len(listed) - len(shown)} more episodes not listed (max_episodes={data.max_episodes},"
            f" sample={data.sample}); use sample='spread', narrow with after/before/channels, or raise max_episodes"
        ]
    if capped:
        out.setdefault("warnings", []).append(
            f"more than {MAX_MATCHES} matches; totals are lower bounds: narrow the query"
        )
    audit(token, len(shown), operation="timeline", matches=len(rows))
    return out
