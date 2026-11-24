#!/usr/bin/env python3
"""Beeper chat image bytes from a beeper-server (Desktop API shape), standard library only.

Runs where the Beeper instance and its token live; the token is read in memory
and never printed or written:

    python3 beeper_fetch.py --requests requests.json --out ./beeper-media \
        [--base-url http://127.0.0.1:23373] [--token-cmd "<command printing BEEPER_ACCESS_TOKEN>"] \
        [--max N] [--rps 3]

requests.json is the list `throughline lib images beeper-requests` writes:
[{"channel", "external_id", "chat_id", "message_id", "ord", "file_name"}].
Per message: GET /v1/chats/{chat}/messages/{id}; its attachment (by index,
checked against the file name) has a srcURL (mxc://); POST /v1/assets/download
{url} answers with a file:// srcURL on the server's disk, which is read here
(the same /opt/data mount; --path-map OLD=NEW rewrites a differing prefix).
Bytes go to OUT/<sha256>.<ext>; OUT/manifest.jsonl gets one line per request:
{channel, external_id, ord, sha256, mime, fileName, bytes, status}.

Never opened: chats with a message expiry (disappearing messages), messages
flagged ephemeral or expiring, view-once media, stickers, avatars, link
previews and thumbnails. Counts only are printed, never message text.
Already-fetched requests are skipped, so a stopped run resumes.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
    "image/heic": "heic", "image/heif": "heif", "image/avif": "avif", "image/bmp": "bmp",
    "image/tiff": "tiff",
}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "avif", "bmp", "tif", "tiff"}
EXCLUDED_CHANNELS = {"chat.snapchat"}


class Api:
    def __init__(self, base, token, rps):
        self.base = base.rstrip("/") + "/"
        self.token = token
        self.gap = 1.0 / rps if rps else 0
        self.last = 0.0

    def call(self, path, body=None):
        wait = self.last + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method="POST" if body is not None else "GET",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    raw = response.read()
                    return response.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as error:
                if error.code in (429, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                return error.code, None
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                return None, None
        return None, None


def ephemeral(obj):
    """True for anything that says it expires or disappears."""
    if not isinstance(obj, dict):
        return False
    for key, value in obj.items():
        lower = key.lower()
        if ("expir" in lower or "ephemeral" in lower or "viewonce" in lower or "disappear" in lower) and value:
            return True
    return False


def image_attachment(att):
    if not isinstance(att, dict) or att.get("isSticker"):
        return False
    mime = (att.get("mimeType") or "").lower()
    name = (att.get("fileName") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return mime.startswith("image/") or att.get("type") == "img" or ext in IMAGE_EXT


def pick_attachment(atts, index, file_name):
    """The requested attachment: the one at index, checked against the file
    name. When that one has another name, the only attachment with the name;
    "name_mismatch" when none or several have it (never a different file).
    None when there is no attachment at index and none has the name."""
    att = atts[index] if 0 <= index < len(atts) else None
    if not file_name or (att is not None and att.get("fileName") == file_name):
        return att
    named = [a for a in atts if isinstance(a, dict) and a.get("fileName") == file_name]
    if len(named) == 1:
        return named[0]
    return None if att is None else "name_mismatch"


def local_path(src, path_map):
    path = unquote(urlparse(src).path)
    for old, new in path_map:
        if path.startswith(old):
            return new + path[len(old):]
    return path


def run(args):
    token = os.environ.get("BEEPER_ACCESS_TOKEN")
    if not token and args.token_cmd:
        token = subprocess.run(shlex.split(args.token_cmd), capture_output=True, text=True).stdout.strip()
    if not token:
        raise SystemExit("no Beeper token (BEEPER_ACCESS_TOKEN or --token-cmd)")
    api = Api(args.base_url, token, args.rps)
    path_map = [tuple(p.split("=", 1)) for p in args.path_map if "=" in p]
    requests = json.loads(Path(args.requests).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.jsonl"
    done = set()
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("status") in ("ok", "ephemeral", "not_image"):
                done.add((row["channel"], row["external_id"], row["ord"]))
    counts = {}
    chats = {}
    messages = {}
    attempted = 0
    with manifest.open("a", encoding="utf-8") as sink:
        for req in requests:
            key = (req["channel"], req["external_id"], int(req.get("ord") or 0))
            if key in done:
                counts["skipped"] = counts.get("skipped", 0) + 1
                continue
            if args.max and attempted >= args.max:
                break
            attempted += 1
            row = {"channel": key[0], "external_id": key[1], "ord": key[2], "fileName": req.get("file_name")}
            status = "failed"
            try:
                if key[0] in EXCLUDED_CHANNELS:
                    status = "ephemeral"
                    raise StopIteration
                chat = req["chat_id"]
                if chat not in chats:
                    code, info = api.call("v1/chats/" + quote(chat, safe=""))
                    chats[chat] = info if code == 200 else {}
                if ephemeral(chats[chat]):
                    status = "ephemeral"
                    raise StopIteration
                mkey = (chat, str(req["message_id"]))
                if mkey not in messages:
                    code, msg = api.call(
                        "v1/chats/" + quote(chat, safe="") + "/messages/" + quote(str(req["message_id"]), safe="")
                    )
                    messages[mkey] = msg if code == 200 and isinstance(msg, dict) else None
                msg = messages[mkey]
                if msg is None:
                    status = "message_not_found"
                    raise StopIteration
                if ephemeral(msg):
                    status = "ephemeral"
                    raise StopIteration
                atts = msg.get("attachments") or []
                att = pick_attachment(atts, key[2], req.get("file_name"))
                if att == "name_mismatch":
                    status = "name_mismatch"
                    raise StopIteration
                if att is None or ephemeral(att):
                    status = "attachment_not_found" if att is None else "ephemeral"
                    raise StopIteration
                if not image_attachment(att):
                    status = "not_image"
                    raise StopIteration
                src = att.get("srcURL")
                if not src:
                    status = "no_src_url"
                    raise StopIteration
                if not src.startswith("file://"):
                    code, answer = api.call("v1/assets/download", {"url": src})
                    if code != 200 or not isinstance(answer, dict) or answer.get("error") or not answer.get("srcURL"):
                        status = f"download_refused_{code}"
                        raise StopIteration
                    src = answer["srcURL"]
                if not src.startswith("file://"):
                    status = "download_not_file"
                    raise StopIteration
                path = local_path(src, path_map)
                try:
                    data = Path(path).read_bytes()
                except OSError:
                    status = "file_unreadable_path_mismatch"
                    raise StopIteration
                sha = hashlib.sha256(data).hexdigest()
                mime = (att.get("mimeType") or "").lower() or None
                ext = EXT.get(mime) or (att.get("fileName") or "x.bin").rsplit(".", 1)[-1].lower()
                (out / f"{sha}.{ext}").write_bytes(data)
                row.update(sha256=sha, mime=mime, bytes=len(data), file=f"{sha}.{ext}", fileName=att.get("fileName"))
                status = "ok"
            except StopIteration:
                pass
            except Exception as error:  # One request's failure costs only itself.
                status = "failed"
                row["error"] = type(error).__name__
            row["status"] = status
            counts[status] = counts.get(status, 0) + 1
            sink.write(json.dumps(row, sort_keys=True) + "\n")
            sink.flush()
    print(json.dumps(counts, sort_keys=True))
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--base-url", default=os.environ.get("BEEPER_URL", "http://127.0.0.1:23373"))
    parser.add_argument("--token-cmd", default=None)
    parser.add_argument("--path-map", action="append", default=[], help="OLD=NEW prefix rewrite of file:// paths")
    parser.add_argument("--max", type=int, default=0)
    parser.add_argument("--rps", type=float, default=3.0)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
    sys.exit(0)
