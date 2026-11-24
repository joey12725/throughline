"""Screen and audio capture from the Throughline Companion.

The Companion's capture engine writes a local spool (screenshots with their
window context, transcripts of speech). This connector runs in the Companion
on the same desktop and empties it:

- `capture.screens`: one record per screenshot. The image goes to Immich
  (file name tl-capture-<device>-<time>, so the image pipeline adopts it as
  a screenshot of this record instead of a camera-roll photo); the record
  carries the app, the window title and the Immich asset id. Not embedded by
  default: the picture is found through Immich's OCR and the image vectors,
  and a window title a minute is noise for text search.
- `capture.transcripts`: one record per transcribed speech segment, embedded
  like messages. Audio is never uploaded.

Spool files are deleted once their records and the checkpoint after them are
stored (CAPTURE_DELETE_AFTER_UPLOAD, default true). Both channels are life
events: they take the owner's location from the track, and the timeline
groups them into episodes (5 minutes between screenshots, 10 between
speech).
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import httpx

from throughline_sdk import Channel, Checkpoint, Declaration, Manifest, Plugin, run

SCREENS = "capture.screens"
TRANSCRIPTS = "capture.transcripts"
BATCH = 50
PREFIX = "tl-capture-"


def _field(name, kind, description):
    return {"name": name, "type": kind, "description": description}


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


class Immich:
    """Upload-only Immich client (an API key with asset.upload is enough)."""

    def __init__(self, url, key, fixture=None):
        self.fixture = fixture
        if not fixture:
            self.http = httpx.Client(
                base_url=url.rstrip("/") + "/api/",
                headers={"x-api-key": key, "Accept": "application/json"},
                timeout=60,
            )

    def upload(self, data, filename, created_at):
        if self.fixture:
            # Recorded mode (tests): a stable fake asset id per image.
            digest = hashlib.sha1(data).hexdigest()
            return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
        response = self.http.post(
            "assets",
            data={"fileCreatedAt": created_at, "fileModifiedAt": created_at, "filename": filename},
            files={"assetData": (filename, data, "application/octet-stream")},
            headers={"x-immich-checksum": hashlib.sha1(data).hexdigest()},
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Immich upload answered {response.status_code}: {response.text[:200]}")
        return response.json()["id"]


class Capture(Plugin):
    manifest = Manifest(
        name="capture",
        version="0.1.0",
        channels=[
            Channel(
                name=SCREENS,
                description=(
                    "Screenshots of the owner's screen taken by the Throughline Companion while "
                    "they were at the computer (about one a minute): the focused app and window "
                    "title; the image itself is in Immich (see images[] for its OCR text)"
                ),
                fields=[
                    _field("device", "text", "Computer the screenshot was taken on"),
                    _field("app", "text", "Focused application"),
                    _field("window_title", "text", "Focused window title"),
                    _field("monitor", "text", "Monitor captured"),
                    _field("width", "integer", "Image width in pixels"),
                    _field("height", "integer", "Image height in pixels"),
                    _field("bytes", "integer", "Image size"),
                    _field("sha256", "text", "SHA-256 of the image"),
                    _field("immich_asset_id", "text", "The image's Immich asset id"),
                ],
                conversation_gap_s=300,
                embed=False,
            ),
            Channel(
                name=TRANSCRIPTS,
                description=(
                    "Transcripts of speech recorded by the Throughline Companion (microphone, "
                    "or what the computer played); one record per speech segment"
                ),
                fields=[
                    _field("device", "text", "Computer that recorded it"),
                    _field("source", "text", "mic or system (what the computer played)"),
                    _field("duration_s", "number", "Seconds of audio"),
                    _field("language", "text", "Detected or configured language"),
                    _field("engine", "text", "Transcription engine (groq, openai, deepgram, custom, local)"),
                    _field("model", "text", "Transcription model"),
                    _field("segments", "json", "Timed pieces [{start, end, text}] in seconds from the start"),
                ],
                conversation_gap_s=600,
            ),
        ],
        settings={
            "spool": Declaration(env="CAPTURE_SPOOL_DIR", description="The Companion's capture spool folder"),
            "immich_url": Declaration(
                env="IMMICH_URL", required=False, description="Immich server for screenshots (e.g. https://photos.example.com)"
            ),
            "delete": Declaration(
                env="CAPTURE_DELETE_AFTER_UPLOAD", required=False, default="true",
                description="Delete spool files once stored (false keeps them)",
            ),
            "batch": Declaration(
                env="CAPTURE_BATCH", required=False, default=str(BATCH),
                description="Items between checkpoints",
            ),
            "immich_fixture": Declaration(
                env="CAPTURE_IMMICH_FIXTURE", required=False, description="Fake Immich uploads (tests)"
            ),
        },
        secrets={
            "immich_key": Declaration(
                env="IMMICH_API_KEY", required=False, description="Immich API key with the asset.upload permission"
            )
        },
        default_interval_s=300,
    )

    def execute(self, client, mode, dry_run=False):
        self.dry_run = dry_run
        return super().execute(client, mode, dry_run)

    def _items(self):
        root = Path(self.settings["spool"])
        items = []
        for kind in ("screens", "transcripts"):
            folder = root / kind
            if folder.exists():
                items += [(p.name, kind, p) for p in folder.glob("*.json")]
        return sorted(items)

    def records(self, cursor, mode):
        dry_run = getattr(self, "dry_run", False)
        delete = truthy(self.settings["delete"]) and not dry_run
        immich = None
        last = cursor.get("last") or ""
        done = []
        batch = max(1, int(self.settings.get("batch") or BATCH))
        for name, kind, path in self._items():
            if name <= last and not delete:
                continue
            item = json.loads(path.read_text(encoding="utf-8"))
            files = [path]
            if kind == "screens":
                image = path.with_name(item["file"])
                files.append(image)
                if not image.exists():
                    if delete:
                        path.unlink(missing_ok=True)
                    continue
                asset = None
                if not dry_run:
                    if immich is None:
                        immich = self._immich()
                    stamp = item["captured_at"].replace(":", "").replace("-", "").replace("+0000", "Z")[:15]
                    asset = immich.upload(
                        image.read_bytes(), f"{PREFIX}{item['device']}-{stamp}{image.suffix}", item["captured_at"]
                    )
                yield screen_message(item, asset)
            else:
                yield transcript_message(item)
            done.append(files)
            last = max(last, name)
            if len(done) >= batch:
                yield Checkpoint(cursor={"last": last})
                self._remove(done, delete)
                done = []
        if done:
            yield Checkpoint(cursor={"last": last})
            self._remove(done, delete)

    def _immich(self):
        fixture = self.settings.get("immich_fixture")
        if not fixture and not (self.settings.get("immich_url") and self.secrets.get("immich_key")):
            raise RuntimeError("screenshots need IMMICH_URL and IMMICH_API_KEY (Immich stores the images)")
        return Immich(self.settings.get("immich_url") or "", self.secrets.get("immich_key") or "", fixture)

    @staticmethod
    def _remove(groups, delete):
        if not delete:
            return
        for files in groups:
            for file in files:
                file.unlink(missing_ok=True)


def _day(value):
    return datetime.fromisoformat(value).astimezone().date().isoformat()


def screen_message(item, asset):
    app = item.get("app") or "unknown app"
    title = (item.get("title") or "").strip()
    stamp = item["captured_at"]
    return dict(
        channel=SCREENS,
        external_id=f"{item['device']}:{stamp}",
        thread={"external_id": f"{item['device']}:{_day(stamp)}", "title": f"Screen · {item['device']} · {_day(stamp)}"},
        happened_at=stamp,
        content=f"Screen: {app}" + (f" — {title}" if title else ""),
        fields=dict(
            device=item["device"],
            app=item.get("app"),
            window_title=title or None,
            monitor=item.get("monitor"),
            width=item.get("width"),
            height=item.get("height"),
            bytes=item.get("bytes"),
            sha256=item.get("sha256"),
            immich_asset_id=asset,
        ),
        payload={k: v for k, v in item.items() if k != "file"},
    )


def transcript_message(item):
    started = item["started_at"]
    return dict(
        channel=TRANSCRIPTS,
        external_id=f"{item['device']}:{item['source']}:{started}",
        thread={"external_id": f"{item['device']}:{_day(started)}", "title": f"Speech · {item['device']} · {_day(started)}"},
        happened_at=started,
        ended_at=item.get("ended_at"),
        content=item["text"],
        fields=dict(
            device=item["device"],
            source=item["source"],
            duration_s=item.get("duration_s"),
            language=item.get("language"),
            engine=item.get("engine"),
            model=item.get("model"),
            segments=item.get("segments") or [],
        ),
        payload=item,
    )


def main():
    run(Capture)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
