"""Retrieval arms: one named, recorded configuration each.

An arm talks to the system over HTTP only and returns an ArmResult. Two
things can keep an arm from answering, and neither is ever scored as zero:
the system lacks something the arm needs (the whole arm is skipped), or the
question needs a capability the arm does not have (that question is skipped
for that arm).

Wave-1 arms read GET /v1/search (keyword, meaning, fused) or the gold labels
(oracle, none). The v1.1 arms drive POST /v1/recall with one switch changed
each (V11_ARMS), so an ablation compares settings that differ in one place;
sessions, match and librarian add the working-set routes, and the three
LIB-13 configurations (ordinary, equipped, delegated) give a simulated main
agent equal access and budgets. Every arm records its settings, and every
result carries the stage candidates the system reported (BEN-9), tokens,
model cost, agent-visible turns and the ids a configuration cites.
"""

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

OK, SKIPPED, ERROR = "ok", "skipped", "error"
# Capabilities a question may need (bench.gold.Question.needs).
CAPABILITIES = frozenset(
    {"as_of", "known_by", "versions", "subject_time", "links", "pages"}
)
# What every recall-based arm can honour: as_of, known_by and subject time
# are hard-scope fields, versions are page revisions found through passages.
RECALL_CAPABILITIES = frozenset({"as_of", "known_by", "versions", "subject_time"})
DEFAULT_K = 10
# Equal budgets for the three LIB-13 configurations (and every recall arm
# that does not ablate the token budget).
LIB13_BUDGETS = dict(tokens=4000, deadline_ms=20000)
# Stage names of BEN-9, in pipeline order.
STAGES = ("before_rerank", "after_rerank", "after_selection")
STOP = frozenset(
    """a about after again all an and any are as at be been before being by can
    did do does for from had has have how i if in is it its many me my no not
    now of on one only or our so some than that the their them then there these
    they this those times to too up was we were what when where which who why
    will with you your""".split()
)


@dataclass
class ArmResult:
    # Delivered message ids, best first.
    records: list = field(default_factory=list)
    # record id -> message ids of the span returned around it.
    spans: dict = field(default_factory=dict)
    # stage name -> candidate ids that stage held (BEN-9).
    stages: dict = field(default_factory=dict)
    tokens: int = 0
    latency_ms: float = 0.0
    status: str = OK
    # The system's own statement that the search was not complete.
    incomplete: bool = False
    disclosures: list = field(default_factory=list)
    reason: str | None = None
    raw: Any = None
    # v1.1: what the configuration cost and how the agent saw it.
    model_cost: float = 0.0
    # Calls the simulated main agent made (agent-visible turns).
    turns: int = 0
    # Ids the configuration cites as evidence (LIB-13 citation faithfulness).
    cited: list = field(default_factory=list)
    # Tokens of everything returned to the simulated main agent.
    context_tokens: int = 0
    execution: dict | None = None
    manifest: Any = None
    # Every stage the system reported, legs included (for leakage).
    seen: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


def skipped(reason):
    return ArmResult(status=SKIPPED, reason=reason)


@dataclass
class Arm:
    name: str
    settings: dict
    # What the system must offer for the arm to run at all (see probe()).
    needs: frozenset
    fn: Callable
    # What the arm can honour of a question's needs.
    capabilities: frozenset = frozenset()
    description: str = ""

    def unmet(self, available):
        return sorted(self.needs - set(available))

    def can_pose(self, question):
        return sorted(set(question.needs) - self.capabilities)

    def run(self, client, question):
        missing = self.can_pose(question)
        if missing:
            return skipped("question needs " + ", ".join(missing))
        started = time.perf_counter()
        result = self.fn(self, client, question)
        if not result.latency_ms:
            result.latency_ms = (time.perf_counter() - started) * 1000
        return result

    def with_settings(self, name=None, **changes):
        """A copy of this arm with settings changed (and recorded)."""
        settings = copy.deepcopy(self.settings)
        settings.update(changes)
        return Arm(
            name=name or self.name,
            settings=settings,
            needs=self.needs,
            fn=self.fn,
            capabilities=self.capabilities,
            description=self.description,
        )


def estimate_tokens(text):
    """The system's own rule of thumb: four characters a token."""
    return (len(text or "") + 3) // 4


def json_tokens(value):
    """Tokens of a JSON body as an agent would read it."""
    return estimate_tokens(json.dumps(value, default=str, separators=(",", ":")))


def _oracle(arm, client, question):
    # One acceptable source for every required group, nothing else: the
    # ceiling every metric is calibrated against.
    records = [g.alternatives[0] for g in question.groups if g.required]
    return ArmResult(
        records=records,
        stages={name: list(records) for name in STAGES},
        incomplete=question.expects_incomplete,
        disclosures=list(question.expects_disclosure),
        cited=list(records),
    )


def _none(arm, client, question):
    return ArmResult()


def _search(arm, client, question):
    params = dict(
        q=question.text, mode=arm.settings["mode"], max_results=arm.settings["k"]
    )
    # Runtime filtering by record time is the only cutoff this route has.
    cutoff = question.as_of or question.cutoff
    if cutoff is not None and arm.settings["honour_cutoff"]:
        params["before_at"] = cutoff.isoformat()
    started = time.perf_counter()
    response = client.get("/v1/search", params=params)
    elapsed = (time.perf_counter() - started) * 1000
    if response.status_code == 400 and arm.settings["mode"] != "keyword":
        # Meaning and fused search answer 400 until embeddings are configured
        # and a model is serving: unavailable, not wrong.
        try:
            detail = response.json().get("detail", "unavailable")
        except (ValueError, AttributeError):
            detail = response.text or "unavailable"
        return ArmResult(status=SKIPPED, reason=str(detail), latency_ms=elapsed)
    if response.status_code != 200:
        return ArmResult(
            status=ERROR,
            reason=f"HTTP {response.status_code}",
            latency_ms=elapsed,
            raw=response.text,
        )
    data = response.json()
    rows = data["rows"]
    records = [row["id"] for row in rows]
    return ArmResult(
        records=records,
        stages={"after_selection": list(records)},
        tokens=sum(estimate_tokens(row.get("content")) for row in rows),
        latency_ms=elapsed,
        incomplete=bool(data.get("truncated")),
        disclosures=["truncated"] if data.get("truncated") else [],
        raw=data,
        turns=1,
        cited=records[:3],
        context_tokens=json_tokens(data),
    )


def _search_arm(mode, needs, description, k=DEFAULT_K):
    return Arm(
        name=mode,
        settings=dict(route="GET /v1/search", mode=mode, k=k, honour_cutoff=True),
        needs=frozenset(needs),
        fn=_search,
        capabilities=frozenset({"as_of"}),
        description=description,
    )


# --- recall and the v1.1 routes ---------------------------------------------------


def _iso(value):
    return value.isoformat() if value is not None else None


def scope_for(question, base=None):
    """The hard scope a question asks for: cutoffs and its time window."""
    scope = copy.deepcopy(base or {})
    if question.as_of is not None:
        scope["as_of"] = _iso(question.as_of)
    if question.known_by is not None:
        scope["known_by"] = _iso(question.known_by)
    if question.after is not None:
        scope["after"] = _iso(question.after)
    if question.before is not None:
        scope["before"] = _iso(question.before)
    if "subject_time" in question.needs:
        scope["time_basis"] = "subject"
    return scope


def recall_body(settings, question, query=None):
    """POST /v1/recall body for a question under an arm's settings."""
    body = dict(
        query=question.text if query is None else query,
        k=settings.get("k", DEFAULT_K),
        trace=True,
    )
    request = copy.deepcopy(settings.get("request") or {})
    scope = scope_for(question, request.pop("scope", None))
    body.update(request)
    if scope:
        body["scope"] = scope
    return body


def message_of(record_id):
    """Message id of a 'm:<id>' record id, else None."""
    if isinstance(record_id, str) and record_id.startswith("m:"):
        try:
            return int(record_id[2:])
        except ValueError:
            return None
    return None


def resolve_record_ids(client, record_ids, known=None):
    """{record id: message id} for message and passage record ids.

    Message ids come from the id itself; passage ids (p:) from one statement
    over api.passage_records. Other kinds have no message and are left out.
    """
    out = dict(known or {})
    passages = []
    for rid in record_ids:
        if rid in out:
            continue
        mid = message_of(rid)
        if mid is not None:
            out[rid] = mid
        elif isinstance(rid, str) and rid.startswith("p:") and rid[2:].isdigit():
            passages.append(int(rid[2:]))
    if passages:
        response = client.post(
            "/v1/sql",
            json=dict(
                statement="SELECT record_id, message_id FROM api.passage_records"
                f" WHERE ref_id IN ({','.join(str(p) for p in sorted(set(passages)))})",
                row_cap=10000,
            ),
        )
        if response.status_code == 200:
            for row in response.json()["rows"]:
                if row.get("message_id") is not None:
                    out[row["record_id"]] = row["message_id"]
    return out


def _span_members(client, records):
    """{message id: [message ids of the thread span returned around it]}."""
    wanted = [
        (r["message_id"], r["thread_id"], r["span"])
        for r in records
        if r.get("message_id") is not None
        and r.get("thread_id") is not None
        and isinstance(r.get("span"), dict)
        and r["span"].get("first_no") is not None
        and r["span"].get("last_no") is not None
        and r["span"]["first_no"] != r["span"]["last_no"]
    ]
    if not wanted:
        return {}
    where = " OR ".join(
        f"(thread_id={int(t)} AND thread_no BETWEEN {int(s['first_no'])} AND {int(s['last_no'])})"
        for _, t, s in wanted
    )
    response = client.post(
        "/v1/sql",
        json=dict(
            statement=f"SELECT id, thread_id, thread_no FROM api.messages WHERE {where}",
            row_cap=10000,
        ),
    )
    if response.status_code != 200:
        return {}
    rows = response.json()["rows"]
    out = {}
    for mid, thread, span in wanted:
        out[mid] = [
            row["id"]
            for row in rows
            if row["thread_id"] == thread
            and span["first_no"] <= row["thread_no"] <= span["last_no"]
        ]
    return out


def stage_view(client, execution, records):
    """(BEN-9 stages, every reported stage) as message ids.

    before_rerank is what the candidate pool held when ranking began (after
    spans, else the fused list), after_rerank the rerank stage's output (the
    same pool when no reranker ran), after_selection what was returned.
    """
    reported = (execution or {}).get("stage_candidates") or {}
    known = {
        r["record_id"]: r["message_id"]
        for r in records
        if r.get("message_id") is not None
    }
    every = set()
    for key, value in reported.items():
        if key == "legs" and isinstance(value, dict):
            for ids in value.values():
                every.update(ids or [])
        elif isinstance(value, list):
            every.update(value)
    mapping = resolve_record_ids(client, sorted(every), known)

    def ids(values):
        out = []
        for rid in values or []:
            mid = mapping.get(rid)
            if mid is not None and mid not in out:
                out.append(mid)
        return out

    seen = {}
    for key, value in reported.items():
        if key == "legs" and isinstance(value, dict):
            for leg, values in value.items():
                seen["leg:" + leg] = ids(values)
        elif isinstance(value, list):
            seen[key] = ids(value)
    if not reported:
        return {}, {}
    before = reported.get("after_spans") or reported.get("fused") or []
    after_rerank = reported.get("after_rerank", before)
    selected = reported.get("selected")
    if selected is None:
        selected = [r["record_id"] for r in records]
    stages = dict(
        before_rerank=ids(before),
        after_rerank=ids(after_rerank),
        after_selection=ids(selected),
    )
    return stages, seen


def disclosures_of(execution):
    """Reasons the system gave that the search was partial, as terms."""
    out = []
    execution = execution or {}
    truncation = execution.get("truncation")
    if truncation:
        out.append("truncated:" + str(truncation.get("reason")))
    for item in execution.get("incomplete") or []:
        out.append(f"{item.get('stage')}:{item.get('reason')}")
    coverage = execution.get("coverage") or {}
    for gap in coverage.get("gaps") or []:
        out.append("import_gap:" + str(gap.get("channel")))
    if coverage.get("absence"):
        out.append("absence:" + str(coverage["absence"]))
    out += [str(w) for w in execution.get("warnings") or []]
    return out


def incomplete_of(execution):
    """Did the system declare the search incomplete?

    A truncation, a failed leg or stage, an expired deadline, or source
    coverage with gaps or partial channels in the asked window. A leg that
    is merely unavailable (no embeddings configured) is disclosed but is
    not an incomplete search.
    """
    execution = execution or {}
    if execution.get("truncation"):
        return True
    for stage in execution.get("stages") or []:
        if stage.get("status") == "failed":
            return True
    budget = execution.get("budget") or {}
    if budget.get("expired"):
        return True
    coverage = execution.get("coverage") or {}
    return bool(coverage.get("gaps"))


def cost_of(execution):
    budget = (execution or {}).get("budget") or {}
    spent = budget.get("spent") or {}
    return float(spent.get("model_cost") or 0.0)


def cite(question, records, delivered, n=3):
    """The scripted main agent's citation policy, the same for every
    configuration: the first n delivered message records whose text shares a
    content word with the question, else the first record."""
    words = {
        w.strip("?.,:;!").lower()
        for w in question.text.split()
        if w.strip("?.,:;!").lower() not in STOP
    }
    chosen = []
    for record in records:
        mid = record.get("message_id")
        if mid is None or mid in chosen:
            continue
        text = (record.get("content") or "").lower()
        if any(w and w in text for w in words):
            chosen.append(mid)
        if len(chosen) >= n:
            break
    if not chosen and delivered:
        chosen = [delivered[0]]
    return chosen


def from_records(client, records, execution=None, follow_support=False):
    """(delivered message ids, spans) from returned records."""
    delivered, spans = [], {}
    span_members = _span_members(client, records)
    for record in records:
        mid = record.get("message_id")
        if mid is None or mid in delivered:
            continue
        delivered.append(mid)
        members = set(span_members.get(mid) or [])
        if follow_support and record.get("generated"):
            members.update(
                s["message_id"]
                for s in record.get("support") or []
                if s.get("message_id") is not None
            )
        if members - {mid}:
            spans[mid] = sorted(members | {mid})
    return delivered, spans


def _http_error(response, started):
    return ArmResult(
        status=ERROR,
        reason=f"HTTP {response.status_code}",
        latency_ms=(time.perf_counter() - started) * 1000,
        raw=response.text[:500],
    )


def recall_result(client, data, question, latency_ms, follow_support=False):
    """An ArmResult from a POST /v1/recall response body."""
    records = data.get("records") or []
    execution = data.get("execution") or {}
    delivered, spans = from_records(client, records, execution, follow_support)
    stages, seen = stage_view(client, execution, records)
    return ArmResult(
        records=delivered,
        spans=spans,
        stages=stages,
        seen=seen,
        tokens=sum(int(r.get("tokens") or 0) for r in records),
        latency_ms=latency_ms,
        incomplete=incomplete_of(execution),
        disclosures=disclosures_of(execution),
        raw=None,
        model_cost=cost_of(execution),
        turns=1,
        cited=cite(question, records, delivered),
        context_tokens=json_tokens(data),
        execution=execution,
        manifest=data.get("manifest"),
        extra=dict(result_set=data.get("result_set")),
    )


def _recall(arm, client, question):
    body = recall_body(arm.settings, question)
    started = time.perf_counter()
    response = client.post("/v1/recall", json=body)
    elapsed = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        return _http_error(response, started)
    result = recall_result(
        client,
        response.json(),
        question,
        elapsed,
        follow_support=arm.settings.get("follow_support", False),
    )
    if arm.settings.get("link_expansion") and result.extra.get("result_set"):
        _expand_links(client, question, result)
    return result


def _expand_links(client, question, result):
    """recall_link_expansion: follow links of the saved set (POST /v1/expand)."""
    body = dict(input=result.extra["result_set"], mode="links", trace=True)
    if question.as_of is not None:
        body["as_of"] = _iso(question.as_of)
    if question.known_by is not None:
        body["known_by"] = _iso(question.known_by)
    started = time.perf_counter()
    response = client.post("/v1/expand", json=body)
    result.latency_ms += (time.perf_counter() - started) * 1000
    result.turns += 1
    if response.status_code != 200:
        result.disclosures.append(f"expand:HTTP {response.status_code}")
        return
    data = response.json()
    result.context_tokens += json_tokens(data)
    added = []
    for record in data.get("records") or []:
        mid = record.get("message_id")
        if mid is not None and mid not in result.records and mid not in added:
            added.append(mid)
    result.records += added
    result.stages["after_selection"] = list(result.records)
    result.seen["expand:links"] = list(added)
    if incomplete_of(data.get("execution")):
        result.incomplete = True


def _recall_arm(name, description, request=None, needs=("recall",), **settings):
    capabilities = set(RECALL_CAPABILITIES)
    if settings.get("follow_support"):
        capabilities.add("pages")
    if settings.get("link_expansion"):
        capabilities.add("links")
    return Arm(
        name=name,
        settings=dict(
            route="POST /v1/recall",
            k=settings.pop("k", DEFAULT_K),
            request=request or {},
            **settings,
        ),
        needs=frozenset(needs),
        fn=_recall,
        capabilities=frozenset(capabilities),
        description=description,
    )


# Kinds of a recall without pages (no passage, no page revision).
# Kinds of a recall without the world model: no passage or page revision,
# and no claim (the planted claims restate pages).
NO_PAGE_KINDS = ["life_event", "reference", "derived", "link", "entity"]


def _sessions(arm, client, question):
    """Refine in a session: recall, refine through starting_sets, then a
    transform; every later call reuses a saved set instead of re-running a
    statement (counted as reused_statements)."""
    started = time.perf_counter()
    turns, reused, tokens_seen = 0, 0, 0
    session = client.post("/v1/sessions", json=dict(note="bench"))
    turns += 1
    if session.status_code != 200:
        return _http_error(session, started)
    session_id = session.json()["id"]
    body = recall_body(arm.settings, question)
    body.update(save=True, session=session_id)
    first = client.post("/v1/recall", json=body)
    turns += 1
    if first.status_code != 200:
        return _http_error(first, started)
    first_data = first.json()
    tokens_seen += json_tokens(first_data)
    handle = first_data.get("result_set")
    handles = [handle] if handle else []
    if handle:
        refine = dict(body, starting_sets=[handle], k=arm.settings.get("refine_k", 5))
        second = client.post("/v1/recall", json=refine)
        turns += 1
        reused += 1
        if second.status_code == 200 and second.json().get("result_set"):
            tokens_seen += json_tokens(second.json())
            handles.append(second.json()["result_set"])
    final = handles[-1] if handles else None
    if len(handles) > 1:
        union = client.post(
            "/v1/sets/transform",
            json=dict(op="union", handles=handles, session=session_id),
        )
        turns += 1
        reused += len(handles)
        if union.status_code == 200:
            final = union.json().get("handle") or final
    records = first_data.get("records") or []
    if final and final != handle:
        read = client.get(f"/v1/sets/{final}")
        turns += 1
        reused += 1
        if read.status_code == 200:
            records = read.json().get("records") or records
            tokens_seen += json_tokens(read.json())
    elapsed = (time.perf_counter() - started) * 1000
    result = recall_result(client, first_data, question, elapsed)
    delivered, spans = from_records(client, records)
    result.records, result.spans = delivered, spans
    result.stages["after_selection"] = list(delivered)
    result.turns = turns
    result.context_tokens = tokens_seen
    result.cited = cite(question, records, delivered)
    result.extra.update(session=session_id, handles=handles, reused_statements=reused)
    return result


PROVIDER_MISSING = ("provider", "not configured", "unavailable", "egress")


def _provider_missing(response):
    """Does an HTTP 400 say no usable provider serves the operation."""
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = str(body.get("detail") or "") if isinstance(body, dict) else response.text
    return any(term in detail.lower() for term in PROVIDER_MISSING)


def _match(arm, client, question):
    """Recall a pool, then keep what POST /v1/match judges a match."""
    started = time.perf_counter()
    body = recall_body(arm.settings, question)
    body.update(save=True, k=arm.settings.get("pool", 20))
    first = client.post("/v1/recall", json=body)
    if first.status_code != 200:
        return _http_error(first, started)
    data = first.json()
    handle = data.get("result_set")
    if not handle:
        return ArmResult(
            status=SKIPPED,
            reason="recall did not save a result set",
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    judged = client.post(
        "/v1/match",
        json=dict(input=handle, condition=question.text[:1000], max_rows=50),
    )
    elapsed = (time.perf_counter() - started) * 1000
    if judged.status_code == 400 and _provider_missing(judged):
        # No usable semantic model: the feature is absent, not failed.
        return ArmResult(status=SKIPPED, reason="match unavailable", latency_ms=elapsed)
    if judged.status_code != 200:
        # The route exists (probed) and the set was just saved: a 404 or an
        # ordinary 400 is a failure of the operation, never a skip.
        return _http_error(judged, started)
    matched = judged.json()
    keep = [
        r
        for r in matched.get("records") or []
        if ((r.get("meta") or {}).get("verdict") or "match") == "match"
    ]
    result = recall_result(client, data, question, elapsed)
    delivered, spans = from_records(client, keep)
    result.records, result.spans = delivered, spans
    result.stages["after_selection"] = list(delivered)
    execution = matched.get("execution") or {}
    result.model_cost += cost_of(execution)
    result.turns = 2
    result.context_tokens += json_tokens(matched)
    result.cited = cite(question, keep, delivered)
    if incomplete_of(execution):
        result.incomplete = True
    result.extra.update(match_status=execution.get("status"))
    return result


def librarian_body(question, budgets=None):
    body = dict(goal=question.text, question=question.text)
    scope = scope_for(question)
    for key in ("as_of", "known_by"):
        if key in scope:
            body[key] = scope.pop(key)
    if scope:
        body["scope"] = scope
    body["budgets"] = dict(budgets or LIB13_BUDGETS)
    return body


def _packet_records(packet):
    """Packet records in the shape of recall records (message_id, content,
    support), so one scorer reads both."""
    out = []
    for record in packet.get("records") or []:
        revision = record.get("revision_id") or {}
        mid = record.get("message_id")
        if mid is None and isinstance(revision, dict):
            mid = revision.get("message_id")
        if mid is None:
            mid = message_of(record.get("record_id"))
        out.append(
            dict(
                record,
                message_id=mid,
                content=record.get("content", record.get("original_excerpt")),
                support=record.get("support", record.get("source_refs")) or [],
            )
        )
    return out


def _overlaps(gap, question):
    """Does a packet gap touch the question's own time window."""
    if question.after is None and question.before is None:
        return False
    start, end = gap.get("from"), gap.get("to")
    if not start or not end:
        return True
    after = _iso(question.after) or ""
    before = _iso(question.before) or "9999"
    return str(start) < before and str(end) > after


def failed_avenues(packet):
    """The avenues an investigation tried that failed (packet.investigation),
    or a top-level list where an older packet carried it."""
    investigation = packet.get("investigation") or {}
    found = investigation.get("failed_avenues")
    if found is None:
        found = packet.get("failed_avenues") or packet.get("failed")
    return list(found or [])


def packet_result(client, packet, question, latency_ms):
    """An ArmResult from an EvidencePacket (POST /v1/librarian, /v1/packets)."""
    records = _packet_records(packet)
    delivered, spans = from_records(client, records, follow_support=True)
    status = packet.get("execution_status") or "complete"
    cost = packet.get("cost_and_latency") or {}
    coverage = packet.get("coverage") or {}
    disclosures = [f"execution_status:{status}"]
    gaps = [
        g
        for g in coverage.get("known_source_gaps") or []
        if isinstance(g, dict)
        and g.get("kind", "gap") == "gap"
        and _overlaps(g, question)
    ]
    for gap in gaps:
        disclosures.append(f"import_gap:{gap.get('channel')}")
    for need in packet.get("unresolved_needs") or []:
        disclosures.append("unresolved:" + str(need))
    cited = []
    for note in packet.get("orientation") or []:
        for ref in note.get("supporting_refs") or []:
            mid = message_of(ref.get("record_id")) if isinstance(ref, dict) else None
            if mid is not None and mid not in cited:
                cited.append(mid)
    return ArmResult(
        records=delivered,
        spans=spans,
        stages={"after_selection": list(delivered)},
        tokens=sum(int(r.get("tokens") or 0) for r in records)
        or sum(estimate_tokens(r.get("content")) for r in records),
        latency_ms=latency_ms,
        incomplete=status != "complete" or bool(gaps),
        disclosures=disclosures,
        model_cost=float(cost.get("model_cost") or cost.get("cost") or 0.0),
        turns=1,
        cited=cited or cite(question, records, delivered),
        context_tokens=json_tokens(packet),
        execution=dict(status=status),
        extra=dict(
            execution_status=status,
            investigation_id=packet.get("investigation_id"),
            # The packet lists failed avenues under investigation.
            failed=failed_avenues(packet),
        ),
    )


def _librarian(arm, client, question):
    started = time.perf_counter()
    response = client.post(
        "/v1/librarian",
        json=librarian_body(question, arm.settings.get("budgets")),
    )
    elapsed = (time.perf_counter() - started) * 1000
    if response.status_code == 404:
        return ArmResult(status=SKIPPED, reason="librarian unavailable")
    if response.status_code != 200:
        return _http_error(response, started)
    return packet_result(client, response.json(), question, elapsed)


# --- LIB-13: three configurations with equal access and budgets ----------------


def _ordinary(arm, client, question):
    """SQL + recall: recall, then one SQL statement reading the hits' threads
    around the top record (what an agent with only SQL and recall does)."""
    budgets = dict(arm.settings["budgets"])
    body = recall_body(dict(arm.settings, request=dict(budgets=budgets)), question)
    started = time.perf_counter()
    response = client.post("/v1/recall", json=body)
    if response.status_code != 200:
        return _http_error(response, started)
    data = response.json()
    result = recall_result(client, data, question, 0.0)
    records = data.get("records") or []
    top = next((r for r in records if r.get("thread_id") is not None), None)
    if top is not None and top.get("thread_no") is not None:
        statement = (
            "SELECT id, content FROM api.messages WHERE thread_id="
            f"{int(top['thread_id'])} AND thread_no BETWEEN"
            f" {int(top['thread_no']) - 1} AND {int(top['thread_no']) + 1}"
            " ORDER BY thread_no"
        )
        # The same cutoffs recall honoured: record time and ingest time.
        cutoffs = ""
        if question.as_of is not None:
            cutoffs += f" AND happened_at <= '{_iso(question.as_of)}'"
        if question.known_by is not None:
            cutoffs += f" AND ingested_at <= '{_iso(question.known_by)}'"
        statement = statement.replace(" ORDER BY", cutoffs + " ORDER BY")
        context = client.post("/v1/sql", json=dict(statement=statement, row_cap=20))
        result.turns += 1
        if context.status_code == 200:
            result.context_tokens += json_tokens(context.json())
    result.latency_ms = (time.perf_counter() - started) * 1000
    return result


def _equipped(arm, client, question):
    """Plans, result sets, expand and match by a scripted external-agent
    policy: one plan (recall, then context expansion), one read of the set."""
    budgets = dict(arm.settings["budgets"])
    # Plan nodes take api.recall's flat arguments: the scope fields go up.
    recall_args = recall_body(dict(arm.settings, request={}), question)
    recall_args.pop("trace", None)
    recall_args.update(recall_args.pop("scope", {}))
    recall_args["tokens"] = budgets.get("tokens")
    nodes = [dict(id="hits", op="recall", args=recall_args)]
    if arm.settings.get("expand", True):
        nodes.append(
            dict(
                id="ctx",
                op="expand",
                args=dict(mode="context", before=1, after=1),
                inputs=dict(input="hits"),
            )
        )
    nodes.append(
        dict(id="look", op="inspect", args=dict(view="summary"), inputs=["hits"])
    )
    started = time.perf_counter()
    response = client.post(
        "/v1/plans", json=dict(plan=dict(nodes=nodes), budgets=budgets)
    )
    if response.status_code == 400 and "expand" in response.text:
        nodes = [n for n in nodes if n["id"] != "ctx"]
        response = client.post(
            "/v1/plans", json=dict(plan=dict(nodes=nodes), budgets=budgets)
        )
    if response.status_code != 200:
        return _http_error(response, started)
    plan = response.json()
    handles = plan.get("handles") or {}
    final = handles.get("ctx") or handles.get("hits")
    turns, context = 1, json_tokens(plan)
    records = []
    if final:
        read = client.get(f"/v1/sets/{final}")
        turns += 1
        if read.status_code == 200:
            records = read.json().get("records") or []
            context += json_tokens(read.json())
    elapsed = (time.perf_counter() - started) * 1000
    hits = [r for r in records if r.get("role") in (None, "hit")] or records
    delivered, spans = from_records(client, records)
    execution = plan.get("execution") or {}
    return ArmResult(
        records=delivered,
        spans=spans,
        stages={"after_selection": list(delivered)},
        tokens=sum(int(r.get("tokens") or 0) for r in records),
        latency_ms=elapsed,
        incomplete=incomplete_of(execution),
        disclosures=disclosures_of(execution),
        model_cost=cost_of(execution),
        turns=turns,
        cited=cite(question, hits, delivered),
        context_tokens=context,
        execution=execution,
        extra=dict(handles=handles),
    )


def _lib13(name, fn, needs, description, **settings):
    return Arm(
        name=name,
        settings=dict(
            configuration=name,
            k=DEFAULT_K,
            budgets=dict(LIB13_BUDGETS),
            policy="scripted",
            **settings,
        ),
        needs=frozenset(needs),
        fn=fn,
        capabilities=RECALL_CAPABILITIES,
        description=description,
    )


ARMS = {
    arm.name: arm
    for arm in (
        Arm(
            name="oracle",
            settings=dict(source="gold"),
            needs=frozenset(),
            fn=_oracle,
            capabilities=CAPABILITIES,
            description="Reads the gold labels; the ceiling.",
        ),
        Arm(
            name="none",
            settings=dict(source="nothing"),
            needs=frozenset(),
            fn=_none,
            capabilities=CAPABILITIES,
            description="Returns no evidence; the floor.",
        ),
        _search_arm("keyword", {"search"}, "Lexical search over current messages."),
        _search_arm(
            "meaning", {"search", "embeddings"}, "Vector search over conversations."
        ),
        _search_arm(
            "fused", {"search", "embeddings"}, "Lexical and vector search, fused."
        ),
    )
}

# The v1.1 arms: POST /v1/recall with one switch changed each (BEN-16).
V11_ARMS = {
    arm.name: arm
    for arm in (
        _recall_arm("recall", "Default recall: every available leg, fused."),
        _recall_arm(
            "recall_raw_only",
            "Raw records only: no passages, entities or links.",
            request=dict(raw_only=True),
        ),
        _recall_arm(
            "recall_no_expand",
            "No spans and no context dependencies.",
            request=dict(expand=False),
        ),
        _recall_arm(
            "recall_rerank",
            "The local cross-encoder reranks the fused pool.",
            request=dict(ranker="cross_encoder"),
        ),
        _recall_arm(
            "recall_selection_500",
            "Selection within 500 tokens.",
            request=dict(budgets=dict(tokens=500)),
        ),
        _recall_arm(
            "recall_selection_2000",
            "Selection within 2,000 tokens.",
            request=dict(budgets=dict(tokens=2000)),
        ),
        _recall_arm(
            "recall_selection_8000",
            "Selection within 8,000 tokens.",
            request=dict(budgets=dict(tokens=8000)),
        ),
        _recall_arm(
            "recall_pages_off",
            "No pages, passages or claims, and no page sources: raw records only.",
            request=dict(
                scope=dict(kinds=NO_PAGE_KINDS), hints=dict(page_sources=False)
            ),
        ),
        _recall_arm(
            "recall_pages_on",
            "Page passages on, and the agent follows a page to its sources.",
            request=dict(hints=dict(page_sources=True)),
            follow_support=True,
        ),
        _recall_arm(
            "recall_link_expansion",
            "Recall, then follow links of the saved set (POST /v1/expand).",
            request=dict(save=True),
            needs=("recall", "expand"),
            link_expansion=True,
        ),
        Arm(
            name="sessions",
            settings=dict(
                route="POST /v1/sessions, /v1/recall, /v1/sets/transform",
                k=DEFAULT_K,
                refine_k=5,
                request={},
            ),
            needs=frozenset({"recall", "sessions"}),
            fn=_sessions,
            capabilities=RECALL_CAPABILITIES,
            description="Refine in a session through starting sets and a transform.",
        ),
        Arm(
            name="match",
            settings=dict(
                route="POST /v1/recall, /v1/match", k=DEFAULT_K, pool=20, request={}
            ),
            needs=frozenset({"recall", "match"}),
            fn=_match,
            capabilities=RECALL_CAPABILITIES,
            description="Recall a pool and keep what the semantic matcher accepts.",
        ),
        Arm(
            name="librarian",
            settings=dict(route="POST /v1/librarian", budgets=dict(LIB13_BUDGETS)),
            needs=frozenset({"librarian"}),
            fn=_librarian,
            capabilities=RECALL_CAPABILITIES,
            description="Delegate the question to the librarian; read its packet.",
        ),
    )
}

# LIB-13: the same simulated main agent, three ways to reach the library.
CONFIGURATIONS = {
    arm.name: arm
    for arm in (
        _lib13(
            "ordinary",
            _ordinary,
            {"recall"},
            "SQL and recall only.",
        ),
        _lib13(
            "equipped",
            _equipped,
            {"recall", "plans", "sets"},
            "Plans, result sets, expand and inspection by a scripted policy.",
        ),
        _lib13(
            "delegated",
            _librarian,
            {"librarian"},
            "The librarian investigates; the agent reads its packet.",
        ),
    )
}


def registry():
    """Every arm by name: wave-1, v1.1 and the LIB-13 configurations."""
    return {**ARMS, **V11_ARMS, **CONFIGURATIONS}


def get(name):
    try:
        return registry()[name]
    except KeyError:
        raise KeyError(f"unknown arm {name}") from None


# Features a probe reports, by the route or catalogue entry that proves them.
FEATURE_PATHS = {
    "recall": "/v1/recall",
    "expand": "/v1/expand",
    "match": "/v1/match",
    "plans": "/v1/plans",
    "packets": "/v1/packets",
    "sets": "/v1/sets/{handle}",
    "sessions": "/v1/sessions",
    "librarian": "/v1/librarian",
    "provenance": "/v1/provenance",
    "link_types": "/v1/link-types",
    "coverage": "/v1/plugins/{name}/coverage",
    "consumers": "/v1/feeds/settings",
    "open_loops": "/v1/open-loops",
    "media_noise": "/v1/transcripts/noise/run",
    "speaker_inbox": "/v1/transcripts/speakers",
    "identifier_status": "/v1/identity/identifiers/{identifier_id}/status",
    "idempotency": "/v1/links/batch",
}


def probe(client):
    """What the system offers right now, by asking it.

    search: the route exists. embeddings: a meaning search is answered
    (HTTP 400 means unconfigured or no serving model yet). The v1.1
    features come from the OpenAPI paths (FEATURE_PATHS) and the catalogue
    (GET /v1/describe: 'subject:<name>' and 'function:<name>'); the episodes
    feature needs api.episodes_of, person_role_between needs the recall scope
    to accept person_role 'between'.
    """
    available = set()
    document = client.get("/v1/openapi.json")
    spec = document.json() if document.status_code == 200 else {}
    paths = spec.get("paths", {})
    if "/v1/search" in paths:
        available.add("search")
        response = client.get(
            "/v1/search", params=dict(q="probe", mode="meaning", max_results=1)
        )
        if response.status_code == 200:
            available.add("embeddings")
    for path in paths:
        available.add("path:" + path)
    for feature, path in FEATURE_PATHS.items():
        if path in paths or any(
            p.replace("{id}", "{identifier_id}") == path for p in paths
        ):
            available.add(feature)
    if "path:/v1/identity/identifiers/{id}/status" in available:
        available.add("identifier_status")
    index = client.get("/v1/describe")
    if index.status_code == 200 and isinstance(index.json(), dict):
        for item in index.json().get("subjects") or []:
            available.add("subject:" + str(item.get("subject")))
        for name in index.json().get("functions") or []:
            available.add("function:" + str(name))
    if "function:episodes_of" in available:
        available.add("episodes")
    if "subject:write_contract" in available:
        available.add("idempotency")
    schemas = spec.get("components", {}).get("schemas", {})
    role = schemas.get("HardScope", {}).get("properties", {}).get("person_role", {})
    if "between" in json.dumps(role):
        available.add("person_role_between")
    return available
