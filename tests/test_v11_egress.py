"""Label-aware egress (migration 0055): content under a sensitivity label is
not handed to a model provider off this host unless the owner allowed that
label to leave.

One world: a channel with an open thread, a thread labelled 'intimate', a
thread holding one message whose access_label is 'medical', and a page that
cites the medical message. Each labelled text carries its own canary, and the
fake remote providers record every text they are given. Synthetic data only.
"""

import pytest
from conftest import ingest, message
from typer.testing import CliRunner

from throughline import access, cli, embeddings, providers
from throughline.db import connect
from throughline.derived import materialize
from throughline.providers import egress
from throughline.providers.base import Reranker
from throughline.providers.local import LocalHashEmbedder, LocalLexicalReranker
from throughline.retrieval import RecallRequest, ranking
from throughline.retrieval.executor import RecallContext
from throughline.scope import Scope

CHAT = "synthetic.egress"
PAGE = "synthetic.egress.page"
INTIMATE = "zqintimatecanary"
MEDICAL = "zqmedicalcanary"
PAGE_CANARY = "zqpagecanary"


class RemoteEmbedder(LocalHashEmbedder):
    """A fake embedder off this host: records every text it is given."""

    name = "fake-remote"
    local = False

    def __init__(self):
        super().__init__(64)
        self.sent = []

    def embed(self, texts, model=None, dimensions=None, input_type=None):
        self.sent.extend(texts)
        return super().embed(texts, model, dimensions, input_type)


class RemoteReranker(Reranker):
    name = "fake-remote"
    version = "fake-remote-v1"
    model = "fake-remote-model"
    local = False
    price_per_million = 0.0

    def __init__(self):
        self.sent = []

    def score(self, query, passages_):
        self.sent.extend(passages_)
        scores, _ = LocalLexicalReranker().score(query, passages_)
        return scores, self._usage(sum(len(p) for p in passages_) // 4)


def rows(statement, params=None):
    with connect() as c:
        return c.execute(statement, params).fetchall()


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def drain():
    materialize(wait=True)
    for _ in range(200):
        if not embeddings.run_once():
            return


def sent_text(provider):
    return "\n".join(provider.sent)


@pytest.fixture()
def world(client, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "64")
    ok(client.post("/v1/channels", json=dict(name=CHAT, plugin="synthetic")))
    ok(
        client.post(
            "/v1/channels",
            json=dict(
                name=PAGE,
                plugin="synthetic",
                is_reference=True,
                record_kind="page_revision",
            ),
        )
    )
    items = (
        [
            message(
                i * 60,
                channel=CHAT,
                external_id=f"open-{i}",
                thread={"external_id": "open"},
                content=f"Synthetic orchard visit {i}",
                fields={},
            )
            for i in range(3)
        ]
        + [
            message(
                i * 60 + 10,
                channel=CHAT,
                external_id=f"prv-{i}",
                thread={"external_id": "private"},
                content=f"Synthetic orchard {INTIMATE} private {i}",
                fields={},
            )
            for i in range(2)
        ]
        + [
            message(
                20,
                channel=CHAT,
                external_id="mix-0",
                thread={"external_id": "mixed"},
                content="Synthetic orchard pruning plan",
                fields={},
            ),
            message(
                80,
                channel=CHAT,
                external_id="med-0",
                thread={"external_id": "mixed"},
                content=f"Synthetic orchard {MEDICAL} note",
                fields={},
                access_label="medical",
            ),
            message(
                900,
                channel=PAGE,
                external_id="wiki",
                thread={"external_id": "wiki"},
                content=f"# Orchard\n\nPage about the orchard, {PAGE_CANARY} upkeep and pruning.",
                fields={},
                author=None,
            ),
        ]
    )
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    ok(
        client.post(
            "/v1/provenance",
            json={
                "derived": {"channel": PAGE, "external_id": "wiki"},
                "sources": [
                    {"channel": CHAT, "external_id": "open-0"},
                    {"channel": CHAT, "external_id": "med-0"},
                ],
            },
        )
    )
    m = {
        r["external_id"]: r["id"]
        for r in rows(
            "SELECT id,external_id FROM tl.messages WHERE superseded_by IS NULL"
        )
    }
    threads = {
        r["external_id"]: r["id"] for r in rows("SELECT id,external_id FROM tl.threads")
    }
    access.label("intimate", thread_id=threads["private"], purpose="synthetic setup")
    return dict(client=client, m=m, threads=threads)


def egress_ok(message_id):
    return rows("SELECT tl.message_egress_ok(%s) AS ok", (message_id,))[0]["ok"]


def test_rule_holds_labelled_messages_and_what_derives_from_them(world):
    m = world["m"]
    assert all(r["egress"] is False for r in rows("SELECT egress FROM tl.labels"))
    assert egress_ok(m["open-0"]) and egress_ok(m["mix-0"])
    # Thread label, the message's own label, and a page citing a held source.
    assert not egress_ok(m["prv-0"])
    assert not egress_ok(m["med-0"])
    assert not egress_ok(m["wiki"])
    # An id naming no stored message is never vouched for.
    assert not egress_ok(10**12)
    held = egress.held(list(m.values()) + [10**12])
    assert held == {m["prv-0"], m["prv-1"], m["med-0"], m["wiki"], 10**12}
    # A chunk leaves only when every current message of its range may.
    chunks = rows(
        "SELECT t.external_id AS thread,bool_and(tl.conversation_egress_ok(c.id)) AS ok"
        " FROM tl.conversations c JOIN tl.threads t ON t.id=c.thread_id GROUP BY 1"
    )
    by_thread = {r["thread"]: r["ok"] for r in chunks}
    assert by_thread["open"] is True
    assert by_thread["private"] is False and by_thread["mixed"] is False
    # A channel label holds everything in the channel.
    access.label("work", channel=CHAT)
    assert not egress_ok(m["open-0"])
    access.unlabel("work", channel=CHAT)
    assert egress_ok(m["open-0"])
    # The runtime reader cannot call the rule; the app role can.
    with connect("app") as c:
        assert c.execute(
            "SELECT tl.message_egress_ok(%s) AS ok", (m["open-0"],)
        ).fetchone()["ok"]


def test_remote_embedding_never_receives_held_text(world):
    remote = RemoteEmbedder()
    with providers.override("embedding", remote):
        drain()
        text = sent_text(remote)
        assert "Synthetic orchard visit" in text
        for canary in (INTIMATE, MEDICAL, PAGE_CANARY, "pruning plan"):
            assert canary not in text
        embedded = rows(
            "SELECT DISTINCT t.external_id AS thread FROM tl.embeddings e"
            " JOIN tl.conversations c ON c.id=e.conversation_id JOIN tl.threads t ON t.id=c.thread_id"
        )
        assert {r["thread"] for r in embedded} == {"open"}
        assert rows("SELECT count(*) AS n FROM tl.passage_embeddings")[0]["n"] == 0
        # Held content does not keep the model from serving.
        assert rows("SELECT active FROM tl.embedding_models")[0]["active"] is True
        assert embeddings.run_once() == 0

        # The owner lets 'medical' leave: the mixed thread and the page follow;
        # the intimate thread still stays.
        changed = access.set_label_egress("medical", True, purpose="synthetic")
        assert changed == dict(label="medical", egress=True, changed=True)
        drain()
        text = sent_text(remote)
        assert MEDICAL in text and PAGE_CANARY in text
        assert INTIMATE not in text

        # Forbidding again stops further sending; nothing else is embedded.
        access.set_label_egress("medical", False)
        before = len(remote.sent)
        drain()
        assert len(remote.sent) == before


def test_remote_context_embedding_leaves_held_chunks_out(world):
    remote = RemoteEmbedder()
    with providers.override("embedding", remote), connect("app") as conn:
        context = embeddings.conversation_context(conn)
        chunks = rows(
            "SELECT c.* FROM tl.conversations c JOIN tl.threads t ON t.id=c.thread_id WHERE t.external_id='mixed' ORDER BY c.chunk_no"
        )
        for row in chunks:
            row = dict(row, chunk_no=row["chunk_no"] + 1, closed=True)
            assert all(MEDICAL not in c["content"] for c in context(row, 10**6))


def test_local_embedding_embeds_everything(world, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    drain()
    embedded = rows(
        "SELECT DISTINCT t.external_id AS thread FROM tl.embeddings e"
        " JOIN tl.conversations c ON c.id=e.conversation_id JOIN tl.threads t ON t.id=c.thread_id"
    )
    assert {r["thread"] for r in embedded} == {"open", "private", "mixed"}
    assert rows("SELECT count(*) AS n FROM tl.passage_embeddings")[0]["n"] >= 1


def test_loopback_endpoint_is_not_egress():
    class Endpoint:
        local = False

        def __init__(self, base):
            self.base = base

    assert not egress.leaves(LocalHashEmbedder(8))
    assert not egress.leaves(Endpoint("http://127.0.0.1:8080/v1"))
    assert egress.leaves(Endpoint("https://api.example.com/v1"))
    assert egress.leaves(RemoteReranker())


def test_remote_reranker_never_receives_held_text(world):
    m = world["m"]
    items = [
        dict(text="Synthetic orchard visit 0", message_ids=[m["open-0"]]),
        dict(text=f"Synthetic orchard {INTIMATE} private 0", message_ids=[m["prv-0"]]),
        dict(
            text=f"Synthetic orchard pruning plan\nSynthetic orchard {MEDICAL} note",
            message_ids=[m["mix-0"], m["med-0"]],
        ),
    ]
    ctx = RecallContext(Scope.local(), RecallRequest(query="orchard"))
    remote = RemoteReranker()
    with providers.override("rerank", remote):
        scores, stats = ranking.score_cached(ctx, "orchard", items)
    assert scores[0] is not None and scores[1] is None and scores[2] is None
    assert remote.sent == ["Synthetic orchard visit 0"]
    assert stats["egress_held"] == 2 and stats["misses"] == 1
    # A local reranker scores everything.
    with providers.override("rerank", LocalLexicalReranker()):
        scores, stats = ranking.score_cached(ctx, "orchard", items)
    assert all(score is not None for score in scores) and stats["egress_held"] == 0


def test_owner_command_and_audit(world):
    runner = CliRunner()
    r = runner.invoke(cli.app, ["lib", "access", "egress", "medical"])
    assert r.exit_code != 0
    r = runner.invoke(
        cli.app,
        ["lib", "access", "egress", "medical", "--allow", "--purpose", "synthetic"],
    )
    assert r.exit_code == 0, r.output
    assert rows("SELECT egress FROM tl.labels WHERE label='medical'")[0]["egress"]
    # Repeating an unchanged setting writes nothing.
    assert access.set_label_egress("medical", True)["changed"] is False
    events = rows("SELECT detail FROM tl.access_events WHERE op='label_egress'")
    assert [e["detail"] for e in events] == [
        dict(label="medical", egress=True, purpose="synthetic")
    ]
    assert {r["label"]: r["egress"] for r in access.list_labels()}["medical"] is True
    r = runner.invoke(cli.app, ["lib", "access", "egress", "medical", "--deny"])
    assert r.exit_code == 0, r.output
    with pytest.raises(ValueError):
        access.set_label_egress("synthetic_unknown", True)
    # The setting never changes what an asker reads.
    version = rows("SELECT api.policy_version() AS v")[0]["v"]
    access.set_label_egress("financial", True)
    assert rows("SELECT api.policy_version() AS v")[0]["v"] == version
