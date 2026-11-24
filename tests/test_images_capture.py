"""Desktop screenshots (capture.screens) adopted from Immich."""

from datetime import UTC, datetime

from test_images_pipeline import FakeImmich, pipeline

from throughline.images.sources import SOURCES, capture


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        return Rows(self.rows)


def test_capture_is_a_source_and_discovers_uploaded_screens():
    assert "capture" in SOURCES
    happened = datetime(2020, 1, 1, 10, 0, tzinfo=UTC)
    conn = Conn([{
        "external_id": "synthbox:2020-01-01T10:00:00+00:00",
        "happened_at": happened,
        "fields": {"device": "synthbox", "immich_asset_id": "11111111-2222-3333-4444-555555555555",
                   "bytes": 1234, "width": 1600, "height": 900, "app": "code"},
        "thread": "Screen · synthbox · 2020-01-01",
    }])
    [c] = list(capture.discover(conn, None))
    assert c.origin == "capture" and c.channel == "capture.screens" and c.role == "self"
    assert c.route == "immich" and c.source_ref["immich_id"].startswith("1111")
    assert c.name == "tl-capture-synthbox-20200101T100000.jpg"
    assert c.context["label"] == "Screen capture"
    assert "capture.screens" in conn.sql[0][1]


def test_resolver_reads_the_original_back(monkeypatch):
    import throughline.images.immich as immich

    class Fake:
        def __init__(self, url, key):
            self.http = type("H", (), {"close": lambda self: None})()

        def original(self, iid):
            return b"bytes-of-" + iid.encode()

    monkeypatch.setattr(immich, "Immich", Fake)
    settings = type("S", (), {"immich_url": "http://immich", "immich_api_key": "k"})()
    resolver = capture.Resolver(settings)
    fetched = resolver.fetch(type("C", (), {"source_ref": {"immich_id": "abc"}})())
    assert fetched.status == "ok" and fetched.data == b"bytes-of-abc"


def test_native_skips_capture_files(tmp_path):
    class Immich(FakeImmich):
        def search(self):
            return [{"id": "00000000-0000-4000-8000-000000000001", "originalFileName": "tl-capture-synthbox-20200101T100000.jpg"}]

        def original(self, iid):
            raise AssertionError("a capture file must not be downloaded as a native photo")

    p = pipeline(tmp_path, immich=Immich())
    counts = p.native(limit=10)
    assert counts["skipped:capture"] == 1 and counts.get("stored", 0) == 0
