"""Behaviour scenarios: a registry and a runner.

Each Behaviour has a name, the features it needs, plant(client, corpus)
(the data itself is built by scenarios.build and loaded with the twin; plant
only adds what must be written at the moment of the check) and
check(client, corpus, phase_times, ctx) -> {status, metrics, disclosures}
with status passed, failed or skipped.

Skip rule: a behaviour whose need is absent is reported 'skipped' with the
missing need named, never as zero and never as failed. A need is a feature
name from bench.arms.probe() (a route in the OpenAPI document or an entry
of GET /v1/describe), or 'principals': other callers (an agent token, a
plugin token, a restricted asker) that only the owner can create outside
HTTP. A behaviour defined here whose check belongs to a later package is
reported skipped with reason 'check_pending' once its needs are met.

All twelve scenarios are planted by scenarios.build and all twelve checks
are implemented here: six by w4-bench-arms (episode_count, newest_relevant,
consumer_exactly_once, coverage_honesty, link_vocabulary, restricted_inference
at gate level) and six by bench-behaviours (dyad_vs_author, open_loops,
media_contaminated, unenrolled_voice, suspected_link, write_replay), which
also extended restricted_inference to person functions and newest_relevant to
last_contact.

Plants. A plant writes, over HTTP, only what the check needs at that moment
and returns what it wrote (reported as 'planted'); it is a no-op when the
target already has it: the owner person (a fresh twin has none), the
'direction' field the corpus keeps in the payload (declared, and the rows
re-delivered as new versions with the same content) and the media channel as
a reference channel with the played interval.

Derived needs. 'labels' (w6-disclosure) is met when the principals can label
a channel; 'commitments' (w6-commitments) when POST /v1/commitments/extract
exists. The parts of restricted_inference that need them are skipped with the
need named in the disclosures until they are.
"""

import hashlib
import inspect
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import arms as arm_registry, gold, scenarios, score

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
PENDING = "check_pending"


class Principals:
    """Other callers for behaviours that need them.

    headers(kind, plugin=None) gives the Authorization header of a new
    token of that kind; asker(name, channels) registers a restricted asker
    granted those channels and gives headers that act as it; revoke(name,
    channel) withdraws a grant. Implementations create tokens and grants
    with the owner's own tools (they are not HTTP operations).
    """

    def headers(self, kind, plugin=None):  # pragma: no cover - interface
        raise NotImplementedError

    def asker(self, name, channels):  # pragma: no cover - interface
        raise NotImplementedError

    def revoke(self, name, channel):  # pragma: no cover - interface
        raise NotImplementedError

    def can_label(self):
        """Whether label() and unlabel() work on this system."""
        return False

    def label(self, channel, label):  # pragma: no cover - interface
        raise NotImplementedError

    def unlabel(self, channel, label):  # pragma: no cover - interface
        raise NotImplementedError


def _access_call(function, **arguments):
    """Call an access function in either form: the wave-1 one that opens its
    own owner connection, or one that takes the connection first (w6)."""
    from throughline import access

    parameters = list(inspect.signature(function).parameters)
    if parameters and parameters[0] == "conn":
        with access._owner_connection() as conn:
            return function(conn, **arguments)
    return function(**arguments)


class LocalPrincipals(Principals):
    """Principals for a system that runs in this process (tests, a local
    disposable instance): tokens, grants and labels through the system's owner
    connection. Never used against a remote URL."""

    def headers(self, kind, plugin=None):
        from throughline.db import create_token

        token = create_token(f"bench {kind}", kind, plugin)["token"]
        return {"Authorization": "Bearer " + token}

    def asker(self, name, channels):
        from throughline import access

        access.register_asker(name)
        held = set()
        try:
            for row in access.list_askers():
                if row.get("name") == name:
                    held = set(row.get("channels") or [])
        except Exception:
            held = set()
        for channel in channels:
            if channel not in held:
                _access_call(access.grant, asker=name, channel=channel)
        return {"X-Throughline-Asker": name}

    def revoke(self, name, channel):
        from throughline import access

        _access_call(access.revoke, asker=name, channel=channel)

    def can_label(self):
        from throughline import access

        if not (
            callable(getattr(access, "label", None))
            and callable(getattr(access, "unlabel", None))
        ):
            return False
        with access._owner_connection() as conn:
            row = conn.execute(
                "SELECT to_regclass('tl.channel_labels') IS NOT NULL AS ok"
            ).fetchone()
        return bool(row and row["ok"])

    def label(self, channel, label):
        from throughline import access

        _access_call(access.label, channel=channel, label=label)

    def unlabel(self, channel, label):
        from throughline import access

        _access_call(access.unlabel, channel=channel, label=label)


def result(status, metrics=None, disclosures=None, reason=None):
    out = dict(status=status, metrics=metrics or {}, disclosures=disclosures or [])
    if reason:
        out["reason"] = reason
    return out


@dataclass
class Behaviour:
    name: str
    needs: list
    description: str
    check: Callable | None = None
    plant: Callable | None = None
    # The package that implements the check when it is not implemented here.
    checked_by: str = "w4-bench-arms"
    tags: list = field(default_factory=list)

    def unmet(self, available):
        return [need for need in self.needs if need not in available]


@dataclass
class Context:
    """What a check may use besides the client: bound behaviour questions by id,
    gold ids of the corpus keys, principals and the probed features."""

    questions: dict
    ids: dict
    principals: Principals | None
    available: set


def _ids(ctx, keys):
    return [ctx.ids[tuple(k)] for k in keys if tuple(k) in ctx.ids]


def _ok(response, what):
    if response.status_code != 200:
        raise RuntimeError(f"{what}: HTTP {response.status_code} {response.text[:300]}")
    return response.json()


def _rows(body):
    """Rows of a response that is {rows}, {records}, a list, or one row."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("rows", "records", "news", "items"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def _sql(client, statement, headers=None, row_cap=10000):
    response = client.post(
        "/v1/sql", json=dict(statement=statement, row_cap=row_cap), headers=headers
    )
    return _ok(response, "sql")["rows"]


def _lit(text):
    """A SQL string literal (the bench quotes every value it puts in SQL)."""
    return "'" + str(text).replace("'", "''") + "'"


def _array(ids):
    return "ARRAY[" + ",".join(str(int(i)) for i in ids) + "]::bigint[]"


# --- episode_count ----------------------------------------------------------------


def check_episode_count(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["episode_count"]
    question = ctx.questions["behaviour_episode_count-000"]
    response = client.post(
        "/v1/recall",
        json=dict(
            query=segment["topic"],
            k=50,
            strategy="keyword",
            group_by="episode",
            scope=dict(channels=segment["channels"]),
        ),
    )
    data = _ok(response, "recall")
    exact = score.incident_count_exact(data.get("manifest"), question)
    ids = _ids(ctx, segment["keys"])
    row = _sql(
        client,
        "SELECT count(DISTINCT e.episode_id) AS incidents, count(*) AS hits"
        f" FROM api.episodes_of({_array(ids)}) e",
    )[0]
    asked = arm_registry.V11_ARMS["recall"].run(client, question)
    f05 = None
    if asked.status == arm_registry.OK:
        f05 = score.f_beta(score.with_spans(asked.records, asked.spans), question)
    metrics = dict(
        manifest_incidents=exact["incidents"],
        manifest_records=exact["records"],
        manifest_unit=exact["unit"],
        sql_incidents=row["incidents"],
        sql_hits=row["hits"],
        f05=f05,
    )
    passed = (
        exact["exact"]
        and exact["records"] == len(segment["keys"])
        and row["incidents"] == question.expects_incidents
    )
    warnings = (data.get("execution") or {}).get("warnings") or []
    return result(PASSED if passed else FAILED, metrics, [str(w) for w in warnings])


# --- newest_relevant --------------------------------------------------------------


def check_newest_relevant(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["newest_relevant"]
    question = ctx.questions["behaviour_newest_relevant-000"]
    newest = ctx.ids[tuple(segment["newest_absolute"])]
    response = client.post(
        "/v1/recall",
        json=dict(
            query=segment["query"],
            k=10,
            hints=dict(order="newest"),
            scope=dict(channels=segment["channels"]),
        ),
    )
    data = _ok(response, "recall")
    records = data.get("records") or []
    returned = [r["message_id"] for r in records if r.get("message_id") is not None]
    top = score.newest_relevant_top1(returned, question, newest)
    hits = [r["message_id"] for r in records if r.get("role") == "hit"]
    execution = data.get("execution") or {}
    metrics = dict(
        top1=top["top1"],
        newest_absolute_first=top["absolute_first"],
        first_hit_is_answer=bool(hits) and hits[0] == question.expects_newest,
        order=execution.get("order"),
        returned=len(returned),
    )
    passed = top["top1"] and not top["absolute_first"]
    disclosures = [str(execution.get("order"))]
    if "function:last_contact" in ctx.available:
        # The answer's author: their last contact is the answer, never the
        # later chatter of the shared thread.
        person = _author_person(client, corpus, segment["answer"])
        fresh = _fresh(client, [segment["answer"], segment["newest_absolute"]])
        last = (
            _ok(client.get(f"/v1/people/{person}/last-contact"), "last contact").get(
                "last_contact"
            )
            or {}
        )
        got = last.get("last_contact_message_id")
        metrics["last_contact_is_answer"] = got == fresh.get(tuple(segment["answer"]))
        metrics["last_contact_is_newest_absolute"] = got == fresh.get(
            tuple(segment["newest_absolute"])
        )
        passed = (
            passed
            and metrics["last_contact_is_answer"]
            and not metrics["last_contact_is_newest_absolute"]
        )
    else:
        disclosures.append("last_contact:skipped (need function:last_contact)")
    return result(PASSED if passed else FAILED, metrics, disclosures)


# --- consumer_exactly_once --------------------------------------------------------

SETTLE = dict(settle_s=3, margin_s=1)


def _cursor(row):
    cursor = row.get("cursor")
    if isinstance(cursor, dict) and cursor:
        return cursor
    return dict(ingested_at=row.get("ingested_at"), message_id=row.get("message_id"))


def _pending(client, name, headers=None):
    response = client.get(f"/v1/feeds/{name}/pending", headers=headers)
    body = _ok(response, "pending")
    rows = _rows(body)
    if rows:
        return int(rows[0].get("pending") or 0)
    return int(body.get("pending") or 0) if isinstance(body, dict) else 0


def _news(client, name, headers=None):
    response = client.get(
        f"/v1/feeds/{name}/news", params=dict(max=500), headers=headers
    )
    return _rows(_ok(response, "news"))


def _wait_pending(client, name, expected, limit_s, step_s=0.25):
    """Poll pending until it reaches `expected` (bounded; no long sleep)."""
    deadline = time.monotonic() + limit_s
    seen = None
    while True:
        seen = _pending(client, name)
        if seen >= expected or time.monotonic() >= deadline:
            return seen
        time.sleep(step_s)


def check_consumer_exactly_once(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["consumer_exactly_once"]
    name = "bench-behaviour-feed"
    previous = _ok(client.get("/v1/feeds/settings"), "settings")
    previous = {k: previous[k] for k in ("settle_s", "margin_s") if k in previous}
    window = SETTLE["settle_s"] + SETTLE["margin_s"] + 5
    metrics, disclosures = {}, []
    _ok(client.put("/v1/feeds/settings", json=SETTLE), "settings")
    try:
        client.delete(f"/v1/feeds/{name}")
        _ok(
            client.post(
                "/v1/feeds",
                json=dict(
                    name=name,
                    scope=dict(channels=segment["channels"]),
                    start="beginning",
                ),
            ),
            "create consumer",
        )
        first_count = len(segment["first"])
        metrics["pending_before_first"] = _wait_pending(
            client, name, first_count, window
        )
        first = _news(client, name)
        metrics["first_news"] = len(first)
        if first:
            _ok(
                client.post(
                    f"/v1/feeds/{name}/mark", json=dict(to=_cursor(first[-1]))
                ),
                "mark",
            )
        late = scenarios.load_held(client, corpus, "consumer_exactly_once")
        metrics["late_loaded"] = late["inserted"]
        metrics["pending_late"] = _wait_pending(
            client, name, len(segment["late"]), window
        )
        news = _news(client, name)
        again = _news(client, name)
        delivered = [r.get("external_id") for r in news]
        expected = [key[1] for key in segment["late"]]
        metrics["late_news"] = delivered
        metrics["redelivered_without_mark"] = [
            r.get("external_id") for r in again
        ] == delivered
        mark_body = dict(to=_cursor(news[-1])) if news else None
        marked = repeat = None
        if mark_body:
            marked = _ok(
                client.post(f"/v1/feeds/{name}/mark", json=mark_body), "mark"
            )
            repeat = _ok(
                client.post(f"/v1/feeds/{name}/mark", json=mark_body), "mark"
            )
        metrics["repeat_mark_same_response"] = marked is not None and marked == repeat
        metrics["pending_after_mark"] = _pending(client, name)
        metrics["news_after_mark"] = len(_news(client, name))
        passed = (
            metrics["first_news"] == first_count
            and sorted(delivered) == sorted(expected)
            and len(delivered) == len(set(delivered))
            and metrics["redelivered_without_mark"]
            and metrics["repeat_mark_same_response"]
            and metrics["pending_after_mark"] == 0
            and metrics["news_after_mark"] == 0
        )
        disclosures.append(
            f"settle_s={SETTLE['settle_s']} margin_s={SETTLE['margin_s']}"
        )
        return result(PASSED if passed else FAILED, metrics, disclosures)
    finally:
        client.delete(f"/v1/feeds/{name}")
        if previous:
            client.put("/v1/feeds/settings", json=previous)


# --- coverage_honesty -------------------------------------------------------------


def _window(start, end):
    return {"from": start.isoformat(), "to": end.isoformat()}


def check_coverage_honesty(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["coverage_honesty"]
    partial, complete = segment["channels"]
    window, gap = segment["window"], segment["gap"]
    plugin = scenarios.PLUGIN
    posted = _ok(
        client.post(
            f"/v1/plugins/{plugin}/coverage",
            json=dict(
                reports=[
                    dict(
                        channel=partial,
                        status="partial",
                        reasons=["source_error"],
                        window=_window(*window),
                        rows_delivered=2,
                        gaps=[
                            {
                                "from": gap[0].isoformat(),
                                "to": gap[1].isoformat(),
                                "reason": "source_error",
                            }
                        ],
                    ),
                    dict(
                        channel=complete,
                        status="complete",
                        window=_window(*window),
                        rows_delivered=2,
                        verified=True,
                    ),
                ]
            ),
        ),
        "coverage report",
    )
    opened = [g.get("id") for g in posted.get("gaps_opened") or []]
    metrics = dict(gaps_opened=len(opened))
    outcomes = {}
    for number, channel in (("000", partial), ("001", complete)):
        question = ctx.questions[f"behaviour_coverage_honesty-{number}"]
        body = dict(
            query=question.text,
            k=10,
            scope=dict(
                channels=[channel], after=gap[0].isoformat(), before=gap[1].isoformat()
            ),
        )
        execution = _ok(client.post("/v1/recall", json=body), "recall")["execution"]
        verdict = score.absence_disclosed(execution, question)
        gaps = (execution.get("coverage") or {}).get("gaps") or []
        listed = any(
            g.get("id") in opened
            or (g.get("channel") == channel and channel == partial)
            for g in gaps
        )
        outcomes[channel] = dict(verdict, gap_listed=listed)
    metrics["partial"] = outcomes[partial]
    metrics["complete"] = outcomes[complete]
    closed = False
    if opened:
        _ok(
            client.post(
                f"/v1/plugins/{plugin}/coverage",
                json=dict(
                    reports=[
                        dict(
                            channel=partial,
                            status="complete",
                            window=_window(*gap),
                            rows_delivered=0,
                            verified=True,
                            closes=opened,
                        )
                    ]
                ),
            ),
            "closing report",
        )
        still = _rows(
            _ok(
                client.get(
                    f"/v1/plugins/{plugin}/coverage/gaps", params=dict(channel=partial)
                ),
                "gaps",
            )
        )
        closed = not any(g.get("id") in opened for g in still)
    metrics["gap_closed"] = closed
    passed = (
        outcomes[partial]["correct"]
        and outcomes[partial]["gap_listed"]
        and outcomes[complete]["correct"]
        and not outcomes[partial]["negative_assertion"]
        and not outcomes[complete]["negative_assertion"]
        and closed
    )
    disclosures = [str(outcomes[c]["statement"]) for c in (partial, complete)]
    return result(PASSED if passed else FAILED, metrics, disclosures)


# --- link_vocabulary --------------------------------------------------------------


def _key(key):
    return dict(channel=key[0], external_id=key[1])


def check_link_vocabulary(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["link_vocabulary"]
    a, b, c, _ = segment["keys"]
    canonical, synonym = segment["canonical"], segment["synonym"]
    candidate = segment["candidate"]
    metrics = {}
    agent = ctx.principals.headers("agent")
    refused = client.post(
        "/v1/links",
        json={"type": segment["free_text"], "from": _key(a), "to": _key(b)},
        headers=agent,
    )
    metrics["free_text_refused"] = refused.status_code == 400
    _ok(
        client.post(
            "/v1/link-types",
            json=dict(type=canonical, description="Synthetic canonical type"),
        ),
        "register",
    )
    _ok(client.post("/v1/link-types", json=dict(type=synonym)), "register synonym")
    _ok(
        client.post(f"/v1/link-types/{synonym}/merge", json=dict(into=canonical)),
        "synonym",
    )
    body = {"type": segment["synonym_raw"], "from": _key(a), "to": _key(b)}
    first = _ok(client.post("/v1/links", json=body), "link")
    again = _ok(client.post("/v1/links", json=body), "link replay")
    metrics["synonym_folded"] = first.get("type") == canonical
    metrics["type_as_written_kept"] = (
        first.get("type_as_written") == segment["synonym_raw"]
    )
    metrics["replay_no_duplicate"] = first.get("id") == again.get("id")
    plugin = ctx.principals.headers("plugin", scenarios.PLUGIN)
    batch = client.post(
        "/v1/ingest/messages",
        json=dict(
            plugin=scenarios.PLUGIN,
            messages=[],
            links=[
                {"type": candidate, "from": _key(a), "to": _key(c)},
                {"type": canonical, "from": _key(a), "to": _key(c)},
            ],
        ),
        headers=plugin,
    )
    metrics["plugin_batch_accepted"] = batch.status_code == 200
    listed = _rows(
        _ok(client.get("/v1/link-types", params=dict(status="candidate")), "candidates")
    )
    metrics["candidate_listed"] = candidate in {r.get("type") for r in listed}
    merged = _ok(
        client.post(f"/v1/link-types/{candidate}/merge", json=dict(into=canonical)),
        "merge",
    )
    metrics["retyped"] = merged.get("retyped")
    metrics["collapsed"] = merged.get("collapsed")
    ids = _ids(ctx, [a, c])
    rows = _sql(
        client,
        "SELECT id,type,type_as_written,coalesce(status,'active') AS status"
        f" FROM api.links WHERE from_id={ids[0]} AND to_id={ids[1]}"
        " AND to_kind='message' ORDER BY id",
    )
    active = [r for r in rows if r["status"] == "active"]
    metrics["active_links_after_merge"] = len(active)
    metrics["old_links_canonical"] = all(r["type"] == canonical for r in rows) and any(
        r["type_as_written"] == candidate for r in rows
    )
    passed = all(
        metrics[k]
        for k in (
            "free_text_refused",
            "synonym_folded",
            "type_as_written_kept",
            "replay_no_duplicate",
            "plugin_batch_accepted",
            "candidate_listed",
            "old_links_canonical",
        )
    ) and (
        metrics["active_links_after_merge"] == 1 and (metrics["collapsed"] or 0) >= 1
    )
    return result(PASSED if passed else FAILED, metrics)


# --- restricted_inference (gate level) ------------------------------------------


def check_restricted_inference(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["restricted_inference"]
    canary = segment["canary"]
    guest = ctx.principals.asker(segment["asker"], segment["granted"])
    visible = _ids(ctx, segment["visible"])
    every = visible + _ids(ctx, segment["hidden"])
    expected = dict(
        sql_count=len(visible),
        recall_records=len(visible),
        manifest_records=len(visible),
        episode_rows=len(visible),
        episodes=segment["visible_episodes"],
    )
    seen, disclosures = {}, []
    seen["sql_count"] = _sql(
        client,
        "SELECT count(*) AS n FROM api.messages WHERE content LIKE "
        + _lit(f"%{canary}%"),
        guest,
    )[0]["n"]
    recalled = _ok(
        client.post(
            "/v1/recall",
            json=dict(query=canary, k=100, strategy="keyword", group_by="channel"),
            headers=guest,
        ),
        "recall",
    )
    seen["recall_records"] = sum(
        1 for r in recalled.get("records") or [] if r.get("role") == "hit"
    )
    seen["manifest_records"] = (recalled.get("manifest") or {}).get("records")
    episodes = _sql(
        client,
        "SELECT count(*) AS n, count(DISTINCT e.episode_id) AS episodes"
        f" FROM api.episodes_of({_array(every)}) e",
        guest,
    )[0]
    seen["episode_rows"], seen["episodes"] = episodes["n"], episodes["episodes"]
    if "function:coverage_for" in ctx.available:
        rows = _sql(
            client,
            "SELECT channel FROM api.coverage_for(ARRAY["
            + ",".join(_lit(c) for c in segment["channels"])
            + "])",
            guest,
        )
        expected["coverage_hidden_rows"] = 0
        seen["coverage_hidden_rows"] = sum(
            1 for r in rows if r.get("channel") not in segment["granted"]
        )
    else:
        disclosures.append("coverage_for:skipped")
    if "consumers" in ctx.available:
        names = ("bench-behaviour-guest-feed", "bench-behaviour-owner-feed")
        try:
            _ok(
                client.post(
                    "/v1/feeds",
                    json=dict(
                        name=names[0],
                        scope=dict(channels=segment["channels"]),
                        start="beginning",
                    ),
                    headers=guest,
                ),
                "consumer",
            )
            _ok(
                client.post(
                    "/v1/feeds",
                    json=dict(
                        name=names[1],
                        scope=dict(channels=segment["granted"]),
                        start="beginning",
                    ),
                ),
                "consumer",
            )
            seen["pending"] = _pending(client, names[0], guest)
            expected["pending"] = _pending(client, names[1])
            # The same count through SQL (api.pending), as each caller.
            if "function:pending" in ctx.available:
                seen["sql_pending"] = _sql(
                    client, f"SELECT pending FROM api.pending({_lit(names[0])})", guest
                )[0]["pending"]
                expected["sql_pending"] = _sql(
                    client, f"SELECT pending FROM api.pending({_lit(names[1])})"
                )[0]["pending"]
            else:
                disclosures.append("sql_pending:skipped")
        finally:
            client.delete(f"/v1/feeds/{names[0]}", headers=guest)
            client.delete(f"/v1/feeds/{names[1]}")
    else:
        disclosures.append("pending:skipped")
    types = _rows(_ok(client.get("/v1/link-types", headers=guest), "link types"))
    expected["link_type_leaks"] = 0
    seen["link_type_leaks"] = sum(
        1
        for r in types
        if r.get("status") not in (None, "active")
        or any(k in r for k in ("active_links", "links", "source_links"))
    )
    if "function:person_brief" in ctx.available:
        _person_level(client, corpus, ctx, segment, guest, expected, seen)
        disclosures.append("person_level:checked")
    else:
        disclosures.append("person_level:skipped (need function:person_brief)")
    errors = {}
    if "labels" in ctx.available:
        try:
            _labelled(client, corpus, ctx, segment, guest, expected, seen)
            disclosures.append("labels:checked")
        except Exception as exc:  # reported, never a silent zero
            errors["labels"] = f"{type(exc).__name__}: {exc}"[:300]
    else:
        disclosures.append("labels:skipped (need labels)")
    leaks = {
        key: score.hidden_count_leak(seen.get(key), expected[key]) for key in expected
    }
    metrics = dict(expected=expected, seen=seen, hidden_count_leak=leaks)
    if errors:
        metrics["errors"] = errors
    passed = all(value == 0 for value in leaks.values()) and not errors
    return result(PASSED if passed else FAILED, metrics, disclosures)


def _guest_counts(client, segment, guest, horizon):
    """The person-level aggregates a restricted asker sees of the segment's
    person, as numbers (compared with what the granted rows alone give)."""
    person = segment["person_id"]
    hidden = set(segment["hidden_channels"])
    brief = _ok(client.get(f"/v1/people/{person}/brief", headers=guest), "brief")
    brief = brief.get("brief") or {}
    arc = _rows(_ok(client.get(f"/v1/people/{person}/arc", headers=guest), "arc"))
    rhythm = _rows(
        _ok(client.get(f"/v1/people/{person}/rhythm", headers=guest), "rhythm")
    )
    last = _ok(
        client.get(f"/v1/people/{person}/last-contact", headers=guest), "last contact"
    ).get("last_contact")
    loops = _rows(
        _ok(
            client.get(
                "/v1/open-loops",
                params=dict(horizon_days=horizon, max_results=1000),
                headers=guest,
            ),
            "open loops",
        )
    )
    last_id = (last or {}).get("last_contact_message_id")
    return dict(
        brief_messages=brief.get("message_count") or 0,
        brief_hidden_channels=sum(
            int(c.get("messages") or 0)
            for c in brief.get("top_channels") or []
            if c.get("channel") in hidden
        ),
        brief_hidden_threads=sum(
            1 for t in brief.get("recent_threads") or [] if t.get("channel") in hidden
        ),
        arc_messages=sum(
            int(r.get("messages_from") or 0) + int(r.get("messages_to") or 0)
            for r in arc
        ),
        rhythm_messages=sum(
            int(r.get("messages") or 0) for r in rhythm if r.get("dimension") == "month"
        ),
        last_contact_hidden=int(
            last_id is not None and last_id not in segment["visible_ids"]
        ),
        open_loops_hidden=sum(1 for r in loops if r.get("channel") in hidden),
    )


def _horizon(client, corpus):
    """Days back to the oldest behaviour segment (person functions default to the
    last 30 days for open loops; the corpus is older than that)."""
    start = min(s["start"] for s in corpus.behaviours.values())
    return max(30, (scenarios.server_time(client) - start).days + 2)


def _person_level(client, corpus, ctx, segment, guest, expected, seen):
    """Circle ranks, rhythm counts, arc, brief counts, last contact and open
    loops of the restricted asker reflect granted rows only."""
    visible_ids = set(_ids(ctx, segment["visible"]))
    person = _author_person(client, corpus, segment["visible"][0])
    hidden = [c for c in segment["channels"] if c not in segment["granted"]]
    info = dict(person_id=person, hidden_channels=hidden, visible_ids=visible_ids)
    counted = _guest_counts(client, info, guest, _horizon(client, corpus))
    n = len(visible_ids)
    wanted = dict(
        brief_messages=n,
        brief_hidden_channels=0,
        brief_hidden_threads=0,
        arc_messages=n,
        rhythm_messages=n,
        last_contact_hidden=0,
        open_loops_hidden=0,
    )
    for key, value in wanted.items():
        expected["person_" + key] = value
        seen["person_" + key] = counted[key]
    # Circle ranks: an asker granted only the two people's direct messages
    # must not see the third person of the group chat, nor the owner, in A's
    # circle, and B stays first.
    dyad = corpus.behaviours["dyad_vs_author"]
    dm_channel = dyad["channels"][0]
    people = _people(client)
    a, b = (people[dyad["people"][k]]["id"] for k in "ab")
    dm_guest = ctx.principals.asker(segment["asker"] + "-dm", [dm_channel])
    circle = _rows(
        _ok(client.get(f"/v1/people/{a}/circle", headers=dm_guest), "circle")
    )
    others = [r.get("other_person_id") for r in circle]
    expected["circle_hidden_people"] = 0
    seen["circle_hidden_people"] = sum(1 for o in others if o != b)
    expected["circle_rank_of_b"] = 1
    seen["circle_rank_of_b"] = next(
        (r.get("rank") for r in circle if r.get("other_person_id") == b), None
    )


def _labelled(client, corpus, ctx, segment, guest, expected, seen):
    """Wave integration (w6-disclosure): a label the grant does not allow,
    added to the granted channel, removes its rows from every count; taking
    it off restores them."""
    channel = segment["granted"][0]
    label = LABEL
    canary = segment["canary"]
    visible_ids = set(_ids(ctx, segment["visible"]))
    person = _author_person(client, corpus, segment["visible"][0])
    info = dict(
        person_id=person,
        hidden_channels=list(segment["channels"]),
        visible_ids=set(),
    )
    ctx.principals.label(channel, label)
    try:
        counts = _guest_counts(client, info, guest, _horizon(client, corpus))
        counts["sql_count"] = _sql(
            client,
            "SELECT count(*) AS n FROM api.messages WHERE content LIKE "
            + _lit(f"%{canary}%"),
            guest,
        )[0]["n"]
        recalled = _ok(
            client.post(
                "/v1/recall",
                json=dict(query=canary, k=100, strategy="keyword"),
                headers=guest,
            ),
            "recall",
        )
        counts["recall_records"] = sum(
            1 for r in recalled.get("records") or [] if r.get("role") == "hit"
        )
        counts["manifest_records"] = (recalled.get("manifest") or {}).get("records")
        every = visible_ids | set(_ids(ctx, segment["hidden"]))
        counts["episode_rows"] = _sql(
            client,
            f"SELECT count(*) AS n FROM api.episodes_of({_array(every)}) e",
            guest,
        )[0]["n"]
        for key, value in counts.items():
            expected["labelled_" + key] = 0
            seen["labelled_" + key] = value
    finally:
        ctx.principals.unlabel(channel, label)
    restored = _sql(
        client,
        "SELECT count(*) AS n FROM api.messages WHERE content LIKE "
        + _lit(f"%{canary}%"),
        guest,
    )[0]["n"]
    expected["unlabelled_sql_count"] = len(visible_ids)
    seen["unlabelled_sql_count"] = restored


# --- helpers for the person-level scenarios ---------------------------------------

# A seeded label kind (w6-disclosure) that no bench grant allows.
LABEL = "work"


def _people(client):
    """{handle: person row} (owner and unrestricted agents only)."""
    return {p["handle"]: p for p in _ok(client.get("/v1/identity/people"), "people")}


def _identifiers(client):
    rows = _ok(client.get("/v1/registry/identifiers"), "identifiers")
    return {(r["system"], r["value"]): r for r in rows}


def _corpus_message(corpus, key):
    key = tuple(key)
    for message in corpus.messages:
        if (message["channel"], message["external_id"]) == key:
            return message
    raise KeyError(key)


def _author_person(client, corpus, key):
    """The person the corpus author account of a message belongs to."""
    author = _corpus_message(corpus, key)["author"]
    return _identifiers(client)[(author["system"], author["value"])]["person_id"]


def _fresh(client, keys):
    """Current message ids of keys, read now (a plant may have re-delivered
    them as new versions)."""
    return gold.resolve(client, [tuple(k) for k in keys])


def _channels(client):
    return {c["name"]: c for c in _ok(client.get("/v1/channels"), "channels")}


def _spec(corpus, name):
    return dict(next(c for c in corpus.channels if c["name"] == name))


def _party(record, owner):
    """The side of a recall record: its person, or the owner when is_me."""
    if (record.get("attribution") or {}).get("is_me"):
        return owner
    return record.get("person_id")


def _hits(body):
    return [r for r in body.get("records") or [] if r.get("role") == "hit"]


def ensure_owner(client, corpus):
    """The owner person decides the owner side (is_me). A target always has
    one; a fresh twin does not, so the corpus's owner is designated by a
    person created with is_owner (the corpus registers it without)."""
    people = _people(client)
    owners = [
        p for p in people.values() if p.get("is_owner") and p.get("merged_into") is None
    ]
    if owners:
        return min(p["id"] for p in owners), []
    base = corpus.behaviours["dyad_vs_author"]["owner"]
    handle = base + "-self"
    if handle in people:
        return people[handle]["id"], []
    created = _ok(
        client.post(
            "/v1/identity/people",
            json=dict(handle=handle, display_name="Bench Owner", is_owner=True),
        ),
        "owner",
    )
    return created["id"], [f"owner_designated:{handle}"]


def deliver_direction(client, corpus, channels):
    """The corpus carries the message direction in the payload; the system
    reads it from the declared field 'direction'. Declare the field and
    re-deliver those rows with it (new versions, same content) unless the
    channel already declares it."""
    stored = _channels(client)
    planted = []
    for name in channels:
        rows = [
            m
            for m in corpus.messages
            if m["channel"] == name
            and "direction" in (m.get("payload") or {})
            and "direction" not in (m.get("fields") or {})
        ]
        declared = list((stored.get(name) or {}).get("fields") or [])
        if not rows or any(f.get("name") == "direction" for f in declared):
            continue
        spec = _spec(corpus, name)
        spec["fields"] = declared + [dict(name="direction", type="text")]
        _ok(client.post("/v1/channels", json=spec), "channel fields")
        messages = [
            dict(
                m,
                fields=dict(m.get("fields") or {}, direction=m["payload"]["direction"]),
            )
            for m in rows
        ]
        _ok(
            client.post(
                "/v1/ingest/messages",
                json=dict(plugin=scenarios.PLUGIN, messages=messages),
            ),
            "direction delivery",
        )
        planted.append(f"direction_field:{name}:{len(messages)}")
    return planted


def plant_owner_side(client, corpus, channels):
    _, planted = ensure_owner(client, corpus)
    return planted + deliver_direction(client, corpus, channels)


# --- dyad_vs_author ---------------------------------------------------------------


def plant_dyad(client, corpus):
    segment = corpus.behaviours["dyad_vs_author"]
    return plant_owner_side(client, corpus, segment["channels"])


def check_dyad_vs_author(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["dyad_vs_author"]
    people = _people(client)
    a, b, c = (people[segment["people"][k]]["id"] for k in "abc")
    owner, _ = ensure_owner(client, corpus)
    keys = [k for group in segment["keys"].values() for k in group]
    ids = _fresh(client, keys)
    authors = {}
    for key in keys:
        author = _corpus_message(corpus, key).get("author")
        if author is None:
            authors[ids[tuple(key)]] = owner
        else:
            authors[ids[tuple(key)]] = _identifiers(client)[
                (author["system"], author["value"])
            ]["person_id"]
    shared = [ids[tuple(k)] for k in segment["keys"]["dm"] + segment["keys"]["group"]]
    dyad = {i for i in shared if authors[i] in (a, b)}
    third = {i for i in shared if authors[i] == c}
    owner_side = {ids[tuple(k)] for k in segment["keys"]["owner"]}
    topic = segment["topic"]
    metrics = {}

    def between(persons):
        rows = _rows(
            _ok(
                client.get(
                    "/v1/between",
                    params=dict(
                        persons=",".join(str(p) for p in persons), max_results=1000
                    ),
                ),
                "between",
            )
        )
        return {r["message_id"]: r.get("party_id") for r in rows}

    def recall(persons, role):
        body = _ok(
            client.post(
                "/v1/recall",
                json=dict(
                    query=topic,
                    k=100,
                    strategy="keyword",
                    scope=dict(
                        persons=persons,
                        person_role=role,
                        channels=segment["channels"],
                    ),
                ),
            ),
            "recall",
        )
        return {r["message_id"]: _party(r, owner) for r in _hits(body)}

    for name, got in (
        ("between", between([a, b])),
        ("recall", recall([a, b], "between")),
    ):
        metrics[name] = dict(
            both_sides=score.dyad_both_sides(got.values(), [a, b]),
            exact=set(got) == dyad,
            third_person_rows=len(set(got) & third),
            returned=len(got),
        )
    author = recall([a], "author")
    metrics["author"] = dict(
        only_a=bool(author) and set(author.values()) == {a},
        presented_as_dyad=score.dyad_both_sides(author.values(), [a, b]) == 1.0,
        returned=len(author),
    )
    # One person is never a dyad: 'between' with A alone (or A twice) is
    # refused, never answered with A's own messages.
    lone = [
        client.post(
            "/v1/recall",
            json=dict(
                query=topic,
                k=10,
                strategy="keyword",
                scope=dict(persons=persons, person_role="between"),
            ),
        ).status_code
        for persons in ([a], [a, a])
    ]
    lone.append(client.get("/v1/between", params=dict(persons=str(a))).status_code)
    metrics["single_person_between"] = dict(
        statuses=lone, refused=all(400 <= code < 500 for code in lone)
    )
    for name, got in (
        ("owner_between", between([owner, b])),
        ("owner_recall", recall([owner, b], "between")),
    ):
        metrics[name] = dict(
            both_sides=score.dyad_both_sides(got.values(), [owner, b]),
            exact=set(got) == owner_side,
            outgoing_rows=sum(1 for p in got.values() if p == owner),
            returned=len(got),
        )
    passed = (
        all(
            metrics[k]["both_sides"] == 1.0
            and metrics[k]["exact"]
            and not metrics[k].get("third_person_rows")
            for k in ("between", "recall", "owner_between", "owner_recall")
        )
        and metrics["author"]["only_a"]
        and not metrics["author"]["presented_as_dyad"]
        and metrics["single_person_between"]["refused"]
    )
    return result(PASSED if passed else FAILED, metrics)


# --- open_loops -------------------------------------------------------------------


def plant_open_loops(client, corpus):
    segment = corpus.behaviours["open_loops"]
    return plant_owner_side(client, corpus, segment["channels"])


def _loop_score(rows, gold_loops, closed, channels):
    got = {
        r["message_id"]: r.get("direction")
        for r in rows
        if r.get("channel") in channels
    }
    right = sum(1 for i, d in got.items() if gold_loops.get(i) == d)
    return dict(
        returned={str(i): d for i, d in got.items()},
        precision=round(right / len(got), 4) if got else 0.0,
        recall=round(right / len(gold_loops), 4) if gold_loops else 1.0,
        wrong_direction=sum(
            1 for i, d in got.items() if i in gold_loops and gold_loops[i] != d
        ),
        closed_returned=len(set(got) & closed),
    )


def check_open_loops(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["open_loops"]
    keys = list(segment["open"].values()) + segment["answered"] + segment["wound_down"]
    ids = _fresh(client, keys)
    gold_loops = {ids[tuple(k)]: direction for direction, k in segment["open"].items()}
    closed = {ids[tuple(k)] for k in segment["answered"] + segment["wound_down"]}
    person = _people(client)[segment["person"]]["id"]
    params = dict(
        older_than=f"{segment['unanswered_after_s']} seconds",
        horizon_days=_horizon(client, corpus),
        max_results=1000,
    )
    every = _rows(_ok(client.get("/v1/open-loops", params=params), "open loops"))
    theirs = _rows(
        _ok(client.get(f"/v1/people/{person}/open-loops", params=params), "open loops")
    )
    metrics = dict(
        all_threads=_loop_score(every, gold_loops, closed, segment["channels"]),
        person=_loop_score(theirs, gold_loops, closed, segment["channels"]),
        gold={str(i): d for i, d in gold_loops.items()},
    )
    passed = all(
        m["precision"] == 1.0 and m["recall"] == 1.0 and m["closed_returned"] == 0
        for m in (metrics["all_threads"], metrics["person"])
    )
    found = {
        d
        for m in (metrics["all_threads"], metrics["person"])
        for d in m["returned"].values()
    }
    missing = sorted(set(gold_loops.values()) - found)
    return result(
        PASSED if passed else FAILED,
        metrics,
        [f"missing_direction:{d}" for d in missing],
    )


# --- media_contaminated -----------------------------------------------------------


def plant_media(client, corpus):
    """Played media is a reference record with its played interval: declare
    the corpus's media channel as reference and give the caption the interval
    of the played turns, unless the target already has both."""
    segment = corpus.behaviours["media_contaminated"]
    channel, _ = segment["media"]
    planted = []
    stored = _channels(client).get(channel) or {}
    if stored.get("record_kind") != "reference":
        spec = _spec(corpus, channel)
        _ok(
            client.post(
                "/v1/channels",
                json=dict(spec, is_reference=True, record_kind="reference"),
            ),
            "media channel",
        )
        planted.append(f"reference_channel:{channel}")
    media = _corpus_message(corpus, segment["media"])
    if not media.get("ended_at"):
        row = _sql(
            client,
            "SELECT ended_at FROM api.messages WHERE channel="
            + _lit(channel)
            + " AND external_id="
            + _lit(media["external_id"]),
        )
        if row and row[0].get("ended_at") is None:
            played = [
                _corpus_message(corpus, k)["happened_at"] for k in segment["played"]
            ]
            _ok(
                client.post(
                    "/v1/ingest/messages",
                    json=dict(
                        plugin=scenarios.PLUGIN,
                        messages=[dict(media, ended_at=max(played))],
                    ),
                ),
                "media interval",
            )
            planted.append(f"played_interval:{media['external_id']}")
    return planted


def _digest(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def check_media_contaminated(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["media_contaminated"]
    transcript = segment["channels"][1]
    played = [tuple(k) for k in segment["played"]]
    discussed = [tuple(k) for k in segment["discussed"]]
    ids = _fresh(client, played + discussed)
    played_ids = {ids[k] for k in played}
    talk_ids = {ids[k] for k in discussed}

    def stored():
        rows = _sql(
            client,
            "SELECT id,content FROM api.messages WHERE channel=" + _lit(transcript),
        )
        return {r["id"]: _digest(r["content"]) for r in rows}

    before = stored()
    run = _ok(
        client.post(
            "/v1/transcripts/noise/run",
            json=dict(
                policy=dict(channels=[transcript], media_channels=[segment["media"][0]])
            ),
        ),
        "noise run",
    )
    after = stored()
    expected = {
        ids[k]: _digest(_corpus_message(corpus, k)["content"])
        for k in played + discussed
    }
    flags = _sql(
        client,
        "SELECT message_id,value FROM api.annotations WHERE channel="
        + _lit(transcript)
        + " AND attribute='media_noise' AND status='active'",
    )
    flagged = {
        r["message_id"]
        for r in flags
        if isinstance(r.get("value"), dict) and r["value"].get("is_noise") is True
    }
    caption = _corpus_message(corpus, played[0])["content"]

    def recall(query, include_ignored=False):
        body = _ok(
            client.post(
                "/v1/recall",
                json=dict(
                    query=query,
                    k=50,
                    strategy="keyword",
                    scope=dict(channels=[transcript], include_ignored=include_ignored),
                ),
            ),
            "recall",
        )
        return {r["message_id"] for r in _hits(body)}

    default = recall(caption)
    ignored = recall(caption, include_ignored=True)
    talk = set()
    for key in discussed:
        talk |= recall(_corpus_message(corpus, key)["content"])
    metrics = dict(
        run_status=run.get("status"),
        run_flagged=run.get("flagged"),
        played_flagged=len(played_ids & flagged),
        talk_flagged=len(talk_ids & flagged),
        played_in_default_recall=len(played_ids & default),
        played_with_include_ignored=len(played_ids & ignored),
        talk_in_default_recall=len(talk_ids & talk),
        content_unchanged=all(before.get(i) == after.get(i) for i in before)
        and all(after.get(i) == d for i, d in expected.items()),
    )
    disclosures = [str(f) for f in run.get("failures") or []]
    if "function:around" in ctx.available:
        at = _corpus_message(corpus, segment["media"])["happened_at"]

        def around(include_ignored):
            rows = _rows(
                _ok(
                    client.get(
                        "/v1/around",
                        params=dict(
                            at=at,
                            window_s=600,
                            max_results=1000,
                            include_ignored=str(include_ignored).lower(),
                        ),
                    ),
                    "around",
                )
            )
            return {r["message_id"] for r in rows}

        default_around, ignored_around = around(False), around(True)
        metrics["played_in_default_around"] = len(played_ids & default_around)
        metrics["played_in_around_include_ignored"] = len(played_ids & ignored_around)
        metrics["talk_in_default_around"] = len(talk_ids & default_around)
    else:
        disclosures.append("around:skipped (need function:around)")
    n_played, n_talk = len(played_ids), len(talk_ids)
    passed = (
        metrics["played_flagged"] == n_played
        and metrics["talk_flagged"] == 0
        and metrics["played_in_default_recall"] == 0
        and metrics["played_with_include_ignored"] == n_played
        and metrics["talk_in_default_recall"] == n_talk
        and metrics["content_unchanged"]
        and metrics.get("played_in_default_around", 0) == 0
        and metrics.get("played_in_around_include_ignored", n_played) == n_played
        and metrics.get("talk_in_default_around", n_talk) == n_talk
    )
    return result(PASSED if passed else FAILED, metrics, disclosures)


# --- unenrolled_voice -------------------------------------------------------------


def _attribution(client, ids):
    rows = _sql(
        client,
        "SELECT message_id,person_id,party_id,attribution_status FROM api.appearances"
        f" WHERE message_id = ANY({_array(ids)})",
    )
    return {r["message_id"]: r for r in rows}


def check_unenrolled_voice(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["unenrolled_voice"]
    labels = segment["labels"]
    keys = [tuple(k) for k in segment["keys"]]
    ids = _fresh(client, keys)
    by_label = {}
    for key in keys:
        value = _corpus_message(corpus, key)["author"]["value"]
        by_label.setdefault(value, []).append(ids[key])
    identifiers = _identifiers(client)
    label_rows = {v: identifiers.get((scenarios.BEHAVIOUR_SYSTEM, v)) or {} for v in labels}
    thread = _sql(
        client,
        f"SELECT DISTINCT thread_id FROM api.messages WHERE id = ANY({_array(ids.values())})",
    )[0]["thread_id"]

    def inbox():
        rows = _ok(
            client.get("/v1/transcripts/speakers", params=dict(max_results=1000)),
            "speaker inbox",
        )["speakers"]
        return {r["label"] for r in rows if r.get("thread_id") == thread}

    waiting = {v for v in labels if label_rows[v].get("status") != "ignored"}
    metrics = dict(
        in_inbox=sorted(inbox() & set(labels)), expected_inbox=sorted(waiting)
    )
    people_before = len(_people(client))
    assignee = _people(client)[corpus.behaviours["dyad_vs_author"]["people"]["c"]]["id"]
    first, second = labels
    # The label must exist as an identifier, else a refusal proves nothing; a
    # refusal is a client error (a server error is not a refusal).
    metrics["label_identifiers"] = sum(1 for v in labels if label_rows[v].get("id"))
    attach = client.post(
        "/v1/identity/attach",
        json=dict(identifier_id=label_rows[first].get("id"), person_id=assignee),
    )
    metrics["attach_status"] = attach.status_code
    metrics["attach_refused"] = bool(label_rows[first].get("id")) and (
        400 <= attach.status_code < 500
    )
    unattributed = _attribution(client, list(ids.values()))
    metrics["person_rows_before"] = sum(
        1
        for r in unattributed.values()
        if r.get("person_id") is not None or r.get("party_id") is not None
    )
    assigned = _ok(
        client.post(
            "/v1/transcripts/speakers/assign",
            json=dict(thread_id=thread, label=first, person_id=assignee),
        ),
        "assign",
    )
    after = _attribution(client, list(ids.values()))
    metrics["assigned_turns"] = assigned.get("turns")
    metrics["annotated"] = sorted(
        r["attribution_status"] for i, r in after.items() if i in by_label[first]
    )
    metrics["person_rows_after"] = sum(
        1
        for r in after.values()
        if r.get("person_id") is not None or r.get("party_id") is not None
    )
    metrics["people_created"] = len(_people(client)) - people_before
    metrics["label_identifier_person"] = (
        _identifiers(client).get((scenarios.BEHAVIOUR_SYSTEM, first)) or {}
    ).get("person_id")
    _ok(
        client.post(
            "/v1/transcripts/speakers/ignore", json=dict(thread_id=thread, label=second)
        ),
        "ignore",
    )
    metrics["ignored_left_inbox"] = second not in inbox()
    passed = (
        set(metrics["in_inbox"]) == waiting
        and metrics["attach_refused"]
        and metrics["person_rows_before"] == 0
        and metrics["annotated"] == ["annotated"] * len(by_label[first])
        and metrics["person_rows_after"] == 0
        and metrics["people_created"] == 0
        and metrics["label_identifier_person"] is None
        and metrics["ignored_left_inbox"]
    )
    return result(PASSED if passed else FAILED, metrics)


# --- suspected_link ---------------------------------------------------------------


def check_suspected_link(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["suspected_link"]
    person = _people(client)[segment["person"]]["id"]
    identifier = _identifiers(client)[tuple(segment["identifier"])]["id"]
    ids = _fresh(client, segment["own"] + segment["suspected"])
    own = {ids[tuple(k)] for k in segment["own"]}
    suspected = {ids[tuple(k)] for k in segment["suspected"]}

    def status(value):
        return _ok(
            client.post(
                f"/v1/identity/identifiers/{identifier}/status",
                json=dict(status=value, reason="bench suspected_link"),
            ),
            "identifier status",
        )

    def seen():
        timeline = _rows(
            _ok(
                client.get(
                    f"/v1/people/{person}/timeline", params=dict(max_results=1000)
                ),
                "timeline",
            )
        )
        brief = (
            _ok(client.get(f"/v1/people/{person}/brief"), "brief").get("brief")
        ) or {}
        body = _ok(
            client.post(
                "/v1/recall",
                json=dict(
                    query="mine",
                    k=50,
                    strategy="keyword",
                    scope=dict(persons=[person], channels=segment["channels"]),
                ),
            ),
            "recall",
        )
        return dict(
            timeline={r["message_id"] for r in timeline if r.get("side") == "from"},
            recall={r["message_id"] for r in _hits(body)},
            brief_messages=brief.get("message_count"),
        )

    def phase(included):
        got = seen()
        want = own | suspected if included else own
        return dict(
            timeline_ok=got["timeline"] == want,
            recall_ok=got["recall"] == want,
            brief_ok=got["brief_messages"] == len(want),
            suspected_in_timeline=len(got["timeline"] & suspected),
            suspected_in_recall=len(got["recall"] & suspected),
            brief_messages=got["brief_messages"],
        )

    status("suspected")
    metrics = dict(suspected=phase(False))
    confirmed = status("confirmed")
    metrics["confirm_attributed"] = confirmed.get("attributed_messages")
    metrics["confirmed"] = phase(True)
    status("suspected")
    metrics["suspected_again"] = phase(False)
    passed = all(
        metrics[p][k]
        for p in ("suspected", "confirmed", "suspected_again")
        for k in ("timeline_ok", "recall_ok", "brief_ok")
    )
    return result(PASSED if passed else FAILED, metrics)


# --- write_replay -----------------------------------------------------------------

RECEIPT_FIELDS = ("kind", "id", "record_id", "version", "stored_hash", "counts")


def _same_receipt(first, again):
    first, again = first or {}, again or {}
    return all(first.get(f) == again.get(f) for f in RECEIPT_FIELDS) and bool(first)


def _count(client, statement):
    return _sql(client, statement)[0]["n"]


def check_write_replay(client, corpus, phase_times, ctx):
    segment = corpus.behaviours["write_replay"]
    channel = segment["channels"][0]
    k0, k1, k2 = (tuple(k) for k in segment["keys"])
    ids = _fresh(client, [k0, k1, k2])
    nonce = uuid.uuid4().hex[:20]
    metrics, disclosures = {}, [f"idempotency_key_prefix:bench-behaviour-{nonce}"]

    def twice(path, body, kind, count_sql):
        headers = {"Idempotency-Key": f"bench-behaviour-{nonce}-{kind}"}
        before = _count(client, count_sql)
        first = _ok(client.post(path, json=body, headers=headers), kind)
        middle = _count(client, count_sql)
        again = _ok(client.post(path, json=body, headers=headers), kind + " replay")
        after = _count(client, count_sql)
        first_receipt, again_receipt = first.get("receipt"), again.get("receipt")
        metrics[kind] = dict(
            rows_added=middle - before,
            rows_added_by_replay=after - middle,
            rows=after,
            replayed=[
                (first_receipt or {}).get("replayed"),
                (again_receipt or {}).get("replayed"),
            ],
            same_receipt=_same_receipt(first_receipt, again_receipt),
        )
        return headers

    link = {"type": "about", "from": _key(k0), "to": _key(k1)}
    links_sql = (
        "SELECT count(*) AS n FROM api.links WHERE type='about' AND status='active'"
        f" AND from_kind='message' AND to_kind='message' AND from_id={ids[k0]} AND to_id="
    )
    headers = twice("/v1/links", link, "link", links_sql + str(ids[k1]))
    other = {"type": "about", "from": _key(k0), "to": _key(k2)}
    other_before = _count(client, links_sql + str(ids[k2]))
    conflict = client.post("/v1/links", json=other, headers=headers)
    metrics["different_body"] = dict(
        status=conflict.status_code,
        rows_added=_count(client, links_sql + str(ids[k2])) - other_before,
    )
    at = _corpus_message(corpus, k2)["happened_at"]
    annotation = dict(
        message_id=ids[k2],
        attribute="effective_time",
        value={"at": at, "precision": "exact"},
        reason="bench write replay",
    )
    twice(
        "/v1/annotations",
        annotation,
        "annotation",
        "SELECT count(*) AS n FROM api.annotations WHERE attribute='effective_time'"
        f" AND channel={_lit(channel)} AND target_external_id={_lit(k2[1])}",
    )
    batch = [
        link,
        {"type": "about", "from": _key(k1), "to": _key(k2)},
        {
            "type": "about",
            "from": _key((channel, "behaviour-writes-missing")),
            "to": _key(k2),
        },
    ]
    answer = _ok(
        client.post(
            "/v1/links/batch",
            json={"links": batch},
            headers={"Idempotency-Key": f"bench-behaviour-{nonce}-batch"},
        ),
        "link batch",
    )
    results = answer.get("results") or []
    metrics["batch"] = dict(
        indexes=[r.get("index") for r in results],
        statuses=[r.get("status") for r in results],
        counts=answer.get("counts"),
    )
    once = all(
        metrics[k]["rows_added"] <= 1
        and metrics[k]["rows_added_by_replay"] == 0
        and metrics[k]["replayed"] == [False, True]
        and metrics[k]["same_receipt"]
        for k in ("link", "annotation")
    )
    statuses = metrics["batch"]["statuses"]
    passed = (
        once
        and metrics["link"]["rows"] == 1
        and metrics["different_body"] == dict(status=409, rows_added=0)
        and metrics["batch"]["indexes"] == [0, 1, 2]
        and len(statuses) == 3
        and statuses[0] == "existing"
        and statuses[1] in ("created", "existing")
        and statuses[2] == "error"
    )
    return result(PASSED if passed else FAILED, metrics, disclosures)


# --- the registry -----------------------------------------------------------------

BEHAVIOURS = {
    b.name: b
    for b in (
        Behaviour(
            "episode_count",
            ["recall", "episodes"],
            "Three incidents of three hits each: the manifest and SQL count"
            " incidents (3), not hits (9).",
            check_episode_count,
        ),
        Behaviour(
            "newest_relevant",
            ["recall"],
            "hints.order 'newest' starts with the newest record that answers,"
            " never the newest chatter of the thread.",
            check_newest_relevant,
        ),
        Behaviour(
            "consumer_exactly_once",
            ["consumers"],
            "A named consumer receives late backfilled rows exactly once;"
            " a repeated mark is a no-op; news without mark redelivers.",
            check_consumer_exactly_once,
        ),
        Behaviour(
            "coverage_honesty",
            ["recall", "coverage"],
            "Nothing found inside a reported gap is 'not found in coverage' with"
            " the gap listed; in a complete channel 'not found in covered"
            " sources'; never a negative assertion; a covering report closes it.",
            check_coverage_honesty,
        ),
        Behaviour(
            "link_vocabulary",
            ["link_types", "principals"],
            "Free-text types refused, synonyms folded with type_as_written kept,"
            " undeclared plugin types held as candidates, merges collapse ends.",
            check_link_vocabulary,
        ),
        Behaviour(
            "restricted_inference",
            ["recall", "principals"],
            "A restricted asker's counts (SQL, recall manifest, episodes,"
            " coverage, pending, link types) and person functions (brief, arc,"
            " rhythm, last contact, open loops, circle ranks) never reflect a"
            " hidden row; labels and commitments when installed.",
            check_restricted_inference,
        ),
        Behaviour(
            "dyad_vs_author",
            ["recall", "person_role_between"],
            "'Between A and B' returns both sides from shared threads (recall"
            " person_role 'between' and /v1/between), never a third person;"
            " author scope returns one side and is not a dyad; the owner side"
            " counts by direction.",
            check_dyad_vs_author,
            plant=plant_dyad,
            checked_by="bench-behaviours",
        ),
        Behaviour(
            "open_loops",
            ["open_loops"],
            "Asks unanswered after a day in either direction (owner_owes,"
            " they_owe); answered asks and wound-down chats are absent.",
            check_open_loops,
            plant=plant_open_loops,
            checked_by="bench-behaviours",
        ),
        Behaviour(
            "media_contaminated",
            ["media_noise", "recall"],
            "Played-media transcript turns are flagged and left out of default"
            " recall and around, back with include_ignored, text unchanged;"
            " turns discussing the media stay.",
            check_media_contaminated,
            plant=plant_media,
            checked_by="bench-behaviours",
        ),
        Behaviour(
            "unenrolled_voice",
            ["speaker_inbox"],
            "Speaker labels wait in an inbox, cannot be attached to a person,"
            " are annotated (never a person) when assigned and leave the inbox"
            " when ignored.",
            check_unenrolled_voice,
            checked_by="bench-behaviours",
        ),
        Behaviour(
            "suspected_link",
            ["identifier_status", "recall"],
            "A suspected identifier's messages stay out of person functions and"
            " person-scoped recall until confirmed, and leave again when set"
            " back to suspected.",
            check_suspected_link,
            checked_by="bench-behaviours",
        ),
        Behaviour(
            "write_replay",
            ["idempotency"],
            "The same Idempotency-Key and body give one row and the same receipt"
            " (link, claim, annotation); a different body is 409; a link batch"
            " reports per item.",
            check_write_replay,
            checked_by="bench-behaviours",
        ),
    )
}


def bind_behaviours(client, corpus, phase_times=None):
    """(behaviour questions by id, bound; gold ids of every behaviour key)."""
    keys = []
    for segment in corpus.behaviours.values():
        for value in segment.values():
            keys += _keys_in(value)
    questions = [q for q in corpus.questions if q.category.startswith("behaviour_")]
    for question in questions:
        keys += [r for r in question.refs() if not isinstance(r, int)]
    ids = gold.resolve(client, list(dict.fromkeys(keys)))
    bound = {q.id: gold.bind(q, ids, phase_times) for q in questions}
    return bound, ids


def _keys_in(value):
    """Corpus keys (channel, external_id) anywhere inside a value."""
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(v, str) for v in value)
        and value[0].startswith(scenarios.PLUGIN + ".")
    ):
        return [value]
    if isinstance(value, (list, tuple)):
        return [k for v in value for k in _keys_in(v)]
    if isinstance(value, dict):
        return [k for v in value.values() for k in _keys_in(v)]
    return []


def run_one(behaviour, client, corpus, phase_times, ctx):
    """One behaviour under the skip rule."""
    missing = behaviour.unmet(ctx.available)
    base = dict(needs=list(behaviour.needs), checked_by=behaviour.checked_by)
    if missing:
        return dict(
            result(SKIPPED, reason="missing need: " + ", ".join(missing)),
            missing=missing,
            **base,
        )
    if behaviour.check is None:
        return dict(result(SKIPPED, reason=PENDING), missing=[], **base)
    started = time.perf_counter()
    planted = []
    try:
        if behaviour.plant is not None:
            planted = list(behaviour.plant(client, corpus) or [])
        out = behaviour.check(client, corpus, phase_times, ctx)
    except Exception as exc:
        out = result(FAILED, reason=f"{type(exc).__name__}: {exc}"[:500])
    out["ms"] = round((time.perf_counter() - started) * 1000, 3)
    out["planted"] = planted
    return dict(out, missing=[], **base)


def derived_needs(available, principals=None):
    """Needs that are not a route or a catalogue entry: other callers, and
    labels and commitments built in wave 6 (probed, never assumed)."""
    found = set()
    if principals is not None:
        found.add("principals")
        try:
            if principals.can_label():
                found.add("labels")
        except Exception:
            pass
    return found


def run_all(
    client, corpus, phase_times=None, principals=None, available=None, names=None
):
    """Every behaviour (or those named): {name: {status, metrics, ...}}."""
    unknown = [name for name in names or () if name not in BEHAVIOURS]
    if unknown:
        raise ValueError("unknown behaviour: " + ", ".join(unknown))
    available = set(arm_registry.probe(client) if available is None else available)
    available |= derived_needs(available, principals)
    questions, ids = bind_behaviours(client, corpus, phase_times)
    ctx = Context(
        questions=questions, ids=ids, principals=principals, available=available
    )
    out = {}
    for name in names or BEHAVIOURS:
        out[name] = run_one(BEHAVIOURS[name], client, corpus, phase_times or {}, ctx)
    return out
