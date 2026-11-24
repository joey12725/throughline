"""Image pipeline (docs/images.md): sources, sniffing, classification, stages.

Everything here runs without a network or a database: Immich, Immich's
database, NudeNet, Gemini, the resolvers and the store are fakes.
"""

import base64
import gzip
import hashlib
import json
import struct
import zlib
from datetime import UTC, datetime, timedelta

import pytest

from throughline.images import email_fetch, gemini, nsfw, sniff, tagging
from throughline.images.config import Settings, load_env_files
from throughline.images.pipeline import (
    Pipeline,
    describe,
    face_rows,
    ocr_text_of,
    photo_sessions,
)
from throughline.images.sources import Candidate, Fetched
from throughline.images.sources import beeper, email, hermes, t3code


# --- helpers -------------------------------------------------------------------------


def png(width=120, height=80, color=(200, 10, 10)):
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_with_exif(taken="2024:05:06 07:08:09", make="Google", model="Pixel 8"):
    def entry(tag, typ, count, value):
        return struct.pack("<HHI", tag, typ, count) + value

    # TIFF header + IFD0 (make, model, exif pointer) + Exif IFD (DateTimeOriginal).
    make_b, model_b, taken_b = make.encode() + b"\0", model.encode() + b"\0", taken.encode() + b"\0"
    ifd0_at = 8
    ifd0_size = 2 + 3 * 12 + 4
    exif_at = ifd0_at + ifd0_size
    exif_size = 2 + 12 + 4
    data_at = exif_at + exif_size
    make_at, model_at = data_at, data_at + len(make_b)
    taken_at = model_at + len(model_b)
    tiff = b"II*\x00" + struct.pack("<I", ifd0_at)
    tiff += struct.pack("<H", 3)
    tiff += entry(0x010F, 2, len(make_b), struct.pack("<I", make_at))
    tiff += entry(0x0110, 2, len(model_b), struct.pack("<I", model_at))
    tiff += entry(0x8769, 4, 1, struct.pack("<I", exif_at))
    tiff += struct.pack("<I", 0)
    tiff += struct.pack("<H", 1) + entry(0x9003, 2, len(taken_b), struct.pack("<I", taken_at)) + struct.pack("<I", 0)
    tiff += make_b + model_b + taken_b
    app1 = b"Exif\x00\x00" + tiff
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, 300, 400, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + sof + b"\xff\xd9"


def settings(tmp_path, **overrides):
    s = Settings.from_env({"IMAGES_STATE_DIR": str(tmp_path / "state"), "IMAGES_NUDENET_PYTHON": "/nonexistent"})
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class FakeStore:
    """In-memory stand-in for throughline.images.store."""

    def __init__(self):
        self.assets = {}
        self.links = {}
        self.faces = {}
        self.vectors = {}

    def upsert_asset(self, conn, *, sha256, origin, mime=None, bytes=None, width=None, height=None, original_name=None):
        for row in self.assets.values():
            if row["sha256"] == sha256:
                for k, v in dict(mime=mime, bytes=bytes, width=width, height=height, original_name=original_name).items():
                    if row.get(k) is None:
                        row[k] = v
                return row["id"]
        aid = len(self.assets) + 1
        self.assets[aid] = dict(
            id=aid, sha256=sha256, origin=origin, mime=mime, bytes=bytes, width=width, height=height,
            original_name=original_name, immich_id=None, status="pending", enrich={}, tags=[], kind=None,
            ocr_text=None, nsfw=None, taken_at=None, lat=None, lon=None, place=None,
        )
        return aid

    def get_asset(self, conn, aid):
        return dict(self.assets[aid])

    def set_asset(self, conn, aid, merge_enrich=None, drop_enrich=None, **cols):
        row = self.assets[aid]
        row.update(cols)
        if cols.get("nsfw") is not None and cols["nsfw"] >= 0.6 and "nsfw" not in row["tags"]:
            row["tags"] = list(row["tags"]) + ["nsfw"]
        enrich = {k: v for k, v in row["enrich"].items() if k not in (drop_enrich or ())}
        row["enrich"] = dict(enrich, **(merge_enrich or {}))
        return dict(row)

    def assets_needing(self, conn, stage, limit=100):
        rows = []
        for row in self.assets.values():
            e = row["enrich"]
            if stage == "upload":
                ok = row["status"] == "pending" and not row["immich_id"]
            elif stage == "pull":
                ok = row["immich_id"] and row["status"] != "missing" and "pull" not in e
            elif stage == "nsfw":
                retry = isinstance(e.get("nsfw"), dict) and "error" in e["nsfw"] and "gave_up" not in e["nsfw"]
                ok = row["immich_id"] and row["status"] != "missing" and "pull" in e and ("nsfw" not in e or retry)
            else:
                ok = row["immich_id"] and row["status"] != "missing" and "pull" in e and stage not in e
            if ok:
                rows.append(dict(row))
        return rows[:limit]

    def link_message(self, conn, *, channel, external_id, asset_id, ord=0, role="attachment", source_ref=None):
        self.links[(channel, external_id, asset_id)] = dict(ord=ord, role=role, source_ref=source_ref)

    def replace_faces(self, conn, asset_id, faces):
        self.faces[asset_id] = list(faces)
        return len(faces)

    def put_vector(self, conn, asset_id, model, vector):
        self.vectors[(asset_id, model)] = list(vector)

    def render_text(self, row, names=()):
        return f"[{row.get('kind') or 'image'}]"


class FakeQueries:
    def __init__(self, store):
        self.store = store

    def tables_ready(self):
        return True

    def linked_keys(self, channel):
        out = {}
        for (ch, ext, aid), link in self.store.links.items():
            if ch == channel:
                out[(ext, link["ord"])] = (aid, self.store.assets[aid]["status"])
        return out

    def linked(self, asset_id):
        return [
            {"channel": ch, "external_id": ext, "happened_at": datetime(2025, 1, 2, tzinfo=UTC), "thread": "general",
             "author": "Alex Kim", "body": "look at this"}
            for (ch, ext, aid) in self.store.links if aid == asset_id
        ]

    def known_immich_ids(self):
        return {r["immich_id"] for r in self.store.assets.values() if r["immich_id"]}

    def needs_describe(self, limit):
        return [
            dict(r) for r in self.store.assets.values()
            if r["immich_id"] and r["origin"] != "immich" and int(r["enrich"].get("describe_v") or 0) < 2
        ][:limit]

    def faces(self, asset_id):
        return self.store.faces.get(asset_id, [])


class FakeImmich:
    def __init__(self):
        self.uploaded = {}
        self.descriptions = {}
        self.albums = {}
        self.tags = {}
        self.renamed = []

    def upload(self, data, filename, *, created_at=None, modified_at=None, metadata=None):
        sha1 = hashlib.sha1(data).hexdigest()
        for iid, item in self.uploaded.items():
            if item["sha1"] == sha1:
                return iid, "duplicate"
        iid = f"00000000-0000-4000-8000-{len(self.uploaded) + 1:012d}"
        self.uploaded[iid] = dict(sha1=sha1, filename=filename, created_at=created_at, metadata=metadata)
        return iid, "created"

    def update(self, iid, **fields):
        self.descriptions[iid] = fields.get("description")

    def add_to_album(self, name, ids):
        self.albums.setdefault(name, set()).update(ids)

    def tag(self, ids, values):
        for i in ids:
            self.tags.setdefault(i, set()).update(values)

    def asset(self, iid):
        return {
            "type": "IMAGE", "width": 400, "height": 300,
            "exifInfo": {"make": "Google", "model": "Pixel", "dateTimeOriginal": "2024-05-06T07:08:09+00:00",
                         "latitude": 29.3, "longitude": -94.8, "city": "Galveston", "state": "Texas", "country": "United States",
                         "exifImageWidth": 400, "exifImageHeight": 300},
        }

    def ocr(self, iid):
        return [{"text": "WELCOME", "x1": 0.1, "x2": 0.9, "x3": 0.9, "x4": 0.1, "y1": 0.02, "y2": 0.02, "y3": 0.1, "y4": 0.1}]

    def faces(self, iid):
        return [{"id": "f1", "imageWidth": 200, "imageHeight": 150, "boundingBoxX1": 10, "boundingBoxY1": 10,
                 "boundingBoxX2": 60, "boundingBoxY2": 70, "person": {"id": "p1", "name": "Alex Kim"}}]

    def thumbnail(self, iid, size="preview"):
        return b"jpegbytes-" + iid.encode()

    def people(self):
        return [{"id": "p1", "name": "Alex"}, {"id": "p2", "name": "Sam Lee"}]

    def rename_person(self, pid, name):
        self.renamed.append((pid, name))


class FakeImmichDB:
    def __init__(self, ready=True):
        self.ready = ready

    def job_status(self, ids):
        return {i: dict(metadata=self.ready, faces=self.ready, ocr=self.ready, clip=self.ready) for i in ids}

    def clip_vectors(self, ids):
        return {i: [1.0, 0.0, 0.0] for i in ids}


class FakeBridge:
    def __init__(self, score=0.0, error=None):
        self.score = score
        self.error = error

    def classify(self, paths):
        if self.error:
            return {p: {"error": self.error} for p in paths}
        return {p: {"nsfw": self.score, "labels": {"FACE_FEMALE": 0.9}} for p in paths}

    def close(self):
        pass


class FakeBank:
    def similarities(self, vector):
        return {"kinds": {"photo": 0.2, "screenshot": 0.05, "document": 0.0, "artwork": 0.0, "other": 0.0},
                "tags": {"beach": 0.05, "meme": -0.05}}


VOCAB = {
    "logit_scale": 100.0,
    "logit_bias": -2.0,
    "kind_temperature": 100.0,
    "default_threshold": 0.5,
    "max_tags": 8,
    "meme": {"threshold": 0.5, "caption_bonus": 0.3, "hint_bonus": 0.15, "hints": ["meme", "imgflip"]},
    "kinds": {k: [k] for k in ("photo", "screenshot", "document", "artwork", "other")},
    "tags": {
        "beach": {"prompts": ["a photo of a beach"], "kinds": ["photo"]},
        "code": {"prompts": ["code"], "kinds": ["screenshot"]},
        "receipt": {"prompts": ["receipt"], "kinds": ["document", "photo"]},
        "meme": {"prompts": ["a meme"]},
    },
}


class Res:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def fetch(self, c):
        self.calls += 1
        return Fetched("ok", self.data)

    def close(self):
        pass


def pipeline(tmp_path, **kw):
    store = kw.pop("store", None) or FakeStore()
    return Pipeline(
        None,
        settings(tmp_path),
        store=store,
        queries=FakeQueries(store),
        immich=kw.pop("immich", None) or FakeImmich(),
        immich_db=kw.pop("immich_db", None) or FakeImmichDB(),
        nudenet=kw.pop("nudenet", None) or FakeBridge(),
        vocab=VOCAB,
        text_bank=FakeBank(),
        commit=False,
        **kw,
    )


# --- sniffing and data URIs ---------------------------------------------------------------


def test_sniff_types_and_sizes():
    assert sniff.sniff(png(120, 80)) == ("image/png", 120, 80)
    assert sniff.sniff(jpeg_with_exif()) == ("image/jpeg", 400, 300)
    assert sniff.sniff(b"GIF89a" + struct.pack("<HH", 7, 9) + b"\0" * 10) == ("image/gif", 7, 9)
    assert sniff.sniff(b"\x00\x00\x00\x18ftypheic" + b"\0" * 20)[0] == "image/heic"
    assert sniff.sniff(b"%PDF-1.7")[0] is None


def test_image_types():
    for name in ("a.jpg", "a.JPEG", "a.png", "a.gif", "a.webp", "a.heic", "a.heif", "a.avif", "a.bmp", "a.tiff"):
        assert sniff.is_image(name), name
    assert not sniff.is_image("a.pdf")
    assert not sniff.is_image("a.mp4")
    assert not sniff.is_image("photo.jpg", "application/pdf")  # declared mime wins
    assert sniff.is_image(None, "image/jpeg")


def test_exif_facts():
    facts = sniff.exif_facts(jpeg_with_exif())
    assert facts["taken"] == datetime(2024, 5, 6, 7, 8, 9)
    assert facts["make"] == "Google" and facts["model"] == "Pixel 8"
    assert sniff.exif_facts(png())["taken"] is None


def test_data_uri_decoding_and_truncation():
    data = png()
    uri = "data:image/png;base64," + base64.b64encode(data).decode()
    assert sniff.decode_data_uri(uri) == (data, "image/png")
    with pytest.raises(sniff.DataUriError) as cut:
        sniff.decode_data_uri(uri[: len(uri) // 2 // 4 * 4])  # cut on a base64 boundary: incomplete PNG
    assert cut.value.reason == "truncated"
    with pytest.raises(sniff.DataUriError) as odd:
        sniff.decode_data_uri(uri[:-3])
    assert odd.value.reason == "truncated"
    with pytest.raises(sniff.DataUriError) as flagged:
        sniff.decode_data_uri(uri, cut=True)  # the exporter listed it as cut
    assert flagged.value.reason == "truncated"
    with pytest.raises(sniff.DataUriError) as bad:
        sniff.decode_data_uri("data:image/png;base64,@@@@")
    assert bad.value.reason == "not_base64"
    with pytest.raises(sniff.DataUriError):
        sniff.decode_data_uri("https://example.com/a.png")


# --- sources -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,expected",
    [
        ("alex@gmail.com", True),
        ("Alex.Kim@GoogleMail.com", True),
        ("x@icloud.com", True),
        ("x@pm.me", True),
        ("x@proton.me", True),
        ("x@hotmail.co.uk", True),
        ("x@yahoo.fr", True),
        ("noreply@gmail.com", False),
        ("no-reply@icloud.com", False),
        ("notifications@outlook.com", False),
        ("alex@company.com", False),
        ("alex@gmail.company.com", False),
        ("alex@mail.yahoo.com", False),
        (None, False),
    ],
)
def test_email_personal_domain_rule(address, expected):
    assert email.personal_sender(address) is expected


def email_row(sender, atts):
    return {
        "external_id": "me@gmail.com:abc",
        "happened_at": datetime(2025, 1, 1, tzinfo=UTC),
        "fields": {"from_address": sender, "gmail_message_id": "abc", "account": "me@gmail.com", "attachments": atts},
    }


def test_email_filters_size_type_sender():
    atts = [
        {"filename": "photo.jpg", "mime_type": "image/jpeg", "size": 200_000, "attachment_id": "A1"},
        {"filename": "logo.png", "mime_type": "image/png", "size": 4_000, "attachment_id": "A2"},
        {"filename": "doc.pdf", "mime_type": "application/pdf", "size": 90_000, "attachment_id": "A3"},
        {"filename": "inline.png", "mime_type": "image/png", "size": 40_000, "attachment_id": "A4", "inline": True},
    ]
    found = email.candidates_of(email_row("friend@gmail.com", atts))
    assert [c.skip for c in found] == [None, "under_15kb", "not_image", None]
    assert found[3].role == "inline"
    assert all(c.skip == "not_personal_sender" for c in email.candidates_of(email_row("shop@store.com", atts[:1])))
    pending = email.candidates_of(email_row("friend@gmail.com", atts[:1]), imported={})
    assert pending[0].skip == "needs_email_export"
    reqs = email.requests(pending)
    assert reqs == [{"account": "me@gmail.com", "message_id": "abc", "attachment_id": "A1", "file_name": "photo.jpg", "mime_type": "image/jpeg"}]


def test_email_import_rejects_tiny_images(tmp_path):
    out = tmp_path / "import"
    (out / "files").mkdir(parents=True)
    small = png(50, 50)
    big = png(300, 300) + b"\0" * 20_000
    (out / "files" / "s.png").write_bytes(small)
    (out / "files" / "b.png").write_bytes(big)
    (out / "index.jsonl").write_text(
        json.dumps({"message_id": "abc", "attachment_id": "S", "file": "files/s.png"}) + "\n"
        + json.dumps({"message_id": "abc", "attachment_id": "B", "file": "files/b.png"}) + "\n"
    )
    r = email.Resolver(settings(tmp_path, email_import_dir=out))

    def mk(aid):
        return Candidate("email", "email.message", "x", source_ref={"message_id": "abc", "attachment_id": aid})

    assert r.fetch(mk("S")).status == "filtered"
    assert r.fetch(mk("B")).status == "ok"
    assert r.fetch(mk("Z")).status == "pending_export"


def test_email_fetch_script_decodes_inline_and_errors():
    data = png()
    answer = {"successful": True, "data": {"file": {"content": base64.urlsafe_b64encode(data).decode().rstrip("=")}}}
    assert email_fetch.attachment_bytes(answer) == data
    with pytest.raises(RuntimeError):
        email_fetch.attachment_bytes({"successful": False, "error": "nope"})


def test_beeper_candidates_routes_and_filters():
    now = 1_800_000_000
    live_url = f"https://cdn.discordapp.com/a/1/x.png?ex={now + 3600:x}&is=1&hm=2"
    dead_url = f"https://cdn.discordapp.com/a/1/x.png?ex={now - 3600:x}&is=1&hm=2"
    meta = json.dumps({"beeper": {"chat_id": "!c:beeper.local", "message_id": "42", "account_id": "acct"}})
    row = {
        "channel": "chat.discord", "external_id": "discord:1", "happened_at": None, "meta": None,
        "attachments": [
            {"id": "1", "url": live_url, "fileName": "a.png", "fileSizeBytes": 10},
            {"id": "2", "url": dead_url, "fileName": "b.png"},
            {"id": "3", "url": dead_url, "fileName": "c.pdf"},
            {"type": "video", "fileName": "v.mp4", "mimeType": "video/mp4"},
        ],
    }
    found = beeper.candidates_of(row, now=now)
    assert [(c.route, c.skip) for c in found] == [("url", None), (None, "expired_url"), (None, "not_image"), (None, "video")]
    sms = {"channel": "chat.sms", "external_id": "sms:beeper:h", "meta": meta,
           "attachments": [{"type": "img", "fileName": "1.jpg", "mimeType": "image/jpeg"}]}
    c = beeper.candidates_of(sms, now=now, imported={})[0]
    assert (c.route, c.skip) == ("export", "needs_beeper_export")
    assert c.source_ref["chat_id"] == "!c:beeper.local" and c.source_ref["message_id"] == "42"
    assert beeper.requests([c]) == [{"channel": "chat.sms", "external_id": "sms:beeper:h", "chat_id": "!c:beeper.local",
                                     "message_id": "42", "ord": 0, "file_name": "1.jpg"}]
    snap = dict(sms, channel="chat.snapchat")
    assert beeper.candidates_of(snap, now=now)[0].skip == "ephemeral_excluded"
    expiring = dict(sms, meta=json.dumps({"beeper": {"chat_id": "c", "message_id": "1"},
                                          "collection_policy": {"chat_expiry_seconds": 86400}}))
    assert beeper.candidates_of(expiring, now=now)[0].skip == "ephemeral_excluded"
    live = {"channel": "chat.sms", "external_id": "beeper:acct:%21room%3Ax:77", "meta": None,
            "attachments": [{"type": "img", "fileName": "p.jpg"}]}
    assert beeper.candidates_of(live, now=now)[0].source_ref["chat_id"] == "!room:x"


def test_beeper_manifest_resolver(tmp_path):
    out = tmp_path / "bm"
    out.mkdir()
    data = png()
    sha = hashlib.sha256(data).hexdigest()
    (out / f"{sha}.png").write_bytes(data)
    (out / "manifest.jsonl").write_text(
        json.dumps({"channel": "chat.sms", "external_id": "e1", "ord": 0, "status": "ok", "file": f"{sha}.png", "mime": "image/png"}) + "\n"
        + json.dumps({"channel": "chat.sms", "external_id": "e2", "ord": 0, "status": "message_not_found"}) + "\n"
    )
    r = beeper.Resolver(settings(tmp_path, beeper_import_dir=out))
    c = Candidate("beeper", "chat.sms", "e1", route="export", source_ref={"chat_id": "c", "message_id": "1"})
    assert r.fetch(c).data == data
    assert r.fetch(Candidate("beeper", "chat.sms", "e2", route="export", source_ref={"chat_id": "c"})).status == "not_found"
    assert r.fetch(Candidate("beeper", "chat.sms", "e3", route="export", source_ref={"chat_id": "c"})).status == "pending_export"


def test_beeper_fetch_script_ephemeral_rule():
    from throughline.images import beeper_fetch

    assert beeper_fetch.ephemeral({"messageExpirySeconds": 3600})
    assert not beeper_fetch.ephemeral({"messageExpirySeconds": None, "title": "x"})
    assert beeper_fetch.ephemeral({"isEphemeral": True})
    assert not beeper_fetch.image_attachment({"type": "img", "isSticker": True})
    assert beeper_fetch.image_attachment({"type": "img", "fileName": "x.jpg"})
    assert beeper_fetch.local_path("file:///opt/data/beeper/a%20b.jpg", [("/opt/data", "/mnt/x")]) == "/mnt/x/beeper/a b.jpg"


def test_t3code_mapping_to_external_ids(tmp_path):
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    (attachments / "th1-u1.png").write_bytes(png())
    payload = {"attachments": [
        {"type": "image", "id": "th1-u1", "name": "image.png", "mimeType": "image/png", "sizeBytes": 10},
        {"type": "image", "id": "th1-u2", "name": "gone.png", "mimeType": "image/png"},
        {"type": "file", "id": "th1-u3", "name": "notes.md", "mimeType": "text/markdown"},
    ]}
    found = t3code.candidates_of("msg-1", "th1", "2026-07-07T19:31:17.581Z", payload, attachments, {"th1": "Fix bug"})
    assert [(c.channel, c.external_id, c.ord, c.skip) for c in found] == [
        ("t3code.messages", "msg-1", 0, None),
        ("t3code.messages", "msg-1", 1, "file_missing"),
        ("t3code.messages", "msg-1", 2, "not_image"),
    ]
    assert found[0].happened_at == datetime(2026, 7, 7, 19, 31, 17, 581000, tzinfo=UTC)
    assert found[0].context["thread"] == "Fix bug"
    assert t3code.Resolver(None).fetch(found[0]).data == png()


def test_hermes_mapping_and_truncation(tmp_path):
    good = "data:image/png;base64," + base64.b64encode(png()).decode()
    records = [
        {"kind": "run", "run": "r"},
        {"kind": "message", "id": 7, "session_id": "s1", "truncated": [], "payload": {
            "message_uid": "uid7", "timestamp": 1_700_000_000, "role": "user",
            "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": good}}]}},
        {"kind": "message", "id": 8, "session_id": "cron_x", "truncated": ["content[0].image_url.url"], "payload": {
            "timestamp": 1_700_000_100, "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": good[:200]}}]}},
        {"kind": "session", "id": "s1", "payload": {"source": "discord"}},
        {"kind": "session", "id": "s2", "payload": {"source": "cron"}},
    ]
    path = tmp_path / "export.jsonl.gz"
    with gzip.open(path, "wt") as handle:
        for r in records:
            handle.write(json.dumps(r) + "\n")
    found, stats = hermes.scan(path)
    assert [(c.channel, c.external_id, c.ord, c.skip) for c in found] == [
        ("hermes.messages", "s1:uid7", 1 - 1, None),
        ("hermes.automation", "cron_x:0000000008", 0, "truncated"),
    ]
    assert hermes.Resolver(None).fetch(found[0]).data == png()
    assert hermes.Resolver(None).fetch(found[1]).status == "truncated"
    assert hermes.message_external_id("s", 5) == "s:0000000005"
    assert hermes.channel_for("abc", "cron") == "hermes.automation"
    assert hermes.channel_for("abc", "api_server") == "hermes.messages"


# --- classification --------------------------------------------------------------------


def sig(**kw):
    base = dict(mime="image/jpeg", width=4000, height=3000, name="", exif={}, ocr_text="", ocr_boxes=[], faces=[])
    base.update(kw)
    return tagging.signals_of(**base)


def sims(kinds=None, tags=None):
    k = {"photo": 0.05, "screenshot": 0.05, "document": 0.05, "artwork": 0.05, "other": 0.05}
    k.update(kinds or {})
    t = {"beach": -0.1, "code": -0.1, "receipt": -0.1, "meme": -0.1}
    t.update(tags or {})
    return {"kinds": k, "tags": t}


def test_kind_from_signals_and_scores():
    # Camera EXIF decides a tie for photo; a screenshot file name for screenshot.
    assert tagging.classify(sims(), sig(exif={"make": "Google"}), VOCAB)["kind"] == "photo"
    assert tagging.classify(sims(), sig(mime="image/png", name="Screenshot_20240101.png", width=1080, height=2400), VOCAB)["kind"] == "screenshot"
    # A phone-shaped PNG without camera data leans screenshot even with neutral scores.
    assert tagging.classify(sims(), sig(mime="image/png", width=1170, height=2532), VOCAB)["kind"] == "screenshot"
    # Strong model evidence wins over a weak prior.
    assert tagging.classify(sims(kinds={"document": 0.15}), sig(ocr_text="x" * 400), VOCAB)["kind"] == "document"
    # Small square PNG: sticker shaped -> artwork.
    assert tagging.classify(sims(kinds={"artwork": 0.06}), sig(mime="image/webp", width=512, height=512), VOCAB)["kind"] == "artwork"
    assert tagging.classify(sims(), sig(mime="video/mp4"), VOCAB)["kind"] == "video"


def test_tags_thresholds_kinds_and_derived():
    r = tagging.classify(sims(kinds={"photo": 0.2}, tags={"beach": 0.05, "code": 0.09}), sig(), VOCAB)
    assert r["kind"] == "photo"
    assert "beach" in r["tags"]  # sigmoid(100*0.05-2) > 0.5
    assert "code" not in r["tags"]  # only for screenshots
    weak = tagging.classify(sims(kinds={"photo": 0.2}, tags={"beach": 0.01}), sig(), VOCAB)
    assert "beach" not in weak["tags"]  # sigmoid(-1) < 0.5
    derived = tagging.classify(
        sims(kinds={"photo": 0.2}),
        sig(ocr_text="y" * 350, faces=[{"bbox": {"w": 10, "h": 10}}] * 3, nsfw=0.7),
        VOCAB,
    )
    assert {"text-heavy", "group-photo", "nsfw"} <= set(derived["tags"])
    selfie = tagging.classify(sims(kinds={"photo": 0.2}), sig(width=100, height=100, faces=[{"bbox": {"w": 50, "h": 50}}]), VOCAB)
    assert "selfie" in selfie["tags"]


def test_meme_combines_model_caption_and_hints():
    caption = [{"text": "WHEN THE CODE", "y1": 0.03, "y2": 0.03, "y3": 0.12, "y4": 0.12},
               {"text": "COMPILES", "y1": 0.88, "y2": 0.88, "y3": 0.97, "y4": 0.97}]
    plain = tagging.classify(sims(kinds={"photo": 0.2}, tags={"meme": 0.015}), sig(), VOCAB)
    assert "meme" not in plain["tags"]  # model alone: sigmoid(-0.5) = 0.38
    captioned = tagging.classify(sims(kinds={"photo": 0.2}, tags={"meme": 0.015}), sig(ocr_text="WHEN THE CODE COMPILES", ocr_boxes=caption), VOCAB)
    assert "meme" in captioned["tags"]
    hinted = tagging.classify(sims(kinds={"photo": 0.2}, tags={"meme": 0.015}), sig(name="funny_meme.jpg"), VOCAB)
    assert "meme" in hinted["tags"]
    # A chat screenshot full of text in the middle is not a caption overlay.
    chat = [{"text": "msg", "y1": y, "y2": y, "y3": y + 0.03, "y4": y + 0.03} for y in (0.3, 0.4, 0.5, 0.6, 0.7)]
    assert not tagging.caption_overlay(chat)
    assert tagging.caption_overlay(caption)


def test_text_bank_margins_against_template_reference():
    vocab = {
        "references": {"a photo of ": "a photo of something", "": "an image"},
        "kinds": {"photo": ["a photo"]},
        "tags": {"beach": {"prompts": ["a photo of a beach"]}, "chart": {"prompts": ["a chart"]}},
    }
    sim = {"a photo": 0.1, "a photo of a beach": 0.12, "a photo of something": 0.09, "a chart": 0.05, "an image": 0.06}
    out = tagging.similarities_from(sim, vocab)
    assert out["kinds"]["photo"] == 0.1
    assert round(out["tags"]["beach"], 6) == 0.03
    assert round(out["tags"]["chart"], 6) == -0.01


def test_shipped_vocabulary_is_valid():
    vocab = tagging.load_vocabulary()
    assert len(vocab["tags"]) >= 150
    assert set(vocab["kinds"]) <= set(tagging.KINDS)
    for name, entry in vocab["tags"].items():
        assert name == name.lower() and " " not in name
        assert entry["prompts"]
        assert set(entry.get("kinds") or []) <= set(tagging.KINDS)
    for need in ("meme", "selfie", "receipt", "code", "terminal", "chat-screenshot", "beach", "sunset", "map"):
        assert need in vocab["tags"]


# --- nsfw -----------------------------------------------------------------------------


def test_nsfw_score_uses_explicit_labels_only():
    found = [
        {"class": "FACE_FEMALE", "score": 0.95},
        {"class": "FEMALE_BREAST_COVERED", "score": 0.8},
        {"class": "BUTTOCKS_EXPOSED", "score": 0.41},
        {"class": "BUTTOCKS_EXPOSED", "score": 0.66},
    ]
    score, labels = nsfw.score(found)
    assert score == 0.66
    assert labels == {"BUTTOCKS_EXPOSED": 0.66, "FACE_FEMALE": 0.95, "FEMALE_BREAST_COVERED": 0.8}
    assert nsfw.score([{"class": "FACE_MALE", "score": 0.9}])[0] == 0.0


def test_nsfw_bridge_protocol():
    class FakeProcess:
        def __init__(self, *a, **k):
            import io

            self.stdin = io.StringIO()
            self.stdout = io.StringIO(
                json.dumps({"ready": True}) + "\n"
                + json.dumps({"results": [{"path": "/a.jpg", "detections": [{"class": "ANUS_EXPOSED", "score": 0.7}]},
                                          {"path": "/b.jpg", "error": "bad"}]}) + "\n"
            )

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    bridge = nsfw.NudeNetBridge("/python", popen=FakeProcess)
    out = bridge.classify(["/a.jpg", "/b.jpg"])
    assert out["/a.jpg"] == {"nsfw": 0.7, "labels": {"ANUS_EXPOSED": 0.7}}
    assert out["/b.jpg"] == {"error": "bad"}
    with pytest.raises(nsfw.BridgeError):
        nsfw.NudeNetBridge(None)


# --- gemini ----------------------------------------------------------------------------------


def test_gemini_402_is_pending_not_failed():
    class ProviderUnavailable(ValueError):
        pass

    def broke(images):
        raise ProviderUnavailable("embedding failed (HTTP 402)")

    with pytest.raises(gemini.GeminiPending):
        gemini.embed([(b"x", "image/jpeg")], broke)

    def bad(images):
        raise ProviderUnavailable("embedding failed (HTTP 400)")

    with pytest.raises(gemini.GeminiFailed):
        gemini.embed([(b"x", "image/jpeg")], bad)
    assert gemini.embed([(b"x", "image/jpeg")], lambda imgs: [[0.1, 0.2]]) == [[0.1, 0.2]]


def test_gemini_stage_marks_pending_and_retries_later(tmp_path):
    p = pipeline(tmp_path)
    for n in range(3):
        aid = p.store.upsert_asset(None, sha256=f"{n:064x}", origin="t3code")
        p.store.set_asset(None, aid, immich_id=f"i{n}", status="enriched", merge_enrich={"pull": {}, "nsfw": {}, "tags": {}})

    def broke(images):
        raise RuntimeError("HTTP 402 Payment Required")

    p.gemini_embed = broke
    counts = p.gemini(limit=10)
    assert counts["pending"] == 3
    assert all("gemini" not in a["enrich"] and "gemini_pending" in a["enrich"] for a in p.store.assets.values())
    assert all(a["status"] == "enriched" for a in p.store.assets.values())
    assert len(p.store.assets_needing(None, "gemini")) == 3  # still eligible next run
    p.gemini_embed = lambda images: [[0.5, 0.5]] * len(images)
    assert p.gemini(limit=10)["embedded"] == 3
    assert not p.store.assets_needing(None, "gemini")


# --- stages end to end -----------------------------------------------------------------------


def candidate(ext="m1", ord=0, channel="chat.discord"):
    return Candidate("beeper", channel, ext, ord=ord, name="image.png", route="url",
                     happened_at=datetime(2025, 1, 2, tzinfo=UTC), source_ref={"url": "u"})


def test_backfill_dedups_by_sha_and_is_idempotent(tmp_path):
    p = pipeline(tmp_path)
    data = png()
    p.resolvers["beeper"] = res = Res(data)
    counts = p.backfill("beeper", candidates=[candidate("m1"), candidate("m2"), candidate("m3", 1)])
    assert counts["stored"] == 3
    assert len(p.store.assets) == 1  # one asset, three links
    assert len(p.store.links) == 3
    asset = p.store.assets[1]
    assert (asset["mime"], asset["width"], asset["height"]) == ("image/png", 120, 80)
    assert p.find_spool(asset["sha256"]).read_bytes() == data
    again = p.backfill("beeper", candidates=[candidate("m1"), candidate("m2"), candidate("m3", 1)])
    assert again["already_linked"] == 3 and res.calls == 3
    dry = p.backfill("beeper", candidates=[candidate("m9")], dry_run=True)
    assert dry["fetchable"] == 1 and res.calls == 3


def test_backfill_limit_and_skips(tmp_path):
    p = pipeline(tmp_path)
    p.resolvers["beeper"] = Res(png())
    skipped = candidate("s")
    skipped.skip = "expired_url"
    counts = p.backfill("beeper", limit=1, candidates=[skipped, candidate("a"), candidate("b")])
    assert counts["skipped:expired_url"] == 1
    assert counts["stored"] == 1 and counts["deferred_by_limit"] == 1


def test_upload_pull_nsfw_tags_flow(tmp_path):
    immich = FakeImmich()
    p = pipeline(tmp_path, immich=immich, nudenet=FakeBridge(0.8))
    p.resolvers["beeper"] = Res(jpeg_with_exif())
    p.backfill("beeper", candidates=[candidate("m1")])
    assert p.upload()["upload:created"] == 1
    asset = p.store.assets[1]
    assert asset["status"] == "uploaded" and asset["immich_id"] in immich.uploaded
    up = immich.uploaded[asset["immich_id"]]
    assert up["created_at"].replace(tzinfo=None) == datetime(2024, 5, 6, 7, 8, 9)  # EXIF beats message time
    assert up["metadata"] == {"sha256": asset["sha256"], "origin": "beeper"}
    assert immich.descriptions[asset["immich_id"]] == "Discord · general · from Alex Kim"  # no message text
    assert asset["enrich"]["describe_v"] == 2
    assert asset["immich_id"] in immich.albums["Source · Discord"]
    assert not p.find_spool(asset["sha256"])
    # A second copy of the same bytes from elsewhere: Immich answers duplicate.
    assert immich.upload(jpeg_with_exif(), "x.jpg")[1] == "duplicate"

    assert p.pull()["pulled"] == 1
    asset = p.store.assets[1]
    assert asset["place"] == {"city": "Galveston", "state": "Texas", "country": "United States"}
    assert asset["ocr_text"] == "WELCOME"
    assert p.store.faces[1][0]["bbox"] == {"x1": 20, "y1": 20, "x2": 120, "y2": 140, "w": 100, "h": 120}
    assert p.store.faces[1][0]["name"] == "Alex Kim"
    assert p.store.vectors[(1, "siglip2-so400m-384")] == [1.0, 0.0, 0.0]

    assert p.nsfw()["nsfw_flagged"] == 1
    assert "nsfw" in p.store.assets[1]["tags"]
    p.tags()
    asset = p.store.assets[1]
    assert asset["kind"] == "photo" and asset["status"] == "enriched"
    assert "nsfw" in asset["tags"]
    assert "kind/photo" in immich.tags[asset["immich_id"]]


def test_pull_waits_for_immich_ml(tmp_path):
    p = pipeline(tmp_path, immich_db=FakeImmichDB(ready=False))
    aid = p.store.upsert_asset(None, sha256="a" * 64, origin="t3code")
    p.store.set_asset(None, aid, immich_id="i1", status="uploaded",
                      merge_enrich={"upload": {"at": datetime.now(UTC).isoformat()}})
    assert p.pull()["waiting_for_immich"] == 1
    p.store.assets[aid]["enrich"]["upload"]["at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert p.pull()["pulled_partial"] == 1


def test_photo_session_threading():
    t0 = datetime(2025, 6, 1, 10, 0, tzinfo=UTC)
    assets = [
        {"immich_id": "a", "taken_at": t0, "lat": 29.30, "lon": -94.80},
        {"immich_id": "b", "taken_at": t0 + timedelta(minutes=30), "lat": 29.31, "lon": -94.80},  # 1.1 km
        {"immich_id": "c", "taken_at": t0 + timedelta(minutes=50), "lat": 29.60, "lon": -94.80},  # 32 km away
        {"immich_id": "d", "taken_at": t0 + timedelta(hours=1), "lat": None, "lon": None},  # time only
        {"immich_id": "e", "taken_at": t0 + timedelta(hours=4), "lat": 29.60, "lon": -94.80},  # 3 h gap
        {"immich_id": "x", "taken_at": None},
    ]
    keys = {a["immich_id"]: k for a, k in photo_sessions(assets)}
    assert keys == {"a": "session:a", "b": "session:a", "c": "session:c", "d": "session:c", "e": "session:e"}


def test_native_messages_shape(tmp_path):
    p = pipeline(tmp_path)
    row = {"id": 3, "immich_id": "abc", "sha256": "f" * 64, "taken_at": datetime(2025, 6, 1, 10, tzinfo=UTC),
           "lat": 29.3, "lon": -94.8, "kind": "photo", "tags": ["beach"], "place": {"city": "Galveston"},
           "original_name": "PXL_1.jpg"}
    m = p.native_message(row, "session:abc")
    assert m["channel"] == "photos.immich" and m["external_id"] == "immich:abc"
    assert m["thread"]["external_id"] == "session:abc"
    assert m["location"] == {"lat": 29.3, "lon": -94.8}
    assert m["author"]["system"] == "immich" and m["author"]["kind"] == "owner"
    from throughline.models import Message

    Message.model_validate(m)


def test_people_sync_pushes_confirmed_names_and_suggests(tmp_path):
    immich = FakeImmich()
    p = pipeline(tmp_path, immich=immich)

    class Q(FakeQueries):
        def face_identifiers(self):
            return [{"immich_person_id": "p1", "status": "confirmed", "person_id": 9, "display_name": "Alex Kim"}]

        def people_by_name(self, name):
            return [{"id": 4, "display_name": "Sam Lee"}] if name == "Sam Lee" else []

    p.q = Q(p.store)
    report = p.people_sync()
    assert immich.renamed == [("p1", "Alex Kim")]
    assert report["suggestions"] == [{"immich_person_id": "p2", "immich_name": "Sam Lee", "person_id": 4,
                                      "display_name": "Sam Lee", "status": "unlinked"}]
    immich.renamed.clear()
    p.people_sync(dry_run=True)
    assert immich.renamed == []


def test_helpers():
    assert describe([]) is None
    # Source, thread title and author only: never the message text.
    links = [{"channel": "chat.discord", "label": "Discord", "thread": "#general", "author": "Alex Kim", "body": "secret words"},
             {"channel": "chat.sms", "label": "SMS", "thread": "Mom", "author": "Mom", "body": "more"}]
    assert describe(links) == "Discord · #general · from Alex Kim"
    assert describe([{"channel": "chat.sms"}]) == "SMS"
    boxes = [
        {"text": "world", "x1": 0.5, "x2": 0.9, "x3": 0.9, "x4": 0.5, "y1": 0.1, "y2": 0.1, "y3": 0.2, "y4": 0.2},
        {"text": "hello", "x1": 0.1, "x2": 0.4, "x3": 0.4, "x4": 0.1, "y1": 0.1, "y2": 0.1, "y3": 0.2, "y4": 0.2},
        {"text": "next line", "x1": 0.1, "x2": 0.4, "x3": 0.4, "x4": 0.1, "y1": 0.5, "y2": 0.5, "y3": 0.6, "y4": 0.6},
    ]
    assert ocr_text_of(boxes) == "hello world\nnext line"
    assert ocr_text_of([]) == ""
    assert face_rows([{"id": "f", "boundingBoxX1": 1, "boundingBoxY1": 2, "boundingBoxX2": 3, "boundingBoxY2": 5}])[0]["bbox"]["h"] == 3


def test_env_file_loader_never_overrides(tmp_path):
    env_file = tmp_path / "a.env"
    env_file.write_text("export IMMICH_URL='http://x:2283'\nIMMICH_API_KEY=k\n# c\nBAD LINE\n")
    environ = {"IMMICH_API_KEY": "kept"}
    loaded = load_env_files(str(env_file), environ)
    assert loaded == ["IMMICH_URL"]
    assert environ == {"IMMICH_API_KEY": "kept", "IMMICH_URL": "http://x:2283"}
    s = Settings.from_env({"DB_PASSWORD": "pw", "IMMICH_URL": "http://h/"})
    assert s.immich_url == "http://h" and "54332" in s.immich_db_url


# --- review fixes -------------------------------------------------------------------------


class Missing(Exception):
    status = 404


def uploaded_asset(p, **cols):
    aid = p.store.upsert_asset(None, sha256="a" * 64, origin="beeper", mime="image/png")
    p.store.set_asset(None, aid, immich_id="i1", status="uploaded",
                      merge_enrich={"upload": {"at": "2020-01-01T00:00:00+00:00"}, **cols.pop("enrich", {})}, **cols)
    return aid


def test_pull_404_forgets_the_immich_id_so_the_asset_uploads_again(tmp_path):
    immich = FakeImmich()

    def gone(iid):
        raise Missing("gone")

    immich.asset = gone
    p = pipeline(tmp_path, immich=immich)
    aid = uploaded_asset(p)
    assert p.pull()["missing_in_immich"] == 1
    row = p.store.assets[aid]
    assert row["immich_id"] is None and row["status"] == "pending" and "pull" not in row["enrich"]
    assert row["enrich"]["gone_from_immich"]["immich_id"] == "i1"
    # No spool: upload marks it missing; the next backfill fetches it again.
    assert p.upload()["spool_missing"] == 1
    p.resolvers["beeper"] = Res(png())
    p.store.links[("chat.discord", "m1", aid)] = dict(ord=0, role="attachment", source_ref={})
    p.store.assets[aid]["sha256"] = hashlib.sha256(png()).hexdigest()
    assert p.backfill("beeper", candidates=[candidate("m1")])["stored"] == 1
    assert p.store.assets[aid]["status"] == "pending"
    assert p.upload()["upload:created"] == 1


def test_ingest_clears_an_old_missing_immich_id(tmp_path):
    p = pipeline(tmp_path)
    data = png()
    aid = p.store.upsert_asset(None, sha256=hashlib.sha256(data).hexdigest(), origin="beeper")
    p.store.set_asset(None, aid, immich_id="old", status="missing", merge_enrich={"pull": {"error": "not in immich"}})
    assert p.ingest_bytes(candidate("m1"), data) == aid
    row = p.store.assets[aid]
    assert row["immich_id"] is None and row["status"] == "pending" and "pull" not in row["enrich"]
    assert p.find_spool(row["sha256"])


def test_ingest_skips_bytes_that_are_not_an_image(tmp_path):
    p = pipeline(tmp_path)
    p.resolvers["beeper"] = Res(b"<html><body>expired link</body></html>")
    counts = p.backfill("beeper", candidates=[candidate("m1")])
    assert counts["skipped:not_image_bytes"] == 1 and counts["stored"] == 0
    assert p.store.assets == {} and p.store.links == {}


def test_nsfw_errors_retry_then_give_up_unsafe(tmp_path):
    from throughline.images.store import asset_safe

    p = pipeline(tmp_path, nudenet=FakeBridge(error="decode failed"))
    aid = uploaded_asset(p, kind="photo", enrich={"pull": {"v": 1}})
    for attempt in (1, 2):
        assert p.nsfw()["nsfw:error"] == 1
        enrich = p.store.assets[aid]["enrich"]
        assert "nsfw" not in enrich and enrich["nsfw_error"]["attempts"] == attempt
        assert not asset_safe(p.store.assets[aid])
    assert p.nsfw()["nsfw:gave_up"] == 1
    row = p.store.assets[aid]
    assert row["enrich"]["nsfw"]["gave_up"] is True and row["nsfw"] is None
    assert not asset_safe(row)
    assert p.nsfw() == {}  # done: not listed again
    # An error stored by the older code (enrich.nsfw.error) is retried.
    p.store.assets[aid]["enrich"] = {"pull": {"v": 1}, "nsfw": {"v": 1, "error": "old"}}
    p._nudenet = FakeBridge(0.1)
    assert p.nsfw()["nsfw_clean"] == 1
    assert asset_safe(p.store.assets[aid]) and "nsfw_error" not in p.store.assets[aid]["enrich"]


def test_asset_safe_rules():
    from throughline.images.store import asset_safe

    assert not asset_safe({"nsfw": None, "kind": "photo", "enrich": {}})
    assert asset_safe({"nsfw": None, "kind": "video", "enrich": {}})
    assert asset_safe({"nsfw": 0.1, "kind": "photo"}) and not asset_safe({"nsfw": 0.6})
    # A score decides (so a new threshold applies at once); the tag counts
    # for an unscored asset only.
    assert asset_safe({"nsfw": 0.1, "tags": ["nsfw"]})
    assert not asset_safe({"nsfw": None, "kind": "video", "tags": ["nsfw"]})
    assert asset_safe({"nsfw": 0.7}, threshold=0.8) and not asset_safe({"nsfw": 0.5}, threshold=0.4)
    assert asset_safe({"nsfw": None, "kind": "photo", "enrich": {}}, unscanned="show")
    assert asset_safe({"nsfw": None, "enrich": {"nsfw": {"v": 1, "skipped": "video"}}})
    assert not asset_safe({"nsfw": None, "enrich": {"nsfw": {"v": 1, "error": "x", "gave_up": True}}})


def test_native_message_text_names_nobody_and_hides_unsafe(tmp_path):
    from throughline.images import store as real_store

    p = pipeline(tmp_path)
    p.store = real_store
    row = {"id": 3, "immich_id": "abc", "sha256": "f" * 64, "taken_at": datetime(2025, 6, 1, 10, tzinfo=UTC),
           "lat": None, "lon": None, "kind": "photo", "tags": ["beach"], "place": {"city": "Galveston"},
           "ocr_text": "HELLO", "nsfw": 0.1, "original_name": "PXL_1.jpg", "enrich": {"nsfw": {"v": 1}}}
    m = p.native_message(row, "session:abc")
    assert m["content"] == '[photo · beach · Galveston]\ntext: "HELLO"'
    nsfw = dict(row, nsfw=0.9, tags=["beach", "nsfw"], caption="explicit")
    m = p.native_message(nsfw, "session:abc")
    assert m["content"] == "[photo · Galveston · 2025-06-01T10:00+00:00]"
    assert m["fields"]["tags"] == [] and "HELLO" not in json.dumps(m)
    unscored = dict(row, nsfw=None, enrich={})
    assert p.native_message(unscored, "s")["content"].startswith("[photo · Galveston · 2025")


def test_redescribe_rewrites_old_descriptions_once(tmp_path):
    immich = FakeImmich()
    p = pipeline(tmp_path, immich=immich)
    aid = uploaded_asset(p)
    p.store.links[("chat.discord", "m1", aid)] = dict(ord=0, role="attachment", source_ref={})
    native = p.store.upsert_asset(None, sha256="b" * 64, origin="immich")
    p.store.set_asset(None, native, immich_id="n1", status="uploaded")
    immich.descriptions["i1"] = "Discord · general · from Alex Kim\nlook at this"
    assert p.redescribe() == {"redescribed": 1}
    assert immich.descriptions == {"i1": "Discord · general · from Alex Kim"}
    assert p.store.assets[aid]["enrich"]["describe_v"] == 2
    assert p.redescribe() == {}


def test_beeper_fetch_never_takes_a_differently_named_attachment():
    from throughline.images.beeper_fetch import pick_attachment

    a, b, b2 = {"fileName": "a.jpg"}, {"fileName": "b.jpg"}, {"fileName": "b.jpg"}
    assert pick_attachment([a, b], 0, "a.jpg") is a
    assert pick_attachment([a, b], 0, "b.jpg") is b  # moved: the one named so
    assert pick_attachment([a, b], 0, "c.jpg") == "name_mismatch"
    assert pick_attachment([a, b, b2], 0, "b.jpg") == "name_mismatch"
    assert pick_attachment([a], 3, "c.jpg") is None
    assert pick_attachment([a], 0, None) is a


# --- settings, shared posts, location (migration 0065) -----------------------------------------


def test_shared_post_detection():
    from throughline.images.shared import other_share, shared_post, shared_post_url

    assert shared_post("https://www.instagram.com/reel/DgA_K5oubuS/") == ("instagram", "https://www.instagram.com/reel/DgA_K5oubuS/")
    assert shared_post_url("https://www.instagram.com/p/Cx1/?igsh=abc\n\nreactions: ❤️ @alice:beeper.com") == "https://www.instagram.com/p/Cx1/?igsh=abc"
    assert shared_post_url("https://instagram.com/someone/reel/Abc_1") is not None
    # Beeper's form of a shared post: the author and caption, then the link.
    caption = "sacurrent Your San Antonio Weekend Guide is here ✨\n\n📅 FRIDAY…\nhttps://www.instagram.com/p/DdsH6N0EQlF/\n\nreactions: 💕 @x"
    assert shared_post_url(caption) == "https://www.instagram.com/p/DdsH6N0EQlF/"
    assert shared_post("see https://example.com/x\nhttps://www.instagram.com/p/A/") is None  # another link
    assert shared_post_url("https://www.instagram.com/tv/Abc") and shared_post_url("https://www.instagram.com/reels/Abc/")
    assert shared_post("https://www.tiktok.com/@a.b/video/7301234567890")[0] == "tiktok"
    assert shared_post("https://vm.tiktok.com/ZMabc123/")[0] == "tiktok"
    assert shared_post("https://youtube.com/shorts/abcDEF12?si=x")[0] == "youtube"
    assert shared_post("https://x.com/someone/status/1790000000000")[0] == "x"
    assert shared_post("https://twitter.com/someone/status/179")[0] == "x"
    # Own words, a profile, a story or a plain video link: not a shared post.
    for text in ("look https://www.instagram.com/reel/Abc/", "https://www.instagram.com/someone/",
                 "https://www.instagram.com/stories/a/123", "https://www.youtube.com/watch?v=abc",
                 "https://www.instagram.com/reel/A/ https://www.instagram.com/reel/B/", "", None):
        assert shared_post(text) is None, text
    assert other_share("Shared R's story\n\nhttps://www.instagram.com/stories/r/39") == "instagram_story"


def test_beeper_candidates_mark_shared_posts():
    row = {"channel": "chat.instagram", "external_id": "ig:1", "meta": None,
           "text": "https://www.instagram.com/reel/DfZimubtAfu/\n\nreactions: 😂 @x:beeper.local",
           "attachments": [{"type": "img", "fileName": "cover.jpg", "mimeType": "image/jpeg", "url": "https://cdn/x.jpg"}]}
    c = beeper.candidates_of(row)[0]
    assert c.role == "preview" and c.source_ref["shared_url"] == "https://www.instagram.com/reel/DfZimubtAfu/"
    assert c.source_ref["shared_network"] == "instagram"
    plain = beeper.candidates_of(dict(row, text="my dog https://www.instagram.com/reel/DfZimubtAfu/"))[0]
    assert plain.role == "attachment" and "shared_url" not in plain.source_ref
    # Older rows without the text column fall back to the 200-character body.
    assert beeper.candidates_of({k: v for k, v in row.items() if k != "text"} | {"body": "https://x.com/a/status/1"})[0].role == "preview"


def test_preview_ingest_tags_and_upload_organizes(tmp_path):
    immich = FakeImmich()
    p = pipeline(tmp_path, immich=immich)
    c = candidate("ig1", channel="chat.instagram")
    c.role = "preview"
    c.source_ref = {"shared_url": "https://www.instagram.com/reel/A/"}
    aid = p.ingest_bytes(c, png())
    assert p.store.links[("chat.instagram", "ig1", aid)]["role"] == "preview"
    assert p.store.assets[aid]["tags"] == ["shared-post"]
    assert p.upload()["upload:created"] == 1
    iid = p.store.assets[aid]["immich_id"]
    assert iid in immich.albums["Shared posts"] and "shared-post" in immich.tags[iid]


def test_tags_stage_keeps_shared_post_and_follows_the_threshold(tmp_path):
    p = pipeline(tmp_path, image_settings={"nsfw_threshold": 0.9})
    aid = uploaded_asset(p, tags=["shared-post"], nsfw=0.7, enrich={"pull": {"v": 1}, "nsfw": {"v": 1}})
    p.store.assets[aid]["tags"] = ["shared-post"]  # the fake tags at 0.6; the real store reads the setting
    p.tags()
    tags = p.store.assets[aid]["tags"]
    assert "shared-post" in tags and "nsfw" not in tags  # 0.7 is below the owner's 0.9
    signals = tagging.signals_of(nsfw=0.95, nsfw_threshold=0.9)
    assert "nsfw" in tagging.classify(FakeBank().similarities(None), signals, VOCAB)["tags"]


def test_pull_keeps_a_pushed_track_location(tmp_path):
    p = pipeline(tmp_path)
    echoed = uploaded_asset(p, lat=29.3, lon=-94.8, loc_method="track", loc_accuracy_m=20.0)
    assert p.pull()["pulled"] == 1
    row = p.store.assets[echoed]
    assert (row["lat"], row["lon"], row["loc_method"], row["loc_accuracy_m"]) == (29.3, -94.8, "track", 20.0)
    p2 = pipeline(tmp_path)
    moved = uploaded_asset(p2, lat=30.0, lon=-97.0, loc_method="track")
    p2.pull()
    row = p2.store.assets[moved]
    assert (row["lat"], row["loc_method"]) == (29.3, "exif")  # EXIF wins


def test_people_sync_skips_clusters_seen_only_on_shared_posts(tmp_path):
    p = pipeline(tmp_path)

    class Q(FakeQueries):
        def face_identifiers(self):
            return [{"immich_person_id": "p2", "status": "suspected", "person_id": None, "display_name": None,
                     "preview_only": True}]

        def people_by_name(self, name):
            return [{"id": 4, "display_name": "Sam Lee"}] if name == "Sam Lee" else []

    p.q = Q(p.store)
    report = p.people_sync(dry_run=True)
    assert report["suggestions"] == [] and report["counts"]["skipped_preview_only"] == 1


def test_before_0065_new_stages_fall_back(tmp_path):
    p = pipeline(tmp_path)  # no database, no settings: DEFAULTS, available False
    assert p.prefs["available"] is False and p.threshold == 0.6
    assert p.locate() == {}
    assert p.retag_nsfw() == {}

    class Q(FakeQueries):
        def share_links(self):
            return [{"channel_id": 1, "channel": "chat.instagram", "external_id": "1", "asset_id": 1, "role": "attachment",
                     "source_ref": {}, "immich_id": None, "tags": [], "content": "https://www.instagram.com/p/X/"}]

    p.q = Q(p.store)
    assert p.reclassify_previews(dry_run=True)["preview_links"] == 1
    with pytest.raises(RuntimeError):
        p.reclassify_previews()
    report = p.sync(sources=())
    assert "error" not in report["locate"] and report["locate"]["seconds"] >= 0


def test_sync_reclassifies_previews_once_before_locate(tmp_path):
    immich = FakeImmich()
    p = pipeline(tmp_path, immich=immich, image_settings={})
    marked = []

    class Q(FakeQueries):
        def share_links(self):
            return [{"channel_id": 1, "channel": "chat.instagram", "external_id": "1", "asset_id": 7, "role": "attachment",
                     "source_ref": {}, "immich_id": "i7", "tags": [], "content": "https://www.instagram.com/p/X/"}]

        def mark_previews(self, links):
            marked.extend(links)
            return len(links), len(links)

        def track_max_id(self):
            return 0

        def locate_candidates(self, *args):
            return []

    p.q = Q(p.store)
    report = p.sync(sources=())
    assert report["reclassify_previews"]["links_changed"] == 1 and "error" not in report["locate"]
    assert marked == [(1, "1", 7, "https://www.instagram.com/p/X/")]
    assert immich.albums["Shared posts"] == {"i7"} and "shared-post" in immich.tags["i7"]
    assert p.previews_reclassified()
    again = p.sync(sources=())
    assert "reclassify_previews" not in again and len(marked) == 1
