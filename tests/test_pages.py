"""Pages as lens output (migration 0070, docs/v11/pages.md): a lens with
output 'pages' runs the Librarian, which answers each work item with page
actions; pages form a graph tied to their evidence; find adds a page map."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ingest

from throughline import lenses, pages
from throughline.db import connect

CH = "synthetic.chat"


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("THROUGHLINE_TIMEZONE", "UTC")
    monkeypatch.setenv("THROUGHLINE_PAGES_CONFIG", str(tmp_path / "pages.toml"))
    monkeypatch.delenv("THROUGHLINE_MONTHLY_MODEL_CAP_USD", raising=False)


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def q(sql, params=()):
    with connect() as c:
        return [dict(r) for r in c.execute(sql, params)]


T0 = datetime(2026, 3, 14, 9, tzinfo=timezone.utc)


class World:
    def __init__(self, client):
        self.client = client
        ok(client.post("/v1/channels", json=dict(name=CH, plugin="synthetic", fields=[])))
        self.owner = self.person("syn-owner", "Owner", owner=True)
        self.n = 0

    def person(self, handle, display, owner=False):
        return ok(self.client.post("/v1/identity/people", json=dict(handle=handle, display_name=display, is_owner=owner)))["id"]

    def attach(self, account, pid):
        ident = q("SELECT id FROM tl.identifiers WHERE value=%s", (account,))[0]["id"]
        ok(self.client.post("/v1/identity/attach", json=dict(identifier_id=ident, person_id=pid)))

    def say(self, when, account, text, thread="t-group"):
        self.n += 1
        item = dict(channel=CH, external_id=f"m-{self.n}", thread={"external_id": thread, "title": thread},
                    happened_at=when.isoformat(), content=text,
                    author=dict(system="fake", kind="account", value=account), payload={"n": self.n})
        assert ingest(self.client, [item]).status_code == 200
        return q("SELECT id FROM tl.messages WHERE external_id=%s AND superseded_by IS NULL", (f"m-{self.n}",))[0]["id"]


@pytest.fixture()
def world(client):
    w = World(client)
    w.alice = w.person("alice", "Alice Moreau")
    w.bob = w.person("bob", "Bob")
    w.ids = {
        "a1": w.say(T0, "acct-alice", "Ramen at Tsuta on Saturday? I love tonkotsu"),
        "b1": w.say(T0 + timedelta(hours=1), "acct-bob", "I'm in. Alice Moreau is bringing her sister"),
        "o1": w.say(T0 + timedelta(hours=2), "acct-owner", "Booked Tsuta for 7pm Saturday"),
        "x1": w.say(T0 + timedelta(days=1), "acct-bob", "unrelated: my bike got a flat"),
    }
    w.attach("acct-alice", w.alice)
    w.attach("acct-bob", w.bob)
    w.attach("acct-owner", w.owner)
    return w


def lens_spec(**over):
    spec = {
        "name": "daily_pages", "output": "pages", "grain": "day", "channels": [CH],
        "prompt": "Keep a page per day and pages for events.",
        "worker_config": {"max_turns": 3},
    }
    spec.update(over)
    return spec


def ids_in(payload):
    out = []
    for block in payload["evidence"]:
        out += [int(m) for m in re.findall(r"^\[m:(\d+)\]", "\n".join(block.get("lines") or []), re.M)]
        if "page" in block:
            out.append(block["id"])
    return out


def test_spec_rules():
    s = lenses.validate_lens_spec(lens_spec())
    assert s["output"] == "pages" and s["worker"] == "librarian" and s["columns"] == []
    with pytest.raises(lenses.LensSpecError, match="no columns"):
        lenses.validate_lens_spec(lens_spec(columns=[{"name": "x", "type": "text"}]))
    with pytest.raises(lenses.LensSpecError, match="librarian"):
        lenses.validate_lens_spec(lens_spec(worker="llm"))
    with pytest.raises(lenses.LensSpecError, match="librarian"):
        lenses.validate_lens_spec({"name": "t", "worker": "librarian", "prompt": "x", "columns": [{"name": "a", "type": "text"}]})
    with pytest.raises(lenses.LensSpecError, match="allow"):
        lenses.validate_lens_spec(lens_spec(worker_config={"allow": ["delete"]}))
    with pytest.raises(lenses.LensSpecError, match="max_turns"):
        lenses.validate_lens_spec(lens_spec(worker_config={"max_turns": 50}))
    import glob
    import os

    from throughline.pages import librarian

    for f in glob.glob(os.path.join(os.path.dirname(librarian.__file__), "examples", "*.toml")):
        assert lenses.load_spec_file(f)["output"] == "pages"


def test_lens_writes_pages_links_and_evidence(world):
    w = world
    day = T0.date().isoformat()
    seen_turns = []

    def librarian(tdef, payload, turn):
        seen_turns.append(payload)
        if payload["window"].get("day") != day:
            return {"actions": []}
        if turn == 0:
            names = {p["name"] for p in payload["people_directory"]}
            assert {"Alice Moreau", "Bob", "Owner"} <= names
            return {"calls": [{"tool": "search_pages", "args": {"query": "Tsuta"}},
                              {"tool": "read_context", "args": {"message_id": w.ids["a1"], "before": 0, "after": 5}}]}
        ev = ids_in(payload)
        assert ev == [w.ids["a1"], w.ids["b1"], w.ids["o1"]]
        assert payload["tool_results"][0]["pages"] == []
        return {"actions": [
            {"op": "write", "slug": f"days/{day}", "kind": "days", "title": day, "summary": "Ramen plans",
             "body": f"## Summary\nPlanned ramen at Tsuta [m:{w.ids['a1']},{w.ids['o1']}] [m:99999999]. "
                     f"See [[events/{day}-tsuta-ramen]] with [[person:{w.alice}|Alice]] and [[nowhere/page]]."},
            {"op": "write", "slug": f"events/{day}-tsuta-ramen", "title": "Ramen at Tsuta", "aliases": ["Tsuta dinner"],
             "when": {"start": "2026-03-15"}, "summary": "Saturday ramen",
             "body": f"## Who was there\n[[person:{w.alice}|Alice]], [[person:{w.bob}|Bob]] [m:{w.ids['b1']}]\n\n## When\nSaturday 7pm [m:{w.ids['o1']}]"},
            {"op": "link", "from": f"events/{day}-tsuta-ramen", "to": f"days/{day}", "type": "part_of", "evidence": [w.ids["o1"]]},
            {"op": "link", "from": f"events/{day}-tsuta-ramen", "to": "person:%d" % w.bob, "type": "related_to", "evidence": [123456789]},
            {"op": "write", "slug": "people/alice-moreau", "kind": "people", "title": "Alice", "body": "## Tastes\nLoves tonkotsu"},
        ]}

    lenses.define_lens(lens_spec())
    with pages.override_model(librarian):
        res = lenses.run_lens("daily_pages")
    assert res["status"] in ("done", "max_items"), res
    assert res["model_calls"] == 3 and res["invalid"] == 2, (res["errors"], res["model_calls"], res["invalid"])  # two turns on the first day, one on the second
    with connect() as c:
        day_page = pages.get(c, f"days/{day}")
        assert "99999999" not in day_page["body"] and day_page["kind"] == "days"
        assert day_page["citations"] == [w.ids["a1"], w.ids["o1"]]
        assert day_page["stats"]["unresolved"] == ["nowhere/page"]
        ev_page = pages.get(c, f"events/{day}-tsuta-ramen")
        rec = c.execute("SELECT subject_from, subject_to_precision FROM tl.messages WHERE id=%s", (ev_page["message_id"],)).fetchone()
        assert rec["subject_from"].date().isoformat() == "2026-03-15" and rec["subject_to_precision"] == "day"
        nb = pages.neighbors(c, f"events/{day}-tsuta-ramen")
        outs = {(n["type"], n["slug"] or f"person:{n['to_id']}") for n in nb["out"]}
        assert ("part_of", f"days/{day}") in outs and ("mentions", f"person:{w.bob}") in outs
        assert ("related_to", f"person:{w.bob}") not in outs  # evidence it never saw: refused
        ins = {(n["type"], n["slug"]) for n in pages.neighbors(c, f"days/{day}")["in"]}
        assert ("part_of", f"events/{day}-tsuta-ramen") in ins
        srcs = {r["source_message_id"] for r in c.execute(
            "SELECT source_message_id FROM tl.provenance WHERE derived_kind='message' AND derived_id=%s", (ev_page["message_id"],))}
        assert srcs == {w.ids["b1"], w.ids["o1"]}
        acts = c.execute("SELECT op, status, detail FROM tl.page_actions WHERE window_key=%s ORDER BY id", (f"day:{day}",)).fetchall()
        assert [(a["op"], a["status"]) for a in acts] == [
            ("write", "applied"), ("write", "applied"), ("link", "applied"), ("link", "refused"), ("write", "refused")]
        assert "cites no record" in acts[4]["detail"]["reason"]
    # The day is read again only when it changes.
    with pages.override_model(lambda *a: pytest.fail("nothing new: no call")):
        again = lenses.run_lens("daily_pages")
    assert again["considered"] == 0


def test_read_before_overwrite_edit_kinds_and_limits(world):
    w = world
    with connect() as c:
        pages.write(c, slug="people/alice", kind="people", title="Alice", body=f"## Tastes\nRamen [m:{w.ids['a1']}]", by="owner", person_id=w.alice)
        pages.write(c, slug="people/unread", kind="people", title="Unread", body=f"x [m:{w.ids['a1']}]", by="owner")
    replies = iter([
        {"calls": [{"tool": "read_page", "args": {"slug": "people/alice"}}]},
        {"actions": [
            {"op": "write", "slug": "people/unread", "title": "Unread", "body": f"## X\nnew [m:{w.ids['a1']}]"},
            {"op": "edit", "slug": "people/alice", "section": "Tastes", "mode": "append", "text": f"Tonkotsu in particular [m:{w.ids['a1']}]"},
            {"op": "edit", "slug": "people/alice", "section": "Family", "mode": "append", "text": f"Has a sister [m:{w.ids['b1']}]"},
            {"op": "write", "slug": "events/x", "title": "X", "body": f"x [m:{w.ids['a1']}]"},
            {"op": "write", "slug": "people/bob", "title": "Bob", "person_id": w.bob, "body": f"## Who\nBob [m:{w.ids['b1']}]"},
            {"op": "write", "slug": "people/third", "title": "Third", "body": f"## Who\nT [m:{w.ids['b1']}]"},
        ]},
    ])
    lenses.define_lens(lens_spec(name="people_pages", worker_config={"kinds": ["people"], "max_new_pages": 1, "max_turns": 2}))
    with pages.override_model(lambda tdef, payload, turn: next(replies) if payload["window"].get("day") == "2026-03-14" else {"actions": []}):
        lenses.run_lens("people_pages")
    st = [(r["op"], r["slug"], r["status"], r["detail"].get("reason", "")[:90]) for r in q("SELECT * FROM tl.page_actions ORDER BY id")]
    assert st[0][2] == "refused" and "read_page" in st[0][3]
    assert st[1][2] == st[2][2] == "applied"
    assert st[3][2] == "refused" and "kind" in st[3][3]
    assert st[4][2] == "applied" and st[5][2] == "refused" and "max_new_pages" in st[5][3]
    with connect() as c:
        alice = pages.get(c, "people/alice")
        assert alice["version"] == 3 and "Tonkotsu" in alice["body"] and "## Family" in alice["body"]
        assert alice["body"].index("Ramen") < alice["body"].index("Tonkotsu")
        assert alice["person_id"] == w.alice and alice["updated_by"] == "lens:people_pages"
        about = c.execute("SELECT count(*) AS n FROM tl.links WHERE from_id=%s AND type='about' AND to_kind='person' AND to_id=%s AND status='active'",
                          (alice["message_id"], w.alice)).fetchone()["n"]
        assert about == 1  # the link moved to the new revision and was not duplicated


def test_linker_lens_reads_pages_but_not_its_own_revisions(world):
    w = world
    with connect() as c:
        pages.write(c, slug="events/tsuta", kind="events", title="Tsuta dinner", body=f"Dinner [m:{w.ids['o1']}]", by="owner")
        pages.write(c, slug="people/alice", kind="people", title="Alice Moreau", body=f"Likes ramen [m:{w.ids['a1']}]", by="owner")
    seen = []

    def linker(tdef, payload, turn):
        blocks = [b for b in payload["evidence"] if "page" in b]
        if turn == 0:
            seen.extend(b["page"] for b in blocks)
        if blocks[0]["page"] == "events/tsuta":
            if turn == 0:
                return {"calls": [{"tool": "read_page", "args": {"slug": "events/tsuta"}}]}
            return {"actions": [
                {"op": "link", "from": "events/tsuta", "to": "people/alice", "type": "related_to", "evidence": [w.ids["o1"]]},
                {"op": "edit", "slug": "events/tsuta", "section": "Who", "mode": "append", "text": f"[[people/alice]] [m:{w.ids['o1']}]"},
            ]}
        return {"actions": []}

    lenses.define_lens(lens_spec(name="page_linker", grain="message", channels=["pages"],
                                 worker_config={"allow": ["link", "edit"], "max_turns": 2}))
    with pages.override_model(linker):
        lenses.run_lens("page_linker")
        assert sorted(seen) == ["events/tsuta", "people/alice"]
        seen.clear()
        lenses.run_lens("page_linker")  # its own edit made a revision: not shown again
    assert seen == []
    with connect() as c:
        outs = {(n["type"], n["slug"], n["job"]) for n in pages.neighbors(c, "events/tsuta")["out"]}
    assert ("related_to", "people/alice", "librarian") in outs and ("mentions", "people/alice", "pages") in outs


def test_local_provider_plumbing(world):
    lenses.define_lens(lens_spec(worker_config={"provider": "local"}))
    res = lenses.run_lens("daily_pages")
    assert res["status"] in ("done", "max_items")
    rows = q("SELECT slug FROM tl.pages ORDER BY slug")
    assert [r["slug"] for r in rows] == ["daily-pages/2026-03-14", "daily-pages/2026-03-15"]


def test_find_adds_graph_ranked_page_map(world, client):
    w = world
    with connect() as c:
        pages.write(c, slug="people/alice-moreau", kind="people", title="Alice Moreau", aliases=["Ali"],
                    body=f"## Tastes\nTonkotsu ramen [m:{w.ids['a1']}]", by="owner", person_id=w.alice, summary="Ramen lover")
        pages.write(c, slug="events/tsuta", kind="events", title="Ramen at Tsuta",
                    body=f"With [[people/alice-moreau]] [m:{w.ids['o1']}]", by="owner")
        pages.write(c, slug="topics/bikes", kind="topics", title="Bikes", body=f"Flat tyre [m:{w.ids['x1']}]", by="owner")
    r = ok(client.post("/v1/find", json={"query": "what does Alice Moreau like", "k": 5}))
    refs = [p["ref"] for p in r["pages"]]
    assert refs[0] == "people/alice-moreau"
    first = r["pages"][0]
    assert first["why"] == "title match" and first["generated"] is True
    assert first["evidence"][0]["message_id"] == w.ids["a1"]
    assert "events/tsuta" in refs  # linked to the matched page
    tsuta = next(p for p in r["pages"] if p["ref"] == "events/tsuta")
    assert tsuta["why"].startswith("linked from")
    assert "topics/bikes" not in refs
    assert r["matched_people"][0]["page"] == "people/alice-moreau"
    assert r["episodes"]  # the messages are still the answer
    assert "pages" not in ok(client.post("/v1/find", json={"query": "Alice Moreau", "pages": 0}))


def test_health_graph_lenses_and_sinks(world, tmp_path, monkeypatch):
    w = world
    for i in range(3):
        w.say(T0 + timedelta(days=2, minutes=i), "acct-bob", f"bob says {i}")
    with connect() as c:
        pages.write(c, slug="people/alice", kind="people", title="Alice", body=f"x [m:{w.ids['a1']}]", by="owner", person_id=w.alice)
        pages.write(c, slug="topics/orphan", kind="topics", title="Orphan", body="nothing cited", by="owner")
    lenses.define_lens(lens_spec(worker_config={"max_turns": 1}))
    with pages.override_model(lambda *a: {"actions": [{"op": "edit", "slug": "nope/x", "section": "A", "mode": "append", "text": "x"}]}):
        lenses.run_lens("daily_pages")
    from throughline.pages import health

    with connect() as c:
        g = health.graph_health(c, now=T0 + timedelta(days=3), gap_messages=3)
        lh = health.lens_health(c, now=T0 + timedelta(days=3))
    assert g["pages"] == 2 and g["orphan_count"] == 2 and g["uncited"] == ["topics/orphan"]
    assert [p["person_id"] for p in g["people_without_page"]] == [w.bob]
    assert lh[0]["lens"] == "daily_pages" and lh[0]["actions_7d"]["edit"]["refused"] >= 1
    assert "no page nope/x" in lh[0]["refusals"][0]
    # sinks
    out = tmp_path / "brain"
    got = tmp_path / "cmd.txt"
    (tmp_path / "pages.toml").write_text(
        f'[[sinks]]\nkind = "markdown"\npath = "{out}/{{slug}}.md"\n\n'
        f'[[sinks]]\nkind = "command"\nname = "gb"\nkinds = ["people"]\nargv = ["sh", "-c", "cat >> {got}", "x", "{{slug}}"]\n'
    )
    res = pages.export()
    assert res["sent"] == 3, res  # two markdown files, one people page to the command
    md = (out / "people" / "alice.md").read_text()
    assert md.startswith("---\n") and 'throughline_page: "people/alice"' in md
    assert "Orphan" not in got.read_text()
    assert pages.export()["sent"] == 0


def test_routes(world, client):
    w = world
    r = ok(client.post("/v1/pages", json={"slug": "people/alice", "title": "Alice", "body": f"Hi [m:{w.ids['a1']}]", "person_id": w.alice},
                       headers={"Idempotency-Key": "pages-route-1"}))
    assert r["outcome"] == "created"
    ok(client.post("/v1/pages", json={"slug": "events/tsuta", "title": "Tsuta", "body": f"Dinner [m:{w.ids['o1']}]"},
                   headers={"Idempotency-Key": "pages-route-2"}))
    link = ok(client.post("/v1/pages/links", json={"from": "events/tsuta", "to": "people/alice", "evidence": [w.ids["o1"]]},
                          headers={"Idempotency-Key": "pages-route-3"}))
    assert link["outcome"] == "created"
    assert client.post("/v1/pages/links", json={"from": "events/tsuta", "to": "people/alice", "evidence": []},
                       headers={"Idempotency-Key": "pages-route-4"}).status_code == 400
    assert client.post("/v1/pages", json={"slug": "Bad Slug!", "title": "x", "body": "y"}, headers={"Idempotency-Key": "pages-route-5"}).status_code == 400
    lst = ok(client.get("/v1/pages", params={"q": "tsuta"}))
    assert [p["slug"] for p in lst] == ["events/tsuta"]
    page = ok(client.get("/v1/pages/events/tsuta"))
    assert page["links"]["out"][0]["slug"] == "people/alice"
    assert client.get("/v1/pages/events/none").status_code == 404
    assert ok(client.get("/v1/pages/health"))["graph"]["pages"] == 2
    assert ok(client.get("/v1/pages/actions")) == []
    rows = q("SELECT kind FROM api.records WHERE channel='pages'")
    assert rows and {r["kind"] for r in rows} == {"page_revision"}


# --- GBrain mechanics -----------------------------------------------------------


def test_compiled_truth_over_append_only_timeline(world):
    w = world
    a1, b1, o1 = w.ids["a1"], w.ids["b1"], w.ids["o1"]
    with connect() as c:
        r1 = pages.write(c, slug="people/alice", kind="people", title="Alice", by="owner",
                         body=f"Alice is a friend [m:{a1}].\n\n## Timeline\n- **2026-03-14** — Suggested ramen [m:{a1}]\n- 2026-03-13 - Texted [m:{b1}]\nnot a dated line")
        assert r1["timeline_added"] == 2
        page = pages.get(c, "people/alice")
        assert page["body"].endswith(f"## Timeline\n- **2026-03-14** — Suggested ramen [m:{a1}]\n- **2026-03-13** — Texted [m:{b1}]")
        assert page["stats"]["timeline_unparsed"] == 1
        truth_at = page["truth_updated_at"]
        # A rewrite that leaves the timeline out keeps it (append-only).
        r2 = pages.write(c, slug="people/alice", kind="people", title="Alice", by="owner", body=f"Alice loves tonkotsu [m:{a1}].")
        page = pages.get(c, "people/alice")
        assert r2["truth_changed"] and "## Timeline" in page["body"] and "Texted" in page["body"]
        # Timeline-only change: the truth timestamp stays.
        truth_at = page["truth_updated_at"]
        r3 = pages.write(c, slug="people/alice", kind="people", title="Alice", by="owner",
                         body=f"Alice loves tonkotsu [m:{a1}].\n\n## Timeline\n- **2026-03-15** — Booked Tsuta [m:{o1}]\n- **2026-03-14** — Suggested ramen [m:{a1}]")
        page = pages.get(c, "people/alice")
        assert r3["timeline_added"] == 1 and not r3["truth_changed"] and page["truth_updated_at"] == truth_at
        assert [r["date"].isoformat() for r in pages.chronicle(c, slug="people/alice")] == ["2026-03-15", "2026-03-14", "2026-03-13"]
        assert pages.chronicle(c, on="2026-03-15")[0]["slug"] == "people/alice"
        # Same content again: no new revision.
        again = pages.write(c, slug="people/alice", kind="people", title="Alice", by="owner", body=page["body"])
        assert again["outcome"] == "unchanged"


def test_librarian_timeline_op_revision_check_and_quotes(world):
    w = world
    with connect() as c:
        pages.write(c, slug="people/alice", kind="people", title="Alice", body=f"Alice [m:{w.ids['a1']}]", by="owner", person_id=w.alice)

    def librarian(tdef, payload, turn):
        if payload["window"].get("day") != "2026-03-14":
            return {"actions": []}
        if turn == 0:
            kinds = {k["kind"] for k in payload["page_kinds"]}
            assert "people" in kinds and "events" in kinds
            return {"calls": [{"tool": "read_page", "args": {"slug": "people/alice"}}]}
        # Someone else revises the page after it was read.
        with connect() as c:
            pages.write(c, slug="people/alice", kind="people", title="Alice", body=f"Alice (updated) [m:{w.ids['a1']}]", by="owner")
        return {"actions": [
            {"op": "edit", "slug": "people/alice", "section": "Tastes", "mode": "append", "text": f"ramen [m:{w.ids['a1']}]"},
            {"op": "timeline", "slug": "people/alice", "reason": "new evidence", "entries": [
                {"date": "2026-03-14", "text": f'Said "I love tonkotsu" [m:{w.ids["a1"]}]'},
                {"date": "2026-03-14", "text": f'Said "I hate all soup forever" [m:{w.ids["a1"]}]'},
                {"date": "2026-03-14", "text": f'Said "I love tonkotsu" [m:{w.ids["a1"]}]'}]},
            {"op": "edit", "slug": "people/alice", "section": "Timeline", "mode": "append",
             "text": f"- **2026-03-16** — Ate at Tsuta [m:{w.ids['o1']}]"},
        ]}

    lenses.define_lens(lens_spec(worker_config={"max_turns": 2}))
    with pages.override_model(librarian):
        lenses.run_lens("daily_pages")
    acts = q("SELECT op, status, detail FROM tl.page_actions ORDER BY id")
    assert acts[0]["status"] == "refused" and "changed after you read it" in acts[0]["detail"]["reason"]
    assert acts[1]["status"] == "applied" and acts[1]["detail"]["unverified_quotes"] == 1 and acts[1]["detail"]["reason"] == "new evidence"
    assert acts[2]["status"] == "applied" and acts[2]["detail"]["entries"] == 1  # an edit of Timeline is a timeline append
    with connect() as c:
        lines = [r["summary"] for r in pages.chronicle(c, slug="people/alice")]
    assert lines[0].startswith("Ate at Tsuta")
    assert f'Said "I love tonkotsu" [m:{w.ids["a1"]}]' in lines  # verified quote kept, duplicate collapsed
    assert f"Said I hate all soup forever [m:{w.ids['a1']}]" in lines  # unverified quote lost its quotation marks
    assert len(lines) == 3
    rev = q("SELECT m.fields->>'reason' AS reason FROM tl.messages m JOIN tl.pages p ON p.message_id=m.id WHERE p.slug='people/alice'")
    assert rev[0]["reason"] == "timeline"


def test_merge_redirects_and_moves_links_and_timeline(world):
    w = world
    with connect() as c:
        pages.write(c, slug="places/tsuta", kind="places", title="Tsuta", aliases=["Tsuta Ramen"], by="owner",
                    body=f"Ramen shop [m:{w.ids['a1']}]\n\n## Timeline\n- **2026-03-15** — Dinner [m:{w.ids['o1']}]")
        pages.write(c, slug="places/tsuta-ramen", kind="places", title="Tsuta (Saturday spot)", by="owner",
                    body=f"Where we ate [m:{w.ids['o1']}]\n\n## Timeline\n- **2026-03-14** — Picked [m:{w.ids['a1']}]")
        pages.write(c, slug="days/2026-03-15", kind="days", title="2026-03-15", by="owner",
                    body=f"Dinner at [[places/tsuta-ramen]] [m:{w.ids['o1']}]")
        pages.link(c, src="places/tsuta-ramen", dst=f"person:{w.alice}", type="related_to", evidence=[w.ids["a1"]], by="owner")
        res = pages.merge(c, src="places/tsuta-ramen", into="places/tsuta", by="owner", reason="same shop")
        assert res["timeline_moved"] == 1
        kept = pages.get(c, "places/tsuta-ramen")  # redirect
        assert kept["slug"] == "places/tsuta" and "Tsuta (Saturday spot)" in kept["aliases"]
        assert [r["date"].isoformat() for r in pages.chronicle(c, slug="places/tsuta")] == ["2026-03-15", "2026-03-14"]
        nb = pages.neighbors(c, "places/tsuta")
        assert any(n["to_id"] == w.alice for n in nb["out"]) and any(n["slug"] == "days/2026-03-15" for n in nb["in"])
        assert c.execute("SELECT status FROM tl.pages WHERE slug='places/tsuta-ramen'").fetchone()["status"] == "archived"
        # A new body link to the old slug resolves through the redirect.
        r = pages.write(c, slug="days/2026-03-16", kind="days", title="x", body=f"Back at [[places/tsuta-ramen]] [m:{w.ids['o1']}]", by="owner")
        assert r["links"]["unresolved"] == []


def test_kinds_registry_and_privacy_wall(world):
    w = world
    with connect() as c:
        pages.write(c, slug="dynamics/ramen-crew", kind="dynamics", title="The ramen crew", body=f"x [m:{w.ids['a1']}]", by="lens:t")
        assert c.execute("SELECT status FROM tl.page_kinds WHERE kind='dynamics'").fetchone()["status"] == "candidate"
        c.execute("UPDATE tl.page_kinds SET status='retired' WHERE kind='trips'")
        with pytest.raises(pages.PageError, match="retired"):
            pages.write(c, slug="trips/x", kind="trips", title="x", body="y", by="owner")
    seen = []

    def librarian(tdef, payload, turn):
        seen.extend(ids_in(payload))
        return {"actions": []}

    lenses.define_lens(lens_spec(worker_config={"exclude_patterns": ["(?i)tonkotsu"], "exclude_people": [w.bob]}))
    with pages.override_model(librarian):
        lenses.run_lens("daily_pages")
    assert seen == [w.ids["o1"]]  # a1 matched a pattern, b1 and x1 are Bob's
    with pytest.raises(lenses.LensSpecError, match="exclude_patterns"):
        lenses.validate_lens_spec(lens_spec(worker_config={"exclude_patterns": ["("]}))


def test_upkeep_lens_reads_flagged_pages_once(world):
    w = world
    with connect() as c:
        pages.write(c, slug="people/alice", kind="people", title="Alice", body=f"Alice [m:{w.ids['a1']}]", by="owner")
        c.execute("UPDATE tl.pages SET truth_updated_at = now() - interval '1 day'")
        pages.write(c, slug="people/alice", kind="people", title="Alice", by="owner",
                    body=f"Alice [m:{w.ids['a1']}]\n\n## Timeline\n- **2026-03-15** — Dinner [m:{w.ids['o1']}]")
        flags = pages.page_flags(c)
    assert flags["people/alice"]["stale_truth"] == 1 and flags["people/alice"]["thin"]
    got = []

    def upkeep(tdef, payload, turn):
        got.append(payload["page_flags"])
        return {"actions": []}

    lenses.define_lens(lens_spec(name="page_upkeep", grain="message", channels=["pages"],
                                 worker_config={"input": "flagged", "allow": ["edit", "link", "archive"]}))
    with pages.override_model(upkeep):
        lenses.run_lens("page_upkeep")
        lenses.run_lens("page_upkeep")
    assert len(got) == 1 and "stale_truth" in got[0]["people/alice"]


def test_find_page_map_shows_recent_timeline(world, client):
    w = world
    with connect() as c:
        pages.write(c, slug="people/alice-moreau", kind="people", title="Alice Moreau", by="owner", person_id=w.alice,
                    body=f"Ramen lover [m:{w.ids['a1']}]\n\n## Timeline\n- **2026-03-15** — Dinner at Tsuta [m:{w.ids['o1']}]")
    r = ok(client.post("/v1/find", json={"query": "Alice Moreau", "k": 5}))
    assert r["pages"][0]["recent"] == ["2026-03-15 — Dinner at Tsuta [m:%d]" % w.ids["o1"]]
    t = ok(client.get("/v1/pages/timeline", params={"on": "2026-03-15"}))
    assert t[0]["slug"] == "people/alice-moreau"
    assert any(k["kind"] == "people" for k in ok(client.get("/v1/pages/kinds")))
    m = client.post("/v1/pages/merge", json={"from": "people/alice-moreau", "into": "people/alice-moreau"},
                    headers={"Idempotency-Key": "pages-merge-self"})
    assert m.status_code == 400


def test_reply_shapes_and_repeat_calls(world):
    from throughline.pages.librarian import coerce_reply

    assert coerce_reply('Sure! {"actions": []} trailing')[0] == {"actions": []}
    assert coerce_reply({"op": "archive", "slug": "x/y"})[0] == {"actions": [{"op": "archive", "slug": "x/y"}]}
    assert coerce_reply([{"tool": "read_page", "args": {}}])[0] == {"calls": [{"tool": "read_page", "args": {}}]}
    bad, problem = coerce_reply('{"actions": [{"op": "write", "body": "aaaa')
    assert bad is None and "cut off" in problem and "aaaa" not in problem
    turns = []

    def lib(tdef, payload, turn):
        turns.append(payload["tool_results"])
        if payload["window"].get("day") != "2026-03-14":
            return {"actions": []}
        if turn < 2:
            return {"calls": [{"tool": "search_pages", "args": {"query": "nobody"}}]}
        return "not json at all"

    lenses.define_lens(lens_spec(worker_config={"max_turns": 3}))
    with pages.override_model(lib):
        res = lenses.run_lens("daily_pages")
    assert "no page matches" in turns[1][0]["note"] and "already made this call" in turns[2][1]["note"]
    assert any("not JSON" in e for e in res["errors"])


# --- moving GBrain in -----------------------------------------------------------


def _gbrain_world(client, w):
    from throughline.pages import gbrain_import  # noqa: F401

    for name, kind in (("gbrain.pages", "page_revision"), ("gbrain.legacy", "reference"), ("gbrain.timeline", "derived")):
        ok(client.post("/v1/channels", json=dict(
            name=name, plugin="synthetic", record_kind=kind, is_reference=kind == "reference",
            fields=[dict(name=n, type="json" if n in ("aliases", "tags") else "text") for n in
                    (["slug", "page_type", "title", "source_hash", "aliases", "tags"] if name != "gbrain.timeline" else ["page_external_id"])])))

    def page(ext, slug, gtype, title, truth, h="h1", channel="gbrain.pages", timeline="", aliases=()):
        return dict(channel=channel, external_id=ext, thread={"external_id": "gb"}, happened_at=T0.isoformat(),
                    content=f"{title}\n\n{truth}", fields={"slug": slug, "page_type": gtype, "title": title, "source_hash": h,
                                                           "aliases": list(aliases), "tags": []},
                    payload={"page": {"slug": slug, "type": gtype, "title": title, "compiled_truth": truth, "timeline": timeline,
                                      "created_at": "2026-01-02T00:00:00Z", "updated_at": "2026-02-03T00:00:00Z", "id": ext},
                             "page_aliases": [{"alias_norm": "ali m"}], "slug_aliases": [], "tags": [{"tag": "friend"}]})

    items = [
        page("g1", "People/Alice_Moreau", "person", "Alice Moreau", "Alice loves ramen.",
             timeline="- **2026-01-05** | Chat — Met at a party", aliases=["Ali"]),
        page("g2", "events/2026-03-15/tsuta dinner/a/b/c/d/e/f", "event", "Tsuta dinner", "Ramen night."),
        page("g3", "documents/old note", "document", "Old note", "Legacy text.", channel="gbrain.legacy"),
    ]
    assert ingest(client, items).status_code == 200
    tl_item = dict(channel="gbrain.timeline", external_id="t1", thread={"external_id": "gb"}, happened_at=T0.isoformat(),
                   content="Booked dinner", fields={"page_external_id": "g1"},
                   payload={"date": "2026-03-14", "summary": "Booked dinner", "source": "sms", "detail": ""})
    r_tl = ingest(client, [tl_item])
    assert r_tl.status_code == 200 and not r_tl.json()["rejected"], r_tl.json()
    ids = {r["external_id"]: r["id"] for r in q("SELECT m.external_id, m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name LIKE 'gbrain.%%' AND m.superseded_by IS NULL")}
    with connect() as c:
        c.execute("INSERT INTO tl.identifiers(person_id, system, kind, value, status) VALUES (%s,'gbrain','page','People/Alice_Moreau','confirmed')", (w.alice,))
        c.execute("INSERT INTO tl.link_types(type, status, origin) VALUES ('knows','active','legacy') ON CONFLICT (type) DO UPDATE SET origin='legacy'")
        c.execute("INSERT INTO tl.links(type, from_kind, from_id, to_kind, to_id, method) VALUES ('knows','message',%s,'message',%s,'source')", (ids["g2"], ids["g1"]))
        c.execute("INSERT INTO tl.links(type, from_kind, from_id, to_kind, to_id, method) VALUES ('part_of','message',%s,'message',%s,'source')", (ids["g1"], ids["g2"]))
    return ids


def test_gbrain_moves_into_native_pages(world, client):
    from throughline.pages import gbrain_import

    w = world
    with connect() as c:  # a profile a lens wrote earlier for the same person
        pages.write(c, slug="people/alice", kind="people", title="Alice", body=f"Alice [m:{w.ids['a1']}]\n\n## Timeline\n- **2026-03-14** — Ramen plan [m:{w.ids['a1']}]",
                    by="lens:people_pages", person_id=w.alice)
    _gbrain_world(client, w)
    dry = gbrain_import.run(dry_run=True)
    assert dry["planned"] == 3 and dry["archived"] == 1 and dry["people_tied"] == 1 and dry["by_kind"]["people"] == 1
    res = gbrain_import.run()
    assert res["created"] == 3 and res["merged_people"] == 1 and not res["errors"], res
    with connect() as c:
        alice = pages.get(c, "people/alice_moreau")
        assert alice["kind"] == "people" and alice["person_id"] == w.alice and alice["tags"] == ["friend"]
        assert "ali m" in alice["aliases"] and "Ali" in alice["aliases"] and "Alice" in alice["aliases"]
        assert alice["truth"] == "Alice loves ramen."
        dates = [(r["date"].isoformat(), r["summary"]) for r in pages.chronicle(c, slug="people/alice_moreau")]
        assert ("2026-01-05", "Met at a party") in dates and ("2026-03-14", "Ramen plan [m:%d]" % w.ids["a1"]) in dates, dates
        assert any(s.startswith("Booked dinner (sms)") for _, s in dates), dates
        assert any(s.startswith("Page created in GBrain") for _, s in dates) and alice["stats"]["citations"] >= 2
        assert pages.get(c, "people/alice")["slug"] == "people/alice_moreau"  # the lens page merged in, redirected
        assert pages.get(c, "People/Alice_Moreau".lower())["slug"] == "people/alice_moreau"  # the GBrain slug redirects
        ev = c.execute("SELECT slug, kind FROM tl.pages WHERE source_ref=%s", ("gbrain.pages:g2",)).fetchone()
        assert ev["kind"] == "events" and ev["slug"].count("/") == 7
        legacy = c.execute("SELECT status, kind FROM tl.pages WHERE source_ref='gbrain.legacy:g3'").fetchone()
        assert legacy["status"] == "archived" and legacy["kind"] == "documents"
        out = {(n["type"], n["slug"]) for n in pages.neighbors(c, ev["slug"])["out"]}
        assert ("related_to", "people/alice_moreau") in out  # 'knows' is unregistered: kept as related_to
        kept = c.execute("SELECT evidence FROM tl.links WHERE job='gbrain-import' AND type='related_to'").fetchone()
        assert kept["evidence"]["gbrain_type"] == "knows"
        assert ("part_of", ev["slug"]) in {(n["type"], n["slug"]) for n in pages.neighbors(c, "people/alice_moreau")["out"]}
    # Re-run: nothing changed.
    again = gbrain_import.run()
    assert again["unchanged"] == 3 and again["created"] == again["updated"] == 0
    # The Librarian edits the page; GBrain changes it too: the native text stays, new lines arrive.
    with connect() as c:
        pages.write(c, slug="people/alice_moreau", kind="people", title="Alice Moreau", body="Alice (edited natively).", by="lens:people_pages")
    upd = [dict(channel="gbrain.pages", external_id="g1", thread={"external_id": "gb"}, happened_at=T0.isoformat(),
                content="Alice Moreau\n\nnew", fields={"slug": "People/Alice_Moreau", "page_type": "person", "title": "Alice Moreau",
                                                      "source_hash": "h2", "aliases": [], "tags": []},
                payload={"page": {"slug": "People/Alice_Moreau", "type": "person", "title": "Alice Moreau", "compiled_truth": "GBrain rewrite",
                                  "timeline": "- **2026-04-01** | x — Moved", "created_at": "2026-01-02T00:00:00Z", "updated_at": "2026-04-02T00:00:00Z"},
                         "page_aliases": [], "slug_aliases": [], "tags": []})]
    assert ingest(client, upd).status_code == 200
    third = gbrain_import.run()
    assert third["kept_native"] == 1 and third["timeline_added"] >= 1
    with connect() as c:
        alice = pages.get(c, "people/alice_moreau")
        assert alice["truth"] == "Alice (edited natively)." and "Moved" in alice["body"]


def test_recall_scoped_to_pages_skips_message_legs_and_finds_passages(world, client):
    from throughline import passages

    with connect() as c:
        pages.write(
            c,
            slug="people/alice",
            kind="people",
            title="Alice Moreau",
            body="Alice loves handmade sourdough ramen in Seattle.",
            by="owner",
        )
        assert passages.build(c, 50) >= 1
    r = client.post(
        "/v1/recall",
        json={"query": "sourdough ramen", "k": 5, "scope": {"channels": ["pages"]}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(rec["kind"] == "passage" and rec["channel"] == "pages" for rec in body["records"])
    skipped = {s["stage"]: s["reason"] for s in body["execution"]["incomplete"]}
    assert skipped.get("lexical") == "kinds_excluded"


def test_passages_build_still_runs_when_embedding_cap_reached(world, client, monkeypatch):
    from throughline import embeddings, passages

    with connect() as c:
        pages.write(
            c,
            slug="topics/ramen",
            kind="topics",
            title="Ramen",
            body="Handmade noodles and rich tonkotsu broth.",
            by="owner",
        )
        c.execute("INSERT INTO tl.embedding_ledger(model,tokens,cost) VALUES('local-hash-v1',1,10.0)")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "16")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MONTHLY_USD", "5")
    assert embeddings.run_once() == 1
    with connect() as c:
        assert passages.pending(c)["revisions_without_passages"] == 0
        assert c.execute("SELECT count(*) n FROM tl.passage_embeddings").fetchone()["n"] == 0
