"""Evidence selection under a token budget (QRY-24, QRY-26, QRY-27, QRY-28).

Pure and deterministic: no statement runs here. The stages dedup (30),
select (40) and group (50) fetch what these functions need and call them.

A candidate's facts (kind, thread, channel, record time, support, text terms,
token count) are read from candidate fields and from candidate.meta['facts'],
which the stages fill from the gated record views.

  support_key(c)            the raw source messages a record rests on
  independent_groups(cs)    records that rest on overlapping sources are one
                            group; independent_sources = distinct raw sources
  dependencies(c, ...)      heuristic context dependencies of one candidate
  select(cs, budget, k, ..) greedy joint selection by marginal gain
  group_manifest(cs, by)    the manifest of a grouping
"""

import os
import re
from datetime import timezone

RAW_KINDS = ("life_event", "reference")
GENERATED_KINDS = ("page_revision", "derived", "passage", "link")
DEFAULT_SHORT_TOKENS = 12
# Leading words that make a message read as a reply to something before it.
ANAPHORA = frozenset(
    """yes yeah yep yup no nope nah ok okay k sure agreed agree same exactly
    right true thanks thank thx ty lol haha hah ha cool nice great wow oh ah
    that this it its it's those these they them he she him her there then so
    also too and but which what why how really indeed definitely totally""".split()
)
REPLY_LINK_TYPES = ("reply_to", "replies_to", "quote", "quotes", "quoted")
DROP_REASONS = ("budget", "redundant", "duplicate_group", "duplicate_alias", "rank")
GRAINS = (
    "conversation",
    "thread",
    "channel",
    "day",
    "month",
    "year",
    "person",
    "entity",
    "support",
    "episode",
)
DEFAULT_WEIGHTS = dict(
    relevance=1.0,
    coverage=1.0,
    entities=0.2,
    conversations=0.2,
    redundancy=0.5,
    conversation=0.15,
    thread=0.1,
    channel=0.05,
    time=0.05,
    entity=0.05,
)
DIVERSITY = ("conversation", "thread", "channel", "time", "entity")
EPISODE_GAP_S = 1800

# The snowball English stop list Postgres uses for 'english' text search.
STOPWORDS = frozenset(
    """i me my myself we our ours ourselves you your yours yourself yourselves
    he him his himself she her hers herself it its itself they them their
    theirs themselves what which who whom this that these those am is are was
    were be been being have has had having do does did doing a an the and but
    if or because as until while of at by for with about against between into
    through during before after above below to from up down in out on off over
    under again further then once here there when where why how all any both
    each few more most other some such no nor not only own same so than too
    very s t can will just don should now""".split()
)
_WORD = re.compile(r"\w+")


def short_tokens():
    try:
        return max(0, int(os.getenv("THROUGHLINE_DEP_SHORT_TOKENS") or 12))
    except ValueError:
        return DEFAULT_SHORT_TOKENS


def _stem(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def terms(text):
    """Content terms of a text: lowercased words without stop words, stemmed."""
    words = _WORD.findall((text or "").lower())
    return {_stem(w) for w in words if w not in STOPWORDS and len(w) > 1}


def has_content_tokens(text):
    """Whether a text has a word that is not a stop word (the same test that
    decides whether an 'english' tsquery is empty)."""
    return any(w not in STOPWORDS for w in _WORD.findall((text or "").lower()))


def leading_words(text, n=2):
    return _WORD.findall((text or "").lower().replace("'", ""))[:n]


# --- facts -------------------------------------------------------------------


def facts(candidate):
    return candidate.meta.get("facts") or {}


def kind_of(candidate):
    return candidate.kind or facts(candidate).get("kind")


def message_ref(candidate):
    """The message id of a message record ('m:'), else None."""
    if candidate.prefix != "m":
        return None
    if candidate.message_id is not None:
        return int(candidate.message_id)
    try:
        return candidate.ref_id
    except (IndexError, ValueError):
        return None


def is_generated(candidate):
    value = candidate.generated
    if value is None:
        value = facts(candidate).get("generated")
    if value is None:
        kind = kind_of(candidate)
        return kind in GENERATED_KINDS or candidate.prefix not in ("m", "e")
    return bool(value)


def _support(candidate):
    support = list(candidate.support or [])
    for entry in facts(candidate).get("support") or []:
        if entry not in support:
            support.append(entry)
    return support


def support_ids(candidate):
    """Message ids a generated record cites (relation 'self' excluded), in
    citation order."""
    own = message_ref(candidate)
    ids = []
    for entry in _support(candidate):
        if not isinstance(entry, dict):
            continue
        mid = entry.get("message_id")
        if mid is None or entry.get("relation") == "self" or mid == own:
            continue
        if mid not in ids:
            ids.append(mid)
    return ids


def support_key(candidate):
    """frozenset of the raw source message ids a record rests on.

    A raw message (life_event or reference) is its own source. A generated
    record (page revision, derived, passage, claim, link) rests on the
    messages it cites. A generated record without support returns None: it
    forms its own group, flagged unsupported, and adds 0 independent sources.
    """
    own = message_ref(candidate)
    if own is not None and not is_generated(candidate):
        return frozenset([own])
    ids = support_ids(candidate)
    return frozenset(ids) if ids else None


def independent_groups(candidates):
    """{record_id: dict(group, independent_sources, unsupported, sources)}.

    Candidates whose support keys overlap are one group (a passage, its page
    and the raw message they were built from). group is 'ig:<lowest source
    id>' (or 'ig:<record_id>' for an unsupported record)."""
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    keys = {}
    for c in candidates:
        key = support_key(c)
        keys[c.record_id] = key
        for mid in key or ():
            parent.setdefault(mid, mid)
        if key:
            first = min(key)
            for mid in key:
                union(first, mid)
    members = {}
    for record_id, key in keys.items():
        if key:
            members.setdefault(find(min(key)), set()).update(key)
    result = {}
    for c in candidates:
        key = keys[c.record_id]
        if not key:
            result[c.record_id] = dict(
                group=f"ig:{c.record_id}",
                independent_sources=0,
                unsupported=True if is_generated(c) or c.unsupported else False,
                sources=frozenset(),
            )
            continue
        root = find(min(key))
        sources = frozenset(members[root])
        result[c.record_id] = dict(
            group=f"ig:{root}",
            independent_sources=len(sources),
            unsupported=False,
            sources=sources,
        )
    return result


def independent_source_count(candidates):
    """Distinct raw source messages behind the candidates (unsupported
    generated records add 0)."""
    sources = set()
    for info in independent_groups(candidates).values():
        sources |= info["sources"]
    return len(sources)


# --- dependencies ------------------------------------------------------------


def dependency_triggers(candidate):
    """Why a candidate may need context: short, anaphora, cited_source."""
    found = []
    info = facts(candidate)
    if message_ref(candidate) is not None and not is_generated(candidate):
        tokens = (
            candidate.tokens if candidate.tokens is not None else info.get("tokens")
        )
        if tokens is not None and tokens <= short_tokens():
            found.append("short")
        lead = info.get("lead") or []
        if lead and lead[0] in ANAPHORA:
            found.append("anaphora")
    elif is_generated(candidate) and support_ids(candidate):
        found.append("cited_source")
    return found


def dependencies(candidate, neighbours=(), links=()):
    """[{message_id, reason}] the candidate depends on (heuristic triggers
    only): the previous turn of a short or anaphoric message, the target of
    an active reply_to/quote link, the one cited source of a generated record.

    neighbours: rows {anchor_id, message_id, thread_id, thread_no} of the
    candidate's thread window; links: rows {type, from_id, to_id}."""
    own = message_ref(candidate)
    triggers = dependency_triggers(candidate)
    result = []

    def add(mid, reason):
        if mid is None or mid == own:
            return
        if any(d["message_id"] == mid for d in result):
            return
        result.append(dict(message_id=int(mid), reason=reason))

    if own is not None and ("short" in triggers or "anaphora" in triggers):
        thread_no = candidate.thread_no
        if thread_no is None:
            thread_no = facts(candidate).get("thread_no")
        before = [
            n
            for n in neighbours
            if n.get("anchor_id") in (None, own)
            and n.get("message_id") != own
            and thread_no is not None
            and n.get("thread_no") is not None
            and n["thread_no"] < thread_no
        ]
        if before:
            previous = max(before, key=lambda n: (n["thread_no"], n["message_id"]))
            add(previous["message_id"], "short" if "short" in triggers else "anaphora")
    if own is not None:
        for link in sorted(links, key=lambda x: x.get("to_id") or 0):
            if link.get("type") in REPLY_LINK_TYPES and link.get("from_id") == own:
                add(link.get("to_id"), "reply_link")
    if "cited_source" in triggers:
        add(support_ids(candidate)[0], "cited_source")
    return result


# --- selection ---------------------------------------------------------------


def _span(c):
    span = c.span or {}
    first = span.get("first_no")
    last = span.get("last_no")
    if first is None or last is None:
        no = c.thread_no if c.thread_no is not None else facts(c).get("thread_no")
        first = first if first is not None else no
        last = last if last is not None else no
    return first, last


def _thread(c):
    return c.thread_id if c.thread_id is not None else facts(c).get("thread_id")


def _conversation(c):
    if c.conversation and c.conversation.get("id") is not None:
        return f"conversation:{c.conversation['id']}"
    thread = _thread(c)
    return f"thread:{thread}" if thread is not None else None


def _day(c):
    when = facts(c).get("record_time")
    if when is None:
        return None
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d")


def entities(c):
    """Entity/person record ids a candidate was reached through (or is)."""
    found = set()
    if c.prefix == "e":
        found.add(c.record_id)
    for entry in c.discovery or []:
        detail = entry.get("detail") if isinstance(entry, dict) else None
        via = detail.get("via") if isinstance(detail, dict) else None
        if isinstance(via, str) and (via.startswith("e:") or via.startswith("person:")):
            found.add(via)
    return found


def _features(c, groups):
    info = facts(c)
    group = groups.get(c.record_id) or {}
    return dict(
        terms=set(info.get("terms") or ()),
        entities=entities(c),
        conversation=_conversation(c),
        thread=_thread(c),
        channel=info.get("channel"),
        time=_day(c),
        span=_span(c),
        group=group.get("group"),
        sources=group.get("sources") or frozenset(),
        raw=message_ref(c) is not None and not is_generated(c),
    )


def _tokens(c):
    if c.tokens is not None:
        return int(c.tokens)
    return int(facts(c).get("tokens") or 0)


class _State:
    def __init__(self):
        self.terms = set()
        self.entities = set()
        self.conversations = set()
        self.counts = {d: {} for d in DIVERSITY}
        self.spans = []  # (thread, first, last)
        self.groups = {}  # group -> [raw?]
        self.sources = set()

    def add(self, f):
        self.terms |= f["terms"]
        self.entities |= f["entities"]
        if f["conversation"] is not None:
            self.conversations.add(f["conversation"])
        for d in DIVERSITY:
            values = f["entities"] if d == "entity" else [f[d]]
            for value in values:
                if value is not None:
                    self.counts[d][value] = self.counts[d].get(value, 0) + 1
        first, last = f["span"]
        if f["thread"] is not None and first is not None:
            self.spans.append((f["thread"], first, last))
        if f["group"] is not None:
            self.groups.setdefault(f["group"], []).append(f["raw"])
        self.sources |= f["sources"]


def _contained(f, state):
    first, last = f["span"]
    if f["thread"] is None or first is None:
        return False
    return any(t == f["thread"] and a <= first and last <= b for t, a, b in state.spans)


def _overlaps(f, state):
    first, last = f["span"]
    if f["thread"] is None or first is None:
        return False
    return any(t == f["thread"] and a <= last and first <= b for t, a, b in state.spans)


def gain(f, relevance, state, query_terms, weights, diversity=True):
    """Marginal gain of adding a candidate with features f to the state."""
    w = weights
    value = w["relevance"] * relevance
    if query_terms:
        new = (f["terms"] & query_terms) - state.terms
        value += w["coverage"] * len(new) / len(query_terms)
    if f["entities"] - state.entities:
        value += w["entities"]
    if f["conversation"] is not None and f["conversation"] not in state.conversations:
        value += w["conversations"]
    if _overlaps(f, state):
        value -= w["redundancy"]
    if f["group"] is not None and f["group"] in state.groups:
        value -= w["redundancy"]
    if diversity:
        for d in DIVERSITY:
            values = f["entities"] if d == "entity" else [f[d]]
            same = max(
                (state.counts[d].get(v, 0) for v in values if v is not None), default=0
            )
            if same:
                value -= w[d] * same / (same + 1)
    return value


def select(
    candidates,
    budget_tokens,
    k,
    weights=None,
    *,
    query="",
    deps=None,
    diversity=True,
    groups=None,
):
    """Greedy joint selection -> (selected, dropped).

    candidates: role 'hit' Candidates in rank order (facts in meta). deps:
    {record_id: [dependency Candidate]} for candidates whose dependencies
    are known; a candidate and its unmet dependencies are taken jointly or
    not at all. budget_tokens None = unlimited; k counts hits only.

    selected keeps rank order of the hits, each preceded by the dependencies
    taken for it (thread order). dropped: [{record_id, reason, ...}] with
    reasons budget, redundant, duplicate_group, rank. Ties go to the earlier
    candidate, so the result is deterministic.
    """
    weights = dict(DEFAULT_WEIGHTS, **(weights or {}))
    deps = deps or {}
    groups = groups if groups is not None else independent_groups(candidates)
    query_terms = terms(query)
    pool = list(enumerate(candidates))
    features = {c.record_id: _features(c, groups) for c in candidates}
    top = max((c.score or 0.0 for c in candidates), default=0.0)
    relevance = {
        c.record_id: ((c.score or 0.0) / top if top > 0 else 0.0) for c in candidates
    }
    state = _State()
    remaining = budget_tokens
    taken = {}  # record_id -> (index, Candidate)
    taken_deps = {}  # record_id -> dependency Candidate
    hits = 0
    dropped = []
    in_pool = {c.record_id for c in candidates}

    def fits(cost):
        return remaining is None or cost <= remaining

    while pool and hits < k:
        best = None
        for index, c in pool:
            f = features[c.record_id]
            score = gain(
                f, relevance[c.record_id], state, query_terms, weights, diversity
            )
            if best is None or score > best[0] + 1e-12:
                best = (score, index, c)
        _, index, c = best
        pool = [(i, x) for i, x in pool if x.record_id != c.record_id]
        f = features[c.record_id]
        if _contained(f, state) and c.record_id not in taken_deps:
            dropped.append(dict(record_id=c.record_id, reason="redundant"))
            continue
        if (
            f["group"] is not None
            and f["group"] in state.groups
            and f["sources"]
            and f["sources"] <= state.sources
            and not ((f["terms"] & query_terms) - state.terms)
            and not (f["raw"] and not any(state.groups[f["group"]]))
            and c.record_id not in taken_deps
        ):
            dropped.append(dict(record_id=c.record_id, reason="duplicate_group"))
            continue
        if c.record_id in taken_deps:
            # Already included as a dependency of another hit: it becomes a hit.
            taken_deps.pop(c.record_id)
            c.role, c.depends_on = "hit", None
            taken[c.record_id] = (index, c)
            hits += 1
            continue
        unmet = [
            d
            for d in deps.get(c.record_id, [])
            if d.record_id not in taken and d.record_id not in taken_deps
        ]
        cost = _tokens(c) + sum(_tokens(d) for d in unmet)
        if not fits(cost):
            entry = dict(record_id=c.record_id, reason="budget")
            if unmet:
                entry["with_dependencies"] = [d.record_id for d in unmet]
            dropped.append(entry)
            continue
        if remaining is not None:
            remaining -= cost
        c.role, c.depends_on = "hit", None
        taken[c.record_id] = (index, c)
        state.add(f)
        hits += 1
        for d in unmet:
            waiting = next(
                ((i, x) for i, x in pool if x.record_id == d.record_id), None
            )
            if waiting is not None:
                pool = [(i, x) for i, x in pool if x.record_id != d.record_id]
            if waiting is not None and hits < k:
                # A dependency that is itself a hit in scope stays a hit (while
                # k allows; past k it is taken as context only).
                hit_index, hit = waiting
                hit.role, hit.depends_on = "hit", None
                taken[hit.record_id] = (hit_index, hit)
                state.add(features[hit.record_id])
                hits += 1
                continue
            if d.record_id in in_pool:
                # Passed over as a hit earlier; it is now needed as context.
                dropped = [x for x in dropped if x["record_id"] != d.record_id]
            d.role, d.depends_on = "dependency", c.record_id
            taken_deps[d.record_id] = d
            state.add(_features(d, groups))
    for _, c in pool:
        dropped.append(dict(record_id=c.record_id, reason="rank"))
    by_anchor = {}
    for d in taken_deps.values():
        by_anchor.setdefault(d.depends_on, []).append(d)
    selected = []
    for _, c in sorted(taken.values(), key=lambda pair: pair[0]):
        for d in sorted(
            by_anchor.get(c.record_id, []),
            key=lambda x: (_span(x)[0] if _span(x)[0] is not None else 0, x.record_id),
        ):
            selected.append(d)
        selected.append(c)
    return selected, dropped


# --- ordering ----------------------------------------------------------------

RELEVANCE_LEGS = ("lexical", "vector", "passages")
DISCOVERY_LEGS = ("entity", "links")


def query_relevant(candidate):
    """Found with query relevance: a score above zero from the lexical, vector
    or passages leg, or entity/link discovery."""
    for entry in candidate.discovery or []:
        leg = entry.get("leg") if isinstance(entry, dict) else None
        if leg in DISCOVERY_LEGS:
            return True
        if leg in RELEVANCE_LEGS and (entry.get("score") or 0) > 0:
            return True
    return False


def newest_relevant(selected, query=""):
    """Hits ordered by record time, newest first, among the query-relevant
    ones; when the query has content tokens, the others (for example found
    only by the structured leg) follow. Dependencies stay before their hit."""
    hits = [c for c in selected if c.role != "dependency"]
    by_anchor = {}
    for c in selected:
        if c.role == "dependency":
            by_anchor.setdefault(c.depends_on, []).append(c)

    def key(c):
        when = facts(c).get("record_time")
        stamp = when.timestamp() if when is not None else float("-inf")
        return (-stamp, c.record_id)

    if has_content_tokens(query):
        relevant = sorted([c for c in hits if query_relevant(c)], key=key)
        rest = sorted([c for c in hits if not query_relevant(c)], key=key)
        ordered = relevant + rest
    else:
        ordered = sorted(hits, key=key)
    result = []
    for c in ordered:
        result.extend(by_anchor.get(c.record_id, []))
        result.append(c)
    return result


# --- grouping ----------------------------------------------------------------


def _bucket(when, unit):
    when = when.astimezone(timezone.utc)
    return when.strftime({"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}[unit])


def group_key(candidate, by, independent=None):
    """The group key of a candidate for a grain other than episode, or None."""
    info = facts(candidate)
    if by == "conversation":
        return _conversation(candidate)
    if by == "thread":
        thread = _thread(candidate)
        return f"thread:{thread}" if thread is not None else None
    if by == "channel":
        return f"channel:{info['channel']}" if info.get("channel") else None
    if by in ("day", "month", "year"):
        when = info.get("record_time")
        return f"{by}:{_bucket(when, by)}" if when is not None else None
    if by == "person":
        pid = info.get("person_id")
        return f"person:{pid}" if pid is not None else None
    if by == "entity":
        found = sorted(entities(candidate))
        return "entity:" + found[0] if found else None
    if by == "support":
        group = (independent or {}).get(candidate.record_id) or candidate.meta.get(
            "independent_group"
        )
        if isinstance(group, dict):
            group = group.get("group")
        return f"support:{group}" if group else None
    raise ValueError("group_by is " + ", ".join(GRAINS))


def fallback_episodes(candidates, gap_s=EPISODE_GAP_S):
    """{record_id: episode id} without api.episodes_of: hits of one thread in
    record-time order form one episode while consecutive hits are at most
    gap_s apart. Ids follow the 'ep:<thread_id>:<first message id>' form."""
    by_thread = {}
    result = {}
    for c in candidates:
        mid = message_ref(c)
        if mid is None:
            continue
        thread = _thread(c)
        if thread is None:
            result[c.record_id] = f"ep:m:{mid}"
            continue
        by_thread.setdefault(thread, []).append(c)
    for thread, items in by_thread.items():

        def when(c):
            value = facts(c).get("record_time")
            return value.timestamp() if value is not None else float("-inf")

        items.sort(key=lambda c: (when(c), message_ref(c)))
        current, last = None, None
        for c in items:
            t = when(c)
            if current is None or last is None or t - last > gap_s:
                current = f"ep:{thread}:{message_ref(c)}"
            last = t
            result[c.record_id] = current
    return result


def group_manifest(candidates, by, keys=None, independent=None):
    """{by, unit, groups:[{key, count, record_ids[, singleton]}], records,
    incidents, omitted}. keys: {record_id: key} (computed with group_key when
    not given). A record without a key forms its own singleton group keyed by
    record_id. unit is 'episode' for the episode grain (incidents = number of
    episode groups; a singleton group of a non-message record is not an
    incident), else 'record' (incidents None: a record count is never an
    incident count)."""
    if by not in GRAINS:
        raise ValueError("group_by is " + ", ".join(GRAINS))
    groups, order, incidents = {}, [], set()
    for c in candidates:
        if keys is not None:
            key = keys.get(c.record_id)
        else:
            key = group_key(c, by, independent)
        singleton = key is None
        key = key if key is not None else c.record_id
        if key not in groups:
            groups[key] = dict(key=key, count=0, record_ids=[])
            if singleton:
                groups[key]["singleton"] = True
            order.append(key)
        if not singleton or message_ref(c) is not None:
            # A singleton page, passage, claim, entity or link is not an
            # incident; a message is one, even without an episode id.
            incidents.add(key)
        groups[key]["count"] += 1
        groups[key]["record_ids"].append(c.record_id)
    episode = by == "episode"
    return dict(
        by=by,
        unit="episode" if episode else "record",
        groups=[groups[k] for k in order],
        records=len(candidates),
        incidents=len(incidents) if episode else None,
        omitted=0,
    )
